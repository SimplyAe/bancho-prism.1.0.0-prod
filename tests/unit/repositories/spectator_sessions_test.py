"""Guards for the spectator-session repository.

``spectator_sessions`` is the durable record of a thing that is otherwise
memory-only: who spectated whom, and for how long. These tests pin the
behaviour the history reader depends on:

- ``start_session`` round-trips a session back through ``_fetch_one``, returning
  the durable id the live spectator stashes so its close can find it;
- ``end_session`` only ever closes a session once -- its ``UPDATE`` is guarded
  by ``ended_at IS NULL``, asserted at the SQL level since the guard is the
  whole point;
- ``fetch_session`` answers ``None`` for an unknown id;
- ``fetch_sessions`` walks ``id`` backwards from newest, honours ``before_id``,
  and applies the ``host_id`` / ``spectator_id`` filters independently and
  together.

No MySQL is involved: a fake ``Database`` stores rows and interprets the Core
statements it is handed. The insert pulls its values from the statement's bound
columns (a fixed clock stands in for the ``now()`` server defaults); reads walk
the ``Select``'s ``WHERE``/``ORDER BY``/``LIMIT``; the end ``UPDATE`` is compiled
against the MySQL dialect so its guard is asserted for real.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Insert
from sqlalchemy import Select
from sqlalchemy import Update
from sqlalchemy.dialects import mysql

from app.repositories.spectator_sessions import SpectatorSessionsRepository

# a fixed clock standing in for the `now()` server defaults, so ordering ties
# and default timestamps never hinge on wall-clock time.
_NOW = datetime(2026, 8, 12, 20, 0, 0)


class _FakeDatabase:
    """Stores spectator-session rows and answers by interpreting the statement.

    ``execute`` handles both the insert (pulling values out of the statement's
    bound columns, assigning an autoincrement id, defaulting ``now()`` columns to
    a fixed clock) and the end ``UPDATE`` (walking its ``WHERE`` so the
    ``ended_at IS NULL`` guard actually decides whether the row changes).
    ``fetch_one`` / ``fetch_all`` walk the ``Select`` so the repository's own
    filtering and ordering decide the result.
    """

    def __init__(self) -> None:
        self.sessions: list[dict[str, Any]] = []
        self._next_id = 1
        # the last compiled end UPDATE, for SQL-shape assertions.
        self.last_update_sql: str | None = None

    async def execute(self, statement: Any) -> int:
        if isinstance(statement, Insert):
            return self._execute_insert(statement)
        if isinstance(statement, Update):
            return self._execute_update(statement)
        raise AssertionError(f"unhandled statement in fake: {type(statement).__name__}")

    def _execute_insert(self, statement: Insert) -> int:
        values = {col.name: _literal(val) for col, val in statement._values.items()}
        row: dict[str, Any] = {
            "id": self._next_id,
            "started_at": _NOW,
            "ended_at": None,
        }
        row.update(values)
        # `now()` server defaults are SQL functions, not binds; resolve them.
        row["started_at"] = _resolve_now(row.get("started_at"))
        self.sessions.append(row)
        self._next_id += 1
        return row["id"]

    def _execute_update(self, statement: Update) -> int:
        self.last_update_sql = str(
            statement.compile(
                dialect=mysql.dialect(),
                compile_kwargs={"literal_binds": True},
            ),
        )
        predicates = _where_predicates(statement.whereclause)
        set_values = {
            col.name: _resolve_now(_literal(val))
            for col, val in statement._values.items()
        }
        changed = 0
        for row in self.sessions:
            if all(p(row) for p in predicates):
                row.update(set_values)
                changed += 1
        return changed

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        matched = self._query(statement)
        return matched[0] if matched else None

    async def fetch_all(self, statement: Any) -> list[dict[str, Any]]:
        return self._query(statement)

    def _query(self, statement: Select) -> list[dict[str, Any]]:
        predicates = _where_predicates(statement.whereclause)
        matched = [row for row in self.sessions if all(p(row) for p in predicates)]

        for column_name, descending in reversed(_order_specs(statement)):
            matched.sort(key=lambda r: r[column_name], reverse=descending)

        limit = _limit_value(statement)
        if limit is not None:
            matched = matched[:limit]
        return matched


def _literal(value: Any) -> Any:
    """Unwrap a bound literal to its Python value; leave SQL functions intact."""
    return getattr(value, "value", value)


def _resolve_now(value: Any) -> Any:
    """A ``func.now()`` node stands in as the fixed clock; pass through reals."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # anything else (a `now()` function element) resolves to the fixed clock.
    return _NOW


def _where_predicates(whereclause: Any) -> list[Any]:
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None) or [whereclause]

    predicates = []
    for clause in clauses:
        column_name = clause.left.name
        operator = clause.operator.__name__
        if operator == "eq":
            predicates.append(_eq(column_name, clause.right.value))
        elif operator == "lt":
            predicates.append(_lt(column_name, clause.right.value))
        elif operator == "is_":  # `col.is_(None)` -> right is a Null node
            predicates.append(_is_none(column_name))
        else:  # pragma: no cover - the repository only uses the three above
            raise AssertionError(f"unhandled operator in fake: {operator}")
    return predicates


def _eq(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] == value


def _lt(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] < value


def _is_none(column_name: str) -> Any:
    return lambda row: row[column_name] is None


def _order_specs(statement: Select) -> list[tuple[str, bool]]:
    specs: list[tuple[str, bool]] = []
    for element in statement._order_by_clauses:
        descending = element.modifier.__name__ == "desc_op"
        specs.append((element.element.name, descending))
    return specs


def _limit_value(statement: Select) -> int | None:
    limit_clause = getattr(statement, "_limit_clause", None)
    return getattr(limit_clause, "value", None) if limit_clause is not None else None


def _repo() -> tuple[SpectatorSessionsRepository, _FakeDatabase]:
    database = _FakeDatabase()
    return SpectatorSessionsRepository(database), database  # type: ignore[arg-type]


async def test_start_session_round_trips_and_returns_the_durable_id() -> None:
    repository, _database = _repo()

    session = await repository.start_session(host_id=3, spectator_id=7)

    assert session.id == 1
    assert session.host_id == 3
    assert session.spectator_id == 7
    assert session.started_at == _NOW
    assert session.ended_at is None

    # the durable id is fetchable back for the close to stamp.
    fetched = await repository.fetch_session(session.id)
    assert fetched is not None
    assert fetched.id == session.id


async def test_fetch_session_returns_none_for_an_unknown_id() -> None:
    repository, _database = _repo()

    assert await repository.fetch_session(999) is None


async def test_end_session_stamps_and_is_guarded_by_null() -> None:
    repository, database = _repo()
    session = await repository.start_session(host_id=3, spectator_id=7)

    await repository.end_session(session.id)

    ended = await repository.fetch_session(session.id)
    assert ended is not None
    assert ended.ended_at is not None

    # the UPDATE only ever closes an open session: the NULL guard is in the SQL,
    # so a duplicate stop cannot rewrite the original end time.
    sql = (database.last_update_sql or "").lower()
    assert "update spectator_sessions" in sql
    assert "set ended_at=now()" in sql
    assert "ended_at is null" in sql


async def test_end_session_does_not_reclose_an_already_ended_session() -> None:
    repository, database = _repo()
    session = await repository.start_session(host_id=3, spectator_id=7)
    await repository.end_session(session.id)
    first = await repository.fetch_session(session.id)
    assert first is not None
    first_time = first.ended_at

    # pin the recorded end time so a (wrongly) re-applied UPDATE would be visible.
    database.sessions[0]["ended_at"] = first_time
    await repository.end_session(session.id)

    second = await repository.fetch_session(session.id)
    assert second is not None
    assert second.ended_at == first_time


async def test_fetch_sessions_pages_newest_first_by_id() -> None:
    repository, _database = _repo()
    for spectator_id in (10, 20, 30):
        await repository.start_session(host_id=3, spectator_id=spectator_id)

    newest = await repository.fetch_sessions(limit=2)
    assert [s.spectator_id for s in newest] == [30, 20]

    older = await repository.fetch_sessions(before_id=newest[-1].id, limit=2)
    assert [s.spectator_id for s in older] == [10]


async def test_fetch_sessions_filters_by_host_id() -> None:
    repository, _database = _repo()
    await repository.start_session(host_id=3, spectator_id=10)
    await repository.start_session(host_id=9, spectator_id=11)
    await repository.start_session(host_id=3, spectator_id=12)

    watched_host_3 = await repository.fetch_sessions(host_id=3)
    assert [s.spectator_id for s in watched_host_3] == [12, 10]
    assert all(s.host_id == 3 for s in watched_host_3)


async def test_fetch_sessions_filters_by_spectator_id() -> None:
    repository, _database = _repo()
    await repository.start_session(host_id=3, spectator_id=7)
    await repository.start_session(host_id=9, spectator_id=7)
    await repository.start_session(host_id=5, spectator_id=8)

    viewer_7_watched = await repository.fetch_sessions(spectator_id=7)
    assert [s.host_id for s in viewer_7_watched] == [9, 3]
    assert all(s.spectator_id == 7 for s in viewer_7_watched)


async def test_fetch_sessions_composes_both_filters() -> None:
    repository, _database = _repo()
    await repository.start_session(host_id=3, spectator_id=7)  # the pinned pair
    await repository.start_session(host_id=3, spectator_id=8)  # same host, other viewer
    await repository.start_session(host_id=9, spectator_id=7)  # same viewer, other host

    pair = await repository.fetch_sessions(host_id=3, spectator_id=7)
    assert [(s.host_id, s.spectator_id) for s in pair] == [(3, 7)]


async def test_fetch_sessions_is_empty_when_nothing_matches() -> None:
    repository, _database = _repo()
    await repository.start_session(host_id=3, spectator_id=7)

    assert await repository.fetch_sessions(host_id=999) == []
