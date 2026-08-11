"""The activity feed: a durable, queryable stream of notable player events.

Nothing in stock bancho.py records *history as it happens* in a form a player or
their friends can read back. The ``stats`` row holds only the current value; a
score row is a raw submission, not "player X set a new personal best"; an
achievement unlock reaches the client once and is never surfaced again. This
table is the append-only event log that closes that gap: one immutable row per
notable thing that happened (a rank milestone, a personal best, a new #1, an
achievement), with enough structured context to render a feed entry without
re-deriving it.

It is deliberately generic. ``event_type`` is an opaque string here -- the
domain vocabulary (which types exist, what each ``data`` payload means) lives in
the services layer (``app/services/social/activity_feed.py``), because a
repository should not import from the services package, exactly as
``anticheat_flags`` keeps ``severity`` a plain string rather than binding the
detector layer's enum. ``data`` is the per-type detail as JSON text (the old pp
and the new pp for a record; the beatmap and rank for a #1), so a new event type
needs no schema change.

Rows are never updated or deleted in the hot path: the feed is a log, so
``record`` only ever inserts. Reads are the interesting part. A single player's
feed and the fan-out friends feed both page by ``id`` (keyset, ``id < before``)
rather than ``OFFSET`` -- the feed only ever scrolls backwards from "now", and
keyset paging stays O(page) as the log grows instead of scanning and discarding
an ever-larger prefix. The composite ``(user_id, id)`` index serves the
per-player scan directly; the friends feed reaches the same index once per
``IN`` member. As with the rest of the schema, the foreign key to ``users`` is
enforced in application logic, not by the DB, so a purged player orphans their
events rather than cascading.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy.dialects.mysql import TINYINT

from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.repositories import Base

# `event_type` is a short slug ("rank_up", "pp_record", ...); the width is a
# defensive cap, not a schema the reader keys on.
_EVENT_TYPE_MAX_LEN = 32


class ActivityEventsTable(Base):
    __tablename__ = "activity_events"

    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    user_id = Column("user_id", Integer, nullable=False)
    event_type = Column("event_type", String(_EVENT_TYPE_MAX_LEN), nullable=False)
    # nullable: not every event is mode-specific (an achievement can be, a
    # profile-level event need not be), so mode is optional context, not a key.
    mode = Column("mode", TINYINT(1), nullable=True)
    # per-type structured detail as JSON text; see module docstring.
    data = Column("data", Text, nullable=True)
    created_at = Column(
        "created_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # the per-player and friends feeds both scan by user then walk id
        # backwards; the composite serves both the filter and the keyset order in
        # one index.
        Index("activity_events_user_id_id_index", user_id, id),
        # "all events of this type" (feed filtering, and pruning by type).
        Index("activity_events_event_type_index", event_type),
        # time-window scans and age-based retention pruning.
        Index("activity_events_created_at_index", created_at),
    )


READ_PARAMS = (
    ActivityEventsTable.id,
    ActivityEventsTable.user_id,
    ActivityEventsTable.event_type,
    ActivityEventsTable.mode,
    ActivityEventsTable.data,
    ActivityEventsTable.created_at,
)


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    id: int
    user_id: int
    event_type: str
    mode: int | None
    data: dict[str, Any] | None
    created_at: datetime


class ActivityEventsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> ActivityEvent:
        data_raw = row["data"]
        return ActivityEvent(
            id=row["id"],
            user_id=row["user_id"],
            event_type=row["event_type"],
            mode=row["mode"],
            data=json.loads(data_raw) if data_raw is not None else None,
            created_at=row["created_at"],
        )

    async def record(
        self,
        *,
        user_id: int,
        event_type: str,
        mode: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> ActivityEvent:
        """Append one event to the feed and return the stored row.

        The feed is an append-only log, so this only ever inserts -- there is no
        upsert or update path. ``data`` is serialised to compact JSON; a missing
        payload is stored as SQL ``NULL`` rather than the string ``"null"`` so
        the round-trip is a true ``None``.
        """
        data_json = (
            json.dumps(data, separators=(",", ":")) if data is not None else None
        )
        insert_stmt = insert(ActivityEventsTable).values(
            user_id=user_id,
            event_type=event_type[:_EVENT_TYPE_MAX_LEN],
            mode=mode,
            data=data_json,
            created_at=func.now(),
        )
        rec_id = await self._database.execute(insert_stmt)
        return await self._fetch(rec_id)

    async def _fetch(self, event_id: int) -> ActivityEvent:
        select_stmt = select(*READ_PARAMS).where(ActivityEventsTable.id == event_id)
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # inserted immediately before every call site.
        return self._deserialize(row)

    async def fetch_for_user(
        self,
        user_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        """One player's events, newest first, keyset-paged by id.

        ``before_id`` scrolls backwards: pass the id of the oldest event you have
        already seen to get the next older page. Absent, the newest page is
        returned. Paging by ``id < before_id`` (not ``OFFSET``) keeps each page
        O(limit) regardless of how long the log has grown.
        """
        select_stmt = select(*READ_PARAMS).where(
            ActivityEventsTable.user_id == user_id,
        )
        if before_id is not None:
            select_stmt = select_stmt.where(ActivityEventsTable.id < before_id)

        select_stmt = select_stmt.order_by(ActivityEventsTable.id.desc()).limit(limit)
        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize(row) for row in rows]

    async def fetch_feed(
        self,
        user_ids: list[int],
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        """The merged feed across several players (e.g. a user's friends).

        An empty ``user_ids`` returns ``[]`` without touching the database -- a
        player with no friends has an empty feed, and an empty ``IN ()`` would be
        invalid SQL. Same keyset paging as ``fetch_for_user``: newest first,
        ``id < before_id`` for the next older page.
        """
        if not user_ids:
            return []

        select_stmt = select(*READ_PARAMS).where(
            ActivityEventsTable.user_id.in_(user_ids),
        )
        if before_id is not None:
            select_stmt = select_stmt.where(ActivityEventsTable.id < before_id)

        select_stmt = select_stmt.order_by(ActivityEventsTable.id.desc()).limit(limit)
        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize(row) for row in rows]
