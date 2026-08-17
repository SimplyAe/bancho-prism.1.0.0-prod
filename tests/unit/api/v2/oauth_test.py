"""Guards for the Discord OAuth HTTP surface.

Four endpoints over ``DiscordLinkingService``; the handlers are plain async
functions called directly with fakes, no ASGI, no HTTP client. What they own (and
what these tests pin) is the mapping between the service's outcomes and the wire:

- authorize / link / delete are **session-gated in-handler** -- a missing cookie
  or an unrecognised session is a 401 in the shared envelope, never a bypass;
- the callback is deliberately **not** session-gated (the ``state`` is the CSRF
  proof), but a missing ``code`` or ``state`` is answered 400 in-envelope rather
  than left to FastAPI's 422 on a required query param;
- each :class:`LinkOutcome` earns a distinct status: LINKED -> 200 with the link,
  INVALID_STATE -> 400, EXCHANGE/IDENTITY -> 502, ALREADY_LINKED -> 409,
  DISABLED -> 503;
- ``begin_link`` returning ``None`` (integration off) is a 503, and a read with
  no link is a 404.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import orjson

from app.api.v2 import oauth as oauth_api
from app.repositories.user_discord_links import DiscordLink
from app.services.social.discord_linking import LinkOutcome
from app.services.social.discord_linking import LinkResult

_LINK = DiscordLink(
    user_id=6,
    discord_id="42",
    discord_username="coolguy",
    linked_at=datetime(2026, 8, 17, 12, 0, 0),
)


class _FakeLinkingService:
    def __init__(
        self,
        *,
        authorize_url: str | None = "https://discord.com/authorize?state=s",
        complete_result: LinkResult | None = None,
        link: DiscordLink | None = None,
    ) -> None:
        self._authorize_url = authorize_url
        self._complete_result = complete_result or LinkResult(
            LinkOutcome.LINKED,
            link=_LINK,
        )
        self._link = link
        self.begin_calls: list[int] = []
        self.complete_calls: list[dict[str, str]] = []
        self.unlink_calls: list[int] = []

    async def begin_link(self, user_id: int) -> str | None:
        self.begin_calls.append(user_id)
        return self._authorize_url

    async def complete_link(self, *, state: str, code: str) -> LinkResult:
        self.complete_calls.append({"state": state, "code": code})
        return self._complete_result

    async def fetch_link(self, user_id: int) -> DiscordLink | None:
        return self._link

    async def unlink(self, user_id: int) -> None:
        self.unlink_calls.append(user_id)


class _FakeWebSessionsService:
    """Resolves a session cookie to a user, or None for an unknown token."""

    def __init__(self, *, user: Any = None) -> None:
        self._user = user

    async def fetch_session_user(self, token: str) -> Any:
        return self._user


def _actor(user_id: int = 6) -> SimpleNamespace:
    # the handlers only read `.id` off the session user.
    return SimpleNamespace(id=user_id)


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


# --- authorize -------------------------------------------------------------


async def test_authorize_without_a_cookie_is_401() -> None:
    linking = _FakeLinkingService()
    sessions = _FakeWebSessionsService(user=None)

    response = await oauth_api.begin_discord_link(
        session_token=None,
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 401
    assert linking.begin_calls == []


async def test_authorize_with_an_unknown_session_is_401() -> None:
    linking = _FakeLinkingService()
    sessions = _FakeWebSessionsService(user=None)

    response = await oauth_api.begin_discord_link(
        session_token="stale",
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 401
    assert linking.begin_calls == []


async def test_authorize_when_disabled_is_503() -> None:
    linking = _FakeLinkingService(authorize_url=None)
    sessions = _FakeWebSessionsService(user=_actor(6))

    response = await oauth_api.begin_discord_link(
        session_token="ok",
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 503


async def test_authorize_returns_the_url_for_the_session_user() -> None:
    linking = _FakeLinkingService(authorize_url="https://discord.com/authorize?x=1")
    sessions = _FakeWebSessionsService(user=_actor(6))

    response = await oauth_api.begin_discord_link(
        session_token="ok",
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert _body(response)["data"] == {
        "authorize_url": "https://discord.com/authorize?x=1",
    }
    # the flow was begun for the account behind the session, not a param.
    assert linking.begin_calls == [6]


# --- callback --------------------------------------------------------------


async def test_callback_without_code_or_state_is_400() -> None:
    linking = _FakeLinkingService()

    missing_code = await oauth_api.complete_discord_link(
        code=None,
        state="s",
        discord_linking_service=linking,  # type: ignore[arg-type]
    )
    missing_state = await oauth_api.complete_discord_link(
        code="c",
        state=None,
        discord_linking_service=linking,  # type: ignore[arg-type]
    )

    assert missing_code.status_code == 400
    assert missing_state.status_code == 400
    # a malformed callback never reaches the service.
    assert linking.complete_calls == []


async def test_callback_links_and_returns_the_link() -> None:
    linking = _FakeLinkingService(
        complete_result=LinkResult(LinkOutcome.LINKED, link=_LINK),
    )

    response = await oauth_api.complete_discord_link(
        code="the-code",
        state="the-state",
        discord_linking_service=linking,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    data = _body(response)["data"]
    assert data["user_id"] == 6
    assert data["discord_id"] == "42"
    assert data["discord_username"] == "coolguy"
    # the state and code from the query were passed straight through.
    assert linking.complete_calls == [{"state": "the-state", "code": "the-code"}]


async def test_callback_maps_each_failure_outcome_to_its_status() -> None:
    cases = {
        LinkOutcome.DISABLED: 503,
        LinkOutcome.INVALID_STATE: 400,
        LinkOutcome.EXCHANGE_FAILED: 502,
        LinkOutcome.IDENTITY_FAILED: 502,
        LinkOutcome.ALREADY_LINKED_ELSEWHERE: 409,
    }
    for outcome, expected_status in cases.items():
        linking = _FakeLinkingService(complete_result=LinkResult(outcome))

        response = await oauth_api.complete_discord_link(
            code="c",
            state="s",
            discord_linking_service=linking,  # type: ignore[arg-type]
        )

        assert response.status_code == expected_status, outcome
        assert _body(response)["status"] == "error"


# --- get link --------------------------------------------------------------


async def test_get_link_without_a_cookie_is_401() -> None:
    linking = _FakeLinkingService(link=_LINK)
    sessions = _FakeWebSessionsService(user=None)

    response = await oauth_api.get_discord_link(
        session_token=None,
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 401


async def test_get_link_when_none_exists_is_404() -> None:
    linking = _FakeLinkingService(link=None)
    sessions = _FakeWebSessionsService(user=_actor(6))

    response = await oauth_api.get_discord_link(
        session_token="ok",
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 404


async def test_get_link_returns_the_current_link() -> None:
    linking = _FakeLinkingService(link=_LINK)
    sessions = _FakeWebSessionsService(user=_actor(6))

    response = await oauth_api.get_discord_link(
        session_token="ok",
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert _body(response)["data"]["discord_id"] == "42"


# --- delete link -----------------------------------------------------------


async def test_delete_link_without_a_cookie_is_401() -> None:
    linking = _FakeLinkingService()
    sessions = _FakeWebSessionsService(user=None)

    response = await oauth_api.delete_discord_link(
        session_token=None,
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 401
    assert linking.unlink_calls == []


async def test_delete_link_unlinks_the_session_user() -> None:
    linking = _FakeLinkingService()
    sessions = _FakeWebSessionsService(user=_actor(6))

    response = await oauth_api.delete_discord_link(
        session_token="ok",
        discord_linking_service=linking,  # type: ignore[arg-type]
        web_sessions_service=sessions,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    # idempotent unlink is delegated for the account behind the session.
    assert linking.unlink_calls == [6]
