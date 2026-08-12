"""bancho.py's v2 apis for reading tournament mappools.

A *pool* is a named, staff-assembled collection of map picks for a tournament
(built in-game with ``!pool`` / ``!pool add``); stock bancho.py only ever reads
these back through the legacy ``/v1/get_pool`` path. This is the v2 read surface
over the ``tourney_pools`` / ``tourney_pool_maps`` rows -- three query paths,
none of which writes (pool authoring stays in-game, where the staff privilege
check already lives):

- ``GET /tourney_pools`` -- every pool, for a browsable index. Pools are a small
  staff-curated set, so the listing is unpaged; ``meta.total`` carries the count.
- ``GET /tourney_pools/{pool_id}`` -- one pool's metadata (name, author, when).
- ``GET /tourney_pools/{pool_id}/maps`` -- the map picks in one pool, each keyed
  by its (mods, slot) pick.

Pools carry no privacy flag -- they are public tournament data, already served
openly by v1 -- so there is no visibility gate here. An unknown pool is a 404;
the maps path distinguishes an unknown pool (404) from a real pool with no picks
yet (``200`` with ``[]``), so an empty pool is never mistaken for a missing one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status

from app.api import dependencies as api_dependencies
from app.api.v2.common import responses
from app.api.v2.common.responses import Failure
from app.api.v2.common.responses import Success
from app.api.v2.models.tourney_pools import TourneyPoolMapModel
from app.api.v2.models.tourney_pools import TourneyPoolModel
from app.services.tourney_pools import TourneyPoolsService

router = APIRouter()


@router.get("/tourney_pools")
async def get_tourney_pools(
    *,
    tourney_pools_service: Annotated[
        TourneyPoolsService,
        Depends(api_dependencies.get_tourney_pools_service),
    ],
) -> Success[list[TourneyPoolModel]]:
    # public tournament data and a small staff-curated set, so no auth gate and
    # no paging; the full listing is returned with its count in meta.
    pools = await tourney_pools_service.fetch_tourney_pools()

    response = [TourneyPoolModel.model_validate(pool) for pool in pools]
    return responses.success(content=response, meta={"total": len(pools)})


@router.get("/tourney_pools/{pool_id}")
async def get_tourney_pool(
    pool_id: int,
    *,
    tourney_pools_service: Annotated[
        TourneyPoolsService,
        Depends(api_dependencies.get_tourney_pools_service),
    ],
) -> Success[TourneyPoolModel] | Failure:
    pool = await tourney_pools_service.fetch_tourney_pool(pool_id)
    if pool is None:
        return responses.failure(
            message="Tourney pool not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return responses.success(TourneyPoolModel.model_validate(pool))


@router.get("/tourney_pools/{pool_id}/maps")
async def get_tourney_pool_maps(
    pool_id: int,
    *,
    tourney_pools_service: Annotated[
        TourneyPoolsService,
        Depends(api_dependencies.get_tourney_pools_service),
    ],
) -> Success[list[TourneyPoolMapModel]] | Failure:
    maps = await tourney_pools_service.fetch_pool_maps(pool_id)
    if maps is None:
        # None means the pool itself does not exist -> 404. An empty list (a real
        # pool with no picks yet) falls through to a 200 below, so an empty pool
        # is never conflated with a missing one.
        return responses.failure(
            message="Tourney pool not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    response = [TourneyPoolMapModel.model_validate(pool_map) for pool_map in maps]
    return responses.success(content=response)
