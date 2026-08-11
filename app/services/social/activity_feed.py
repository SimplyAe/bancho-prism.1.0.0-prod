"""The activity feed's domain seam: what events mean and how a feed is read.

``app/repositories/activity_events.py`` is deliberately dumb -- it stores a
``(user_id, event_type, mode, data)`` row and reads it back, treating
``event_type`` as an opaque string and ``data`` as opaque JSON. This module is
where those strings acquire meaning. It owns:

- :class:`ActivityEventType` -- the closed vocabulary of event kinds. Living in
  the services layer (not the repository) keeps the repository free of domain
  knowledge, the same layering the anticheat code follows by keeping ``severity``
  a plain string in the flags table.
- one typed ``publish_*`` method per event kind, each of which builds the exact
  ``data`` payload a renderer will expect. A caller cannot publish a "pp record"
  without supplying the old and new pp, so the feed's payloads stay well-formed
  by construction rather than by convention.
- the two read paths: a single player's feed, and the fan-out **friends feed**
  (the events of everyone a viewer has friended). The friend edge already exists
  as ``relationships`` rows of type ``FRIEND`` -- an asymmetric "follow" graph --
  so the friends feed is just "resolve the viewer's friend ids, then read the
  merged feed across them", with no new social primitive.

Publishing is not a security boundary and never punishes or notifies anyone; it
appends a fact to a log. Reads here are unfiltered by visibility on purpose: the
HTTP layer decides who may call which method (your own feed vs. a public
profile), exactly as it gates the rest of the v2 surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.repositories.activity_events import ActivityEvent
from app.repositories.activity_events import ActivityEventsRepository
from app.repositories.relationships import RelationshipsRepository
from app.repositories.relationships import RelationshipType

# a friends feed fans out to one IN-list read; cap the number of friend ids so a
# user with a huge friend list cannot turn a single feed read into a pathological
# query. The newest events across the most recent friendships still dominate a
# feed, so this bound is not felt in practice.
_MAX_FEED_SOURCES = 500


class ActivityEventType(StrEnum):
    """The closed set of feed event kinds.

    The string value is what lands in ``activity_events.event_type``; keeping the
    vocabulary here (not in the repository) means a new kind is a one-line
    addition with a matching ``publish_*`` method, and the stored column stays a
    plain slug.
    """

    RANK_UP = "rank_up"
    PP_RECORD = "pp_record"
    FIRST_PLACE = "first_place"
    ACHIEVEMENT = "achievement"


@dataclass(frozen=True)
class ActivityFeedService:
    events: ActivityEventsRepository
    relationships: RelationshipsRepository

    # --- publishing: one typed method per event kind ------------------------

    async def publish_rank_up(
        self,
        *,
        user_id: int,
        mode: int,
        old_rank: int | None,
        new_rank: int,
    ) -> ActivityEvent:
        """A player reached a new best global rank in a mode.

        ``old_rank`` is ``None`` the first time a player becomes ranked; the
        payload records it either way so a renderer can show "entered the
        leaderboard" versus "climbed from N".
        """
        return await self.events.record(
            user_id=user_id,
            event_type=ActivityEventType.RANK_UP.value,
            mode=mode,
            data={"old_rank": old_rank, "new_rank": new_rank},
        )

    async def publish_pp_record(
        self,
        *,
        user_id: int,
        mode: int,
        old_pp: int,
        new_pp: int,
    ) -> ActivityEvent:
        """A player set a new personal-best total pp in a mode."""
        return await self.events.record(
            user_id=user_id,
            event_type=ActivityEventType.PP_RECORD.value,
            mode=mode,
            data={"old_pp": old_pp, "new_pp": new_pp},
        )

    async def publish_first_place(
        self,
        *,
        user_id: int,
        mode: int,
        score_id: int,
        beatmap_id: int,
        beatmap_title: str,
        pp: float,
    ) -> ActivityEvent:
        """A player took the #1 leaderboard score on a beatmap."""
        return await self.events.record(
            user_id=user_id,
            event_type=ActivityEventType.FIRST_PLACE.value,
            mode=mode,
            data={
                "score_id": score_id,
                "beatmap_id": beatmap_id,
                "beatmap_title": beatmap_title,
                "pp": pp,
            },
        )

    async def publish_achievement(
        self,
        *,
        user_id: int,
        mode: int,
        achievement_id: int,
        achievement_name: str,
    ) -> ActivityEvent:
        """A player unlocked an achievement."""
        return await self.events.record(
            user_id=user_id,
            event_type=ActivityEventType.ACHIEVEMENT.value,
            mode=mode,
            data={"id": achievement_id, "name": achievement_name},
        )

    # --- reading ------------------------------------------------------------

    async def fetch_user_feed(
        self,
        user_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        """One player's own events, newest first (keyset-paged by id)."""
        return await self.events.fetch_for_user(
            user_id,
            before_id=before_id,
            limit=limit,
        )

    async def fetch_friends_feed(
        self,
        viewer_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        """The merged feed of everyone the viewer has friended.

        Resolves the viewer's ``FRIEND`` edges to their target ids, then reads
        the merged feed across them. A viewer with no friends gets an empty feed
        (the repository short-circuits an empty id list without a query). The
        viewer's own events are deliberately excluded -- this is "what my friends
        did", and their own feed is ``fetch_user_feed``.
        """
        friendships = await self.relationships.fetch_all(
            user1=viewer_id,
            type=RelationshipType.FRIEND,
        )
        friend_ids = [rel.user2 for rel in friendships][:_MAX_FEED_SOURCES]
        return await self.events.fetch_feed(
            friend_ids,
            before_id=before_id,
            limit=limit,
        )
