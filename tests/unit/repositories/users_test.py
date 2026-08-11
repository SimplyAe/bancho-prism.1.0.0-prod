"""Guard for DB-authoritative api-key authentication.

The `/calculate_pp` endpoint (and any future api-key caller) authenticates by
asking `UsersRepository.fetch_by_api_key` on every request instead of reading a
RAM snapshot loaded at startup. The snapshot was the bug: a key issued, revoked,
or rotated after boot stayed wrong until the process restarted -- a revoked key
kept working, a freshly-issued one kept failing.

These tests pin the two properties that make the fix correct:

- the lookup is keyed on the api-key argument (compiled straight into the SQL
  `WHERE`), so it is the database -- not a cache that can drift -- that decides;
- an unrecognised key resolves to `None` (the endpoint turns that into 401),
  never into a stray user.
"""

from __future__ import annotations

from typing import Any

from app.repositories.users import UsersRepository


def _row(user_id: int, api_key: str) -> dict[str, Any]:
    # only the columns `_deserialize_user` reads; the rest of the schema is
    # irrelevant to the api-key lookup and would just be noise here.
    return {
        "id": user_id,
        "name": "cmyui",
        "safe_name": "cmyui",
        "email": "cmyui@example.com",
        "priv": 1,
        "country": "ca",
        "silence_end": 0,
        "donor_end": 0,
        "creation_time": 0,
        "latest_activity": 0,
        "clan_id": 0,
        "clan_priv": 0,
        "preferred_mode": 0,
        "play_style": 0,
        "custom_badge_name": None,
        "custom_badge_icon": None,
        "userpage_content": None,
        "api_key": api_key,
    }


class _FakeDatabase:
    """A stand-in that answers `fetch_one` from an api_key -> row table.

    It compiles the statement it is handed and pulls the bound parameter out of
    the `WHERE`, so the row it returns is decided by the *key the repository
    asked for* -- exactly what "authoritative against the database" has to mean.
    """

    def __init__(self, rows_by_api_key: dict[str, dict[str, Any]]) -> None:
        self._rows_by_api_key = rows_by_api_key
        self.fetch_one_call_count = 0

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        self.fetch_one_call_count += 1
        compiled = statement.compile()
        # the query has a single bound parameter: the api key being looked up.
        (queried_api_key,) = compiled.params.values()
        return self._rows_by_api_key.get(queried_api_key)


async def test_fetch_by_api_key_returns_the_owning_user_for_a_known_key() -> None:
    database = _FakeDatabase({"known-key": _row(user_id=3, api_key="known-key")})
    repository = UsersRepository(database)  # type: ignore[arg-type]

    user = await repository.fetch_by_api_key("known-key")

    assert user is not None
    assert user.id == 3
    assert user.api_key == "known-key"


async def test_fetch_by_api_key_returns_none_for_an_unknown_key() -> None:
    database = _FakeDatabase({"known-key": _row(user_id=3, api_key="known-key")})
    repository = UsersRepository(database)  # type: ignore[arg-type]

    user = await repository.fetch_by_api_key("revoked-or-never-issued")

    assert user is None


async def test_fetch_by_api_key_queries_the_database_on_every_call() -> None:
    # the whole point of the fix: no RAM snapshot short-circuits the lookup, so
    # a key's validity is re-decided by mysql each time it is presented.
    database = _FakeDatabase({"known-key": _row(user_id=3, api_key="known-key")})
    repository = UsersRepository(database)  # type: ignore[arg-type]

    await repository.fetch_by_api_key("known-key")
    await repository.fetch_by_api_key("known-key")

    assert database.fetch_one_call_count == 2
