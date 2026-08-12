"""bancho.py's v2 apis for durable multiplayer match history.

Stock bancho.py forgets every multiplayer lobby the moment its last player
leaves; the ``mp_matches`` / ``mp_match_games`` tables are the fork's durable
record of one, written fire-and-forget off the packet handlers (see
``app/services/multiplayer/match_history.py``). This is the read surface over
those rows -- three query paths, none of which writes:

- ``GET /matches`` -- a browsable index of recent **public** lobbies, newest
  first. Public, needs no session; a private lobby never appears here.
- ``GET /matches/{match_id}`` -- one lobby's metadata (name, host, timestamps).
- ``GET /matches/{match_id}/games`` -- the games played in one lobby, newest
  first.
- ``GET /matches/{match_id}/games/{game_id}/scores`` -- one game's per-player
  scoreboard, ordered by finishing placement. The game is confirmed to belong
  to the match it is requested under, so a private game's scores cannot be read
  by pairing its id with a public match.

The two per-match paths honour the ``//private`` flag: a public lobby is
readable by anyone, a private one only by its host or by staff. When a match is
unknown *or* not visible to the caller, both return 404 with the same body -- a
private lobby's existence is never disclosed to someone who may not read it.

Both listing paths scroll backwards by keyset: pass ``before_id`` (the ``id`` of
the oldest row you have already seen) for the next older page; ``meta`` carries
``next_before_id`` -- the cursor for the following page, or ``null`` at the end.

As elsewhere in v2, auth is resolved in-handler and failures return the
``{"status": "error", ...}`` envelope rather than FastAPI's default shape.
"""

from __future__ import annotations

from typing import Annotated
from typing import Protocol
from typing import Sequence

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from fastapi.param_functions import Query

from app.api import dependencies as api_dependencies
from app.api.v2.common import actors
from app.api.v2.common import responses
from app.api.v2.common.responses import Failure
from app.api.v2.common.responses import Success
from app.api.v2.models.matches import MatchGameModel
from app.api.v2.models.matches import MatchGameScoreModel
from app.api.v2.models.matches import MatchModel
from app.constants.privileges import Privileges
from app.repositories.users import User
from app.services.multiplayer.match_history_reader import MatchHistoryService

router = APIRouter()


class _HasId(Protocol):
    @property
    def id(self) -> int: ...


def _page_meta(rows: Sequence[_HasId], limit: int) -> dict[str, object]:
    """Keyset cursor for the next page.

    ``next_before_id`` is the id of the oldest row on this page -- pass it back
    as ``before_id`` to continue scrolling. It is ``None`` when the page came
    back short (fewer than ``limit``), which means there is nothing older, so a
    client stops paging without a trailing empty request.
    """
    next_before_id = rows[-1].id if len(rows) == limit else None
    return {"limit": limit, "next_before_id": next_before_id}


def _is_staff(actor: User | None) -> bool:
    return actor is not None and actor.priv & Privileges.STAFF.value != 0


@router.get("/matches")
async def get_matches(
    *,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
    match_history_service: Annotated[
        MatchHistoryService,
        Depends(api_dependencies.get_match_history_service),
    ],
) -> Success[list[MatchModel]]:
    # a browsable index of public lobbies; private lobbies never appear, so no
    # auth gate -- exactly like a public profile.
    matches = await match_history_service.fetch_recent_matches(
        before_id=before_id,
        limit=limit,
    )

    response = [MatchModel.model_validate(match) for match in matches]
    return responses.success(content=response, meta=_page_meta(matches, limit))


@router.get("/matches/{match_id}")
async def get_match(
    match_id: int,
    *,
    actor: Annotated[
        User | None,
        Depends(actors.get_optional_actor),
    ],
    match_history_service: Annotated[
        MatchHistoryService,
        Depends(api_dependencies.get_match_history_service),
    ],
) -> Success[MatchModel] | Failure:
    match = await match_history_service.fetch_match(
        match_id,
        viewer_id=actor.id if actor is not None else None,
        viewer_is_staff=_is_staff(actor),
    )
    if match is None:
        # unknown or not visible -- reported identically so a private lobby's
        # existence is not disclosed to a caller who may not read it.
        return responses.failure(
            message="Match not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return responses.success(MatchModel.model_validate(match))


@router.get("/matches/{match_id}/games")
async def get_match_games(
    match_id: int,
    *,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
    actor: Annotated[
        User | None,
        Depends(actors.get_optional_actor),
    ],
    match_history_service: Annotated[
        MatchHistoryService,
        Depends(api_dependencies.get_match_history_service),
    ],
) -> Success[list[MatchGameModel]] | Failure:
    games = await match_history_service.fetch_games(
        match_id,
        before_id=before_id,
        limit=limit,
        viewer_id=actor.id if actor is not None else None,
        viewer_is_staff=_is_staff(actor),
    )
    if games is None:
        # None means "no match you may read" (unknown or private-and-not-yours);
        # an empty list means a visible match with no games yet -- distinct, so a
        # 404 here is only ever the former.
        return responses.failure(
            message="Match not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    response = [MatchGameModel.model_validate(game) for game in games]
    return responses.success(content=response, meta=_page_meta(games, limit))


@router.get("/matches/{match_id}/games/{game_id}/scores")
async def get_match_game_scores(
    match_id: int,
    game_id: int,
    *,
    actor: Annotated[
        User | None,
        Depends(actors.get_optional_actor),
    ],
    match_history_service: Annotated[
        MatchHistoryService,
        Depends(api_dependencies.get_match_history_service),
    ],
) -> Success[list[MatchGameScoreModel]] | Failure:
    scores = await match_history_service.fetch_game_scores(
        match_id,
        game_id,
        viewer_id=actor.id if actor is not None else None,
        viewer_is_staff=_is_staff(actor),
    )
    if scores is None:
        # None means the game is not readable here -- an unknown match/game, a
        # private match the caller may not see, or a game that belongs to some
        # other match. All 404 identically, so pairing a game id with the wrong
        # match discloses nothing. An empty scoreboard, by contrast, is a 200
        # with [] -- a visible game that simply has no recorded scores.
        return responses.failure(
            message="Match game not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    response = [MatchGameScoreModel.model_validate(score) for score in scores]
    return responses.success(content=response)
