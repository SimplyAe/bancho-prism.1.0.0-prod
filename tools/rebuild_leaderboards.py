#!/usr/bin/env python3.11
"""Rebuild the redis leaderboards from mysql.

Player ranks live in redis sorted sets derived from `stats.pp`. If redis
loses that data -- a flush, an eviction, a restart without a persisted
AOF -- every player reads as unranked, and only recovers if they log in
or submit a score again. This tool restores them from mysql, which is
the source of truth.

Safe to run against a live server: leaderboards are staged into
temporary keys and swapped in atomically, so readers see either the old
or the new set, never a half-built one.

    python -m tools.rebuild_leaderboards            # all modes
    python -m tools.rebuild_leaderboards -m 0 4     # vn!std and rx!std
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from redis import asyncio as aioredis

import app.settings
from app.adapters.database import Database
from app.constants.gamemodes import GameMode
from app.logging import Ansi
from app.logging import log
from app.repositories.leaderboard_ranks import LeaderboardRanksRepository
from app.repositories.stats import StatsRepository
from app.services.leaderboard_recovery import DEFAULT_BATCH_SIZE
from app.services.leaderboard_recovery import LeaderboardRecoveryService

VALID_MODES = [str(int(mode)) for mode in GameMode.valid_gamemodes()]


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-m",
        "--mode",
        nargs=argparse.ONE_OR_MORE,
        required=False,
        default=VALID_MODES,
        choices=VALID_MODES,
        help="gamemode(s) to rebuild; defaults to all",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="rows to read from mysql per batch",
    )
    args = parser.parse_args(argv)

    database = Database(app.settings.DB_DSN)
    await database.connect()

    redis: aioredis.Redis = aioredis.from_url(app.settings.REDIS_DSN)
    _ = await redis.initialize()

    service = LeaderboardRecoveryService(
        stats=StatsRepository(database),
        leaderboard_ranks=LeaderboardRanksRepository(redis),
        batch_size=args.batch_size,
    )

    try:
        total_players = 0
        total_leaderboards = 0

        for raw_mode in args.mode:
            mode = GameMode(int(raw_mode))
            players, leaderboards = await service.rebuild_mode(int(mode))

            log(
                f"{mode!r}: {players} ranked player(s) across "
                f"{leaderboards} leaderboard(s).",
                Ansi.LCYAN,
            )

            total_players += players
            total_leaderboards += leaderboards

        log(
            f"Done. {total_players} ranked entries across "
            f"{total_leaderboards} leaderboard(s).",
            Ansi.LGREEN,
        )
    finally:
        await database.disconnect()
        await redis.aclose()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
