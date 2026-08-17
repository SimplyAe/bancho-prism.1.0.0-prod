"""Guards for the single-use Discord OAuth state store.

The ``state`` token is the flow's CSRF proof: minted at ``begin_link`` bound to
the requesting osu! account, echoed back by Discord on the callback, and consumed
exactly once. These tests pin what the service relies on:

- a stored state round-trips back to the user id that owns it;
- ``consume`` is single-use -- a second consume (a replayed callback) finds
  nothing and returns ``None``, so a captured callback cannot be reused;
- an unknown state returns ``None`` rather than raising;
- the write is given a TTL, so an abandoned flow expires on its own.

No Redis: a fake implements just ``set``/``getdel`` and returns ``bytes`` the way
aioredis does, so the repository's own ``int(...)`` decode is exercised.
"""

from __future__ import annotations

from app.repositories.discord_oauth_state import DiscordOAuthStateRepository
from app.repositories.discord_oauth_state import _state_key


class _FakeRedis:
    """In-memory stand-in for the two commands the store uses.

    Values are held as ``bytes`` to mirror aioredis; ``set`` records the ``ex``
    TTL so a test can assert the key is given an expiry, and ``getdel`` deletes
    on read to make the single-use property real rather than assumed.
    """

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value.encode()
        if ex is not None:
            self.expiries[key] = ex

    async def getdel(self, key: str) -> bytes | None:
        return self.values.pop(key, None)


def _repo() -> tuple[DiscordOAuthStateRepository, _FakeRedis]:
    redis = _FakeRedis()
    return DiscordOAuthStateRepository(redis), redis  # type: ignore[arg-type]


async def test_store_then_consume_round_trips_the_user_id() -> None:
    repo, _ = _repo()

    await repo.store("state-abc", user_id=7, expiry_seconds=600)

    assert await repo.consume("state-abc") == 7


async def test_store_sets_a_ttl() -> None:
    repo, redis = _repo()

    await repo.store("state-abc", user_id=7, expiry_seconds=600)

    # an abandoned flow must not linger forever; the write carries the expiry.
    assert redis.expiries[_state_key("state-abc")] == 600


async def test_consume_is_single_use() -> None:
    repo, _ = _repo()
    await repo.store("state-abc", user_id=7, expiry_seconds=600)

    assert await repo.consume("state-abc") == 7
    # a replayed callback finds nothing the second time.
    assert await repo.consume("state-abc") is None


async def test_consume_of_an_unknown_state_returns_none() -> None:
    repo, _ = _repo()

    assert await repo.consume("never-stored") is None


async def test_keys_are_namespaced_per_state() -> None:
    repo, redis = _repo()

    await repo.store("one", user_id=1, expiry_seconds=60)
    await repo.store("two", user_id=2, expiry_seconds=60)

    # distinct states never collide, and the namespace keeps them out of other
    # bancho keyspaces.
    assert set(redis.values) == {_state_key("one"), _state_key("two")}
    assert await repo.consume("one") == 1
    assert await repo.consume("two") == 2
