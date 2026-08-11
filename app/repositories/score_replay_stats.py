"""Persisted per-score replay analysis records.

One row per submitted score, holding the outcome of running the replay feature
extractor (``app/services/anticheat/features.py``) over that score's ``.osr``.
The row is the durable source of truth for *whether* a score has been analysed;
the Redis analysis queue (Track 2.5) is only the ephemeral work list on top of
it. Keeping the terminal state in MySQL is what lets a queue flush recover:
scores with no row here are exactly the ones that still need analysing.

The ``status`` field carries the skip path the plan calls for. A score whose
``.osr`` never landed (the durability reorder in score submission means a row
can outlive a missing file) is marked ``replay_missing`` once, and the queue
then leaves it alone instead of retrying a file that will never appear. A
transient parse/analysis failure is ``error`` and stays eligible for a re-run.

Two granularities of feature data are stored together:

- a handful of scalar signals promoted to indexed columns, so triage and
  per-mode baseline aggregation are cheap SQL and don't deserialize every blob;
- the full ``ReplayFeatures`` document as JSON text, so detectors can be re-run
  and thresholds re-derived without re-parsing the ``.osr``.

``extractor_version`` stamps which vintage of the extractor produced a row, so a
later extractor change can re-queue stale rows rather than trust an old vector
against new thresholds. Following the rest of this codebase, the foreign-key
relationship to ``scores`` is enforced in application logic, not by a DB
constraint, so a purged score doesn't block on cascade rules here.
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
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.mysql import FLOAT
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.dialects.mysql import Insert as MysqlInsert
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.repositories import Base
from app.repositories.scores import ScoresTable


class ReplayAnalysisStatus(StrEnum):
    """Lifecycle of one score's replay analysis.

    ``PENDING`` exists so a row can be reserved before analysis completes;
    the terminal states are the ones the queue reads to decide what to skip.
    """

    PENDING = "pending"
    ANALYZED = "analyzed"
    REPLAY_MISSING = "replay_missing"
    ERROR = "error"


class ScoreReplayStatsTable(Base):
    __tablename__ = "score_replay_stats"

    # scores.id is `bigint unsigned`; the ORM models it as Integer elsewhere in
    # this codebase (see ScoresTable), and we mirror that here. No DB-level FK,
    # matching the rest of the schema. autoincrement is disabled explicitly: the
    # value is always the caller-supplied score id, never generated -- without
    # this, SQLAlchemy would make a lone integer PK AUTO_INCREMENT.
    score_id = Column(
        "score_id",
        Integer,
        nullable=False,
        primary_key=True,
        autoincrement=False,
    )
    mode = Column("mode", TINYINT(1), nullable=False)
    status = Column(
        "status",
        Enum(ReplayAnalysisStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=ReplayAnalysisStatus.PENDING.value,
    )
    extractor_version = Column(
        "extractor_version",
        Integer,
        nullable=False,
        server_default="0",
    )
    error_detail = Column("error_detail", String(255), nullable=True)

    frame_count = Column("frame_count", Integer, nullable=False, server_default="0")
    duration_ms = Column("duration_ms", Integer, nullable=False, server_default="0")
    tap_count = Column("tap_count", Integer, nullable=False, server_default="0")
    uses_keyboard = Column(
        "uses_keyboard",
        TINYINT(1),
        nullable=False,
        server_default="0",
    )
    tortuosity = Column(
        "tortuosity",
        FLOAT(precision=12, scale=6),
        nullable=False,
        server_default="0.000000",
    )
    jitter_spike_count = Column(
        "jitter_spike_count",
        Integer,
        nullable=False,
        server_default="0",
    )
    robotic_tap_run_count = Column(
        "robotic_tap_run_count",
        Integer,
        nullable=False,
        server_default="0",
    )
    max_robotic_run_taps = Column(
        "max_robotic_run_taps",
        Integer,
        nullable=False,
        server_default="0",
    )
    frozen_span_count = Column(
        "frozen_span_count",
        Integer,
        nullable=False,
        server_default="0",
    )
    straight_run_count = Column(
        "straight_run_count",
        Integer,
        nullable=False,
        server_default="0",
    )
    constant_velocity_run_count = Column(
        "constant_velocity_run_count",
        Integer,
        nullable=False,
        server_default="0",
    )

    # full serialized ReplayFeatures document; null until analysed / when missing.
    features = Column("features", MEDIUMTEXT, nullable=True)
    analyzed_at = Column("analyzed_at", DateTime, nullable=True)

    __table_args__ = (
        Index("score_replay_stats_status_index", status),
        Index("score_replay_stats_mode_index", mode),
        Index("score_replay_stats_extractor_version_index", extractor_version),
    )


READ_PARAMS = (
    ScoreReplayStatsTable.score_id,
    ScoreReplayStatsTable.mode,
    ScoreReplayStatsTable.status,
    ScoreReplayStatsTable.extractor_version,
    ScoreReplayStatsTable.error_detail,
    ScoreReplayStatsTable.frame_count,
    ScoreReplayStatsTable.duration_ms,
    ScoreReplayStatsTable.tap_count,
    ScoreReplayStatsTable.uses_keyboard,
    ScoreReplayStatsTable.tortuosity,
    ScoreReplayStatsTable.jitter_spike_count,
    ScoreReplayStatsTable.robotic_tap_run_count,
    ScoreReplayStatsTable.max_robotic_run_taps,
    ScoreReplayStatsTable.frozen_span_count,
    ScoreReplayStatsTable.straight_run_count,
    ScoreReplayStatsTable.constant_velocity_run_count,
    ScoreReplayStatsTable.features,
    ScoreReplayStatsTable.analyzed_at,
)


@dataclass(frozen=True, slots=True)
class PendingScore:
    """A score awaiting analysis: just the id and mode a producer needs.

    Returned by the backfill anti-join; carries the mode so the worker (and the
    queue payload it builds) needs no second lookup.
    """

    score_id: int
    mode: int


@dataclass(frozen=True, slots=True)
class ScoreReplayStats:
    score_id: int
    mode: int
    status: ReplayAnalysisStatus
    extractor_version: int
    error_detail: str | None
    frame_count: int
    duration_ms: int
    tap_count: int
    uses_keyboard: bool
    tortuosity: float
    jitter_spike_count: int
    robotic_tap_run_count: int
    max_robotic_run_taps: int
    frozen_span_count: int
    straight_run_count: int
    constant_velocity_run_count: int
    features: dict[str, Any] | None
    analyzed_at: datetime | None


# The scalar column names the analysed-feature dict is expected to carry at its
# top level. Kept beside the table so `features_to_dict` and this repository can
# be checked against one another (see the repositories test).
PROMOTED_FEATURE_COLUMNS = (
    "frame_count",
    "duration_ms",
    "tap_count",
    "uses_keyboard",
    "tortuosity",
    "jitter_spike_count",
    "robotic_tap_run_count",
    "max_robotic_run_taps",
    "frozen_span_count",
    "straight_run_count",
    "constant_velocity_run_count",
)


class ScoreReplayStatsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> ScoreReplayStats:
        features_raw = row["features"]
        return ScoreReplayStats(
            score_id=row["score_id"],
            mode=row["mode"],
            status=ReplayAnalysisStatus(row["status"]),
            extractor_version=row["extractor_version"],
            error_detail=row["error_detail"],
            frame_count=row["frame_count"],
            duration_ms=row["duration_ms"],
            tap_count=row["tap_count"],
            uses_keyboard=bool(row["uses_keyboard"]),
            tortuosity=row["tortuosity"],
            jitter_spike_count=row["jitter_spike_count"],
            robotic_tap_run_count=row["robotic_tap_run_count"],
            max_robotic_run_taps=row["max_robotic_run_taps"],
            frozen_span_count=row["frozen_span_count"],
            straight_run_count=row["straight_run_count"],
            constant_velocity_run_count=row["constant_velocity_run_count"],
            features=json.loads(features_raw) if features_raw is not None else None,
            analyzed_at=row["analyzed_at"],
        )

    async def _fetch(self, score_id: int) -> ScoreReplayStats:
        select_stmt = select(*READ_PARAMS).where(
            ScoreReplayStatsTable.score_id == score_id,
        )
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # written immediately before every call site.
        return self._deserialize(row)

    async def fetch_one(self, score_id: int) -> ScoreReplayStats | None:
        """Fetch one score's analysis record, or None if never enqueued."""
        select_stmt = select(*READ_PARAMS).where(
            ScoreReplayStatsTable.score_id == score_id,
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def mark_analyzed(
        self,
        score_id: int,
        *,
        mode: int,
        extractor_version: int,
        features: dict[str, Any],
    ) -> ScoreReplayStats:
        """Record a completed analysis, upserting on ``score_id``.

        The scalar columns are taken from the top level of ``features`` (see
        ``features_to_dict``) so there is a single source of truth for them, and
        the whole document is stored as JSON text. Re-analysing a score (e.g.
        after an extractor-version bump) overwrites the previous row.
        """
        promoted = {name: features[name] for name in PROMOTED_FEATURE_COLUMNS}
        values = {
            "score_id": score_id,
            "mode": mode,
            "status": ReplayAnalysisStatus.ANALYZED.value,
            "extractor_version": extractor_version,
            "error_detail": None,
            "features": json.dumps(features, separators=(",", ":")),
            "analyzed_at": func.now(),
            **promoted,
        }
        update_on_duplicate = {
            key: value for key, value in values.items() if key != "score_id"
        }
        insert_stmt: MysqlInsert = (
            mysql_insert(ScoreReplayStatsTable)
            .values(**values)
            .on_duplicate_key_update(**update_on_duplicate)
        )

        await self._database.execute(insert_stmt)
        return await self._fetch(score_id)

    async def mark_replay_missing(
        self,
        score_id: int,
        *,
        mode: int,
    ) -> ScoreReplayStats:
        """Mark a score whose ``.osr`` is absent so the queue stops retrying it.

        Idempotent: re-marking an already-missing score is a no-op update. If a
        prior analysis somehow exists, this deliberately does not clobber its
        stored features -- only the status/mode/error columns are rewritten.
        """
        values = {
            "score_id": score_id,
            "mode": mode,
            "status": ReplayAnalysisStatus.REPLAY_MISSING.value,
            "error_detail": None,
        }
        insert_stmt: MysqlInsert = (
            mysql_insert(ScoreReplayStatsTable)
            .values(**values)
            .on_duplicate_key_update(
                status=ReplayAnalysisStatus.REPLAY_MISSING.value,
                mode=mode,
                error_detail=None,
            )
        )

        await self._database.execute(insert_stmt)
        return await self._fetch(score_id)

    async def mark_error(
        self,
        score_id: int,
        *,
        mode: int,
        error_detail: str,
    ) -> ScoreReplayStats:
        """Record a transient analysis failure; the score stays re-runnable.

        ``error_detail`` is truncated to the column width so an over-long
        traceback message can never fail the write it is trying to describe.
        """
        detail = error_detail[:255]
        values = {
            "score_id": score_id,
            "mode": mode,
            "status": ReplayAnalysisStatus.ERROR.value,
            "error_detail": detail,
        }
        insert_stmt: MysqlInsert = (
            mysql_insert(ScoreReplayStatsTable)
            .values(**values)
            .on_duplicate_key_update(
                status=ReplayAnalysisStatus.ERROR.value,
                mode=mode,
                error_detail=detail,
            )
        )

        await self._database.execute(insert_stmt)
        return await self._fetch(score_id)

    async def fetch_many(
        self,
        *,
        status: ReplayAnalysisStatus | None = None,
        mode: int | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> list[ScoreReplayStats]:
        """Fetch analysis rows, optionally filtered by status/mode and paged."""
        select_stmt = select(*READ_PARAMS)
        if status is not None:
            select_stmt = select_stmt.where(
                ScoreReplayStatsTable.status == status.value,
            )
        if mode is not None:
            select_stmt = select_stmt.where(ScoreReplayStatsTable.mode == mode)
        if page is not None and page_size is not None:
            select_stmt = select_stmt.limit(page_size).offset((page - 1) * page_size)

        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize(row) for row in rows]

    async def fetch_count(
        self,
        *,
        status: ReplayAnalysisStatus | None = None,
        mode: int | None = None,
    ) -> int:
        """Count analysis rows by status/mode -- backs the queue-depth metric."""
        select_stmt = select(func.count().label("count")).select_from(
            ScoreReplayStatsTable,
        )
        if status is not None:
            select_stmt = select_stmt.where(
                ScoreReplayStatsTable.status == status.value,
            )
        if mode is not None:
            select_stmt = select_stmt.where(ScoreReplayStatsTable.mode == mode)

        row = await self._database.fetch_one(select_stmt)
        assert row is not None
        return int(row["count"])

    async def fetch_unanalyzed_scores(
        self,
        *,
        limit: int,
        min_score_id: int = 0,
    ) -> list[PendingScore]:
        """Scores that have no analysis row yet -- the backfill work list.

        This is the durability net behind the ephemeral Redis queue: if the
        queue is flushed (or an enqueue was lost to a crash between the score
        commit and the push), the outstanding work is exactly the ``scores``
        rows with no matching ``score_replay_stats`` row. A ``LEFT JOIN ... IS
        NULL`` anti-join finds them without materialising the analysed set.

        Results are ordered by ``scores.id`` and paged with ``min_score_id`` (an
        exclusive lower bound on the id, not an ``OFFSET``) so a long backfill
        walks forward through the table in stable id order even as new rows and
        new analyses land underneath it. Pass the last id seen to get the next
        page.
        """
        select_stmt = (
            select(
                ScoresTable.id.label("score_id"),
                ScoresTable.mode.label("mode"),
            )
            .select_from(ScoresTable)
            .join(
                ScoreReplayStatsTable,
                ScoreReplayStatsTable.score_id == ScoresTable.id,
                isouter=True,
            )
            .where(ScoreReplayStatsTable.score_id.is_(None))
            .where(ScoresTable.id > min_score_id)
            .order_by(ScoresTable.id)
            .limit(limit)
        )
        rows = await self._database.fetch_all(select_stmt)
        return [
            PendingScore(score_id=row["score_id"], mode=row["mode"]) for row in rows
        ]
