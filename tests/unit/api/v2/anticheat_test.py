"""Guards for the staff anticheat review-queue HTTP surface.

The queue's endpoints are the one place a human touches a flag, so the tests
that matter most are the *authorization* ones -- and the guarantee that the
mutation records a decision rather than acting on the player:

- every endpoint is staff-only: anonymous is 401, a signed-in non-staff user is
  403, and both come back in the v2 ``{"status": "error", ...}`` envelope;
- a staff listing carries the page's flags plus the filtered total in ``meta``,
  and passes the query filters straight through to the service;
- reading or resolving a score that was never flagged is a 404, never a
  fabricated row;
- ``PATCH`` maps the request's status string to the repository enum and records
  the *acting staff member* as ``resolved_by`` -- the resolution is always
  attributed to a human.

The handlers are plain async functions, so they are called directly with a fake
review service and hand-built actors; no database, no HTTP client. Because the
query parameters default to FastAPI ``Query(...)`` markers (resolved per-request
in a live server), every one is passed explicitly here.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import orjson

from app.api.v2 import anticheat as anticheat_api
from app.api.v2.models.anticheat import ResolveFlagRequest
from app.constants.privileges import Privileges
from app.repositories.anticheat_flags import AnticheatFlag
from app.repositories.anticheat_flags import AnticheatFlagStatus
from app.repositories.users import User
from app.services.anticheat.flag_review_queue import FlagListing

_FIXED_TIME = datetime(2026, 8, 12, 12, 0, 0)


def _flag(score_id: int = 5) -> AnticheatFlag:
    return AnticheatFlag(
        score_id=score_id,
        user_id=7,
        mode=0,
        status=AnticheatFlagStatus.OPEN,
        severity="high",
        top_signal_code="B3_AIM_CONTROLLER",
        top_signal_title="Aim-controller cleanliness",
        confidence=0.9,
        triggered_count=1,
        detail="jump aim is too clean for a human",
        evidence={"aim_machineness": 0.95},
        first_flagged_at=_FIXED_TIME,
        last_flagged_at=_FIXED_TIME,
        resolved_by=None,
        resolved_at=None,
        resolution_note=None,
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


# a signed-in player with no staff bit; a staff member (moderator suffices).
_PLAYER = _user(user_id=42, priv=Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value)
_STAFF = _user(
    user_id=99,
    priv=Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value | Privileges.MODERATOR.value,
)


class _FakeReviewService:
    def __init__(self) -> None:
        self.listing = FlagListing(flags=[], total=0)
        self.one: AnticheatFlag | None = None
        self.resolved: AnticheatFlag | None = None
        self.calls: list[SimpleNamespace] = []

    async def list_flags(
        self,
        *,
        status: AnticheatFlagStatus | None,
        mode: int | None,
        user_id: int | None,
        page: int,
        page_size: int,
    ) -> FlagListing:
        self.calls.append(
            SimpleNamespace(
                method="list_flags",
                status=status,
                mode=mode,
                user_id=user_id,
                page=page,
                page_size=page_size,
            ),
        )
        return self.listing

    async def fetch_flag(self, score_id: int) -> AnticheatFlag | None:
        self.calls.append(SimpleNamespace(method="fetch_flag", score_id=score_id))
        return self.one

    async def resolve_flag(
        self,
        score_id: int,
        *,
        status: AnticheatFlagStatus,
        resolved_by: int,
        note: str | None = None,
    ) -> AnticheatFlag | None:
        self.calls.append(
            SimpleNamespace(
                method="resolve_flag",
                score_id=score_id,
                status=status,
                resolved_by=resolved_by,
                note=note,
            ),
        )
        return self.resolved


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


async def _list(actor: User | None, service: _FakeReviewService) -> Any:
    return await anticheat_api.get_anticheat_flags(
        status_filter=None,
        mode=None,
        user_id=None,
        page=1,
        page_size=50,
        actor=actor,
        review_service=service,  # type: ignore[arg-type]
    )


async def _get_one(actor: User | None, service: _FakeReviewService, score_id: int = 5) -> Any:
    return await anticheat_api.get_anticheat_flag(
        score_id,
        actor=actor,
        review_service=service,  # type: ignore[arg-type]
    )


async def _resolve(
    actor: User | None,
    service: _FakeReviewService,
    args: ResolveFlagRequest,
    score_id: int = 5,
) -> Any:
    return await anticheat_api.resolve_anticheat_flag(
        score_id,
        args,
        actor=actor,
        review_service=service,  # type: ignore[arg-type]
    )


# --- authorization: every endpoint is staff-only ---------------------------


async def test_every_endpoint_rejects_anonymous_with_401() -> None:
    service = _FakeReviewService()
    args = ResolveFlagRequest(status="dismissed")

    for response in (
        await _list(None, service),
        await _get_one(None, service),
        await _resolve(None, service, args),
    ):
        assert response.status_code == 401
        assert _body(response) == {
            "status": "error",
            "error": "Authentication required.",
        }
    assert service.calls == []  # never reached the service


async def test_every_endpoint_forbids_a_signed_in_non_staff_user_with_403() -> None:
    service = _FakeReviewService()
    args = ResolveFlagRequest(status="dismissed")

    for response in (
        await _list(_PLAYER, service),
        await _get_one(_PLAYER, service),
        await _resolve(_PLAYER, service, args),
    ):
        assert response.status_code == 403
        assert _body(response) == {
            "status": "error",
            "error": "Staff privileges required.",
        }
    assert service.calls == []


# --- listing ----------------------------------------------------------------


async def test_list_returns_the_page_and_filtered_total_for_staff() -> None:
    service = _FakeReviewService()
    service.listing = FlagListing(flags=[_flag(1), _flag(2)], total=3)

    response = await anticheat_api.get_anticheat_flags(
        status_filter=AnticheatFlagStatus.OPEN,
        mode=0,
        user_id=7,
        page=2,
        page_size=10,
        actor=_STAFF,
        review_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    body = _body(response)
    assert [f["score_id"] for f in body["data"]] == [1, 2]
    assert body["meta"] == {"total": 3, "page": 2, "page_size": 10}

    call = service.calls[0]
    assert call.method == "list_flags"
    assert call.status is AnticheatFlagStatus.OPEN
    assert (call.mode, call.user_id, call.page, call.page_size) == (0, 7, 2, 10)


# --- single fetch -----------------------------------------------------------


async def test_get_one_returns_the_flag_for_staff() -> None:
    service = _FakeReviewService()
    service.one = _flag(5)

    response = await _get_one(_STAFF, service, score_id=5)

    assert response.status_code == 200
    assert _body(response)["data"]["score_id"] == 5


async def test_get_one_is_404_when_the_score_was_never_flagged() -> None:
    service = _FakeReviewService()
    service.one = None

    response = await _get_one(_STAFF, service, score_id=123)

    assert response.status_code == 404
    assert _body(response) == {"status": "error", "error": "Flag not found."}


# --- resolve ----------------------------------------------------------------


async def test_resolve_records_the_acting_staff_decision() -> None:
    service = _FakeReviewService()
    service.resolved = _flag(5)
    args = ResolveFlagRequest(status="actioned", note="cheating confirmed")

    response = await _resolve(_STAFF, service, args, score_id=5)

    assert response.status_code == 200
    call = service.calls[0]
    assert call.method == "resolve_flag"
    assert call.score_id == 5
    # the request's status string is mapped to the repository enum...
    assert call.status is AnticheatFlagStatus.ACTIONED
    # ...and the resolution is attributed to the acting staff member.
    assert call.resolved_by == _STAFF.id
    assert call.note == "cheating confirmed"


async def test_resolve_is_404_when_there_is_no_such_flag() -> None:
    service = _FakeReviewService()
    service.resolved = None
    args = ResolveFlagRequest(status="dismissed")

    response = await _resolve(_STAFF, service, args, score_id=404)

    assert response.status_code == 404
    assert _body(response) == {"status": "error", "error": "Flag not found."}
