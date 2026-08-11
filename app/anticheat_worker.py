#!/usr/bin/env python3.11
"""Standalone entrypoint for the anticheat replay-analysis worker.

Runs the queue consumer and backfill loops in their *own* process, separate from
the web server, exactly as the reliability plan calls for. Analysing a replay is
CPU-bound (LZMA decompress + geometry over thousands of frames); doing it inside
the API process would contend with latency-sensitive score submission and
leaderboard requests. A separate process also means analysis can be scaled or
restarted independently of serving traffic.

The worker shares the same durable stores as the web server -- the same MySQL
``score_replay_stats`` table and the same Redis queue -- so the two coordinate
purely through those, never in-process. Run one (or several; duplicates are
safe) alongside the server:

    python -m app.anticheat_worker
"""

from __future__ import annotations

import asyncio
import logging

import app.logging
import app.settings
import app.state.services
import app.utils
from app.logging import Ansi
from app.logging import log
from app.services.anticheat.worker_runtime import run_replay_analysis_worker

app.logging.configure_logging()


async def _serve() -> None:
    app.utils.ensure_persistent_volumes_are_available()

    await app.state.services.database.connect()
    await app.state.services.redis.initialize()  # type: ignore[unused-awaitable]
    await app.state.services.run_sql_migrations()

    try:
        await run_replay_analysis_worker()
    finally:
        # mirror the web server's shutdown order for the stores we opened.
        await app.state.services.database.disconnect()
        await app.state.services.redis.aclose()


def main() -> int:
    logging.getLogger().setLevel(logging.WARNING)
    log("Anticheat replay-analysis worker starting.", Ansi.LMAGENTA)
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        # a clean operator-initiated stop; the loops were cancelled and the
        # stores closed by the `finally` above.
        log("Anticheat worker stopped.", Ansi.LMAGENTA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
