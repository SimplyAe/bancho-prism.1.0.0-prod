#!/usr/bin/env python3.11
"""Reconcile score rows against replay files on disk.

Score submission writes two things: a row in `scores`, and a `.osr` file
named after that row's id. They can drift apart:

- **orphaned pending replays** -- a replay staged to `{checksum}.osr.pending`
  whose score row was never committed (the process died mid-submission).
  Harmless, but they accumulate.
- **missing replays** -- a score row with no `.osr` on disk. Usually the
  legacy failure mode: older builds wrote the replay *after* committing
  the row, so a crash in between lost the replay permanently. These
  cannot be repaired, only reported -- the bytes only ever existed in
  the original request.
- **orphaned replays** -- an `.osr` whose score row is gone (e.g. wiped
  by a moderator). Safe to delete.

Reports by default; pass --delete to actually remove reclaimable files.

    python -m tools.reconcile_replays
    python -m tools.reconcile_replays --delete --pending-age-hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Sequence
from pathlib import Path

import app.settings
from app.adapters.database import Database
from app.logging import Ansi
from app.logging import log

# kept local rather than imported from app.api.dependencies, which would pull
# the whole FastAPI/state layer into a standalone script (see tools/recalc.py).
REPLAYS_PATH = Path.cwd() / ".data/osr"


async def _fetch_all_score_ids(database: Database) -> set[int]:
    rows = await database.fetch_all("SELECT id FROM scores")
    return {int(row["id"]) for row in rows}


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replays-path",
        type=Path,
        default=REPLAYS_PATH,
        help="directory holding .osr files",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete reclaimable files instead of only reporting them",
    )
    parser.add_argument(
        "--pending-age-hours",
        type=float,
        default=24.0,
        help=(
            "only treat a .pending replay as orphaned once it is this old, "
            "so an in-flight submission is never deleted out from under itself"
        ),
    )
    args = parser.parse_args(argv)

    replays_path: Path = args.replays_path
    if not replays_path.is_dir():
        log(f"No such replays directory: {replays_path}", Ansi.LRED)
        return 1

    database = Database(app.settings.DB_DSN)
    await database.connect()

    try:
        score_ids = await _fetch_all_score_ids(database)
        log(f"{len(score_ids)} score row(s) in the database.", Ansi.LCYAN)

        orphaned_pending: list[Path] = []
        orphaned_replays: list[Path] = []
        on_disk: set[int] = set()

        pending_cutoff = time.time() - (args.pending_age_hours * 3600)

        for path in replays_path.iterdir():
            if not path.is_file():
                continue

            if path.name.endswith(".osr.pending"):
                # a staged replay whose submission never completed.
                if path.stat().st_mtime < pending_cutoff:
                    orphaned_pending.append(path)
                continue

            if path.suffix != ".osr":
                continue

            try:
                score_id = int(path.stem)
            except ValueError:
                continue

            on_disk.add(score_id)
            if score_id not in score_ids:
                orphaned_replays.append(path)

        missing_replays = sorted(score_ids - on_disk)

        log(
            f"{len(orphaned_pending)} orphaned pending replay(s) "
            f"older than {args.pending_age_hours}h.",
            Ansi.LYELLOW if orphaned_pending else Ansi.LGREEN,
        )
        log(
            f"{len(orphaned_replays)} replay file(s) with no score row.",
            Ansi.LYELLOW if orphaned_replays else Ansi.LGREEN,
        )
        log(
            f"{len(missing_replays)} score row(s) with no replay on disk.",
            Ansi.LRED if missing_replays else Ansi.LGREEN,
        )

        if missing_replays:
            preview = ", ".join(str(score_id) for score_id in missing_replays[:20])
            suffix = ", ..." if len(missing_replays) > 20 else ""
            log(
                f"  unrecoverable score ids: {preview}{suffix}",
                Ansi.GRAY,
            )

        if args.delete:
            deleted = 0
            for path in (*orphaned_pending, *orphaned_replays):
                try:
                    path.unlink(missing_ok=True)
                    deleted += 1
                except OSError as exc:
                    log(f"Failed to delete {path}: {exc!r}", Ansi.LRED)
            log(f"Deleted {deleted} file(s).", Ansi.LGREEN)
        elif orphaned_pending or orphaned_replays:
            log("Re-run with --delete to remove them.", Ansi.GRAY)
    finally:
        await database.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
