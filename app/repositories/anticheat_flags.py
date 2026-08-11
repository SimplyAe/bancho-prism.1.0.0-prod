"""The staff review queue: durable records of flagged replay analyses.

This is the other side of **flag, never auto-ban**. The detector pipeline
(``app/services/anticheat/detectors.py``) grades a replay and the worker decides
a report is worth a human's attention (``report.flagged`` -- any signal reached
MEDIUM). Until now that decision only reached a log line, which is not something
a reviewer can query, sort, or act on. This table is the queue: one durable row
per flagged score, holding the strongest signal and its evidence, plus the
resolution a staff member records once they have looked at it.

One row per score, keyed on ``score_id`` (no DB-level foreign key, matching the
rest of the schema -- a purged score simply orphans its flag rather than
cascading). Re-analysis is expected and safe: a score can be re-flagged after a
threshold change or a re-queue, so ``record`` is an upsert. It deliberately
refreshes only the *detection* columns (severity, top signal, evidence,
``last_flagged_at``) and never touches the *resolution* columns (``status``,
``resolved_by``, ``resolved_at``, ``resolution_note``) or ``first_flagged_at``.
That is the important half: a reviewer who has already dismissed a flag does not
see it silently re-open every time the backfill loop re-analyses the score, and
the original flag time is preserved -- while the refreshed evidence is still
there if they choose to look again.

``severity`` is stored as an opaque string rather than an ``Enum`` bound to the
detector layer's ``Severity``: a repository should not import from the services
package, and only MEDIUM/HIGH ever reach here anyway (that is what "flagged"
means). ``evidence`` is the raw numbers behind the top signal, stored as JSON so
a reviewer -- or a later re-derivation of thresholds -- can see exactly what
tripped it without re-parsing the ``.osr``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.mysql import FLOAT
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.dialects.mysql import Insert as MysqlInsert
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.repositories import Base

# defensive column-width caps: a flag write must never fail because a detector's
# human-readable title or detail string grew longer than the column. The numbers
# behind the call live in `evidence` (TEXT), so truncating prose is lossless.
_TITLE_MAX_LEN = 128
_DETAIL_MAX_LEN = 512
_NOTE_MAX_LEN = 512


class AnticheatFlagStatus(StrEnum):
    """Lifecycle of one flagged score in the review queue.

    ``OPEN`` is a fresh flag no one has looked at. ``REVIEWING`` lets a staff
    member claim it. ``ACTIONED`` / ``DISMISSED`` are the terminal verdicts --
    the policy is that *a human* records these, never the detector.
    """

    OPEN = "open"
    REVIEWING = "reviewing"
    ACTIONED = "actioned"
    DISMISSED = "dismissed"


class AnticheatFlagsTable(Base):
    __tablename__ = "anticheat_flags"

    # scores.id is modelled as Integer elsewhere (see ScoresTable); mirror it.
    # autoincrement is disabled explicitly: the value is always the supplied
    # score id, never generated -- without this a lone integer PK would become
    # AUTO_INCREMENT.
    score_id = Column(
        "score_id",
        Integer,
        nullable=False,
        primary_key=True,
        autoincrement=False,
    )
    # denormalised from `scores.userid` so "all flags for this player" (the
    # repeat-offender view) does not join the large scores table on every read.
    user_id = Column("user_id", Integer, nullable=False)
    mode = Column("mode", TINYINT(1), nullable=False)
    status = Column(
        "status",
        Enum(AnticheatFlagStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=AnticheatFlagStatus.OPEN.value,
    )
    # the report's rolled-up severity string ("medium" / "high"); see module
    # docstring for why this is not an Enum bound to the detector layer.
    severity = Column("severity", String(16), nullable=False)
    top_signal_code = Column("top_signal_code", String(16), nullable=False)
    top_signal_title = Column("top_signal_title", String(_TITLE_MAX_LEN), nullable=False)
    # 0..1 ranking aid, not a calibrated probability (see the detectors module).
    confidence = Column(
        "confidence",
        FLOAT(precision=7, scale=6),
        nullable=False,
        server_default="0.000000",
    )
    # how many detectors flagged this score -- a cheap "how many agree?" triage
    # signal without deserialising anything.
    triggered_count = Column(
        "triggered_count",
        Integer,
        nullable=False,
        server_default="0",
    )
    detail = Column("detail", String(_DETAIL_MAX_LEN), nullable=False)
    # raw evidence numbers behind the top signal, as JSON text.
    evidence = Column("evidence", Text, nullable=True)

    first_flagged_at = Column(
        "first_flagged_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    last_flagged_at = Column(
        "last_flagged_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    # resolution columns: written by staff via `resolve`, never by the worker.
    resolved_by = Column("resolved_by", Integer, nullable=True)
    resolved_at = Column("resolved_at", DateTime, nullable=True)
    resolution_note = Column("resolution_note", String(_NOTE_MAX_LEN), nullable=True)

    __table_args__ = (
        Index("anticheat_flags_status_index", status),
        Index("anticheat_flags_user_id_index", user_id),
        Index("anticheat_flags_mode_index", mode),
    )


READ_PARAMS = (
    AnticheatFlagsTable.score_id,
    AnticheatFlagsTable.user_id,
    AnticheatFlagsTable.mode,
    AnticheatFlagsTable.status,
    AnticheatFlagsTable.severity,
    AnticheatFlagsTable.top_signal_code,
    AnticheatFlagsTable.top_signal_title,
    AnticheatFlagsTable.confidence,
    AnticheatFlagsTable.triggered_count,
    AnticheatFlagsTable.detail,
    AnticheatFlagsTable.evidence,
    AnticheatFlagsTable.first_flagged_at,
    AnticheatFlagsTable.last_flagged_at,
    AnticheatFlagsTable.resolved_by,
    AnticheatFlagsTable.resolved_at,
    AnticheatFlagsTable.resolution_note,
)


@dataclass(frozen=True, slots=True)
class AnticheatFlag:
    score_id: int
    user_id: int
    mode: int
    status: AnticheatFlagStatus
    severity: str
    top_signal_code: str
    top_signal_title: str
    confidence: float
    triggered_count: int
    detail: str
    evidence: dict[str, Any] | None
    first_flagged_at: datetime
    last_flagged_at: datetime
    resolved_by: int | None
    resolved_at: datetime | None
    resolution_note: str | None


class AnticheatFlagsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> AnticheatFlag:
        evidence_raw = row["evidence"]
        return AnticheatFlag(
            score_id=row["score_id"],
            user_id=row["user_id"],
            mode=row["mode"],
            status=AnticheatFlagStatus(row["status"]),
            severity=row["severity"],
            top_signal_code=row["top_signal_code"],
            top_signal_title=row["top_signal_title"],
            confidence=row["confidence"],
            triggered_count=row["triggered_count"],
            detail=row["detail"],
            evidence=json.loads(evidence_raw) if evidence_raw is not None else None,
            first_flagged_at=row["first_flagged_at"],
            last_flagged_at=row["last_flagged_at"],
            resolved_by=row["resolved_by"],
            resolved_at=row["resolved_at"],
            resolution_note=row["resolution_note"],
        )

    async def _fetch(self, score_id: int) -> AnticheatFlag:
        select_stmt = select(*READ_PARAMS).where(
            AnticheatFlagsTable.score_id == score_id,
        )
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # written immediately before every call site.
        return self._deserialize(row)

    async def fetch_one(self, score_id: int) -> AnticheatFlag | None:
        """Fetch one score's flag, or None if the score was never flagged."""
        select_stmt = select(*READ_PARAMS).where(
            AnticheatFlagsTable.score_id == score_id,
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def record(
        self,
        score_id: int,
        *,
        user_id: int,
        mode: int,
        severity: str,
        top_signal_code: str,
        top_signal_title: str,
        confidence: float,
        triggered_count: int,
        detail: str,
        evidence: dict[str, float],
    ) -> AnticheatFlag:
        """Persist (upsert) a flagged score for staff review.

        On a first flag this inserts an ``OPEN`` row. On re-analysis of an
        already-flagged score it refreshes only the detection columns and
        ``last_flagged_at``; it deliberately leaves ``status``, the resolution
        columns, and ``first_flagged_at`` untouched, so a prior staff decision
        is never clobbered and the queue does not re-open dismissed flags every
        backfill pass (see module docstring).
        """
        evidence_json = json.dumps(evidence, separators=(",", ":"))
        title = top_signal_title[:_TITLE_MAX_LEN]
        trimmed_detail = detail[:_DETAIL_MAX_LEN]
        values = {
            "score_id": score_id,
            "user_id": user_id,
            "mode": mode,
            "status": AnticheatFlagStatus.OPEN.value,
            "severity": severity,
            "top_signal_code": top_signal_code,
            "top_signal_title": title,
            "confidence": confidence,
            "triggered_count": triggered_count,
            "detail": trimmed_detail,
            "evidence": evidence_json,
            "first_flagged_at": func.now(),
            "last_flagged_at": func.now(),
        }
        insert_stmt: MysqlInsert = (
            mysql_insert(AnticheatFlagsTable)
            .values(**values)
            .on_duplicate_key_update(
                user_id=user_id,
                mode=mode,
                severity=severity,
                top_signal_code=top_signal_code,
                top_signal_title=title,
                confidence=confidence,
                triggered_count=triggered_count,
                detail=trimmed_detail,
                evidence=evidence_json,
                last_flagged_at=func.now(),
            )
        )

        await self._database.execute(insert_stmt)
        return await self._fetch(score_id)

    async def resolve(
        self,
        score_id: int,
        *,
        status: AnticheatFlagStatus,
        resolved_by: int,
        note: str | None = None,
    ) -> AnticheatFlag | None:
        """Record a staff decision on a flag; None if there is no such flag.

        ``resolved_at`` stamps the time of this action. The status is whatever
        the reviewer chose (claiming as ``REVIEWING`` or a terminal
        ``ACTIONED`` / ``DISMISSED``); this method is the only writer of the
        resolution columns, which ``record`` never touches.
        """
        trimmed_note = note[:_NOTE_MAX_LEN] if note is not None else None
        update_stmt = (
            update(AnticheatFlagsTable)
            .where(AnticheatFlagsTable.score_id == score_id)
            .values(
                status=status.value,
                resolved_by=resolved_by,
                resolved_at=func.now(),
                resolution_note=trimmed_note,
            )
        )
        await self._database.execute(update_stmt)
        return await self.fetch_one(score_id)

    async def fetch_many(
        self,
        *,
        status: AnticheatFlagStatus | None = None,
        mode: int | None = None,
        user_id: int | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> list[AnticheatFlag]:
        """Fetch flags, newest re-flag first, optionally filtered and paged."""
        select_stmt = select(*READ_PARAMS)
        if status is not None:
            select_stmt = select_stmt.where(
                AnticheatFlagsTable.status == status.value,
            )
        if mode is not None:
            select_stmt = select_stmt.where(AnticheatFlagsTable.mode == mode)
        if user_id is not None:
            select_stmt = select_stmt.where(AnticheatFlagsTable.user_id == user_id)

        # most-recently-flagged first; score_id breaks ties for a stable order.
        select_stmt = select_stmt.order_by(
            AnticheatFlagsTable.last_flagged_at.desc(),
            AnticheatFlagsTable.score_id.desc(),
        )
        if page is not None and page_size is not None:
            select_stmt = select_stmt.limit(page_size).offset((page - 1) * page_size)

        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize(row) for row in rows]

    async def fetch_count(
        self,
        *,
        status: AnticheatFlagStatus | None = None,
        mode: int | None = None,
        user_id: int | None = None,
    ) -> int:
        """Count flags by status/mode/user -- backs the review-queue badge."""
        select_stmt = select(func.count().label("count")).select_from(
            AnticheatFlagsTable,
        )
        if status is not None:
            select_stmt = select_stmt.where(
                AnticheatFlagsTable.status == status.value,
            )
        if mode is not None:
            select_stmt = select_stmt.where(AnticheatFlagsTable.mode == mode)
        if user_id is not None:
            select_stmt = select_stmt.where(AnticheatFlagsTable.user_id == user_id)

        row = await self._database.fetch_one(select_stmt)
        assert row is not None
        return int(row["count"])
