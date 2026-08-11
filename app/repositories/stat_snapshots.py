"""Daily per-player, per-mode stat snapshots -- the historical time series.

The live ``stats`` table only ever holds a player's *current* value: today's pp,
today's rank, today's play count. The moment it is overwritten the previous
value is gone. Rank history graphs, peak rank, "pp gained this week", and the
behavioural baselines the anticheat track needs all require the past, and none
of it can be reconstructed after the fact. So this table records one immutable
row per ``(user, mode, day)`` capturing the numbers as they stood that day.

Ranks are captured here rather than read live from Redis: the snapshot must be a
durable fact even if the Redis leaderboards are cold or being rebuilt, so global
and country rank are computed straight from ``stats`` (the source of truth for
pp) with a window function, over the *same* ranked-player predicate the live
leaderboard uses (unrestricted + verified, pp > 0). That way a historical rank
agrees with what the player saw, without depending on Redis being warm.

One snapshot per calendar day is the granularity: the unique key is
``(user_id, mode, snapshot_date)``. The bulk daily capture is ``INSERT IGNORE``
so re-running it the same day (e.g. after a restart) is a harmless no-op rather
than a duplicate-key error or a churned row; the single-row ``record`` path is a
true upsert for targeted re-captures. Following the rest of the schema, the
foreign key to ``users`` is enforced in application logic, not by the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import select
from sqlalchemy.dialects.mysql import FLOAT
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.dialects.mysql import Insert as MysqlInsert
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.constants.privileges import Privileges
from app.repositories import Base
from app.repositories.stats import StatsTable
from app.repositories.users import UsersTable

# the exact predicate the live leaderboard ranks by (see
# `StatsRepository.fetch_ranked_pp_for_mode`): only unrestricted, verified
# players are ranked. Kept identical so a captured historical rank matches what
# the player actually saw on the leaderboard that day.
_RANKED_PRIV = Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value


class StatSnapshotsTable(Base):
    __tablename__ = "stat_snapshots"

    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    user_id = Column("user_id", Integer, nullable=False)
    mode = Column("mode", TINYINT(1), nullable=False)
    snapshot_date = Column("snapshot_date", Date, nullable=False)
    pp = Column("pp", Integer, nullable=False, server_default="0")
    # nullable: a snapshot can exist for a player who is not currently ranked
    # (the bulk capture only inserts ranked players, but the single-row `record`
    # path does not assume rank is known).
    global_rank = Column("global_rank", Integer, nullable=True)
    country_rank = Column("country_rank", Integer, nullable=True)
    tscore = Column("tscore", BigInteger, nullable=False, server_default="0")
    rscore = Column("rscore", BigInteger, nullable=False, server_default="0")
    acc = Column(
        "acc",
        FLOAT(precision=6, scale=3),
        nullable=False,
        server_default="0.000",
    )
    plays = Column("plays", Integer, nullable=False, server_default="0")
    playtime = Column("playtime", Integer, nullable=False, server_default="0")
    max_combo = Column("max_combo", Integer, nullable=False, server_default="0")
    total_hits = Column("total_hits", BigInteger, nullable=False, server_default="0")
    created_at = Column(
        "created_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "mode",
            "snapshot_date",
            name="stat_snapshots_user_mode_date_uindex",
        ),
        # serves "everyone on this date" and per-date pruning; the unique key
        # above already serves per-player history and peak-rank lookups.
        Index("stat_snapshots_mode_date_index", "mode", "snapshot_date"),
    )


READ_PARAMS = (
    StatSnapshotsTable.id,
    StatSnapshotsTable.user_id,
    StatSnapshotsTable.mode,
    StatSnapshotsTable.snapshot_date,
    StatSnapshotsTable.pp,
    StatSnapshotsTable.global_rank,
    StatSnapshotsTable.country_rank,
    StatSnapshotsTable.tscore,
    StatSnapshotsTable.rscore,
    StatSnapshotsTable.acc,
    StatSnapshotsTable.plays,
    StatSnapshotsTable.playtime,
    StatSnapshotsTable.max_combo,
    StatSnapshotsTable.total_hits,
    StatSnapshotsTable.created_at,
)


@dataclass(frozen=True, slots=True)
class StatSnapshot:
    id: int
    user_id: int
    mode: int
    snapshot_date: date
    pp: int
    global_rank: int | None
    country_rank: int | None
    tscore: int
    rscore: int
    acc: float
    plays: int
    playtime: int
    max_combo: int
    total_hits: int
    created_at: datetime


class StatSnapshotsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> StatSnapshot:
        return StatSnapshot(
            id=row["id"],
            user_id=row["user_id"],
            mode=row["mode"],
            snapshot_date=row["snapshot_date"],
            pp=row["pp"],
            global_rank=row["global_rank"],
            country_rank=row["country_rank"],
            tscore=row["tscore"],
            rscore=row["rscore"],
            acc=row["acc"],
            plays=row["plays"],
            playtime=row["playtime"],
            max_combo=row["max_combo"],
            total_hits=row["total_hits"],
            created_at=row["created_at"],
        )

    async def capture_mode(self, *, mode: int, snapshot_date: date) -> int:
        """Snapshot every ranked player in one mode for ``snapshot_date``.

        Computes each player's global and country rank with a window function
        over the ranked set (the same predicate the leaderboard uses), and
        inserts one row per player in a single ``INSERT ... SELECT``. Returns the
        number of rows inserted.

        ``INSERT IGNORE`` makes a same-day re-run idempotent: the unique
        ``(user_id, mode, snapshot_date)`` key means an already-captured player
        is skipped rather than duplicated or errored. Ranks use ``ROW_NUMBER``
        (a strict 1..N position, ordered by pp then id) to mirror the live
        leaderboard's positional rank rather than producing tie gaps.
        """
        order = (StatsTable.pp.desc(), UsersTable.id.asc())
        global_rank = func.row_number().over(order_by=order)
        country_rank = func.row_number().over(
            partition_by=UsersTable.country,
            order_by=order,
        )

        ranked_players = (
            select(
                UsersTable.id.label("user_id"),
                StatsTable.mode.label("mode"),
                literal(snapshot_date).label("snapshot_date"),
                StatsTable.pp.label("pp"),
                global_rank.label("global_rank"),
                country_rank.label("country_rank"),
                StatsTable.tscore.label("tscore"),
                StatsTable.rscore.label("rscore"),
                StatsTable.acc.label("acc"),
                StatsTable.plays.label("plays"),
                StatsTable.playtime.label("playtime"),
                StatsTable.max_combo.label("max_combo"),
                StatsTable.total_hits.label("total_hits"),
            )
            .select_from(StatsTable)
            .join(UsersTable, UsersTable.id == StatsTable.id)
            .where(
                StatsTable.mode == mode,
                UsersTable.priv.bitwise_and(_RANKED_PRIV) == _RANKED_PRIV,
                StatsTable.pp > 0,
            )
        )

        insert_columns = [
            "user_id",
            "mode",
            "snapshot_date",
            "pp",
            "global_rank",
            "country_rank",
            "tscore",
            "rscore",
            "acc",
            "plays",
            "playtime",
            "max_combo",
            "total_hits",
        ]
        insert_stmt = mysql_insert(StatSnapshotsTable).from_select(
            insert_columns,
            ranked_players,
        )
        # first-capture-of-the-day wins; a re-run the same day is a no-op.
        insert_stmt = insert_stmt.prefix_with("IGNORE")

        return await self._database.execute(insert_stmt)

    async def record(
        self,
        *,
        user_id: int,
        mode: int,
        snapshot_date: date,
        pp: int,
        tscore: int,
        rscore: int,
        acc: float,
        plays: int,
        playtime: int,
        max_combo: int,
        total_hits: int,
        global_rank: int | None = None,
        country_rank: int | None = None,
    ) -> StatSnapshot:
        """Upsert a single snapshot for one player/mode/day.

        The targeted counterpart to ``capture_mode`` (e.g. a manual re-capture
        or a test): unlike the bulk path this is a true upsert, so re-recording
        the same day overwrites that day's row with the supplied values.
        """
        values = {
            "user_id": user_id,
            "mode": mode,
            "snapshot_date": snapshot_date,
            "pp": pp,
            "global_rank": global_rank,
            "country_rank": country_rank,
            "tscore": tscore,
            "rscore": rscore,
            "acc": acc,
            "plays": plays,
            "playtime": playtime,
            "max_combo": max_combo,
            "total_hits": total_hits,
        }
        insert_stmt: MysqlInsert = (
            mysql_insert(StatSnapshotsTable)
            .values(**values)
            .on_duplicate_key_update(
                pp=pp,
                global_rank=global_rank,
                country_rank=country_rank,
                tscore=tscore,
                rscore=rscore,
                acc=acc,
                plays=plays,
                playtime=playtime,
                max_combo=max_combo,
                total_hits=total_hits,
            )
        )
        await self._database.execute(insert_stmt)
        return await self._fetch(user_id=user_id, mode=mode, snapshot_date=snapshot_date)

    async def _fetch(
        self,
        *,
        user_id: int,
        mode: int,
        snapshot_date: date,
    ) -> StatSnapshot:
        select_stmt = (
            select(*READ_PARAMS)
            .where(StatSnapshotsTable.user_id == user_id)
            .where(StatSnapshotsTable.mode == mode)
            .where(StatSnapshotsTable.snapshot_date == snapshot_date)
        )
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # written immediately before every call site.
        return self._deserialize(row)

    async def fetch_history(
        self,
        *,
        user_id: int,
        mode: int,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[StatSnapshot]:
        """A player's snapshots for a mode, oldest first (for graphs)."""
        select_stmt = (
            select(*READ_PARAMS)
            .where(StatSnapshotsTable.user_id == user_id)
            .where(StatSnapshotsTable.mode == mode)
            .order_by(StatSnapshotsTable.snapshot_date.asc())
        )
        if since is not None:
            select_stmt = select_stmt.where(
                StatSnapshotsTable.snapshot_date >= since,
            )
        if limit is not None:
            select_stmt = select_stmt.limit(limit)

        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize(row) for row in rows]

    async def fetch_latest(
        self,
        *,
        user_id: int,
        mode: int,
    ) -> StatSnapshot | None:
        """The most recent snapshot for a player/mode, if any exists."""
        select_stmt = (
            select(*READ_PARAMS)
            .where(StatSnapshotsTable.user_id == user_id)
            .where(StatSnapshotsTable.mode == mode)
            .order_by(StatSnapshotsTable.snapshot_date.desc())
            .limit(1)
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def fetch_peak_rank(
        self,
        *,
        user_id: int,
        mode: int,
    ) -> StatSnapshot | None:
        """The snapshot at the player's best-ever global rank for a mode.

        Peak rank is the *lowest* ``global_rank`` ever recorded, so the whole
        snapshot at that point is returned (rank, the date it happened, and the
        pp it took), which is what a profile "peak rank" line wants. ``None`` if
        the player has never held a ranked snapshot.
        """
        select_stmt = (
            select(*READ_PARAMS)
            .where(StatSnapshotsTable.user_id == user_id)
            .where(StatSnapshotsTable.mode == mode)
            .where(StatSnapshotsTable.global_rank.is_not(None))
            .order_by(
                StatSnapshotsTable.global_rank.asc(),
                # tie-break on the earliest date the peak was first reached.
                StatSnapshotsTable.snapshot_date.asc(),
            )
            .limit(1)
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None
