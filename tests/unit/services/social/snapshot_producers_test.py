"""Guards for the snapshot -> activity-feed producer.

``publish_snapshot_peak_activity`` is the fire-and-forget bridge that turns the
daily stat-snapshot capture into ``rank_up`` / ``pp_record`` feed events. It runs
off the daily loop, not a request, so these tests pin the things that matter
there:

- it publishes exactly the events a peak implies -- a row with both a higher pp
  and a better rank produces one ``pp_record`` *and* one ``rank_up``; a row that
  is only a rank peak (pp unchanged) produces only the ``rank_up``, and vice
  versa. A first-ever ranking (no prior best rank) still counts as a rank peak,
  with ``old_rank`` carried through as ``None``;
- it is gated on the capture having actually written rows: a same-day re-run
  writes nothing (``INSERT IGNORE``), so ``rows_written == 0`` must publish
  nothing and never even query for peaks -- otherwise a restart re-announces
  peaks already announced today;
- it queries every valid gamemode, since the capture writes them all;
- it is inert on failure: a feed service that raises never propagates, because a
  feed write must not disturb the (already durable) snapshot rows.

The feed service and the ``fetch_new_peaks`` edge are faked; no database, no loop.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from app.constants.gamemodes import GameMode
from app.repositories.stat_snapshots import SnapshotPeakChange
from app.services.social import snapshot_producers

_TODAY = date(2026, 8, 12)


class _FakeFeedService:
    """Records publish_* calls; can be told to raise to prove isolation."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.calls: list[SimpleNamespace] = []
        self._raise = raise_on_publish

    async def publish_pp_record(self, **kwargs: Any) -> None:
        self.calls.append(SimpleNamespace(method="pp_record", **kwargs))
        if self._raise:
            raise RuntimeError("redis is down")

    async def publish_rank_up(self, **kwargs: Any) -> None:
        self.calls.append(SimpleNamespace(method="rank_up", **kwargs))
        if self._raise:
            raise RuntimeError("redis is down")


class _FakeNewPeaks:
    """Stands in for ``StatSnapshotsRepository.fetch_new_peaks``.

    Returns the given peaks for mode 0 (osu!std) only, so a test can assert that
    the producer sweeps every mode while keeping the expected event set small.
    """

    def __init__(self, peaks: list[SnapshotPeakChange]) -> None:
        self._peaks = peaks
        self.modes_queried: list[int] = []

    async def __call__(
        self,
        *,
        mode: int,
        snapshot_date: date,
    ) -> list[SnapshotPeakChange]:
        self.modes_queried.append(mode)
        return self._peaks if mode == 0 else []


def _peak(
    *,
    user_id: int = 7,
    mode: int = 0,
    new_pp: int = 4200,
    prior_best_pp: int = 4000,
    new_rank: int | None = 12,
    prior_best_rank: int | None = 20,
) -> SnapshotPeakChange:
    return SnapshotPeakChange(
        user_id=user_id,
        mode=mode,
        new_pp=new_pp,
        prior_best_pp=prior_best_pp,
        new_rank=new_rank,
        prior_best_rank=prior_best_rank,
    )


async def _run(
    feed: _FakeFeedService,
    fetch_new_peaks: _FakeNewPeaks,
    *,
    rows_written: int = 5,
) -> None:
    await snapshot_producers.publish_snapshot_peak_activity(
        feed,  # type: ignore[arg-type]
        fetch_new_peaks,  # type: ignore[arg-type]
        snapshot_date=_TODAY,
        rows_written=rows_written,
    )


async def test_a_pp_and_rank_peak_publishes_both_events() -> None:
    feed = _FakeFeedService()
    peaks = _FakeNewPeaks([_peak(new_pp=4200, prior_best_pp=4000, new_rank=12, prior_best_rank=20)])

    await _run(feed, peaks)

    assert [c.method for c in feed.calls] == ["pp_record", "rank_up"]
    pp, rank = feed.calls
    assert (pp.user_id, pp.mode, pp.old_pp, pp.new_pp) == (7, 0, 4000, 4200)
    assert (rank.user_id, rank.mode, rank.old_rank, rank.new_rank) == (7, 0, 20, 12)


async def test_a_pp_only_peak_publishes_only_the_pp_record() -> None:
    feed = _FakeFeedService()
    # rank did not improve (same number), so only pp is a new peak.
    peaks = _FakeNewPeaks([_peak(new_pp=5000, prior_best_pp=4000, new_rank=20, prior_best_rank=20)])

    await _run(feed, peaks)

    assert [c.method for c in feed.calls] == ["pp_record"]


async def test_a_rank_only_peak_publishes_only_the_rank_up() -> None:
    feed = _FakeFeedService()
    # pp did not improve (same value), so only rank is a new peak.
    peaks = _FakeNewPeaks([_peak(new_pp=4000, prior_best_pp=4000, new_rank=8, prior_best_rank=15)])

    await _run(feed, peaks)

    assert [c.method for c in feed.calls] == ["rank_up"]


async def test_a_first_ever_ranking_publishes_rank_up_with_a_null_old_rank() -> None:
    feed = _FakeFeedService()
    # newly ranked: no prior best rank, pp unchanged -> a rank peak only.
    peaks = _FakeNewPeaks([_peak(new_pp=900, prior_best_pp=900, new_rank=50, prior_best_rank=None)])

    await _run(feed, peaks)

    assert [c.method for c in feed.calls] == ["rank_up"]
    assert feed.calls[0].old_rank is None
    assert feed.calls[0].new_rank == 50


async def test_a_null_rank_today_is_never_a_rank_peak() -> None:
    feed = _FakeFeedService()
    # fell off the leaderboard today (rank NULL) but set a pp record: pp only.
    peaks = _FakeNewPeaks([_peak(new_pp=5000, prior_best_pp=4000, new_rank=None, prior_best_rank=10)])

    await _run(feed, peaks)

    assert [c.method for c in feed.calls] == ["pp_record"]


async def test_nothing_captured_publishes_nothing_and_does_not_query() -> None:
    feed = _FakeFeedService()
    peaks = _FakeNewPeaks([_peak()])

    # a same-day re-run's INSERT IGNORE wrote no rows.
    await _run(feed, peaks, rows_written=0)

    assert feed.calls == []
    # gated *before* the query, so a restart cannot re-announce today's peaks.
    assert peaks.modes_queried == []


async def test_every_valid_gamemode_is_queried() -> None:
    feed = _FakeFeedService()
    peaks = _FakeNewPeaks([_peak()])

    await _run(feed, peaks)

    assert peaks.modes_queried == [int(m) for m in GameMode.valid_gamemodes()]


async def test_a_failing_feed_write_never_propagates() -> None:
    feed = _FakeFeedService(raise_on_publish=True)
    peaks = _FakeNewPeaks([_peak()])

    # no exception escapes -- the producer swallows and logs it.
    await _run(feed, peaks)

    # it did try (so the swallow is real, not an early return).
    assert len(feed.calls) == 1
