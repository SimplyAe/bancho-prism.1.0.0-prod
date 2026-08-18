"""Moderation state for beatmap sets hosted on this server.

One row per submitted *set*. The `maps` table already holds everything the game
needs about each difficulty; what it has no room for is the submission's own
history -- who uploaded it, whether a human has looked at it yet, and what they
decided. That is what lives here.

``review_state`` is the field that matters, and it is staff-only. It gates two
things: whether other players can see the submission at all, and whether the
submitter may raise their own map to a status that gives it a leaderboard. A
submitter can never write it, and can never reach a pp-awarding status through it
-- see ``app.services.beatmap_submissions`` for the full matrix and why.

``declared_creator`` records what the uploaded ``.osu`` files *claimed* about their
author, which is attacker-controlled and kept only for review context: the
authoritative creator is the submitting account, written into ``maps.creator``.
``osz_sha256`` identifies the exact archive that was accepted, so a re-upload of
the same bytes is recognisable.

As elsewhere in this schema the foreign keys (``submitter_user_id`` /
``reviewed_by`` -> users) are enforced in application logic rather than the DB, so
a purged player orphans a submission rather than cascading it away.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update

from app._typing import UNSET
from app._typing import _UnsetSentinel
from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.repositories import Base

_DECLARED_CREATOR_MAX_LEN = 64
_REVIEW_NOTE_MAX_LEN = 512
_SHA256_HEX_LEN = 64


class MapSubmissionReviewState(StrEnum):
    """Where a submission sits in review.

    ``PENDING`` is where every submission starts: playable by its owner, invisible
    to everyone else. ``APPROVED`` makes it publicly visible and unlocks the
    owner's ability to give it a leaderboard. ``REJECTED`` forces every difficulty
    back to Pending, so a rejection removes any leaderboard immediately.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MapSubmissionsTable(Base):
    __tablename__ = "map_submissions"

    # the privately-allocated set id; never generated here, always supplied.
    set_id = Column(
        "set_id",
        Integer,
        nullable=False,
        primary_key=True,
        autoincrement=False,
    )
    submitter_user_id = Column("submitter_user_id", Integer, nullable=False)
    review_state = Column(
        "review_state",
        Enum(MapSubmissionReviewState, name="review_state"),
        nullable=False,
        server_default=MapSubmissionReviewState.PENDING.value,
    )
    declared_creator = Column(
        "declared_creator",
        String(_DECLARED_CREATOR_MAX_LEN, collation="utf8"),
        nullable=False,
    )
    difficulty_count = Column("difficulty_count", Integer, nullable=False)
    osz_size_bytes = Column("osz_size_bytes", Integer, nullable=False)
    osz_sha256 = Column("osz_sha256", String(_SHA256_HEX_LEN), nullable=False)
    submitted_at = Column(
        "submitted_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        "updated_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    reviewed_by = Column("reviewed_by", Integer, nullable=True)
    reviewed_at = Column("reviewed_at", DateTime, nullable=True)
    review_note = Column(
        "review_note",
        String(_REVIEW_NOTE_MAX_LEN, collation="utf8"),
        nullable=True,
    )

    __table_args__ = (
        Index("map_submissions_submitter_user_id_index", "submitter_user_id"),
        Index("map_submissions_review_state_index", "review_state"),
    )


READ_PARAMS = (
    MapSubmissionsTable.set_id,
    MapSubmissionsTable.submitter_user_id,
    MapSubmissionsTable.review_state,
    MapSubmissionsTable.declared_creator,
    MapSubmissionsTable.difficulty_count,
    MapSubmissionsTable.osz_size_bytes,
    MapSubmissionsTable.osz_sha256,
    MapSubmissionsTable.submitted_at,
    MapSubmissionsTable.updated_at,
    MapSubmissionsTable.reviewed_by,
    MapSubmissionsTable.reviewed_at,
    MapSubmissionsTable.review_note,
)


@dataclass(frozen=True, slots=True)
class MapSubmission:
    set_id: int
    submitter_user_id: int
    review_state: str
    declared_creator: str
    difficulty_count: int
    osz_size_bytes: int
    osz_sha256: str
    submitted_at: datetime
    updated_at: datetime
    reviewed_by: int | None
    reviewed_at: datetime | None
    review_note: str | None


class MapSubmissionsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> MapSubmission:
        return MapSubmission(
            set_id=row["set_id"],
            submitter_user_id=row["submitter_user_id"],
            review_state=str(row["review_state"]),
            declared_creator=row["declared_creator"],
            difficulty_count=row["difficulty_count"],
            osz_size_bytes=row["osz_size_bytes"],
            osz_sha256=row["osz_sha256"],
            submitted_at=row["submitted_at"],
            updated_at=row["updated_at"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_note=row["review_note"],
        )

    async def _fetch(self, set_id: int) -> MapSubmission:
        submission = await self.fetch_one(set_id)
        assert submission is not None  # written immediately before every call site.
        return submission

    async def create(
        self,
        *,
        set_id: int,
        submitter_user_id: int,
        declared_creator: str,
        difficulty_count: int,
        osz_size_bytes: int,
        osz_sha256: str,
    ) -> MapSubmission:
        """Record a newly submitted set, pending review."""
        insert_stmt = insert(MapSubmissionsTable).values(
            set_id=set_id,
            submitter_user_id=submitter_user_id,
            # every submission starts unreviewed; nothing may create an approved
            # one, so there is no parameter for it.
            review_state=MapSubmissionReviewState.PENDING,
            declared_creator=declared_creator[:_DECLARED_CREATOR_MAX_LEN],
            difficulty_count=difficulty_count,
            osz_size_bytes=osz_size_bytes,
            osz_sha256=osz_sha256,
            submitted_at=func.now(),
            updated_at=func.now(),
        )
        await self._database.execute(insert_stmt)
        return await self._fetch(set_id)

    async def fetch_one(self, set_id: int) -> MapSubmission | None:
        """A single submission, or None if the set was never submitted here."""
        select_stmt = select(*READ_PARAMS).where(
            MapSubmissionsTable.set_id == set_id,
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def fetch_many(
        self,
        *,
        submitter_user_id: int | None = None,
        review_state: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> list[MapSubmission]:
        """Submissions, newest first, optionally filtered by owner and state."""
        select_stmt = select(*READ_PARAMS)
        if submitter_user_id is not None:
            select_stmt = select_stmt.where(
                MapSubmissionsTable.submitter_user_id == submitter_user_id,
            )
        if review_state is not None:
            select_stmt = select_stmt.where(
                MapSubmissionsTable.review_state == review_state,
            )

        select_stmt = select_stmt.order_by(MapSubmissionsTable.set_id.desc())

        if page is not None and page_size is not None:
            select_stmt = select_stmt.limit(page_size).offset((page - 1) * page_size)

        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize(row) for row in rows]

    async def fetch_count(
        self,
        *,
        submitter_user_id: int | None = None,
        review_state: str | None = None,
    ) -> int:
        """How many submissions match; the same filters as ``fetch_many``."""
        select_stmt = select(func.count().label("count")).select_from(
            MapSubmissionsTable,
        )
        if submitter_user_id is not None:
            select_stmt = select_stmt.where(
                MapSubmissionsTable.submitter_user_id == submitter_user_id,
            )
        if review_state is not None:
            select_stmt = select_stmt.where(
                MapSubmissionsTable.review_state == review_state,
            )

        row = await self._database.fetch_one(select_stmt)
        assert row is not None
        return int(row["count"])

    async def partial_update(
        self,
        set_id: int,
        *,
        review_state: str | _UnsetSentinel = UNSET,
        reviewed_by: int | None | _UnsetSentinel = UNSET,
        review_note: str | None | _UnsetSentinel = UNSET,
    ) -> MapSubmission | None:
        """Update review fields, leaving anything not passed untouched."""
        update_stmt = update(MapSubmissionsTable).where(
            MapSubmissionsTable.set_id == set_id,
        )
        if not isinstance(review_state, _UnsetSentinel):
            update_stmt = update_stmt.values(review_state=review_state)
        if not isinstance(reviewed_by, _UnsetSentinel):
            # stamped together: a decision always records who made it and when.
            update_stmt = update_stmt.values(
                reviewed_by=reviewed_by,
                reviewed_at=func.now(),
            )
        if not isinstance(review_note, _UnsetSentinel):
            update_stmt = update_stmt.values(
                review_note=(
                    review_note[:_REVIEW_NOTE_MAX_LEN]
                    if review_note is not None
                    else None
                ),
            )

        update_stmt = update_stmt.values(updated_at=func.now())
        await self._database.execute(update_stmt)
        return await self.fetch_one(set_id)

    async def delete_one(self, set_id: int) -> None:
        """Remove a submission record; a no-op if it was never there."""
        delete_stmt = delete(MapSubmissionsTable).where(
            MapSubmissionsTable.set_id == set_id,
        )
        await self._database.execute(delete_stmt)
