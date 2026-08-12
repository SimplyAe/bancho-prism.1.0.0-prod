from __future__ import annotations

from datetime import datetime

from . import BaseModel

# output models


class TourneyPoolModel(BaseModel):
    """One tournament mappool, mirroring the repository's
    :class:`app.repositories.tourney_pools.TourneyPool`.

    A pool is a named, ordered collection of map picks a staff member assembled
    for a tournament (``!pool`` in-game). ``created_by`` is the user id of the
    staff member who made it; ``name`` is unique across pools.
    """

    id: int
    name: str
    created_at: datetime
    created_by: int


class TourneyPoolMapModel(BaseModel):
    """One map pick within a pool, mirroring the repository's
    :class:`app.repositories.tourney_pool_maps.TourneyPoolMap`.

    A pick is identified within its pool by (``mods``, ``slot``) -- e.g. the
    second HD pick. ``mods`` is the required mod bitfield for the pick and
    ``slot`` its 1-based position within that mod bracket; ``map_id`` is the
    beatmap that occupies it.
    """

    map_id: int
    pool_id: int
    mods: int
    slot: int
