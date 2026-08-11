"""The ephemeral work list for replay analysis, backed by a Redis list.

This queue is deliberately *not* the source of truth for what has been analysed
-- ``score_replay_stats`` (MySQL) is. The queue only answers "what should a
worker pick up next", so that the common path (a score just submitted) is
processed promptly without a table scan. If Redis is flushed the queue is empty
but nothing is lost: the worker's backfill scan re-derives the outstanding work
from the durable rows (see ``ScoreReplayStatsRepository.fetch_unanalyzed_scores``).

Because the durable row is what decides "done", a duplicate entry in this list is
harmless -- the worker re-checks the row and skips an already-terminal score. So
we push freely and never try to deduplicate the list itself.

A payload is just ``"{score_id}:{mode}"``. Carrying the mode avoids a scores
lookup on the hot dequeue path; both the enqueue-on-submit and the backfill
producers already know it.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis import asyncio as aioredis


@dataclass(frozen=True, slots=True)
class QueuedReplay:
    """One unit of outstanding analysis work: a score and its mode."""

    score_id: int
    mode: int


class ReplayAnalysisQueue:
    """A FIFO of scores awaiting replay analysis, over a single Redis list.

    ``LPUSH`` at the head and ``BRPOP`` from the tail gives first-in-first-out
    order, so a submission burst is drained in the order it arrived rather than
    newest-first (which would starve the oldest scores under sustained load).
    """

    _KEY = "bancho:replay_analysis:queue"

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    @staticmethod
    def _encode(score_id: int, mode: int) -> str:
        return f"{score_id}:{mode}"

    @staticmethod
    def _decode(payload: str) -> QueuedReplay | None:
        # a malformed entry must not kill the worker loop; skip it instead.
        score_part, sep, mode_part = payload.partition(":")
        if not sep:
            return None
        try:
            return QueuedReplay(score_id=int(score_part), mode=int(mode_part))
        except ValueError:
            return None

    async def enqueue(self, score_id: int, mode: int) -> None:
        """Add a score to the head of the work list."""
        await self._redis.lpush(self._KEY, self._encode(score_id, mode))

    async def dequeue(self, *, timeout: float) -> QueuedReplay | None:
        """Block up to ``timeout`` seconds for the next score, tail-first.

        Returns ``None`` on timeout (so the caller can re-check for shutdown) or
        when the popped payload is malformed. ``BRPOP`` returns a ``(key, value)``
        pair; only the value is meaningful here.
        """
        result = await self._redis.brpop([self._KEY], timeout=timeout)
        if result is None:
            return None
        _key, raw = result
        payload = raw.decode() if isinstance(raw, bytes) else str(raw)
        return self._decode(payload)

    async def depth(self) -> int:
        """Number of scores currently waiting -- backs the queue-depth metric."""
        return int(await self._redis.llen(self._KEY))
