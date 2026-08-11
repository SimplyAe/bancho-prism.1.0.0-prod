"""bancho.py's v2 apis for the staff anticheat review queue.

This is the HTTP surface over the durable side of **flag, never auto-ban**: it
lets a staff member list what the detector pipeline has flagged, read one flag's
evidence, and record a decision on it. Every endpoint is staff-gated, and the
only mutation -- ``PATCH`` -- records *a human's* verdict (``resolved_by`` is the
acting staff member). Nothing here touches the player: actioning a flag is a
note in the queue, not a restriction.

Authorization is checked in-handler rather than via a raising dependency, so a
denied request returns the same ``{"status": "error", "error": ...}`` body shape
as every other v2 failure (401 with no session, 403 for a signed-in non-staff
user) instead of FastAPI's default error envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from fastapi.param_functions import Query

from app.api import dependencies as api_dependencies
from app.api.v2.common import actors
from app.api.v2.common import responses
from app.api.v2.common.responses import Failure
from app.api.v2.common.responses import Success
from app.api.v2.models.anticheat import AnticheatFlagModel
from app.api.v2.models.anticheat import ResolveFlagRequest
from app.constants.privileges import Privileges
from app.repositories.anticheat_flags import AnticheatFlagStatus
from app.repositories.users import User
from app.services.anticheat.flag_review_queue import AnticheatReviewService

router = APIRouter()


def _staff_gate(actor: User | None) -> Failure | None:
    """The review queue is staff-only; returns a Failure to send, or None.

    Kept out-of-band from FastAPI's dependency system on purpose: returning the
    v2 ``Failure`` envelope (rather than raising) means an unauthorized caller
    gets the same body shape as every other error here.
    """
    if actor is None:
        return responses.failure(
            message="Authentication required.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if actor.priv & Privileges.STAFF.value == 0:
        return responses.failure(
            message="Staff privileges required.",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return None


@router.get("/anticheat/flags")
async def get_anticheat_flags(
    *,
    status_filter: AnticheatFlagStatus | None = Query(None, alias="status"),
    mode: int | None = Query(None, ge=0, le=7),
    user_id: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    actor: Annotated[
        User | None,
        Depends(actors.get_optional_actor),
    ],
    review_service: Annotated[
        AnticheatReviewService,
        Depends(api_dependencies.get_anticheat_review_service),
    ],
) -> Success[list[AnticheatFlagModel]] | Failure:
    denied = _staff_gate(actor)
    if denied is not None:
        return denied

    listing = await review_service.list_flags(
        status=status_filter,
        mode=mode,
        user_id=user_id,
        page=page,
        page_size=page_size,
    )

    response = [AnticheatFlagModel.model_validate(rec) for rec in listing.flags]
    return responses.success(
        content=response,
        meta={
            "total": listing.total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/anticheat/flags/{score_id}")
async def get_anticheat_flag(
    score_id: int,
    *,
    actor: Annotated[
        User | None,
        Depends(actors.get_optional_actor),
    ],
    review_service: Annotated[
        AnticheatReviewService,
        Depends(api_dependencies.get_anticheat_review_service),
    ],
) -> Success[AnticheatFlagModel] | Failure:
    denied = _staff_gate(actor)
    if denied is not None:
        return denied

    flag = await review_service.fetch_flag(score_id)
    if flag is None:
        return responses.failure(
            message="Flag not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return responses.success(AnticheatFlagModel.model_validate(flag))


@router.patch("/anticheat/flags/{score_id}")
async def resolve_anticheat_flag(
    score_id: int,
    args: ResolveFlagRequest,
    *,
    actor: Annotated[
        User | None,
        Depends(actors.get_optional_actor),
    ],
    review_service: Annotated[
        AnticheatReviewService,
        Depends(api_dependencies.get_anticheat_review_service),
    ],
) -> Success[AnticheatFlagModel] | Failure:
    denied = _staff_gate(actor)
    if denied is not None:
        return denied
    # `_staff_gate` returns None only for a signed-in staff user, so the actor is
    # present here; assert it so the resolver id below type-checks.
    assert actor is not None

    flag = await review_service.resolve_flag(
        score_id,
        status=AnticheatFlagStatus(args.status),
        resolved_by=actor.id,
        note=args.note,
    )
    if flag is None:
        return responses.failure(
            message="Flag not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return responses.success(AnticheatFlagModel.model_validate(flag))
