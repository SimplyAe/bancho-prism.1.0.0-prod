"""Guards for the multiplayer match-history HTTP surface.

Three read paths over the durable ``mp_matches`` / ``mp_match_games`` rows, with
the visibility split that is the point of this surface:

- ``GET /matches`` is a *public* index -- no session, and it asks the service
  for public matches only (a private lobby never appears);
- ``GET /matches/{id}`` and ``GET /matches/{id}/games`` honour the ``//private``
  flag: an unknown match and a match the caller may not read both answer 404
  with the same body, so a private lobby's existence is never disclosed;
- every listing surfaces a ``next_before_id`` cursor in ``meta`` -- the oldest id
  on a full page, ``null`` on a short one so a client stops.

The viewer identity the handler derives from the actor (its id, and whether it
is staff) is threaded to the service, so these tests also pin that the handler
passes *the caller's own* identity -- never a client-supplied one.

The handlers are plain async functions, called directly with a fake service and
hand-built actors; no database, no HTTP client. The query parameters default to
FastAPI ``Query(...)`` markers, so every one is passed explicitly.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import orjson

from app.api.v2 import matches as matches_api
from app.constants.privileges import Privileges
from app.repositories.mp_matches import MpMatch
from app.repositories.mp_matches import MpMatchGame
from app.repositories.users import User

_FIXED_TIME = datetime(2026, 8, 12, 12, 0, 0)


def _match(
    match_id: int, *, host_id: int = 3, has_public_history: bool = True
) -> MpMatch:
    return MpMatch(
        id=match_id,
        name=f"lobby {match_id}",
        host_id=host_id,
        has_public_history=has_public_history,
        created_at=_FIXED_TIME,
        disbanded_at=None,
    )


def _game(game_id: int, *, match_id: int = 5) -> MpMatchGame:
    return MpMatchGame(
        id=game_id,
        match_id=match_id,
        map_md5="a" * 32,
        map_id=1,
        map_name="Artist - Title [Insane]",
        mode=0,
        mods=64,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=True,
        participant_count=2,
        participants=[4, 5],
        started_at=_FIXED_TIME,
        ended_at=_FIXED_TIME,
    )


def _user(*, user_id: int, priv: int) -> User:
    return User(
        id=user_id,
        name="tester",
        safe_name="tester",
        email="tester@example.com",
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


_PLAYER = _user(
    user_id=42, priv=Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value
)
_STAFF = _user(
    user_id=7,
    priv=Privileges.UNRESTRICTED.value | Privileges.STAFF.value,
)


class _FakeHistoryService:
    """Records how it was queried and returns canned rows.

    ``matches`` / ``games`` may be set to ``None`` to simulate the service's
    "no match you may read" result, distinct from an empty list.
    """

    def __init__(self) -> None:
        self.recent: list[MpMatch] = []
        self.match: MpMatch | None = None
        self.games: list[MpMatchGame] | None = []
        self.calls: list[SimpleNamespace] = []

    async def fetch_recent_matches(
        self,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[MpMatch]:
        self.calls.append(
            SimpleNamespace(
                method="fetch_recent_matches",
                before_id=before_id,
                limit=limit,
            ),
        )
        return self.recent

    async def fetch_match(
        self,
        match_id: int,
        *,
        viewer_id: int | None = None,
        viewer_is_staff: bool = False,
    ) -> MpMatch | None:
        self.calls.append(
            SimpleNamespace(
                method="fetch_match",
                match_id=match_id,
                viewer_id=viewer_id,
                viewer_is_staff=viewer_is_staff,
            ),
        )
        return self.match

    async def fetch_games(
        self,
        match_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
        viewer_id: int | None = None,
        viewer_is_staff: bool = False,
    ) -> list[MpMatchGame] | None:
        self.calls.append(
            SimpleNamespace(
                method="fetch_games",
                match_id=match_id,
                before_id=before_id,
                limit=limit,
                viewer_id=viewer_id,
                viewer_is_staff=viewer_is_staff,
            ),
        )
        return self.games


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


async def _list(
    service: _FakeHistoryService,
    *,
    before_id: int | None = None,
    limit: int = 50,
) -> Any:
    return await matches_api.get_matches(
        before_id=before_id,
        limit=limit,
        match_history_service=service,  # type: ignore[arg-type]
    )


async def _get(service: _FakeHistoryService, match_id: int, actor: User | None) -> Any:
    return await matches_api.get_match(
        match_id,
        actor=actor,
        match_history_service=service,  # type: ignore[arg-type]
    )


async def _get_games(
    service: _FakeHistoryService,
    match_id: int,
    actor: User | None,
    *,
    before_id: int | None = None,
    limit: int = 50,
) -> Any:
    return await matches_api.get_match_games(
        match_id,
        before_id=before_id,
        limit=limit,
        actor=actor,
        match_history_service=service,  # type: ignore[arg-type]
    )


# --- listing: public, keyset cursor ----------------------------------------


async def test_matches_listing_is_public_and_pages() -> None:
    service = _FakeHistoryService()
    service.recent = [_match(3), _match(2), _match(1)]

    response = await _list(service, before_id=10, limit=50)

    assert response.status_code == 200
    body = _body(response)
    assert [m["id"] for m in body["data"]] == [3, 2, 1]

    call = service.calls[0]
    assert call.method == "fetch_recent_matches"
    assert (call.before_id, call.limit) == (10, 50)


async def test_matches_listing_short_page_ends_the_cursor() -> None:
    service = _FakeHistoryService()
    service.recent = [_match(2), _match(1)]  # fewer than the limit

    response = await _list(service, limit=50)

    assert _body(response)["meta"] == {"limit": 50, "next_before_id": None}


async def test_matches_listing_full_page_advances_the_cursor() -> None:
    service = _FakeHistoryService()
    service.recent = [_match(5), _match(4), _match(3)]  # exactly the limit

    response = await _list(service, limit=3)

    assert _body(response)["meta"] == {"limit": 3, "next_before_id": 3}


# --- single match: visibility identity threaded, 404 on invisible ----------


async def test_get_match_returns_a_visible_match() -> None:
    service = _FakeHistoryService()
    service.match = _match(5)

    response = await _get(service, 5, _PLAYER)

    assert response.status_code == 200
    assert _body(response)["data"]["id"] == 5


async def test_get_match_threads_the_callers_own_identity() -> None:
    service = _FakeHistoryService()
    service.match = _match(5)

    await _get(service, 5, _PLAYER)

    # the viewer identity handed to the service is the caller's own, not a
    # client-supplied one; a non-staff player is not treated as staff.
    call = service.calls[0]
    assert call.viewer_id == _PLAYER.id
    assert call.viewer_is_staff is False


async def test_get_match_marks_staff_callers_as_staff() -> None:
    service = _FakeHistoryService()
    service.match = _match(5, has_public_history=False)

    await _get(service, 5, _STAFF)

    call = service.calls[0]
    assert call.viewer_id == _STAFF.id
    assert call.viewer_is_staff is True


async def test_get_match_anonymous_caller_has_no_viewer_id() -> None:
    service = _FakeHistoryService()
    service.match = _match(5)

    await _get(service, 5, None)

    call = service.calls[0]
    assert call.viewer_id is None
    assert call.viewer_is_staff is False


async def test_get_match_unknown_or_invisible_is_404() -> None:
    service = _FakeHistoryService()
    service.match = None  # the service reports unknown/invisible identically

    response = await _get(service, 5, _PLAYER)

    assert response.status_code == 404
    assert _body(response) == {"status": "error", "error": "Match not found."}


# --- games: visibility gate, empty != missing, cursor ----------------------


async def test_get_match_games_returns_a_page() -> None:
    service = _FakeHistoryService()
    service.games = [_game(2), _game(1)]

    response = await _get_games(service, 5, _PLAYER, before_id=10, limit=50)

    assert response.status_code == 200
    body = _body(response)
    assert [g["id"] for g in body["data"]] == [2, 1]

    call = service.calls[0]
    assert call.method == "fetch_games"
    assert (call.match_id, call.before_id, call.limit) == (5, 10, 50)
    assert call.viewer_id == _PLAYER.id


async def test_get_match_games_full_page_advances_the_cursor() -> None:
    service = _FakeHistoryService()
    service.games = [_game(9), _game(8)]

    response = await _get_games(service, 5, _PLAYER, limit=2)

    assert _body(response)["meta"] == {"limit": 2, "next_before_id": 8}


async def test_get_match_games_empty_history_is_ok_not_404() -> None:
    service = _FakeHistoryService()
    service.games = []  # visible match, no games recorded yet

    response = await _get_games(service, 5, _PLAYER)

    assert response.status_code == 200
    body = _body(response)
    assert body["data"] == []
    assert body["meta"] == {"limit": 50, "next_before_id": None}


async def test_get_match_games_invisible_or_unknown_is_404() -> None:
    service = _FakeHistoryService()
    service.games = None  # "no match you may read"

    response = await _get_games(service, 5, None)

    assert response.status_code == 404
    assert _body(response) == {"status": "error", "error": "Match not found."}
