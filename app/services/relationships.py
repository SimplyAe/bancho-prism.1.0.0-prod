from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.constants.privileges import Privileges
from app.objects.player import Player
from app.repositories.relationships import RelationshipsRepository
from app.repositories.relationships import RelationshipType
from app.repositories.users import User
from app.repositories.users import UsersRepository
from app.services.visibility import can_view_player


class OnlinePlayers(Protocol):
    def get(
        self,
        token: str | None = None,
        id: int | None = None,
        name: str | None = None,
    ) -> Player | None: ...


class AddFriendResult(StrEnum):
    ADDED = "added"
    ALREADY_FRIENDS = "already_friends"
    TARGET_NOT_FOUND = "target_not_found"
    CANNOT_FRIEND_SELF = "cannot_friend_self"


@dataclass(frozen=True, slots=True)
class FollowStats:
    """A player's follow-graph standing, as a profile wants to render it.

    The ``FRIEND`` edge is directional (osu!'s "friend" is a follow), so a
    profile has two counts: how many players this one follows, and how many
    follow them. ``viewer_*`` are the signed-in viewer's own relationship to the
    target -- all ``False`` for an anonymous viewer or a viewer looking at
    themselves -- letting a client render a follow/unfollow button and a "follows
    you" / "mutuals" badge without a second round trip.
    """

    following_count: int
    followers_count: int
    viewer_follows: bool
    viewer_followed_by: bool

    @property
    def viewer_mutuals(self) -> bool:
        """The viewer and target follow each other (a reciprocated edge)."""
        return self.viewer_follows and self.viewer_followed_by


@dataclass(frozen=True)
class RelationshipsService:
    relationships: RelationshipsRepository
    users: UsersRepository
    online_players: OnlinePlayers

    async def fetch_friends(self, viewer: User) -> list[User]:
        relationships = await self.relationships.fetch_all(
            user1=viewer.id,
            type=RelationshipType.FRIEND,
        )
        friend_ids = [relationship.user2 for relationship in relationships]
        if not friend_ids:
            return []
        # friends who have since become hidden (restricted or unverified)
        # are omitted unless the viewer is staff, matching their
        # visibility everywhere else
        viewer_is_staff = viewer.priv & Privileges.STAFF.value != 0
        return await self.users.fetch_many(
            ids=friend_ids,
            include_hidden=viewer_is_staff,
        )

    async def fetch_followers(self, target_id: int, *, viewer: User | None) -> list[User]:
        """The players who have friended (follow) ``target_id``.

        The reverse of :meth:`fetch_friends`: it resolves the incoming ``FRIEND``
        edges (``user2 == target``) to the followers themselves. Hidden followers
        are omitted unless the viewer is staff, matching player visibility
        everywhere else; an anonymous viewer sees only publicly-visible ones.
        """
        relationships = await self.relationships.fetch_all_incoming(
            user2=target_id,
            type=RelationshipType.FRIEND,
        )
        follower_ids = [relationship.user1 for relationship in relationships]
        if not follower_ids:
            return []
        viewer_is_staff = (
            viewer is not None and viewer.priv & Privileges.STAFF.value != 0
        )
        return await self.users.fetch_many(
            ids=follower_ids,
            include_hidden=viewer_is_staff,
        )

    async def fetch_follow_stats(
        self,
        target_id: int,
        *,
        viewer: User | None,
    ) -> FollowStats:
        """Follower/following counts for a player, plus the viewer's relation.

        Counts are over the raw edges (they are a public tally, not a member
        list, so hidden-player filtering does not apply -- a hidden account still
        counts as one follow). ``viewer_*`` reflect the signed-in viewer's own
        edges to the target; they are ``False`` when there is no viewer or the
        viewer is the target (you do not follow yourself), so a self-view renders
        no follow button.
        """
        following_count = await self.relationships.count_outgoing(
            user1=target_id,
            type=RelationshipType.FRIEND,
        )
        followers_count = await self.relationships.count_incoming(
            user2=target_id,
            type=RelationshipType.FRIEND,
        )

        viewer_follows = False
        viewer_followed_by = False
        if viewer is not None and viewer.id != target_id:
            outgoing = await self.relationships.fetch_one(viewer.id, target_id)
            viewer_follows = (
                outgoing is not None and outgoing.type is RelationshipType.FRIEND
            )
            incoming = await self.relationships.fetch_one(target_id, viewer.id)
            viewer_followed_by = (
                incoming is not None and incoming.type is RelationshipType.FRIEND
            )

        return FollowStats(
            following_count=following_count,
            followers_count=followers_count,
            viewer_follows=viewer_follows,
            viewer_followed_by=viewer_followed_by,
        )

    async def add_friend(self, viewer: User, target_id: int) -> AddFriendResult:
        player_id = viewer.id
        if target_id == player_id:
            return AddFriendResult.CANNOT_FRIEND_SELF

        target = await self.users.fetch_one(id=target_id)
        if target is None or not can_view_player(
            viewer=viewer,
            target_id=target.id,
            target_priv=target.priv,
        ):
            # hidden players are reported as missing, not revealed
            return AddFriendResult.TARGET_NOT_FOUND

        existing = await self.relationships.fetch_one(player_id, target_id)
        if existing is not None:
            if existing.type is RelationshipType.FRIEND:
                return AddFriendResult.ALREADY_FRIENDS
            # replace a block with a friendship
            await self.relationships.delete(player_id, target_id)

        await self.relationships.create(
            player_id,
            target_id,
            type=RelationshipType.FRIEND,
        )

        # the game server caches friends in memory for online players
        online_player = self.online_players.get(id=player_id)
        if online_player is not None:
            online_player.friends.add(target_id)

        return AddFriendResult.ADDED

    async def remove_friend(self, player_id: int, target_id: int) -> None:
        existing = await self.relationships.fetch_one(player_id, target_id)
        if existing is None or existing.type is not RelationshipType.FRIEND:
            return

        await self.relationships.delete(player_id, target_id)

        online_player = self.online_players.get(id=player_id)
        if online_player is not None:
            online_player.friends.discard(target_id)
