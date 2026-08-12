"""Guards for the follow-graph HTTP surface on the players router.

Two public reads over the directional friend (follow) edge, both of which must
gate on target visibility the same way the rest of the player surface does -- a
hidden (restricted or unverified) player is reported missing so its follower
list and counts are not a side channel to its existence:

- ``GET /players/{id}/followers`` lists the players who follow the target
  (visible ones for a normal or anonymous viewer, all for staff);
- ``GET /players/{id}/follow-stats`` returns the public follower/following counts
  plus, for a signed-in viewer, their own relation to the target (follows /
  followed-by / mutuals) so a client renders the button and badge in one call.

The handlers are plain async functions called directly with fake services and
hand-built actors; no database, no HTTP client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import orjson

from app.api.v2 import players as players_api
from app.constants.privileges import Privileges
from app.repositories.users import User
from app.services.relationships import FollowStats

_VISIBLE = Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value
_HIDDEN = Privileges.UNRESTRICTED.value  # unverified


def _user(*, user_id: int, priv: int = _VISIBLE, name: str = "tester") -> User:
    return User(
        id=user_id,
        name=name,
        safe_name=name,
        email=f"{name}@example.com",
        priv=priv,
        country="us",
        silence_end=0,
        donor_end=0,
        creation_time=0,
        latest_activity=0,
        clan_id=0,
        clan_priv=0,
        preferred_mode=0,
        play_style=0,
        custom_badge_name=None,
        custom_badge_icon=None,
        userpage_content=None,
        api_key=None,
    )


class _FakePlayersService:
    def __init__(self, target: User | None) -> None:
        self._target = target

    async def fetch_player(self, player_id: int) -> User | None:
        if self._target is not None and self._target.id == player_id:
            return self._target
        return None


class _FakeRelationshipsService:
    def __init__(
        self,
        *,
        followers: list[User] | None = None,
        stats: FollowStats | None = None,
    ) -> None:
        self._followers = followers or []
        self._stats = stats
        self.calls: list[SimpleNamespace] = []

    async def fetch_followers(
        self,
        target_id: int,
        *,
        viewer: User | None,
    ) -> list[User]:
        self.calls.append(
            SimpleNamespace(method="fetch_followers", target_id=target_id, viewer=viewer),
        )
        return self._followers

    async def fetch_follow_stats(
        self,
        target_id: int,
        *,
        viewer: User | None,
    ) -> FollowStats:
        self.calls.append(
            SimpleNamespace(method="fetch_follow_stats", target_id=target_id, viewer=viewer),
        )
        assert self._stats is not None
        return self._stats


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


# --- followers list --------------------------------------------------------


async def test_followers_of_a_hidden_target_is_a_404() -> None:
    players = _FakePlayersService(_user(user_id=3, priv=_HIDDEN))
    relationships = _FakeRelationshipsService(followers=[_user(user_id=4)])

    response = await players_api.get_player_followers(
        3,
        actor=None,  # anonymous
        players_service=players,  # type: ignore[arg-type]
        relationships_service=relationships,  # type: ignore[arg-type]
    )

    assert response.status_code == 404
    # never reached the follower read, so the hidden player is not revealed.
    assert relationships.calls == []


async def test_followers_of_a_missing_target_is_a_404() -> None:
    players = _FakePlayersService(None)
    relationships = _FakeRelationshipsService()

    response = await players_api.get_player_followers(
        999,
        actor=None,
        players_service=players,  # type: ignore[arg-type]
        relationships_service=relationships,  # type: ignore[arg-type]
    )

    assert response.status_code == 404


async def test_followers_of_a_visible_target_lists_and_threads_the_viewer() -> None:
    players = _FakePlayersService(_user(user_id=3))
    relationships = _FakeRelationshipsService(
        followers=[_user(user_id=4, name="a"), _user(user_id=5, name="b")],
    )
    viewer = _user(user_id=9)

    response = await players_api.get_player_followers(
        3,
        actor=viewer,
        players_service=players,  # type: ignore[arg-type]
        relationships_service=relationships,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    body = _body(response)
    assert [p["id"] for p in body["data"]] == [4, 5]
    assert body["meta"] == {"total": 2}
    # the follower read is scoped to the target, carrying the viewer for
    # visibility filtering.
    call = relationships.calls[0]
    assert call.method == "fetch_followers"
    assert call.target_id == 3
    assert call.viewer is viewer


# --- follow stats ----------------------------------------------------------


async def test_follow_stats_of_a_hidden_target_is_a_404() -> None:
    players = _FakePlayersService(_user(user_id=3, priv=_HIDDEN))
    relationships = _FakeRelationshipsService(
        stats=FollowStats(
            following_count=1,
            followers_count=1,
            viewer_follows=False,
            viewer_followed_by=False,
        ),
    )

    response = await players_api.get_player_follow_stats(
        3,
        actor=None,
        players_service=players,  # type: ignore[arg-type]
        relationships_service=relationships,  # type: ignore[arg-type]
    )

    assert response.status_code == 404
    assert relationships.calls == []


async def test_follow_stats_flattens_the_service_result_including_mutuals() -> None:
    players = _FakePlayersService(_user(user_id=3))
    relationships = _FakeRelationshipsService(
        stats=FollowStats(
            following_count=7,
            followers_count=12,
            viewer_follows=True,
            viewer_followed_by=True,
        ),
    )
    viewer = _user(user_id=4)

    response = await players_api.get_player_follow_stats(
        3,
        actor=viewer,
        players_service=players,  # type: ignore[arg-type]
        relationships_service=relationships,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert _body(response)["data"] == {
        "following": 7,
        "followers": 12,
        "viewer_follows": True,
        "viewer_followed_by": True,
        # both directions set -> the derived mutuals flag is surfaced.
        "viewer_mutuals": True,
    }
