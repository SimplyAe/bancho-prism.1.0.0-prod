"""Turning the daily stat-snapshot capture into activity-feed entries.

Two feed events -- ``rank_up`` and ``pp_record`` -- are deliberately *not*
derived on the score-submission hot path (see
``app.services.social.activity_producers``): a meaningful "new personal best" is a
day-over-day fact, not a per-score one, and firing on every best score would
announce the same climb dozens of times a session. The daily snapshot capture is
where that day-over-day comparison already lives, so these producers run
alongside it.

The capture itself (``StatSnapshotService.capture_today`` /
``StatSnapshotsRepository.capture_mode``) is a set-based ``INSERT ... SELECT``
that returns only a row count -- it has no per-player before/after in Python. So
this module asks the repository for the diff explicitly:
``fetch_new_peaks`` returns exactly the players whose freshly-captured snapshot
beat their best-ever prior pp and/or rank, and this producer publishes one
``pp_record`` and/or ``rank_up`` per such player.

Like the submission producer, it is fire-and-forget and defensive:

- it runs off the daily loop, not a request, and never raises into it -- a failed
  feed write is logged and dropped, because the durable ``stat_snapshots`` rows
  are the source of truth and the feed is a convenience log;
- it publishes only when the capture actually wrote today's rows
  (``rows_written > 0``). A same-day re-run's ``INSERT IGNORE`` writes nothing, so
  gating on it keeps a restart from re-announcing peaks already announced today.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from app.constants.gamemodes import GameMode
from app.logging import Ansi
from app.logging import log
from app.repositories.stat_snapshots import SnapshotPeakChange
from app.services.social.activity_feed import ActivityFeedService


class FetchNewPeaks(Protocol):
    """Structural type of ``StatSnapshotsRepository.fetch_new_peaks``.

    Declared as an injected callable so the producer needs no database to test,
    matching the injected-edge style the rest of the services layer uses.
    """

    async def __call__(
        self,
        *,
        mode: int,
        snapshot_date: date,
    ) -> list[SnapshotPeakChange]: ...


async def publish_snapshot_peak_activity(
    feed_service: ActivityFeedService,
    fetch_new_peaks: FetchNewPeaks,
    *,
    snapshot_date: date,
    rows_written: int,
) -> None:
    """Publish the rank_up / pp_record events the day's capture implies.

    Intended to run as a fire-and-forget background task off the daily snapshot
    loop: it never raises into the caller (any failure is logged and swallowed),
    because a feed write must not disturb the capture that is already durably
    committed.
    """
    try:
        await _publish_snapshot_peak_activity(
            feed_service,
            fetch_new_peaks,
            snapshot_date=snapshot_date,
            rows_written=rows_written,
        )
    except Exception as exc:  # never let a feed write escape into the loop.
        log(
            f"Failed to publish snapshot activity for {snapshot_date}: {exc!r}",
            Ansi.LYELLOW,
        )


async def _publish_snapshot_peak_activity(
    feed_service: ActivityFeedService,
    fetch_new_peaks: FetchNewPeaks,
    *,
    snapshot_date: date,
    rows_written: int,
) -> None:
    # nothing was captured this run -- either an empty server or a same-day
    # re-run whose INSERT IGNORE wrote nothing. Publishing now would either find
    # no new peaks or re-announce ones already announced today, so skip entirely.
    if rows_written <= 0:
        return

    for mode in GameMode.valid_gamemodes():
        peaks = await fetch_new_peaks(mode=int(mode), snapshot_date=snapshot_date)
        for peak in peaks:
            await _publish_one(feed_service, peak)


async def _publish_one(
    feed_service: ActivityFeedService,
    peak: SnapshotPeakChange,
) -> None:
    """Publish the pp_record and/or rank_up a single peak change implies.

    A row is only returned by ``fetch_new_peaks`` when at least one of pp/rank is
    a new best, but a given row may satisfy just one; each condition is re-checked
    here so exactly the events that are true get published.
    """
    if peak.new_pp > peak.prior_best_pp:
        await feed_service.publish_pp_record(
            user_id=peak.user_id,
            mode=peak.mode,
            old_pp=peak.prior_best_pp,
            new_pp=peak.new_pp,
        )

    # a lower rank number is better; a first-ever ranking (no prior best) counts.
    if peak.new_rank is not None and (
        peak.prior_best_rank is None or peak.new_rank < peak.prior_best_rank
    ):
        await feed_service.publish_rank_up(
            user_id=peak.user_id,
            mode=peak.mode,
            old_rank=peak.prior_best_rank,
            new_rank=peak.new_rank,
        )
