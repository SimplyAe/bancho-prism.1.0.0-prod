"""Capturing the daily per-player, per-mode stat snapshots.

The ``stat_snapshots`` table (see ``app.repositories.stat_snapshots``) is the
historical time series that rank graphs, peak rank, and the anticheat
behavioural baselines all read out of. Something has to actually write a row per
player per mode each day, and that is this service: once a day it asks the
repository to snapshot every ranked player in every mode under today's date.

It is deliberately tiny and pure -- a frozen dataclass over two injected edges (a
per-mode capture callable and a "what day is it" clock) with no globals -- so the
schedule that drives it (a daily supervised loop) and the capture itself can be
tested apart. Idempotency is not this service's job: the repository's bulk
capture is ``INSERT IGNORE`` on the per-day unique key, so running twice on the
same calendar day (a restart, a manual re-trigger) simply writes nothing the
second time rather than duplicating or churning rows. That is why the loop can
safely ``run_immediately`` on every boot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.constants.gamemodes import GameMode
from app.logging import Ansi
from app.logging import log


class CaptureModeSnapshot(Protocol):
    """Snapshot every ranked player in one mode for a day; returns rows written.

    Satisfied by ``StatSnapshotsRepository.capture_mode``; injected as a bare
    callable so the service needs no database to test.
    """

    async def __call__(self, *, mode: int, snapshot_date: date) -> int: ...


class TodayProvider(Protocol):
    """The current calendar day the snapshot is filed under (injected clock)."""

    def __call__(self) -> date: ...


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    snapshot_date: date
    modes_captured: int
    rows_written: int


@dataclass(frozen=True)
class StatSnapshotService:
    capture_mode: CaptureModeSnapshot
    today: TodayProvider

    async def capture_today(self) -> SnapshotResult:
        """Snapshot every ranked player in every mode under today's date.

        Modes are limited to the ones bancho.py actually assigns scores to
        (``GameMode.valid_gamemodes()``, matching the leaderboard rebuild), so
        the unused rx!mania / ap!* modes are never touched. Re-running the same
        day is a harmless no-op thanks to the repository's per-day ``INSERT
        IGNORE``.
        """
        snapshot_date = self.today()

        modes_captured = 0
        rows_written = 0
        for mode in GameMode.valid_gamemodes():
            rows_written += await self.capture_mode(
                mode=int(mode),
                snapshot_date=snapshot_date,
            )
            modes_captured += 1

        log(
            f"Captured stat snapshots for {snapshot_date}: "
            f"{rows_written} row(s) across {modes_captured} mode(s).",
            Ansi.LGREEN,
        )
        return SnapshotResult(
            snapshot_date=snapshot_date,
            modes_captured=modes_captured,
            rows_written=rows_written,
        )
