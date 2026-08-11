"""Guards for the ephemeral replay-analysis work queue.

The queue is deliberately *not* the source of truth (that is the MySQL
``score_replay_stats`` table); it only answers "what should a worker pick up
next". These tests pin the properties the worker relies on:

- ``LPUSH`` head + ``BRPOP`` tail gives FIFO order, so a submission burst is
  drained oldest-first rather than starving the oldest scores under load;
- a payload round-trips ``score_id``/``mode`` through the ``"{id}:{mode}"``
  encoding, so the mode carried on the hot path survives;
- a malformed or timed-out pop returns ``None`` instead of raising, so one bad
  entry (or an idle queue) costs the consume loop nothing;
- ``depth`` reflects the live list length that backs the queue-depth metric.

No Redis is involved: a fake stands in with just the three list commands the
queue uses, matching aioredis' bytes-returning decode behaviour.
"""

from __future__ import annotations

from app.repositories.replay_analysis_queue import QueuedReplay
from app.repositories.replay_analysis_queue import ReplayAnalysisQueue


class _FakeRedis:
    """An in-memory stand-in for the one Redis list the queue uses.

    Only ``lpush``/``brpop``/``llen`` are implemented, and values are stored as
    ``bytes`` to mirror aioredis returning raw bytes (the queue decodes them).
    ``brpop`` never actually blocks here -- the tests only exercise the
    immediately-available and immediately-empty cases -- so ``timeout`` is
    accepted and ignored.
    """

    def __init__(self) -> None:
        # left end is the head (LPUSH target); right end is the tail (BRPOP).
        self.lists: dict[str, list[bytes]] = {}

    async def lpush(self, key: str, value: str) -> int:
        items = self.lists.setdefault(key, [])
        items.insert(0, value.encode())
        return len(items)

    async def brpop(
        self,
        keys: list[str],
        timeout: float = 0,
    ) -> tuple[bytes, bytes] | None:
        for key in keys:
            items = self.lists.get(key)
            if items:
                return key.encode(), items.pop()
        return None

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))


def _queue() -> tuple[ReplayAnalysisQueue, _FakeRedis]:
    redis = _FakeRedis()
    return ReplayAnalysisQueue(redis), redis  # type: ignore[arg-type]


async def test_enqueue_then_dequeue_round_trips_score_and_mode() -> None:
    queue, _ = _queue()

    await queue.enqueue(42, 3)
    popped = await queue.dequeue(timeout=0.0)

    assert popped == QueuedReplay(score_id=42, mode=3)


async def test_dequeue_is_fifo_across_a_burst() -> None:
    queue, _ = _queue()

    for score_id in (1, 2, 3):
        await queue.enqueue(score_id, 0)

    order = [
        (await queue.dequeue(timeout=0.0)),
        (await queue.dequeue(timeout=0.0)),
        (await queue.dequeue(timeout=0.0)),
    ]

    # oldest-enqueued comes out first, not newest.
    assert [q.score_id for q in order if q is not None] == [1, 2, 3]


async def test_dequeue_returns_none_when_empty() -> None:
    queue, _ = _queue()

    assert await queue.dequeue(timeout=0.0) is None


async def test_dequeue_skips_a_malformed_payload_without_raising() -> None:
    queue, redis = _queue()
    # a hand-planted entry that is not "{int}:{int}" must not kill the loop.
    redis.lists[ReplayAnalysisQueue._KEY] = [b"not-a-valid-payload"]

    assert await queue.dequeue(timeout=0.0) is None


async def test_depth_reflects_the_pending_count() -> None:
    queue, _ = _queue()
    assert await queue.depth() == 0

    await queue.enqueue(1, 0)
    await queue.enqueue(2, 0)

    assert await queue.depth() == 2

    await queue.dequeue(timeout=0.0)
    assert await queue.depth() == 1
