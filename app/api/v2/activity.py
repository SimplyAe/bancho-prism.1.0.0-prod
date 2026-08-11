"""bancho.py's v2 apis for the player activity feed.

Two read paths over the append-only ``activity_events`` log (writes happen in
the hot path, off the back of score submission and stat capture -- never here):

- ``GET /activity/feed`` -- the signed-in user's **friends feed**, the merged
  stream of everyone they have friended. It is personal, so it requires a
  session (401 when anonymous); the viewer's own events are not included (that is
  what the per-user feed below is for).
- ``GET /activity/users/{user_id}`` -- one player's **own public event stream**.
  A profile is public, so this needs no session; it is also how a signed-in user
  reads their own feed (pass their id).

Both scroll backwards by keyset: pass ``before_id`` (the ``id`` of the oldest
event you have already seen) to get the next older page. The response's ``meta``
carries ``next_before_id`` -- the cursor for the following page, or ``null`` at
the end of the feed -- so a client pages without ever computing an offset.

As elsewhere in v2, auth is checked in-handler and failures return the
``{"status": "error", ...}`` envelope rather than FastAPI's default shape.
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
from app.api.v2.models.activity import ActivityEventModel
from app.repositories.activity_events import ActivityEvent
from app.repositories.users import User
from app.services.social.activity_feed import ActivityFeedService

router = APIRouter()


def _page_meta(events: list[ActivityEvent], limit: int) -> dict[str, object]:
    """Keyset cursor for the next page.

    ``next_before_id`` is the id of the oldest event on this page -- pass it back
    as ``before_id`` to continue scrolling. It is ``None`` when the page came
    back short (fewer than ``limit``), which means there is nothing older, so a
    client stops paging without a trailing empty request.
    """
    next_before_id = events[-1].id if len(events) == limit else None
    return {"limit": limit, "next_before_id": next_before_id}


@router.get("/activity/feed")
async def get_activity_feed(
    *,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
    actor: Annotated[
        User | None,
        Depends(actors.get_optional_actor),
    ],
    activity_feed_service: Annotated[
        ActivityFeedService,
        Depends(api_dependencies.get_activity_feed_service),
    ],
) -> Success[list[ActivityEventModel]] | Failure:
    if actor is None:
        return responses.failure(
            message="Authentication required.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    events = await activity_feed_service.fetch_friends_feed(
        actor.id,
        before_id=before_id,
        limit=limit,
    )

    response = [ActivityEventModel.model_validate(event) for event in events]
    return responses.success(content=response, meta=_page_meta(events, limit))


@router.get("/activity/users/{user_id}")
async def get_user_activity(
    user_id: int,
    *,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
    activity_feed_service: Annotated[
        ActivityFeedService,
        Depends(api_dependencies.get_activity_feed_service),
    ],
) -> Success[list[ActivityEventModel]]:
    # a player's own event stream is public, like their profile; no auth gate.
    events = await activity_feed_service.fetch_user_feed(
        user_id,
        before_id=before_id,
        limit=limit,
    )

    response = [ActivityEventModel.model_validate(event) for event in events]
    return responses.success(content=response, meta=_page_meta(events, limit))
