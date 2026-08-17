"""Short-lived CSRF state for the Discord OAuth2 flow, stored in Redis.

OAuth2's ``state`` parameter is the flow's CSRF defence: we mint a random token
when a player *begins* linking, hand it to Discord in the authorize URL, and
Discord hands it back on the callback. If the value coming back is not one we
issued, the callback is forged (or stale) and we reject it. Binding the token to
the ``user_id`` that started the flow does one more thing the bare-CSRF use does
not: it means the callback links the Discord account to *the account that asked*,
never to whoever's session happens to be attached to the callback request.

Redis, not MySQL, because this is ephemeral by design: a state is valid only for
the couple of minutes between the redirect out and the redirect back, so it
carries a short TTL and expires on its own if the player abandons the flow. It is
also **single-use** -- ``consume`` reads and deletes in one atomic ``GETDEL`` so a
token cannot be replayed, and a duplicated callback finds nothing the second time.
"""

from __future__ import annotations

from redis import asyncio as aioredis


def _state_key(state: str) -> str:
    return f"bancho:discord_oauth_state:{state}"


class DiscordOAuthStateRepository:
    """Issued OAuth ``state`` tokens, each mapped to the user who began the flow."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def store(self, state: str, user_id: int, expiry_seconds: int) -> None:
        """Remember that ``state`` was issued for ``user_id``, expiring on its own."""
        await self._redis.set(_state_key(state), str(user_id), ex=expiry_seconds)

    async def consume(self, state: str) -> int | None:
        """Atomically fetch-and-delete the user id behind a state token.

        Returns None if the token was never issued, already used, or expired --
        every "reject this callback" case collapses to the same answer. The
        ``GETDEL`` makes the read single-use: a replayed callback gets None.
        """
        value = await self._redis.getdel(_state_key(state))
        if value is None:
            return None
        return int(value)
