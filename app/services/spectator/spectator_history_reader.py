"""The read side of durable spectator-session history.

``app/services/spectator/spectator_history.py`` is the *write* side -- the
fire-and-forget producers that sit on the in-memory spectate lifecycle and
append the durable rows. This is its counterpart: the query side that reads
those rows back for the v2 HTTP surface. Keeping the two apart mirrors the
shapes each needs -- the producers are loose functions invoked off ``Player``
methods, while a reader is a ``frozen`` service injected into request handlers
exactly like :class:`app.services.social.activity_feed.ActivityFeedService`.

Unlike match history there is no visibility gate here: spectating is an
inherently public relationship in osu! (the host and every watcher already see
each other join and leave in real time), so a spectate session carries no
privacy flag and is public data, like a profile's activity feed. What this
layer owns instead is the *shape* of the reads:

- ``fetch_sessions`` exposes the two independently-composable filters --
  ``host_id`` ("who watched this host") and ``spectator_id`` ("who did this
  viewer watch") -- and keyset paging, without the HTTP handler touching the
  repository directly;
- ``fetch_session`` returns ``None`` for an unknown id, so the HTTP layer
  answers 404 rather than conflating "no such session" with any other state.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.repositories.spectator_sessions import SpectatorSession
from app.repositories.spectator_sessions import SpectatorSessionsRepository


@dataclass(frozen=True)
class SpectatorHistoryService:
    sessions: SpectatorSessionsRepository

    async def fetch_sessions(
        self,
        *,
        host_id: int | None = None,
        spectator_id: int | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[SpectatorSession]:
        """Spectate sessions, newest first, keyset-paged by id.

        ``host_id`` and ``spectator_id`` are optional, independently composable
        filters (both, either, or neither); ``before_id`` scrolls backwards.
        Public data, so no viewer identity is threaded through.
        """
        return await self.sessions.fetch_sessions(
            host_id=host_id,
            spectator_id=spectator_id,
            before_id=before_id,
            limit=limit,
        )

    async def fetch_session(self, session_id: int) -> SpectatorSession | None:
        """One session by id, or ``None`` (HTTP 404) if unknown."""
        return await self.sessions.fetch_session(session_id)
