"""Guards for the daily stat-snapshot repository.

``stat_snapshots`` is the historical time series the live ``stats`` table can
never reconstruct: rank history, peak rank, and the anticheat behavioural
baselines all read the past out of here. These tests pin the behaviour those
readers depend on, split by the two write paths:

- the single-row ``record`` upsert round-trips a snapshot back through
  ``_fetch``, and re-recording the same ``(user, mode, day)`` overwrites in
  place rather than duplicating -- the per-day key is authoritative;
- ``fetch_history`` returns a player's mode snapshots oldest-first (the shape a
  graph wants), honouring the ``since`` lower bound;
- ``fetch_latest`` returns the most recent day, ``fetch_peak_rank`` the snapshot
  at the *lowest* global rank ever held (skipping days with no rank), so a
  profile's "peak rank" line is a real historical fact, not today's value;
- ``capture_mode`` emits a single ``INSERT IGNORE ... SELECT`` that ranks the
  ranked-player set with a window function -- idempotent per day, and computing
  rank from ``stats`` (not live Redis) so a captured rank is durable.

No MySQL is involved: a fake ``Database`` stores rows and interprets the Core
``Select`` it is handed (its ``WHERE``/``ORDER BY``), so the read methods'
filtering and ordering are exercised for real; the bulk insert is asserted at
the SQL level against the MySQL dialect, since it has no per-row bind params to
round-trip.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from typing import Any

from sqlalchemy.dialects import mysql

from app.repositories.stat_snapshots import StatSnapshot
from app.repositories.stat_snapshots import StatSnapshotsRepository
from app.repositories.stat_snapshots import StatSnapshotsTable

# the columns the single-row insert binds by name; everything else in the
# compiled params (on-duplicate-key `param_N`, the limit bind) is ignored.
_COLUMN_NAMES = frozenset(column.name for column in StatSnapshotsTable.__table__.columns)

# a fixed clock for the server_default `created_at`; deterministic so ordering
# ties never hinge on wall-clock time.
_CREATED_AT = datetime(2026, 8, 11, 12, 0, 0)


class _FakeDatabase:
    """Stores snapshot rows and answers reads by interpreting the statement.

    ``execute`` pulls the single-row insert's values straight out of the
    compiled params (keyed by column name) and upserts on
    ``(user_id, mode, snapshot_date)`` the way the unique key would. ``fetch_one``
    / ``fetch_all`` walk the ``Select``'s ``WHERE`` and ``ORDER BY`` so the
    repository's own filtering and ordering decide the result -- not the fake.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1

    async def execute(self, statement: Any) -> int:
        compiled = statement.compile()
        params = compiled.params
        values = {k: v for k, v in params.items() if k in _COLUMN_NAMES}
        key = (values["user_id"], values["mode"], values["snapshot_date"])

        for row in self.rows:
            if (row["user_id"], row["mode"], row["snapshot_date"]) == key:
                # on-duplicate-key upsert: overwrite in place, keep the id.
                row.update(values)
                return 1

        row = self._new_row(values)
        self.rows.append(row)
        return 1

    def _new_row(self, values: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self._next_id,
            "global_rank": None,
            "country_rank": None,
            "created_at": _CREATED_AT,
        }
        row.update(values)
        self._next_id += 1
        return row

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        matched = self._query(statement)
        return matched[0] if matched else None

    async def fetch_all(self, statement: Any) -> list[dict[str, Any]]:
        return self._query(statement)

    def _query(self, statement: Any) -> list[dict[str, Any]]:
        predicates = _where_predicates(statement.whereclause)
        matched = [row for row in self.rows if all(p(row) for p in predicates)]

        for column_name, descending in reversed(_order_specs(statement)):
            matched.sort(key=lambda r: r[column_name], reverse=descending)

        limit = _limit_value(statement)
        if limit is not None:
            matched = matched[:limit]
        return matched


def _where_predicates(whereclause: Any) -> list[Any]:
    """Turn a Core ``WHERE`` tree into a list of row -> bool predicates."""
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None) or [whereclause]

    predicates = []
    for clause in clauses:
        column_name = clause.left.name
        operator = clause.operator.__name__
        if operator == "eq":
            predicates.append(_eq(column_name, clause.right.value))
        elif operator == "ge":
            predicates.append(_ge(column_name, clause.right.value))
        elif operator == "is_not":  # `col.is_not(None)` -> right is a Null node
            predicates.append(_is_not_none(column_name))
        else:  # pragma: no cover - the repository only uses the three above
            raise AssertionError(f"unhandled operator in fake: {operator}")
    return predicates


def _eq(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] == value


def _ge(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] >= value


def _is_not_none(column_name: str) -> Any:
    return lambda row: row[column_name] is not None


def _order_specs(statement: Any) -> list[tuple[str, bool]]:
    specs: list[tuple[str, bool]] = []
    for element in statement._order_by_clauses:
        descending = element.modifier.__name__ == "desc_op"
        specs.append((element.element.name, descending))
    return specs


def _limit_value(statement: Any) -> int | None:
    limit_clause = getattr(statement, "_limit_clause", None)
    return getattr(limit_clause, "value", None) if limit_clause is not None else None


def _repo() -> tuple[StatSnapshotsRepository, _FakeDatabase]:
    database = _FakeDatabase()
    return StatSnapshotsRepository(database), database  # type: ignore[arg-type]


async def _record(
    repository: StatSnapshotsRepository,
    *,
    user_id: int,
    mode: int,
    day: date,
    pp: int,
    global_rank: int | None = None,
    country_rank: int | None = None,
) -> StatSnapshot:
    return await repository.record(
        user_id=user_id,
        mode=mode,
        snapshot_date=day,
        pp=pp,
        tscore=0,
        rscore=0,
        acc=0.0,
        plays=0,
        playtime=0,
        max_combo=0,
        total_hits=0,
        global_rank=global_rank,
        country_rank=country_rank,
    )


async def test_record_round_trips_a_snapshot() -> None:
    repository, _ = _repo()

    snapshot = await _record(
        repository,
        user_id=3,
        mode=0,
        day=date(2026, 8, 11),
        pp=4200,
        global_rank=7,
        country_rank=2,
    )

    assert snapshot.user_id == 3
    assert snapshot.mode == 0
    assert snapshot.snapshot_date == date(2026, 8, 11)
    assert snapshot.pp == 4200
    assert snapshot.global_rank == 7
    assert snapshot.country_rank == 2


async def test_record_is_a_per_day_upsert_not_a_duplicate() -> None:
    repository, database = _repo()

    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=100)
    # same (user, mode, day): a re-capture overwrites in place.
    updated = await _record(
        repository,
        user_id=3,
        mode=0,
        day=date(2026, 8, 11),
        pp=250,
        global_rank=5,
    )

    assert updated.pp == 250
    assert updated.global_rank == 5
    assert len(database.rows) == 1  # overwritten, not duplicated


async def test_record_keeps_distinct_days_and_modes_separate() -> None:
    repository, database = _repo()

    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 10), pp=100)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=150)
    await _record(repository, user_id=3, mode=1, day=date(2026, 8, 11), pp=90)

    assert len(database.rows) == 3


async def test_fetch_history_is_oldest_first() -> None:
    repository, _ = _repo()
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=300)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 9), pp=100)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 10), pp=200)

    history = await repository.fetch_history(user_id=3, mode=0)

    assert [s.snapshot_date for s in history] == [
        date(2026, 8, 9),
        date(2026, 8, 10),
        date(2026, 8, 11),
    ]


async def test_fetch_history_respects_the_since_lower_bound() -> None:
    repository, _ = _repo()
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 9), pp=100)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 10), pp=200)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=300)

    history = await repository.fetch_history(
        user_id=3,
        mode=0,
        since=date(2026, 8, 10),
    )

    assert [s.snapshot_date for s in history] == [date(2026, 8, 10), date(2026, 8, 11)]


async def test_fetch_history_only_returns_the_requested_player_and_mode() -> None:
    repository, _ = _repo()
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=300)
    await _record(repository, user_id=4, mode=0, day=date(2026, 8, 11), pp=999)
    await _record(repository, user_id=3, mode=1, day=date(2026, 8, 11), pp=50)

    history = await repository.fetch_history(user_id=3, mode=0)

    assert len(history) == 1
    assert history[0].pp == 300


async def test_fetch_latest_returns_the_most_recent_day() -> None:
    repository, _ = _repo()
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 9), pp=100)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=300)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 10), pp=200)

    latest = await repository.fetch_latest(user_id=3, mode=0)

    assert latest is not None
    assert latest.snapshot_date == date(2026, 8, 11)
    assert latest.pp == 300


async def test_fetch_latest_is_none_when_no_snapshot_exists() -> None:
    repository, _ = _repo()
    assert await repository.fetch_latest(user_id=999, mode=0) is None


async def test_fetch_peak_rank_is_the_lowest_global_rank_ever_held() -> None:
    repository, _ = _repo()
    # rank improved (number went down) then regressed; peak is the best-ever.
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 9), pp=100, global_rank=50)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 10), pp=400, global_rank=8)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=300, global_rank=20)

    peak = await repository.fetch_peak_rank(user_id=3, mode=0)

    assert peak is not None
    assert peak.global_rank == 8
    assert peak.snapshot_date == date(2026, 8, 10)  # the whole snapshot at peak


async def test_fetch_peak_rank_skips_days_with_no_rank() -> None:
    repository, _ = _repo()
    # an unranked day (global_rank NULL) must not count as rank 0/best.
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 10), pp=0, global_rank=None)
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=300, global_rank=12)

    peak = await repository.fetch_peak_rank(user_id=3, mode=0)

    assert peak is not None
    assert peak.global_rank == 12


async def test_fetch_peak_rank_is_none_when_never_ranked() -> None:
    repository, _ = _repo()
    await _record(repository, user_id=3, mode=0, day=date(2026, 8, 11), pp=0, global_rank=None)

    assert await repository.fetch_peak_rank(user_id=3, mode=0) is None


class _CapturingDatabase:
    """Records the last statement it was handed, for SQL-shape assertions."""

    def __init__(self) -> None:
        self.last_statement: Any = None

    async def execute(self, statement: Any) -> int:
        self.last_statement = statement
        return 0


async def test_capture_mode_emits_an_idempotent_ranked_insert_select() -> None:
    database = _CapturingDatabase()
    repository = StatSnapshotsRepository(database)  # type: ignore[arg-type]

    await repository.capture_mode(mode=0, snapshot_date=date(2026, 8, 11))

    sql = str(
        database.last_statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        ),
    ).lower()

    # one bulk insert, idempotent per day (INSERT IGNORE), sourced from a SELECT.
    assert "insert ignore into stat_snapshots" in sql
    assert "select" in sql
    # rank is computed with a window function, globally and per country, from the
    # ranked-player set (unrestricted+verified, pp>0) -- not read from Redis.
    assert "row_number() over" in sql
    assert "partition by users.country" in sql
    assert "order by stats.pp desc" in sql
    assert "from stats" in sql
    assert "stats.mode = 0" in sql
    assert "priv & 3 = 3" in sql
    assert "stats.pp > 0" in sql


async def test_capture_mode_returns_the_rows_inserted() -> None:
    # the bulk path returns the DB rowcount so a caller/loop can log progress;
    # the fake reports 0, but the plumbing (await execute -> return) is pinned.
    database = _CapturingDatabase()
    repository = StatSnapshotsRepository(database)  # type: ignore[arg-type]

    inserted = await repository.capture_mode(mode=0, snapshot_date=date(2026, 8, 11))

    assert inserted == 0
