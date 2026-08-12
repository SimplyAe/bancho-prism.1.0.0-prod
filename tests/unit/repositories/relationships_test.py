"""Guards for the relationships repository's follow-graph reads.

The ``relationships`` table stores a directional edge (``user1`` -> ``user2``);
for the ``FRIEND`` type that edge is a follow. The forward reads (``fetch_all``,
``count_outgoing``: "who I follow") and the reverse reads (``fetch_all_incoming``,
``count_incoming``: "who follows me") differ only in which column the filter
pins, and getting that column wrong silently swaps followers for following. A
fake ``Database`` interprets the ``Select`` it is handed -- its ``WHERE`` -- so
these pin, for real SQL, that the reverse reads filter on ``user2`` (not
``user1``) and that counts are scoped to the requested type.
"""

from __future__ import annotations

from typing import Any

from app.repositories.relationships import RelationshipsRepository
from app.repositories.relationships import RelationshipsTable
from app.repositories.relationships import RelationshipType

_COLUMN_NAMES = frozenset(
    column.name for column in RelationshipsTable.__table__.columns
)


class _FakeDatabase:
    """Stores relationship rows and answers reads by walking the ``WHERE``.

    ``fetch_all`` and ``fetch_val`` interpret the statement's predicates so the
    repository's own column choice (user1 vs user2) and type filter decide the
    result, rather than the fake assuming a direction.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, *, user1: int, user2: int, type: str) -> None:
        self.rows.append({"user1": user1, "user2": user2, "type": type})

    async def fetch_all(self, statement: Any) -> list[dict[str, Any]]:
        return self._query(statement)

    async def fetch_val(self, statement: Any, params: Any = None, column: Any = 0) -> Any:
        return len(self._query(statement))

    def _query(self, statement: Any) -> list[dict[str, Any]]:
        predicates = _where_predicates(statement.whereclause)
        return [row for row in self.rows if all(p(row) for p in predicates)]


def _where_predicates(whereclause: Any) -> list[Any]:
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None) or [whereclause]

    predicates = []
    for clause in clauses:
        column_name = clause.left.name
        right = clause.right.value
        # the type column stores the StrEnum's *value* ("friend"), matching the
        # enum('friend','block') column and how the repository deserialises it.
        if isinstance(right, RelationshipType):
            right = right.value
        predicates.append(_eq(column_name, right))
    return predicates


def _eq(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] == value


def _repo() -> tuple[RelationshipsRepository, _FakeDatabase]:
    database = _FakeDatabase()
    return RelationshipsRepository(database), database  # type: ignore[arg-type]


async def test_fetch_all_incoming_filters_on_the_target_as_user2() -> None:
    repository, database = _repo()
    # 4 and 5 follow 3; 3 follows 6 (an outgoing edge, must not appear).
    database.add(user1=4, user2=3, type="friend")
    database.add(user1=5, user2=3, type="friend")
    database.add(user1=3, user2=6, type="friend")

    incoming = await repository.fetch_all_incoming(
        user2=3,
        type=RelationshipType.FRIEND,
    )

    assert sorted(rel.user1 for rel in incoming) == [4, 5]
    assert all(rel.user2 == 3 for rel in incoming)


async def test_fetch_all_incoming_scopes_to_the_requested_type() -> None:
    repository, database = _repo()
    database.add(user1=4, user2=3, type="friend")
    database.add(user1=5, user2=3, type="block")

    incoming = await repository.fetch_all_incoming(
        user2=3,
        type=RelationshipType.FRIEND,
    )

    assert [rel.user1 for rel in incoming] == [4]


async def test_count_incoming_counts_followers_of_the_type() -> None:
    repository, database = _repo()
    database.add(user1=4, user2=3, type="friend")
    database.add(user1=5, user2=3, type="friend")
    database.add(user1=6, user2=3, type="block")
    database.add(user1=3, user2=7, type="friend")  # outgoing, not a follower

    assert await repository.count_incoming(user2=3, type=RelationshipType.FRIEND) == 2


async def test_count_outgoing_counts_following_of_the_type() -> None:
    repository, database = _repo()
    database.add(user1=3, user2=6, type="friend")
    database.add(user1=3, user2=7, type="friend")
    database.add(user1=3, user2=8, type="block")
    database.add(user1=4, user2=3, type="friend")  # incoming, not following

    assert await repository.count_outgoing(user1=3, type=RelationshipType.FRIEND) == 2
