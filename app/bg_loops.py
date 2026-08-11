from __future__ import annotations

import asyncio
import time
from datetime import date
from datetime import datetime
from datetime import timezone

import app.metrics
import app.packets
import app.settings
import app.state
from app.bg_task_supervision import run_supervised_loop
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log
from app.repositories.stat_snapshots import StatSnapshotsRepository
from app.services.stat_snapshots import StatSnapshotService

OSU_CLIENT_MIN_PING_INTERVAL = 300000 // 1000  # defined by osu!

# once per day: the snapshot is filed under a calendar date, and the per-day
# unique key makes an extra run within the same day a no-op, so exact alignment
# to midnight does not matter -- one capture per day is what the key enforces.
STAT_SNAPSHOT_INTERVAL = 24 * 60 * 60


async def initialize_housekeeping_tasks() -> None:
    """Create tasks for each housekeeping tasks."""
    log("Initializing housekeeping tasks.", Ansi.LCYAN)

    loop = asyncio.get_running_loop()

    app.state.sessions.housekeeping_tasks.update(
        {
            loop.create_task(task)
            for task in (
                run_supervised_loop(
                    "expired-donation-privileges",
                    interval=30 * 60,
                    run_immediately=True,
                    work=_remove_expired_donation_privileges,
                ),
                run_supervised_loop(
                    "bot-status",
                    interval=5 * 60,
                    run_immediately=True,
                    work=_update_bot_status,
                ),
                run_supervised_loop(
                    "disconnect-ghosts",
                    interval=OSU_CLIENT_MIN_PING_INTERVAL // 3,
                    run_immediately=True,
                    work=_disconnect_ghosts,
                ),
                run_supervised_loop(
                    "stat-snapshots",
                    interval=STAT_SNAPSHOT_INTERVAL,
                    run_immediately=True,
                    work=_capture_stat_snapshots,
                ),
            )
        },
    )


async def _capture_stat_snapshots() -> None:
    """Write today's per-player, per-mode stat snapshots (Track 3.1).

    The historical time series has to be written *while it is still today* --
    once the day rolls over the live ``stats`` values are the next day's, and
    the row that should have been captured is gone for good. So this runs on
    every boot (``run_immediately``) and then daily; the per-day unique key
    makes the repeated same-day runs harmless no-ops.
    """
    service = StatSnapshotService(
        capture_mode=StatSnapshotsRepository(
            app.state.services.database,
        ).capture_mode,
        today=_utc_today,
    )

    result = await service.capture_today()

    # distinct from the loop's iteration counter: this exposes whether the
    # capture actually wrote anything, so a silently-empty capture is visible.
    app.metrics.set_stat_snapshot_rows_written(result.rows_written)


def _utc_today() -> date:
    """The current UTC calendar day a snapshot is filed under.

    UTC, not local time, so the day boundary is stable and identical across
    every process regardless of host timezone.
    """
    return datetime.now(timezone.utc).date()


async def _remove_expired_donation_privileges() -> None:
    """Remove donation privileges from users with expired sessions."""
    if app.settings.DEBUG:
        log("Removing expired donation privileges.", Ansi.LMAGENTA)

    expired_donors = await app.state.services.database.fetch_all(
        "SELECT id FROM users "
        "WHERE donor_end <= UNIX_TIMESTAMP() "
        "AND priv & :donor_priv",
        {"donor_priv": Privileges.DONATOR.value},
    )

    for expired_donor in expired_donors:
        player = await app.state.sessions.players.from_cache_or_sql(
            id=expired_donor["id"],
        )

        if player is None:
            # a user can be deleted between the SELECT above and this
            # lookup; skipping one donor is correct, while an assert here
            # would kill the whole loop -- and donor privileges would
            # then silently never expire again.
            log(
                f"Expired-donor sweep: user {expired_donor['id']} not found; "
                "skipping.",
                Ansi.LYELLOW,
            )
            continue

        # TODO: perhaps make a `revoke_donor` method?
        await player.remove_privs(Privileges.DONATOR)
        player.donor_end = 0
        await app.state.services.database.execute(
            "UPDATE users SET donor_end = 0 WHERE id = :id",
            {"id": player.id},
        )

        if player.is_online:
            player.enqueue(
                app.packets.notification("Your supporter status has expired."),
            )

        log(f"{player}'s supporter status has expired.", Ansi.LMAGENTA)


async def _disconnect_ghosts() -> None:
    """Actively disconnect users above the
    disconnection time threshold on the osu! server."""
    current_time = time.time()

    for player in app.state.sessions.players:
        if current_time - player.last_recv_time > OSU_CLIENT_MIN_PING_INTERVAL:
            log(f"Auto-dced {player}.", Ansi.LMAGENTA)
            player.logout()


async def _update_bot_status() -> None:
    """Re roll the bot status, every `interval`."""
    app.packets.bot_stats.cache_clear()
