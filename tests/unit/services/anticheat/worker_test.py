"""Guards for the replay-analysis worker loops.

The worker is two cooperating loops over injected side effects:

- **consume** pops one score off the queue and analyses it (the fast path);
- **backfill** re-derives outstanding work from the durable table and pushes it
  (the safety net that makes the queue recoverable if Redis is flushed).

These tests pin the behaviour the design leans on, using fakes for all four
edges (queue, analysis, unanalyzed-fetcher, depth reporter) -- no Redis, no DB:

- consume analyses exactly the popped score, forwards the result to ``on_result``,
  and publishes depth; an idle queue is a no-op that still publishes depth;
- backfill walks the table forward in bounded pages by ``min_score_id`` (keyset,
  not offset) and stops early when a page comes back short;
- backfill respects ``backfill_max_batches`` so a huge backlog is drained over
  several passes rather than one unbounded burst;
- duplicates are safe -- enqueuing a score already on the queue just re-analyses
  it, since correctness rests on the durable row, not queue exactly-once.
"""

from __future__ import annotations

from app.repositories.replay_analysis_queue import QueuedReplay
from app.repositories.score_replay_stats import PendingScore
from app.services.anticheat.replay_analysis import AnalysisOutcome
from app.services.anticheat.replay_analysis import AnalysisResult
from app.services.anticheat.worker import ReplayAnalysisWorker


class _FakeQueue:
    """In-memory FIFO with the three methods the worker calls on the queue."""

    def __init__(self) -> None:
        self.items: list[QueuedReplay] = []

    async def enqueue(self, score_id: int, mode: int) -> None:
        self.items.append(QueuedReplay(score_id=score_id, mode=mode))

    async def dequeue(self, *, timeout: float) -> QueuedReplay | None:
        if not self.items:
            return None
        return self.items.pop(0)

    async def depth(self) -> int:
        return len(self.items)


class _FakeAnalysis:
    """Records analysed scores and returns a benign (unflagged) result."""

    def __init__(self) -> None:
        self.analyzed: list[tuple[int, int]] = []

    async def analyze_score(self, score_id: int, *, mode: int) -> AnalysisResult:
        self.analyzed.append((score_id, mode))
        return AnalysisResult(
            score_id=score_id,
            mode=mode,
            outcome=AnalysisOutcome.ANALYZED,
        )


def _worker(
    queue: _FakeQueue,
    analysis: _FakeAnalysis,
    *,
    fetch_unanalyzed: object,
    on_result: object = None,
    report_queue_depth: object = None,
    backfill_batch_size: int = 500,
    backfill_max_batches: int = 20,
) -> ReplayAnalysisWorker:
    kwargs: dict[str, object] = {
        "queue": queue,
        "analysis": analysis,
        "fetch_unanalyzed": fetch_unanalyzed,
        "backfill_batch_size": backfill_batch_size,
        "backfill_max_batches": backfill_max_batches,
    }
    if on_result is not None:
        kwargs["on_result"] = on_result
    if report_queue_depth is not None:
        kwargs["report_queue_depth"] = report_queue_depth
    return ReplayAnalysisWorker(**kwargs)  # type: ignore[arg-type]


async def _no_backfill(*, limit: int, min_score_id: int) -> list[PendingScore]:
    return []


async def test_consume_analyses_the_popped_score_and_reports_it() -> None:
    queue = _FakeQueue()
    await queue.enqueue(42, 3)
    analysis = _FakeAnalysis()
    seen: list[AnalysisResult] = []
    depths: list[int] = []

    async def on_result(result: AnalysisResult) -> None:
        # the observer is awaited by the worker, so it may do async work; here it
        # just records what it saw.
        seen.append(result)

    worker = _worker(
        queue,
        analysis,
        fetch_unanalyzed=_no_backfill,
        on_result=on_result,
        report_queue_depth=depths.append,
    )
    result = await worker.consume_once()

    assert result is not None and result.score_id == 42
    assert analysis.analyzed == [(42, 3)]
    assert [r.score_id for r in seen] == [42]
    # depth is published after the pop (queue now empty).
    assert depths == [0]


async def test_consume_on_idle_queue_is_a_noop_that_still_publishes_depth() -> None:
    queue = _FakeQueue()
    analysis = _FakeAnalysis()
    depths: list[int] = []

    worker = _worker(
        queue,
        analysis,
        fetch_unanalyzed=_no_backfill,
        report_queue_depth=depths.append,
    )
    result = await worker.consume_once()

    assert result is None
    assert analysis.analyzed == []
    assert depths == [0]


async def test_backfill_enqueues_every_outstanding_score() -> None:
    queue = _FakeQueue()
    analysis = _FakeAnalysis()
    pending = [PendingScore(score_id=i, mode=0) for i in (1, 2, 3)]

    async def fetch(*, limit: int, min_score_id: int) -> list[PendingScore]:
        # single short page: everything outstanding, then nothing more.
        return [p for p in pending if p.score_id > min_score_id]

    worker = _worker(queue, analysis, fetch_unanalyzed=fetch, backfill_batch_size=500)
    enqueued = await worker.backfill_once()

    assert enqueued == 3
    assert [q.score_id for q in queue.items] == [1, 2, 3]


async def test_backfill_walks_forward_by_keyset_across_pages() -> None:
    queue = _FakeQueue()
    analysis = _FakeAnalysis()
    all_pending = [PendingScore(score_id=i, mode=0) for i in range(1, 8)]
    seen_cursors: list[int] = []

    async def fetch(*, limit: int, min_score_id: int) -> list[PendingScore]:
        seen_cursors.append(min_score_id)
        page = [p for p in all_pending if p.score_id > min_score_id]
        return page[:limit]

    # batch_size 3 over 7 ids -> pages of [1,2,3], [4,5,6], [7]; the short last
    # page stops the walk.
    worker = _worker(queue, analysis, fetch_unanalyzed=fetch, backfill_batch_size=3)
    enqueued = await worker.backfill_once()

    assert enqueued == 7
    assert [q.score_id for q in queue.items] == [1, 2, 3, 4, 5, 6, 7]
    # each page advanced the cursor to the last id of the previous page.
    assert seen_cursors == [0, 3, 6]


async def test_backfill_stops_at_max_batches() -> None:
    queue = _FakeQueue()
    analysis = _FakeAnalysis()

    # a fetcher that always returns a full page would loop forever without the
    # max-batches cap; here every page is full (batch_size items).
    async def fetch(*, limit: int, min_score_id: int) -> list[PendingScore]:
        return [
            PendingScore(score_id=min_score_id + i + 1, mode=0)
            for i in range(limit)
        ]

    worker = _worker(
        queue,
        analysis,
        fetch_unanalyzed=fetch,
        backfill_batch_size=2,
        backfill_max_batches=3,
    )
    enqueued = await worker.backfill_once()

    # bounded to batch_size * max_batches, not an unbounded burst.
    assert enqueued == 6
    assert len(queue.items) == 6


async def test_duplicate_enqueue_is_safe_reanalysis() -> None:
    queue = _FakeQueue()
    analysis = _FakeAnalysis()
    # a score already queued (e.g. by the submit hook) that backfill also finds.
    await queue.enqueue(99, 0)

    async def fetch(*, limit: int, min_score_id: int) -> list[PendingScore]:
        return [PendingScore(score_id=99, mode=0)] if min_score_id == 0 else []

    worker = _worker(queue, analysis, fetch_unanalyzed=fetch)
    await worker.backfill_once()

    # now two copies on the queue; draining both just analyses twice, harmlessly.
    first = await worker.consume_once()
    second = await worker.consume_once()

    assert first is not None and second is not None
    assert analysis.analyzed == [(99, 0), (99, 0)]


async def test_run_iterations_drive_one_pass_each() -> None:
    queue = _FakeQueue()
    await queue.enqueue(7, 0)
    analysis = _FakeAnalysis()

    async def fetch(*, limit: int, min_score_id: int) -> list[PendingScore]:
        return [PendingScore(score_id=8, mode=0)] if min_score_id == 0 else []

    worker = _worker(queue, analysis, fetch_unanalyzed=fetch)

    # the supervised-loop adapters return None by contract, but each must drive
    # exactly one pass of its loop -- consume analyses the queued score, backfill
    # enqueues the outstanding one.
    await worker.run_consume_iteration()
    assert analysis.analyzed == [(7, 0)]

    await worker.run_backfill_iteration()
    assert [q.score_id for q in queue.items] == [8]


async def test_default_hooks_do_not_raise_when_unset() -> None:
    # with no on_result / report_queue_depth injected, the no-op defaults apply.
    queue = _FakeQueue()
    await queue.enqueue(1, 0)
    worker = ReplayAnalysisWorker(
        queue=queue,  # type: ignore[arg-type]
        analysis=_FakeAnalysis(),  # type: ignore[arg-type]
        fetch_unanalyzed=_no_backfill,
    )
    # exercises _ignore_result and _ignore_depth without error.
    result = await worker.consume_once()
    assert result is not None
