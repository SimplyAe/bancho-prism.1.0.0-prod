"""Durable spectator-session history.

Stock bancho.py models spectating as a purely in-memory relationship. A viewer
who starts spectating a host is appended to ``Player.spectators`` and points
back through ``Player.spectating`` (see ``app.objects.player``); both are live
attributes that exist only for the length of a connection and vanish on logout
or restart. Nothing is written down, so "who watched whom, and for how long"
has no answer once either side disconnects. This table is that record.

One row per spectate session:

- ``host_id`` -- the player being watched.
- ``spectator_id`` -- the player watching.
- ``started_at`` -- stamped when the viewer starts spectating (START_SPECTATING).
- ``ended_at`` -- stamped when they stop (STOP_SPECTATING, or logout, which
  stops it for them). ``NULL`` marks a session that was still open when the
  server last ran; like ``mp_matches.disbanded_at`` it is simply never stamped
  if the process died mid-session, never back-filled.

Reads page by ``id`` (keyset, ``id < before_id``), never ``OFFSET``: session
history only ever scrolls backwards from "now", and keyset paging stays O(page)
as the history grows. The composite ``(host_id, id)`` and ``(spectator_id, id)``
indexes serve the two filtered listings -- "who watched this host" and "who did
this viewer watch" -- each in a single scan. As with the rest of the schema the
foreign keys (``host_id`` / ``spectator_id`` -> users) are enforced in
application logic, not the DB, so a purged player orphans rather than cascades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update

from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.repositories import Base


class SpectatorSessionsTable(Base):
    __tablename__ = "spectator_sessions"

    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    host_id = Column("host_id", Integer, nullable=False)
    spectator_id = Column("spectator_id", Integer, nullable=False)
    started_at = Column(
        "started_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    ended_at = Column("ended_at", DateTime, nullable=True)

    __table_args__ = (
        # "who watched this host", newest-first keyset scan.
        Index("spectator_sessions_host_id_id_index", host_id, id),
        # "who did this viewer watch", newest-first keyset scan.
        Index("spectator_sessions_spectator_id_id_index", spectator_id, id),
        # time-window scans and retention pruning.
        Index("spectator_sessions_started_at_index", started_at),
    )


READ_PARAMS = (
    SpectatorSessionsTable.id,
    SpectatorSessionsTable.host_id,
    SpectatorSessionsTable.spectator_id,
    SpectatorSessionsTable.started_at,
    SpectatorSessionsTable.ended_at,
)


@dataclass(frozen=True, slots=True)
class SpectatorSession:
    id: int
    host_id: int
    spectator_id: int
    started_at: datetime
    ended_at: datetime | None


class SpectatorSessionsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> SpectatorSession:
        return SpectatorSession(
            id=row["id"],
            host_id=row["host_id"],
            spectator_id=row["spectator_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )

    async def start_session(
        self,
        *,
        host_id: int,
        spectator_id: int,
    ) -> SpectatorSession:
        """Record a spectate session opening and return the stored row.

        The returned ``id`` is the durable session id the live spectator stashes
        so its later close can stamp the matching ``ended_at``.
        """
        insert_stmt = insert(SpectatorSessionsTable).values(
            host_id=host_id,
            spectator_id=spectator_id,
            started_at=func.now(),
        )
        rec_id = await self._database.execute(insert_stmt)
        return await self._fetch_one(rec_id)

    async def _fetch_one(self, session_id: int) -> SpectatorSession:
        select_stmt = select(*READ_PARAMS).where(
            SpectatorSessionsTable.id == session_id,
        )
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # inserted immediately before every call site.
        return self._deserialize(row)

    async def end_session(self, session_id: int) -> None:
        """Stamp ``ended_at`` when a spectate session closes.

        Guarded by ``ended_at IS NULL`` so a session is only ever closed once: a
        duplicate stop (or a late-arriving event) cannot rewrite the original end
        time.
        """
        update_stmt = (
            update(SpectatorSessionsTable)
            .where(SpectatorSessionsTable.id == session_id)
            .where(SpectatorSessionsTable.ended_at.is_(None))
            .values(ended_at=func.now())
        )
        await self._database.execute(update_stmt)

    async def fetch_session(self, session_id: int) -> SpectatorSession | None:
        """One session by its durable id, or ``None`` if unknown."""
        select_stmt = select(*READ_PARAMS).where(
            SpectatorSessionsTable.id == session_id,
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def fetch_sessions(
        self,
        *,
        host_id: int | None = None,
        spectator_id: int | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[SpectatorSession]:
        """Sessions, newest first, keyset-paged by id.

        ``host_id`` and ``spectator_id`` are optional, independently composable
        filters: pass ``host_id`` for "who watched this host", ``spectator_id``
        for "who did this viewer watch", both to pin a single pair, or neither
        for the global feed. ``before_id`` scrolls backwards -- pass the id of
        the oldest session already seen for the next older page. Paging by
        ``id < before_id`` (not ``OFFSET``) keeps each page O(limit) as history
        grows.
        """
        select_stmt = select(*READ_PARAMS)
        if host_id is not None:
            select_stmt = select_stmt.where(
                SpectatorSessionsTable.host_id == host_id,
            )
        if spectator_id is not None:
            select_stmt = select_stmt.where(
                SpectatorSessionsTable.spectator_id == spectator_id,
            )
        if before_id is not None:
            select_stmt = select_stmt.where(SpectatorSessionsTable.id < before_id)

        select_stmt = select_stmt.order_by(SpectatorSessionsTable.id.desc()).limit(
            limit,
        )
        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize(row) for row in rows]
