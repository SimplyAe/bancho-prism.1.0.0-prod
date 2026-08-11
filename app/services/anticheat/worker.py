"""The replay-analysis worker: the loops that drain the queue and backfill it.

Two cooperating loops, both meant to be driven by ``run_supervised_loop`` so a
transient failure costs one iteration, not the whole worker:

- **consume** blocks on the Redis queue and analyses whatever is popped. This is
  the fast path: a score submitted a moment ago is picked up within the block
  timeout and analysed.
- **backfill** periodically asks the durable table for ``scores`` rows that have
  no analysis row yet and pushes them onto the queue. This is the safety net:
  it makes the queue *recoverable*. If Redis is flushed, or an enqueue was lost
  to a crash in the gap between committing a score and pushing it, the work is
  not gone -- the next backfill pass re-derives it from MySQL, which is the
  source of truth for "analysed?". In steady state backfill finds nothing.

Duplicates are deliberately fine. A score can be on the queue twice (submit hook
plus a backfill that ran before the consumer got to it); re-analysis just
re-upserts the same terminal row. So neither loop tries to coordinate with the
other -- correctness rests on the durable table, not on queue exactly-once.

The worker itself is a frozen dataclass with its side effects injected, so it is
unit-testable with a fake queue, a fake analysis service, and a fake producer,
with no Redis and no database. It never punishes a player: flagged results are
handed to ``on_result`` (a logger in production) for the staff-review track.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from app.repositories.replay_analysis_queue import ReplayAnalysisQueue
from app.repositories.score_replay_stats import PendingScore
from app.services.anticheat.replay_analysis import AnalysisResult
from app.services.anticheat.replay_analysis import ReplayAnalysisService


class UnanalyzedScoreFetcher(Protocol):
    """Fetch a page of scores with no analysis row yet (the backfill source)."""

    def __call__(
        self,
        *,
        limit: int,
        min_score_id: int,
    ) -> Awaitable[list[PendingScore]]: ...


class ResultObserver(Protocol):
    """Side-effect hook for each finished analysis (logging, review queue).

    Awaited by the consume loop, so the hook may do async work (e.g. persist a
    flagged result to the review queue). Awaiting it -- rather than firing it
    off unawaited -- means a persistence failure surfaces as a failed iteration
    the supervised loop retries, not a silently dropped flag; re-analysis is
    idempotent, so the retry is safe.
    """

    def __call__(self, result: AnalysisResult) -> Awaitable[None]: ...


class QueueDepthReporter(Protocol):
    """Publish the current queue depth (backs the Prometheus gauge)."""

    def __call__(self, depth: int) -> None: ...


async def _ignore_result(result: AnalysisResult) -> None:
    return None


def _ignore_depth(depth: int) -> None:
    return None


@dataclass(frozen=True)
class ReplayAnalysisWorker:
    queue: ReplayAnalysisQueue
    analysis: ReplayAnalysisService
    fetch_unanalyzed: UnanalyzedScoreFetcher
    on_result: ResultObserver = _ignore_result
    report_queue_depth: QueueDepthReporter = _ignore_depth
    # how long one BRPOP waits before the consume loop cycles (so shutdown /
    # cancellation is observed promptly even with an idle queue).
    dequeue_timeout: float = 5.0
    # backfill paging: one pass fetches up to batch_size ids at a time and walks
    # forward at most max_batches pages, so a huge cold-start backlog is drained
    # over several passes rather than in one unbounded burst.
    backfill_batch_size: int = 500
    backfill_max_batches: int = 20

    async def consume_once(self) -> AnalysisResult | None:
        """Pop at most one score and analyse it; ``None`` if the queue was idle.

        The block timeout means an idle queue returns ``None`` rather than
        hanging, giving the supervised loop a chance to notice cancellation.
        """
        queued = await self.queue.dequeue(timeout=self.dequeue_timeout)
        if queued is None:
            await self._publish_depth()
            return None

        result = await self.analysis.analyze_score(
            queued.score_id,
            mode=queued.mode,
        )
        await self.on_result(result)
        await self._publish_depth()
        return result

    async def backfill_once(self) -> int:
        """Enqueue outstanding scores from the durable table; return the count.

        Walks forward by score id in bounded pages. Stops early once a page
        comes back short (the table is drained) so a quiet server does almost no
        work. The number returned is how many ids were pushed this pass.
        """
        cursor = 0
        enqueued = 0
        for _ in range(self.backfill_max_batches):
            pending = await self.fetch_unanalyzed(
                limit=self.backfill_batch_size,
                min_score_id=cursor,
            )
            if not pending:
                break

            for score in pending:
                await self.queue.enqueue(score.score_id, score.mode)
                enqueued += 1

            cursor = pending[-1].score_id
            if len(pending) < self.backfill_batch_size:
                break

        await self._publish_depth()
        return enqueued

    async def _publish_depth(self) -> None:
        self.report_queue_depth(await self.queue.depth())

    # -- supervised-loop adapters (return None, as run_supervised_loop wants) --

    async def run_consume_iteration(self) -> None:
        _ = await self.consume_once()

    async def run_backfill_iteration(self) -> None:
        _ = await self.backfill_once()
