"""Guards for the daily stat-snapshot service.

The service is the scheduler-facing half of the snapshot machinery: once a day
it captures every ranked player, in every mode bancho.py actually uses, under a
single calendar date. These tests pin exactly that contract against a fake
capture callable and a fixed clock -- no database, no wall clock:

- one capture per valid gamemode, and *only* the valid ones (the unused
  rx!mania / ap!* modes are never touched), all filed under the same date the
  injected clock reports;
- the reported result totals the rows the repository said it wrote, so a
  silently-empty capture is distinguishable from a busy one;
- the date comes entirely from the injected clock, so the snapshot the loop
  writes at boot is filed under "today" and nothing reaches for the real clock.

Idempotency itself is the repository's job (its per-day ``INSERT IGNORE``), not
this service's, so it is asserted there rather than here.
"""

from __future__ import annotations

from datetime import date

from app.constants.gamemodes import GameMode
from app.services.stat_snapshots import StatSnapshotService


class _RecordingCapture:
    """Records each (mode, date) captured and reports a fixed rows-written."""

    def __init__(self, rows_per_mode: int = 1) -> None:
        self.calls: list[tuple[int, date]] = []
        self._rows_per_mode = rows_per_mode

    async def __call__(self, *, mode: int, snapshot_date: date) -> int:
        self.calls.append((mode, snapshot_date))
        return self._rows_per_mode


def _service(capture: _RecordingCapture, today: date) -> StatSnapshotService:
    return StatSnapshotService(capture_mode=capture, today=lambda: today)


async def test_captures_every_valid_gamemode_exactly_once() -> None:
    capture = _RecordingCapture()
    service = _service(capture, date(2026, 8, 11))

    result = await service.capture_today()

    captured_modes = [mode for mode, _ in capture.calls]
    expected_modes = [int(mode) for mode in GameMode.valid_gamemodes()]
    assert captured_modes == expected_modes
    assert result.modes_captured == len(expected_modes)


async def test_does_not_touch_the_unused_gamemodes() -> None:
    capture = _RecordingCapture()
    service = _service(capture, date(2026, 8, 11))

    await service.capture_today()

    captured_modes = {mode for mode, _ in capture.calls}
    # the four modes bancho.py never assigns scores to must never be captured.
    for unused in (
        GameMode.RELAX_MANIA,
        GameMode.AUTOPILOT_TAIKO,
        GameMode.AUTOPILOT_CATCH,
        GameMode.AUTOPILOT_MANIA,
    ):
        assert int(unused) not in captured_modes


async def test_files_every_capture_under_the_clock_date() -> None:
    capture = _RecordingCapture()
    today = date(2026, 8, 11)
    service = _service(capture, today)

    result = await service.capture_today()

    # the date comes from the clock, uniformly, for every mode.
    assert all(snapshot_date == today for _, snapshot_date in capture.calls)
    assert result.snapshot_date == today


async def test_rows_written_totals_what_the_repository_reported() -> None:
    capture = _RecordingCapture(rows_per_mode=10)
    service = _service(capture, date(2026, 8, 11))

    result = await service.capture_today()

    assert result.rows_written == 10 * len(GameMode.valid_gamemodes())


async def test_reports_zero_rows_without_error_when_nothing_is_ranked() -> None:
    # a fresh server with no ranked players still captures cleanly (0 rows);
    # this is the signal the metric surfaces, not an error condition.
    capture = _RecordingCapture(rows_per_mode=0)
    service = _service(capture, date(2026, 8, 11))

    result = await service.capture_today()

    assert result.rows_written == 0
    assert result.modes_captured == len(GameMode.valid_gamemodes())


async def test_reads_the_clock_once_per_capture() -> None:
    # a single logical "today" per run: every mode in one capture shares the
    # same date even if the clock would tick between modes.
    reads: list[int] = []

    def clock() -> date:
        reads.append(1)
        return date(2026, 8, 11)

    capture = _RecordingCapture()
    service = StatSnapshotService(capture_mode=capture, today=clock)

    await service.capture_today()

    assert sum(reads) == 1
