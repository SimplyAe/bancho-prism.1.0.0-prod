"""The read side of durable multiplayer match history.

``app/services/multiplayer/match_history.py`` is the *write* side -- the
fire-and-forget producers that sit on the in-memory match lifecycle and append
the durable rows. This is its counterpart: the query side that reads those rows
back for the v2 HTTP surface. Keeping the two apart mirrors the shapes each
needs -- the producers are loose functions invoked off packet handlers, while a
reader is a ``frozen`` service injected into request handlers exactly like
:class:`app.services.social.activity_feed.ActivityFeedService`.

The one piece of domain knowledge this layer owns is **visibility**. The
``mp_matches`` row carries ``has_public_history`` -- the ``//private`` lobby flag,
captured at creation because it is unrecoverable afterwards. The repository is
deliberately dumb about it (it just stores and reads the bit); this service is
where it acquires meaning:

- the public *listing* (``fetch_recent_matches``) only ever surfaces public
  matches -- a private lobby never appears in a browsable index, for anyone;
- a *specific* match (and its games) is visible when it is public, or the
  viewer is its host, or the viewer is staff. So a host can always review their
  own private lobby, and staff can audit any, without a private lobby leaking
  into the public index.

A read that resolves to "not visible" is reported the same as "unknown" -- both
return ``None`` here, so the HTTP layer answers 404 either way and a private
match's mere existence is not disclosed. ``fetch_games`` distinguishes "no such
visible match" (``None`` -> 404) from "visible match with no games yet"
(``[]``), so an empty history is not mistaken for a missing one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.repositories.mp_matches import MpMatch
from app.repositories.mp_matches import MpMatchesRepository
from app.repositories.mp_matches import MpMatchGame
from app.repositories.mp_matches import MpMatchGameScore


def _is_visible_to(
    match: MpMatch,
    *,
    viewer_id: int | None,
    viewer_is_staff: bool,
) -> bool:
    """Whether ``viewer`` may read this specific match's history.

    Public history is visible to everyone; a private lobby is visible only to
    its host or to staff. ``viewer_id`` is ``None`` for an anonymous caller,
    who therefore sees public matches alone.
    """
    if match.has_public_history:
        return True
    if viewer_is_staff:
        return True
    return viewer_id is not None and viewer_id == match.host_id


@dataclass(frozen=True)
class MatchHistoryService:
    matches: MpMatchesRepository

    async def fetch_recent_matches(
        self,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[MpMatch]:
        """Recent *public* matches, newest first, keyset-paged by id.

        The public/private filter is pushed into the query (not applied after
        paging) so a page never comes back short because private lobbies were
        stripped from it -- which would corrupt the keyset cursor.
        """
        return await self.matches.fetch_recent_matches(
            before_id=before_id,
            limit=limit,
            public_only=True,
        )

    async def fetch_match(
        self,
        match_id: int,
        *,
        viewer_id: int | None = None,
        viewer_is_staff: bool = False,
    ) -> MpMatch | None:
        """One match by id if the viewer may see it, else ``None``.

        ``None`` covers both "no such match" and "not visible to you", so the
        HTTP layer answers 404 in both cases and the existence of a private
        lobby is not disclosed to a caller who may not read it.
        """
        match = await self.matches.fetch_match(match_id)
        if match is None:
            return None
        if not _is_visible_to(
            match,
            viewer_id=viewer_id,
            viewer_is_staff=viewer_is_staff,
        ):
            return None
        return match

    async def fetch_games(
        self,
        match_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
        viewer_id: int | None = None,
        viewer_is_staff: bool = False,
    ) -> list[MpMatchGame] | None:
        """The games of one match, newest first, keyset-paged by id.

        Returns ``None`` when there is no match the viewer may read (unknown or
        private-and-not-yours) so the HTTP layer answers 404; returns ``[]`` for
        a visible match that has no recorded games yet, so an empty history is
        never conflated with a missing match.
        """
        match = await self.matches.fetch_match(match_id)
        if match is None or not _is_visible_to(
            match,
            viewer_id=viewer_id,
            viewer_is_staff=viewer_is_staff,
        ):
            return None
        return await self.matches.fetch_games_for_match(
            match_id,
            before_id=before_id,
            limit=limit,
        )

    async def fetch_game_scores(
        self,
        match_id: int,
        game_id: int,
        *,
        viewer_id: int | None = None,
        viewer_is_staff: bool = False,
    ) -> list[MpMatchGameScore] | None:
        """One game's per-player scoreboard, ordered by finishing placement.

        Gated by the same visibility rule as the rest of the match: the caller
        must be able to read the parent match (public, its host, or staff). The
        game is then confirmed to actually belong to that match -- so a caller
        cannot pair a private game's id with a public match they *can* see and
        read a scoreboard they may not. A mismatch, an unknown game, and an
        unreadable match all collapse to ``None`` (HTTP 404), disclosing
        nothing about a game that is not theirs to see.

        Returns ``[]`` for a game that belongs to a visible match but has no
        recorded scores (an older game, or one where no participant sent a
        usable frame) -- an empty scoreboard, distinct from a missing game.
        """
        match = await self.matches.fetch_match(match_id)
        if match is None or not _is_visible_to(
            match,
            viewer_id=viewer_id,
            viewer_is_staff=viewer_is_staff,
        ):
            return None

        game = await self.matches.fetch_game(game_id)
        if game is None or game.match_id != match_id:
            # unknown game, or a real game but under a different match than the
            # one it was requested through -- both are "not here", so 404.
            return None

        return await self.matches.fetch_scores_for_game(game_id)
