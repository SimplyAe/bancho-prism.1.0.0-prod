from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from typing import cast

from redis import asyncio as aioredis


class LeaderboardRanksRepository:
    """Player leaderboard positions, stored in redis sorted sets."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def fetch_global_rank(self, player_id: int, mode: int) -> int | None:
        """Fetch a player's 1-indexed global rank for a mode, if ranked."""
        rank = cast(
            "int | None",
            await self._redis.zrevrank(
                f"bancho:leaderboard:{mode}",
                str(player_id),
            ),
        )
        return rank + 1 if rank is not None else None

    async def fetch_country_rank(
        self,
        player_id: int,
        mode: int,
        country: str,
    ) -> int | None:
        """Fetch a player's 1-indexed country rank for a mode, if ranked."""
        rank = cast(
            "int | None",
            await self._redis.zrevrank(
                f"bancho:leaderboard:{mode}:{country}",
                str(player_id),
            ),
        )
        return rank + 1 if rank is not None else None

    async def add_to_country_leaderboard(
        self,
        player_id: int,
        mode: int,
        country: str,
        pp: float,
    ) -> None:
        """Add or update a player's entry on a country leaderboard."""
        await self._redis.zadd(
            f"bancho:leaderboard:{mode}:{country}",
            {str(player_id): pp},
        )

    async def remove_from_country_leaderboard(
        self,
        player_id: int,
        mode: int,
        country: str,
    ) -> None:
        """Remove a player's entry from a country leaderboard."""
        await self._redis.zrem(
            f"bancho:leaderboard:{mode}:{country}",
            str(player_id),
        )

    # -- rebuild support --
    #
    # Redis holds leaderboard *ranks*, but mysql holds the pp they are
    # derived from. If redis loses data (eviction, a flush, a restart with
    # no persisted AOF) the ranks silently become wrong while every other
    # part of the server keeps working -- players simply appear unranked.
    # These methods rebuild the sorted sets from mysql.
    #
    # The rebuild writes to staging keys and swaps them in with RENAME,
    # which is atomic: a concurrent reader sees either the old complete
    # leaderboard or the new one, never a partially-filled set. Building
    # in place would make every player transiently unranked.

    @staticmethod
    def _staging_key(key: str) -> str:
        return f"{key}:rebuilding"

    async def stage_scores(self, key: str, scores: Mapping[str, float]) -> None:
        """Add a batch of scores to a staging key."""
        if not scores:
            return
        await self._redis.zadd(self._staging_key(key), dict(scores))

    async def discard_staging(self, keys: Iterable[str]) -> None:
        """Delete staging keys, e.g. after a failed rebuild."""
        staging_keys = [self._staging_key(key) for key in keys]
        if staging_keys:
            await self._redis.delete(*staging_keys)

    async def fetch_existing_keys(self, mode: int) -> list[str]:
        """Every live leaderboard key for a mode (global + per-country).

        A rebuild needs these so it can clear leaderboards that *should*
        now be empty. Consider a country whose only ranked player was
        restricted: nothing in the fresh data mentions that country, so
        without this its stale sorted set would survive the rebuild and
        keep serving a player who is no longer ranked.

        Uses SCAN rather than KEYS to avoid blocking the redis event loop
        on a large keyspace.
        """
        global_key = self.global_key(mode)
        keys: list[str] = []

        # matches `bancho:leaderboard:{mode}` and
        # `bancho:leaderboard:{mode}:{country}`, but must not match another
        # mode's keys (e.g. mode 1 vs 11), so the global key is added
        # separately and the pattern is anchored with the ':' separator.
        async for raw_key in self._redis.scan_iter(match=f"{global_key}:*"):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            # skip our own staging keys from a previous interrupted run.
            if key.endswith(":rebuilding"):
                continue
            keys.append(key)

        keys.append(global_key)
        return keys

    async def commit_staged(self, keys: Iterable[str]) -> None:
        """Atomically swap staged leaderboards over the live ones.

        A staging key is absent when a mode/country ended up with no
        ranked players. In that case the live key is deleted rather than
        renamed, so a leaderboard that should now be empty does not keep
        serving stale entries.
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            for key in keys:
                staging_key = self._staging_key(key)
                # RENAME errors when the source is missing, so the
                # existence check cannot be deferred into the transaction.
                # buffered into the transaction, not awaited here: the
                # async pipeline returns itself for chaining, though its
                # stubs are typed as awaitable. `_ =` consumes that value
                # so it is not mistaken for a forgotten await.
                if await self._redis.exists(staging_key):
                    _ = pipe.rename(staging_key, key)
                else:
                    _ = pipe.delete(key)
            await pipe.execute()

    @staticmethod
    def global_key(mode: int) -> str:
        return f"bancho:leaderboard:{mode}"

    @staticmethod
    def country_key(mode: int, country: str) -> str:
        return f"bancho:leaderboard:{mode}:{country}"

    async def count_ranked(self, key: str) -> int:
        """Number of players on a leaderboard."""
        return await self._redis.zcard(key)
