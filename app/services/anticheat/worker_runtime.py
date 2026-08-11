"""Wire the replay-analysis worker to a running process's services.

Kept separate from the worker itself (which is pure/injected) so that the worker
stays unit-testable while this module owns the impure edges: reading ``.osr``
files off the replay volume, the live Redis queue, the MySQL stats repository,
and the Prometheus depth gauge. Both the in-process caller and the standalone
worker entrypoint (``app/anticheat_worker.py``) build the worker through here, so
there is one wiring definition rather than two that can drift.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import app.state.services
from app.bg_task_supervision import run_supervised_loop
from app.logging import Ansi
from app.logging import log
from app.metrics import increment_anticheat_flag
from app.metrics import set_replay_analysis_queue_depth
from app.repositories.anticheat_flags import AnticheatFlagsRepository
from app.repositories.replay_analysis_queue import ReplayAnalysisQueue
from app.repositories.score_replay_stats import ScoreReplayStatsRepository
from app.repositories.scores import ScoresRepository
from app.services.anticheat.flag_review import FlagReviewService
from app.services.anticheat.replay_analysis import AnalysisResult
from app.services.anticheat.replay_analysis import ReplayAnalysisService
from app.services.anticheat.worker import ReplayAnalysisWorker

# where committed replays live on disk, as `{score_id}.osr` (raw frame blocks).
# Mirrors `app.api.dependencies.REPLAYS_PATH`; referenced here without importing
# that module so the standalone worker need not pull in the FastAPI dependency
# graph.
REPLAYS_PATH = Path.cwd() / ".data/osr"

CONSUME_TASK_NAME = "replay-analysis-consume"
BACKFILL_TASK_NAME = "replay-analysis-backfill"

# backfill is the recovery net, not the hot path -- five minutes between passes
# is frequent enough to catch a lost enqueue while doing almost no work on a
# steady server (where every pass finds nothing outstanding).
DEFAULT_BACKFILL_INTERVAL_SECONDS = 5 * 60


def _read_replay_bytes_sync(path: Path) -> bytes | None:
    """Read a replay file, or ``None`` if it is not there.

    ``None`` is the deliberate signal for "no replay on disk" -- the analysis
    service turns it into the ``replay_missing`` terminal state. Any other OS
    error propagates, so a genuinely broken volume surfaces as a supervised-loop
    failure rather than being silently recorded as a missing replay.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def build_replay_analysis_worker(
    *,
    replays_path: Path = REPLAYS_PATH,
) -> ReplayAnalysisWorker:
    """Assemble the worker from the process's live Redis and database services."""
    queue = ReplayAnalysisQueue(app.state.services.redis)
    stats = ScoreReplayStatsRepository(app.state.services.database)
    flags = AnticheatFlagsRepository(app.state.services.database)
    scores = ScoresRepository(app.state.services.database)

    async def read_replay_bytes(score_id: int) -> bytes | None:
        # file I/O off the event loop, like the rest of the replay path.
        return await asyncio.to_thread(
            _read_replay_bytes_sync,
            replays_path / f"{score_id}.osr",
        )

    analysis = ReplayAnalysisService(read_replay_bytes=read_replay_bytes, stats=stats)

    # the durable half of "flag, never ban": a flagged report becomes a review
    # queue row a human can action. Owner is resolved from `scores` and
    # denormalised onto the flag; a purged score is skipped inside the service.
    flag_review = FlagReviewService(
        record_flag=flags.record,
        fetch_score_user=scores.fetch_user_id,
        on_flag_recorded=increment_anticheat_flag,
    )

    async def on_result(result: AnalysisResult) -> None:
        # flag, never ban: a flagged result is logged for staff and persisted to
        # the review queue only. Persistence is awaited (not fired off), so a
        # write failure fails the iteration and is retried rather than lost;
        # re-analysis re-upserts the same row, so the retry is idempotent.
        report = result.report
        if report is not None and report.flagged and report.triggered:
            top = report.triggered[0]
            log(
                f"[anticheat] score {result.score_id} flagged "
                f"{report.severity.value} -- {top.code}: {top.detail}",
                Ansi.LYELLOW,
            )
        await flag_review.observe(result)

    return ReplayAnalysisWorker(
        queue=queue,
        analysis=analysis,
        fetch_unanalyzed=stats.fetch_unanalyzed_scores,
        on_result=on_result,
        report_queue_depth=set_replay_analysis_queue_depth,
    )


async def run_replay_analysis_worker(
    *,
    replays_path: Path = REPLAYS_PATH,
    backfill_interval: float = DEFAULT_BACKFILL_INTERVAL_SECONDS,
) -> None:
    """Run the consume and backfill loops until cancelled.

    The consume loop uses a zero inter-iteration interval: each iteration blocks
    on the queue for up to the worker's dequeue timeout, so it is effectively a
    continuous consumer that still wakes often enough to observe cancellation.
    The backfill loop runs on a slow timer. Both are supervised, so one failing
    iteration backs off instead of killing the worker.
    """
    worker = build_replay_analysis_worker(replays_path=replays_path)
    log("Starting replay-analysis worker loops.", Ansi.LCYAN)

    await asyncio.gather(
        run_supervised_loop(
            CONSUME_TASK_NAME,
            interval=0.0,
            run_immediately=True,
            work=worker.run_consume_iteration,
        ),
        run_supervised_loop(
            BACKFILL_TASK_NAME,
            interval=backfill_interval,
            run_immediately=True,
            work=worker.run_backfill_iteration,
        ),
    )
