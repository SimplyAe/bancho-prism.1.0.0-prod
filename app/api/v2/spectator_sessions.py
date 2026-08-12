"""bancho.py's v2 apis for durable spectator-session history.

Stock bancho.py forgets every spectate relationship the moment either side
disconnects; the ``spectator_sessions`` table is the fork's durable record of
one, written fire-and-forget off the ``Player`` spectate lifecycle (see
``app/services/spectator/spectator_history.py``). This is the read surface over
those rows -- two query paths, neither of which writes:

- ``GET /spectator_sessions`` -- a browsable, newest-first listing. Two optional
  filters narrow it: ``host_id`` ("who watched this host") and ``spectator_id``
  ("who did this viewer watch"); pass both to pin a single pair, or neither for
  the global feed.
- ``GET /spectator_sessions/{session_id}`` -- one session's record; 404 when the
  id is unknown.

Spectating is an inherently public relationship in osu! -- the host and every
watcher see each other in real time -- so, unlike the match surface, there is no
``//private`` flag and no visibility gate: these rows are public data, like a
profile's activity feed. The listing scrolls backwards by keyset: pass
``before_id`` (the ``id`` of the oldest row you have already seen) for the next
older page; ``meta`` carries ``next_before_id`` -- the cursor for the following
page, or ``null`` at the end.

As elsewhere in v2, responses use the ``{"status": ...}`` envelope rather than
FastAPI's default shape.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from fastapi.param_functions import Query

from app.api import dependencies as api_dependencies
from app.api.v2.common import responses
from app.api.v2.common.responses import Failure
from app.api.v2.common.responses import Success
from app.api.v2.models.spectator_sessions import SpectatorSessionModel
from app.repositories.spectator_sessions import SpectatorSession
from app.services.spectator.spectator_history_reader import SpectatorHistoryService

router = APIRouter()


def _page_meta(sessions: list[SpectatorSession], limit: int) -> dict[str, object]:
    """Keyset cursor for the next page.

    ``next_before_id`` is the id of the oldest session on this page -- pass it
    back as ``before_id`` to continue scrolling. It is ``None`` when the page
    came back short (fewer than ``limit``), which means there is nothing older,
    so a client stops paging without a trailing empty request.
    """
    next_before_id = sessions[-1].id if len(sessions) == limit else None
    return {"limit": limit, "next_before_id": next_before_id}


@router.get("/spectator_sessions")
async def get_spectator_sessions(
    *,
    host_id: int | None = Query(None, ge=1),
    spectator_id: int | None = Query(None, ge=1),
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
    spectator_history_service: Annotated[
        SpectatorHistoryService,
        Depends(api_dependencies.get_spectator_history_service),
    ],
) -> Success[list[SpectatorSessionModel]]:
    # public data (spectating is visible to both sides in real time), so no auth
    # gate; the two filters and keyset paging are all optional.
    sessions = await spectator_history_service.fetch_sessions(
        host_id=host_id,
        spectator_id=spectator_id,
        before_id=before_id,
        limit=limit,
    )

    response = [SpectatorSessionModel.model_validate(session) for session in sessions]
    return responses.success(content=response, meta=_page_meta(sessions, limit))


@router.get("/spectator_sessions/{session_id}")
async def get_spectator_session(
    session_id: int,
    *,
    spectator_history_service: Annotated[
        SpectatorHistoryService,
        Depends(api_dependencies.get_spectator_history_service),
    ],
) -> Success[SpectatorSessionModel] | Failure:
    session = await spectator_history_service.fetch_session(session_id)
    if session is None:
        return responses.failure(
            message="Spectator session not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return responses.success(SpectatorSessionModel.model_validate(session))
