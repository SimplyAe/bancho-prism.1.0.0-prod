"""Guards for the activity-feed repository.

``activity_events`` is an append-only log: notable player events (rank
milestones, personal bests, new #1s, achievements) written once and read back as
a feed. These tests pin the behaviour the feed depends on:

- ``record`` only ever inserts (the log never updates in place) and round-trips
  the stored row back through ``_fetch``, including a JSON ``data`` payload -- and
  a missing payload round-trips as a true ``None``, not the string ``"null"``;
- ``fetch_for_user`` returns one player's events newest-first, scrolls backwards
  by the keyset ``before_id`` (``id <`` the last-seen id), and honours ``limit``;
- ``fetch_feed`` merges several players' events into one newest-first stream, and
  short-circuits an empty id list to ``[]`` *without* querying (a friendless
  player's empty feed must not compile to an invalid ``IN ()``).

No MySQL: a fake ``Database`` stores rows and interprets the Core statement it is
handed -- the insert's bound values, and each read's ``WHERE`` / ``ORDER BY`` /
``LIMIT`` -- so the repository's own filtering and ordering decide the result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.repositories.activity_events import ActivityEvent
from app.repositories.activity_events import ActivityEventsRepository
from app.repositories.activity_events import ActivityEventsTable

# the columns the insert binds by name; `created_at` is a SQL `func.now()` (not a
# bind param) so the fake supplies it, and `id` is auto-increment.
_COLUMN_NAMES = frozenset(column.name for column in ActivityEventsTable.__table__.columns)

# a fixed clock for the server-side `created_at`, so ordering never hinges on
# wall-clock time (ordering is by id anyway, but this keeps rows deterministic).
_CREATED_AT = datetime(2026, 8, 12, 12, 0, 0)


class _FakeDatabase:
    """Stores event rows and answers reads by interpreting the statement."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1
        self.query_count = 0  # reads that actually reached the DB

    async def execute(self, statement: Any) -> int:
        compiled = statement.compile()
        params = compiled.params
        values = {k: v for k, v in params.items() if k in _COLUMN_NAMES}

        row: dict[str, Any] = {
            "id": self._next_id,
            "mode": None,
            "data": None,
            "created_at": _CREATED_AT,
        }
        row.update(values)
        self.rows.append(row)
        self._next_id += 1
        return row["id"]

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        matched = self._query(statement)
        return matched[0] if matched else None

    async def fetch_all(self, statement: Any) -> list[dict[str, Any]]:
        return self._query(statement)

    def _query(self, statement: Any) -> list[dict[str, Any]]:
        self.query_count += 1
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
        elif operator == "lt":
            predicates.append(_lt(column_name, clause.right.value))
        elif operator == "in_op":  # `col.in_([...])` -> expanding bind, .value is the list
            predicates.append(_in(column_name, list(clause.right.value)))
        else:  # pragma: no cover - the repository only uses the three above
            raise AssertionError(f"unhandled operator in fake: {operator}")
    return predicates


def _eq(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] == value


def _lt(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] < value


def _in(column_name: str, values: list[Any]) -> Any:
    return lambda row: row[column_name] in values


def _order_specs(statement: Any) -> list[tuple[str, bool]]:
    specs: list[tuple[str, bool]] = []
    for element in statement._order_by_clauses:
        descending = element.modifier.__name__ == "desc_op"
        specs.append((element.element.name, descending))
    return specs


def _limit_value(statement: Any) -> int | None:
    limit_clause = getattr(statement, "_limit_clause", None)
    return getattr(limit_clause, "value", None) if limit_clause is not None else None


def _repo() -> tuple[ActivityEventsRepository, _FakeDatabase]:
    database = _FakeDatabase()
    return ActivityEventsRepository(database), database  # type: ignore[arg-type]


async def test_record_round_trips_an_event_with_its_payload() -> None:
    repository, _ = _repo()

    event = await repository.record(
        user_id=3,
        event_type="pp_record",
        mode=0,
        data={"old_pp": 4000, "new_pp": 4200},
    )

    assert event.id == 1
    assert event.user_id == 3
    assert event.event_type == "pp_record"
    assert event.mode == 0
    assert event.data == {"old_pp": 4000, "new_pp": 4200}
    assert event.created_at == _CREATED_AT


async def test_record_stores_a_missing_payload_as_none_not_the_string_null() -> None:
    repository, _ = _repo()

    event = await repository.record(user_id=3, event_type="login_streak")

    assert event.mode is None
    assert event.data is None  # true None, not json.loads("null")


async def test_record_is_append_only() -> None:
    repository, database = _repo()

    first = await repository.record(user_id=3, event_type="rank_up")
    second = await repository.record(user_id=3, event_type="rank_up")

    # two writes -> two rows with increasing ids; nothing is overwritten.
    assert (first.id, second.id) == (1, 2)
    assert len(database.rows) == 2


async def test_event_type_longer_than_the_column_is_truncated() -> None:
    repository, _ = _repo()

    event = await repository.record(user_id=3, event_type="x" * 100)

    assert len(event.event_type) == 32


async def test_fetch_for_user_is_newest_first() -> None:
    repository, _ = _repo()
    await repository.record(user_id=3, event_type="a")  # id 1
    await repository.record(user_id=3, event_type="b")  # id 2
    await repository.record(user_id=3, event_type="c")  # id 3

    feed = await repository.fetch_for_user(3)

    assert [e.id for e in feed] == [3, 2, 1]


async def test_fetch_for_user_scrolls_backwards_by_before_id() -> None:
    repository, _ = _repo()
    for _ in range(5):  # ids 1..5
        await repository.record(user_id=3, event_type="e")

    older = await repository.fetch_for_user(3, before_id=3)

    # strictly older than id 3, still newest-first.
    assert [e.id for e in older] == [2, 1]


async def test_fetch_for_user_honours_the_limit() -> None:
    repository, _ = _repo()
    for _ in range(5):
        await repository.record(user_id=3, event_type="e")

    page = await repository.fetch_for_user(3, limit=2)

    assert [e.id for e in page] == [5, 4]


async def test_fetch_for_user_only_returns_that_player() -> None:
    repository, _ = _repo()
    await repository.record(user_id=3, event_type="mine")
    await repository.record(user_id=4, event_type="theirs")

    feed = await repository.fetch_for_user(3)

    assert [e.user_id for e in feed] == [3]


async def test_fetch_feed_merges_several_players_newest_first() -> None:
    repository, _ = _repo()
    await repository.record(user_id=3, event_type="a")  # id 1
    await repository.record(user_id=4, event_type="b")  # id 2
    await repository.record(user_id=5, event_type="c")  # id 3 -- not a friend
    await repository.record(user_id=3, event_type="d")  # id 4

    feed = await repository.fetch_feed([3, 4])

    # friends 3 and 4 only, merged and newest-first; 5's event is excluded.
    assert [e.id for e in feed] == [4, 2, 1]


async def test_fetch_feed_scrolls_backwards_by_before_id() -> None:
    repository, _ = _repo()
    for uid in (3, 4, 3, 4, 3):  # ids 1..5 across two friends
        await repository.record(user_id=uid, event_type="e")

    older = await repository.fetch_feed([3, 4], before_id=4)

    assert [e.id for e in older] == [3, 2, 1]


async def test_fetch_feed_short_circuits_an_empty_id_list_without_querying() -> None:
    repository, database = _repo()
    await repository.record(user_id=3, event_type="e")
    queries_before = database.query_count

    feed = await repository.fetch_feed([])

    assert feed == []
    # a friendless player's empty feed must never compile to `IN ()`; the
    # repository returns early, so the database is not touched by this call.
    assert database.query_count == queries_before
