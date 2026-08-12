"""Guards for the spectator-session history HTTP surface.

Two read paths over the durable ``spectator_sessions`` rows. Unlike the match
surface there is no visibility split: spectating is an inherently public
relationship in osu!, so every session is public data and neither handler takes
an actor. What these tests pin instead is the query shape:

- ``GET /spectator_sessions`` is a browsable, newest-first index; its two
  optional filters (``host_id`` / ``spectator_id``) and the keyset paging args
  are threaded to the service untouched, and every listing surfaces a
  ``next_before_id`` cursor in ``meta`` -- the oldest id on a full page, ``null``
  on a short one so a client stops;
- ``GET /spectator_sessions/{id}`` answers 404 with the v2 error envelope when
  the id is unknown.

The handlers are plain async functions, called directly with a fake service; no
database, no HTTP client. The query parameters default to FastAPI ``Query(...)``
markers, so every one is passed explicitly.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import orjson

from app.api.v2 import spectator_sessions as spectator_sessions_api
from app.repositories.spectator_sessions import SpectatorSession

_FIXED_TIME = datetime(2026, 8, 12, 12, 0, 0)


def _session(
    session_id: int,
    *,
    host_id: int = 3,
    spectator_id: int = 7,
) -> SpectatorSession:
    return SpectatorSession(
        id=session_id,
        host_id=host_id,
        spectator_id=spectator_id,
        started_at=_FIXED_TIME,
        ended_at=None,
    )


class _FakeHistoryService:
    """Records how it was queried and returns canned rows.

    ``session`` may be set to ``None`` to simulate an unknown id, distinct from a
    real row.
    """

    def __init__(self) -> None:
        self.sessions: list[SpectatorSession] = []
        self.session: SpectatorSession | None = None
        self.calls: list[SimpleNamespace] = []

    async def fetch_sessions(
        self,
        *,
        host_id: int | None = None,
        spectator_id: int | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[SpectatorSession]:
        self.calls.append(
            SimpleNamespace(
                method="fetch_sessions",
                host_id=host_id,
                spectator_id=spectator_id,
                before_id=before_id,
                limit=limit,
            ),
        )
        return self.sessions

    async def fetch_session(self, session_id: int) -> SpectatorSession | None:
        self.calls.append(
            SimpleNamespace(method="fetch_session", session_id=session_id),
        )
        return self.session


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


async def _list(
    service: _FakeHistoryService,
    *,
    host_id: int | None = None,
    spectator_id: int | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> Any:
    return await spectator_sessions_api.get_spectator_sessions(
        host_id=host_id,
        spectator_id=spectator_id,
        before_id=before_id,
        limit=limit,
        spectator_history_service=service,  # type: ignore[arg-type]
    )


async def _get(service: _FakeHistoryService, session_id: int) -> Any:
    return await spectator_sessions_api.get_spectator_session(
        session_id,
        spectator_history_service=service,  # type: ignore[arg-type]
    )


# --- listing: filters threaded, keyset cursor ------------------------------


async def test_listing_is_public_and_threads_filters() -> None:
    service = _FakeHistoryService()
    service.sessions = [_session(3), _session(2), _session(1)]

    response = await _list(service, host_id=3, spectator_id=7, before_id=10, limit=50)

    assert response.status_code == 200
    body = _body(response)
    assert [s["id"] for s in body["data"]] == [3, 2, 1]

    # the two optional filters and the paging args are threaded untouched; the
    # handler takes no actor (spectating is public).
    call = service.calls[0]
    assert call.method == "fetch_sessions"
    assert (call.host_id, call.spectator_id) == (3, 7)
    assert (call.before_id, call.limit) == (10, 50)


async def test_listing_defaults_to_the_global_feed() -> None:
    service = _FakeHistoryService()
    service.sessions = []

    await _list(service)

    call = service.calls[0]
    assert (call.host_id, call.spectator_id, call.before_id) == (None, None, None)
    assert call.limit == 50


async def test_listing_short_page_ends_the_cursor() -> None:
    service = _FakeHistoryService()
    service.sessions = [_session(2), _session(1)]  # fewer than the limit

    response = await _list(service, limit=50)

    assert _body(response)["meta"] == {"limit": 50, "next_before_id": None}


async def test_listing_full_page_advances_the_cursor() -> None:
    service = _FakeHistoryService()
    service.sessions = [_session(5), _session(4), _session(3)]  # exactly the limit

    response = await _list(service, limit=3)

    assert _body(response)["meta"] == {"limit": 3, "next_before_id": 3}


async def test_listing_renders_the_session_fields() -> None:
    service = _FakeHistoryService()
    service.sessions = [_session(5, host_id=3, spectator_id=7)]

    response = await _list(service)

    row = _body(response)["data"][0]
    assert row["host_id"] == 3
    assert row["spectator_id"] == 7
    assert row["ended_at"] is None


# --- single session: passthrough id, 404 on unknown ------------------------


async def test_get_session_returns_a_known_session() -> None:
    service = _FakeHistoryService()
    service.session = _session(5)

    response = await _get(service, 5)

    assert response.status_code == 200
    assert _body(response)["data"]["id"] == 5
    assert service.calls[0].session_id == 5


async def test_get_session_unknown_is_404() -> None:
    service = _FakeHistoryService()
    service.session = None

    response = await _get(service, 5)

    assert response.status_code == 404
    assert _body(response) == {
        "status": "error",
        "error": "Spectator session not found.",
    }
