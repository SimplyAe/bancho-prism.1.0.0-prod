"""Guards for the tournament-mappool HTTP surface.

Three read paths over the ``tourney_pools`` / ``tourney_pool_maps`` rows. Pools
carry no privacy flag -- they are public tournament data -- so, unlike the match
surface, there is no visibility gate to pin here. What matters instead is the
missing-vs-empty distinction and the response envelope:

- ``GET /tourney_pools`` lists every pool (unpaged, a small staff set) and
  reports the count in ``meta.total``;
- ``GET /tourney_pools/{id}`` answers 404 for an unknown pool;
- ``GET /tourney_pools/{id}/maps`` answers 404 for an unknown pool but ``200``
  with ``[]`` for a real pool that simply has no picks yet -- an empty pool is
  never conflated with a missing one.

The handlers are plain async functions, called directly with a fake service; no
database, no HTTP client.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import orjson

from app.api.v2 import tourney_pools as tourney_pools_api
from app.repositories.tourney_pool_maps import TourneyPoolMap
from app.repositories.tourney_pools import TourneyPool

_FIXED_TIME = datetime(2024, 1, 1, 12, 0, 0)


def _pool(pool_id: int, *, name: str = "OWC2024", created_by: int = 3) -> TourneyPool:
    return TourneyPool(
        id=pool_id,
        name=name,
        created_at=_FIXED_TIME,
        created_by=created_by,
    )


def _pool_map(
    map_id: int, *, pool_id: int = 1, mods: int = 0, slot: int = 1
) -> TourneyPoolMap:
    return TourneyPoolMap(map_id=map_id, pool_id=pool_id, mods=mods, slot=slot)


class _FakeTourneyPoolsService:
    """Records how it was queried and returns canned rows.

    ``pool`` may be ``None`` (unknown pool) and ``maps`` may be ``None`` (the
    pool itself does not exist) -- distinct from an empty list of picks.
    """

    def __init__(self) -> None:
        self.pools: list[TourneyPool] = []
        self.pool: TourneyPool | None = None
        self.maps: list[TourneyPoolMap] | None = []
        self.calls: list[SimpleNamespace] = []

    async def fetch_tourney_pools(self) -> list[TourneyPool]:
        self.calls.append(SimpleNamespace(method="fetch_tourney_pools"))
        return self.pools

    async def fetch_tourney_pool(self, pool_id: int) -> TourneyPool | None:
        self.calls.append(
            SimpleNamespace(method="fetch_tourney_pool", pool_id=pool_id),
        )
        return self.pool

    async def fetch_pool_maps(self, pool_id: int) -> list[TourneyPoolMap] | None:
        self.calls.append(
            SimpleNamespace(method="fetch_pool_maps", pool_id=pool_id),
        )
        return self.maps


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


async def _list(service: _FakeTourneyPoolsService) -> Any:
    return await tourney_pools_api.get_tourney_pools(
        tourney_pools_service=service,  # type: ignore[arg-type]
    )


async def _get(service: _FakeTourneyPoolsService, pool_id: int) -> Any:
    return await tourney_pools_api.get_tourney_pool(
        pool_id,
        tourney_pools_service=service,  # type: ignore[arg-type]
    )


async def _get_maps(service: _FakeTourneyPoolsService, pool_id: int) -> Any:
    return await tourney_pools_api.get_tourney_pool_maps(
        pool_id,
        tourney_pools_service=service,  # type: ignore[arg-type]
    )


# --- listing: unpaged, count in meta ---------------------------------------


async def test_tourney_pools_listing_returns_every_pool_with_a_count() -> None:
    service = _FakeTourneyPoolsService()
    service.pools = [_pool(1), _pool(2, name="Corsace")]

    response = await _list(service)

    assert response.status_code == 200
    body = _body(response)
    assert [pool["id"] for pool in body["data"]] == [1, 2]
    # unpaged: the count is the length of the returned listing.
    assert body["meta"] == {"total": 2}


async def test_tourney_pools_listing_is_empty_when_there_are_no_pools() -> None:
    service = _FakeTourneyPoolsService()

    response = await _list(service)

    assert response.status_code == 200
    body = _body(response)
    assert body["data"] == []
    assert body["meta"] == {"total": 0}


# --- single pool: 404 on unknown -------------------------------------------


async def test_get_tourney_pool_returns_a_pool() -> None:
    service = _FakeTourneyPoolsService()
    service.pool = _pool(5)

    response = await _get(service, 5)

    assert response.status_code == 200
    data = _body(response)["data"]
    assert data["id"] == 5
    assert data["name"] == "OWC2024"
    assert data["created_by"] == 3

    call = service.calls[0]
    assert (call.method, call.pool_id) == ("fetch_tourney_pool", 5)


async def test_get_tourney_pool_unknown_is_404() -> None:
    service = _FakeTourneyPoolsService()
    service.pool = None

    response = await _get(service, 5)

    assert response.status_code == 404
    assert _body(response) == {"status": "error", "error": "Tourney pool not found."}


# --- pool maps: unknown pool 404, empty pool 200 with [] -------------------


async def test_get_tourney_pool_maps_returns_the_picks() -> None:
    service = _FakeTourneyPoolsService()
    service.maps = [
        _pool_map(101, mods=0, slot=1),
        _pool_map(102, mods=8, slot=2),
    ]

    response = await _get_maps(service, 5)

    assert response.status_code == 200
    body = _body(response)
    assert [(m["map_id"], m["mods"], m["slot"]) for m in body["data"]] == [
        (101, 0, 1),
        (102, 8, 2),
    ]

    call = service.calls[0]
    assert (call.method, call.pool_id) == ("fetch_pool_maps", 5)


async def test_get_tourney_pool_maps_empty_pool_is_ok_not_404() -> None:
    service = _FakeTourneyPoolsService()
    service.maps = []  # a real pool with no picks yet

    response = await _get_maps(service, 5)

    assert response.status_code == 200
    assert _body(response)["data"] == []


async def test_get_tourney_pool_maps_unknown_pool_is_404() -> None:
    service = _FakeTourneyPoolsService()
    service.maps = None  # the pool itself does not exist

    response = await _get_maps(service, 5)

    assert response.status_code == 404
    assert _body(response) == {"status": "error", "error": "Tourney pool not found."}
