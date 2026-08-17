"""Guards for the osu! <-> Discord link repository.

The table keeps one row per osu! account (``user_id`` is the primary key) and a
unique ``discord_id``, so it enforces one-Discord-per-osu! itself and hands the
service the reverse lookup it needs to enforce one-osu!-per-Discord. These tests
pin the behaviour the linking flow leans on:

- ``upsert_link`` inserts a first link and round-trips it back through ``_fetch``;
- re-linking the *same* osu! account is a clean replace -- the ``user_id`` key
  overwrites in place rather than duplicating -- so a player relinking to a new
  Discord leaves one row, not two;
- the username is truncated to Discord's documented 32-char cap before it is
  written, so a length change on Discord's side can never overflow the column;
- ``fetch_by_discord_id`` is the conflict lookup (does this Discord already back
  an account?), and both reads return ``None`` rather than raising when absent;
- ``delete`` removes the link and is a no-op when there was none.

No MySQL: a fake ``Database`` interprets the statements it is handed. The upsert
pulls its values from the compiled params (keyed by column name) and upserts on
``user_id`` the way the primary key would, supplying ``linked_at`` for the
``func.now()`` default the real column fills server-side; the reads walk the
``Select``'s ``WHERE`` so the repository's own column choice decides the result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Delete

from app.repositories.user_discord_links import DiscordLink
from app.repositories.user_discord_links import DiscordLinksRepository
from app.repositories.user_discord_links import UserDiscordLinksTable

# the columns the insert binds by name; the on-duplicate-key clause's binds get
# `_1`-suffixed by the compiler, and `linked_at` is a `func.now()` SQL call (not
# a bind), so neither lands here -- exactly the values the row is built from.
_COLUMN_NAMES = frozenset(
    column.name for column in UserDiscordLinksTable.__table__.columns
)

# a fixed stand-in for the server-filled `linked_at`; deterministic so tests
# never hinge on wall-clock time.
_LINKED_AT = datetime(2026, 8, 17, 12, 0, 0)


class _FakeDatabase:
    """Stores link rows and interprets the upsert / delete / select it is given.

    ``execute`` handles both DML paths: an ``Insert ... ON DUPLICATE KEY UPDATE``
    upserts on ``user_id``, and a ``Delete`` removes by its ``WHERE``. ``fetch_one``
    walks a ``Select``'s predicates so the repository's own filter column decides
    the match.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def execute(self, statement: Any) -> int:
        if isinstance(statement, Delete):
            return self._delete(statement)
        return self._upsert(statement)

    def _upsert(self, statement: Any) -> int:
        params = statement.compile().params
        values = {k: v for k, v in params.items() if k in _COLUMN_NAMES}
        # `linked_at` is func.now() in the statement, so the DB fills it; the fake
        # supplies its own so the stored row carries every column the DTO reads.
        values.setdefault("linked_at", _LINKED_AT)

        for row in self.rows:
            if row["user_id"] == values["user_id"]:
                row.update(values)  # same osu! account: replace in place.
                return 1

        self.rows.append(dict(values))
        return 1

    def _delete(self, statement: Any) -> int:
        predicates = _where_predicates(statement.whereclause)
        before = len(self.rows)
        self.rows = [row for row in self.rows if not all(p(row) for p in predicates)]
        return before - len(self.rows)

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        predicates = _where_predicates(statement.whereclause)
        for row in self.rows:
            if all(p(row) for p in predicates):
                return row
        return None


def _where_predicates(whereclause: Any) -> list[Any]:
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None) or [whereclause]
    return [_eq(clause.left.name, clause.right.value) for clause in clauses]


def _eq(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] == value


def _repo() -> tuple[DiscordLinksRepository, _FakeDatabase]:
    database = _FakeDatabase()
    return DiscordLinksRepository(database), database  # type: ignore[arg-type]


async def test_upsert_inserts_and_round_trips_the_link() -> None:
    repository, _ = _repo()

    link = await repository.upsert_link(
        user_id=6,
        discord_id="1234567890",
        discord_username="coolguy",
    )

    assert link == DiscordLink(
        user_id=6,
        discord_id="1234567890",
        discord_username="coolguy",
        linked_at=_LINKED_AT,
    )
    assert await repository.fetch_by_user_id(6) == link


async def test_relinking_the_same_account_replaces_in_place() -> None:
    repository, database = _repo()
    await repository.upsert_link(
        user_id=6,
        discord_id="1111",
        discord_username="old",
    )

    # the same osu! account links a *different* Discord: a clean replace, keyed
    # on user_id, not a second row.
    updated = await repository.upsert_link(
        user_id=6,
        discord_id="2222",
        discord_username="new",
    )

    assert updated.discord_id == "2222"
    assert updated.discord_username == "new"
    assert len(database.rows) == 1


async def test_upsert_truncates_the_username_to_the_column_cap() -> None:
    repository, _ = _repo()

    # Discord's usernames are documented at <=32; a longer value must be capped
    # before the write, never overflow the column.
    long_name = "x" * 50
    link = await repository.upsert_link(
        user_id=6,
        discord_id="1234",
        discord_username=long_name,
    )

    assert link.discord_username == "x" * 32


async def test_fetch_by_discord_id_is_the_conflict_lookup() -> None:
    repository, _ = _repo()
    await repository.upsert_link(
        user_id=6,
        discord_id="9999",
        discord_username="owner",
    )

    found = await repository.fetch_by_discord_id("9999")

    assert found is not None
    assert found.user_id == 6


async def test_reads_return_none_when_absent() -> None:
    repository, _ = _repo()

    assert await repository.fetch_by_user_id(404) is None
    assert await repository.fetch_by_discord_id("nope") is None


async def test_delete_removes_the_link() -> None:
    repository, _ = _repo()
    await repository.upsert_link(
        user_id=6,
        discord_id="1234",
        discord_username="gone",
    )

    await repository.delete(6)

    assert await repository.fetch_by_user_id(6) is None


async def test_delete_of_an_unlinked_account_is_a_no_op() -> None:
    repository, _ = _repo()

    # unlinking is idempotent: removing a link that was never there does not raise.
    await repository.delete(6)

    assert await repository.fetch_by_user_id(6) is None
