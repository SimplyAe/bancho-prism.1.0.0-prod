"""Guards for the activity-feed HTTP surface.

Two read paths over the append-only event log, with different auth and different
scope -- these tests pin exactly that split, plus the keyset paging cursor:

- ``GET /activity/feed`` is the signed-in user's *friends* feed: anonymous is a
  401 in the v2 ``{"status": "error", ...}`` envelope, and a signed-in caller
  gets *their own* id fanned out to the friends read (never someone else's);
- ``GET /activity/users/{user_id}`` is a *public* per-player stream: it needs no
  session and reads exactly the requested player's feed;
- both surface a ``next_before_id`` cursor in ``meta`` -- the oldest id on a full
  page (scroll here for the next page), and ``null`` on a short page so a client
  stops without a trailing empty request.

The handlers are plain async functions, so they are called directly with a fake
feed service and hand-built actors; no database, no HTTP client. The query
parameters default to FastAPI ``Query(...)`` markers, so every one is passed
explicitly.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import orjson

from app.api.v2 import activity as activity_api
from app.constants.privileges import Privileges
from app.repositories.activity_events import ActivityEvent
from app.repositories.users import User

_FIXED_TIME = datetime(2026, 8, 12, 12, 0, 0)


def _event(event_id: int, *, user_id: int = 7) -> ActivityEvent:
    return ActivityEvent(
        id=event_id,
        user_id=user_id,
        event_type="pp_record",
        mode=0,
        data={"old_pp": 4000, "new_pp": 4200},
        created_at=_FIXED_TIME,
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


_PLAYER = _user(user_id=42, priv=Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value)


class _FakeFeedService:
    """Records how it was queried and returns canned events."""

    def __init__(self) -> None:
        self.friends: list[ActivityEvent] = []
        self.user: list[ActivityEvent] = []
        self.calls: list[SimpleNamespace] = []

    async def fetch_friends_feed(
        self,
        viewer_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        self.calls.append(
            SimpleNamespace(
                method="fetch_friends_feed",
                viewer_id=viewer_id,
                before_id=before_id,
                limit=limit,
            ),
        )
        return self.friends

    async def fetch_user_feed(
        self,
        user_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        self.calls.append(
            SimpleNamespace(
                method="fetch_user_feed",
                user_id=user_id,
                before_id=before_id,
                limit=limit,
            ),
        )
        return self.user


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


async def _feed(
    actor: User | None,
    service: _FakeFeedService,
    *,
    before_id: int | None = None,
    limit: int = 50,
) -> Any:
    return await activity_api.get_activity_feed(
        before_id=before_id,
        limit=limit,
        actor=actor,
        activity_feed_service=service,  # type: ignore[arg-type]
    )


async def _user_feed(
    service: _FakeFeedService,
    user_id: int,
    *,
    before_id: int | None = None,
    limit: int = 50,
) -> Any:
    return await activity_api.get_user_activity(
        user_id,
        before_id=before_id,
        limit=limit,
        activity_feed_service=service,  # type: ignore[arg-type]
    )


# --- friends feed: personal, so session-gated ------------------------------


async def test_friends_feed_requires_a_session() -> None:
    service = _FakeFeedService()

    response = await _feed(None, service)

    assert response.status_code == 401
    assert _body(response) == {"status": "error", "error": "Authentication required."}
    assert service.calls == []  # never reached the service


async def test_friends_feed_fans_out_the_callers_own_id() -> None:
    service = _FakeFeedService()
    service.friends = [_event(3), _event(2), _event(1)]

    response = await _feed(_PLAYER, service, before_id=10, limit=50)

    assert response.status_code == 200
    body = _body(response)
    assert [e["id"] for e in body["data"]] == [3, 2, 1]

    # the friends read is scoped to the *caller's* id -- never a client-supplied
    # one -- with the paging args threaded through.
    call = service.calls[0]
    assert call.method == "fetch_friends_feed"
    assert call.viewer_id == _PLAYER.id
    assert (call.before_id, call.limit) == (10, 50)


async def test_friends_feed_short_page_ends_the_cursor() -> None:
    service = _FakeFeedService()
    service.friends = [_event(2), _event(1)]  # fewer than the limit

    response = await _feed(_PLAYER, service, limit=50)

    # a short page means nothing older -> null cursor, so a client stops.
    assert _body(response)["meta"] == {"limit": 50, "next_before_id": None}


async def test_friends_feed_full_page_advances_the_cursor() -> None:
    service = _FakeFeedService()
    service.friends = [_event(5), _event(4), _event(3)]  # exactly the limit

    response = await _feed(_PLAYER, service, limit=3)

    # a full page -> the oldest id becomes the next before_id.
    assert _body(response)["meta"] == {"limit": 3, "next_before_id": 3}


# --- per-user feed: public, so no session ----------------------------------


async def test_user_feed_is_public_and_reads_that_player() -> None:
    service = _FakeFeedService()
    service.user = [_event(3, user_id=7), _event(1, user_id=7)]

    response = await _user_feed(service, user_id=7, before_id=None, limit=50)

    assert response.status_code == 200
    body = _body(response)
    assert [e["id"] for e in body["data"]] == [3, 1]
    assert all(e["user_id"] == 7 for e in body["data"])

    call = service.calls[0]
    assert call.method == "fetch_user_feed"
    assert call.user_id == 7


async def test_user_feed_full_page_advances_the_cursor() -> None:
    service = _FakeFeedService()
    service.user = [_event(9), _event(8)]

    response = await _user_feed(service, user_id=7, limit=2)

    assert _body(response)["meta"] == {"limit": 2, "next_before_id": 8}
