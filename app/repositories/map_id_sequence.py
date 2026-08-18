"""Id allocation for privately-hosted beatmaps and beatmap sets.

Maps uploaded to this server need beatmap ids and set ids, and those ids share a
namespace with every real osu! beatmap: `maps.id` and `maps.md5` each carry their
own unique index, and `BeatmapSet._save_to_sql` writes with `REPLACE INTO`. A
`REPLACE` whose id matches an existing row does not raise -- it **deletes** that
row and inserts in its place. So an id collision here is not a constraint error a
caller can retry, it is silent destruction of a mirrored beatmap.

The ranges are therefore kept disjoint by construction: allocation starts at
:data:`PRIVATE_BEATMAP_ID_FLOOR` (2e9), roughly 400x above osu!'s current id
space and still inside signed int32, leaving ~147M ids.

The counter is its own table rather than an `auto_increment` on `maps`, for two
reasons. Neither `maps.id` nor `mapsets.id` is `auto_increment` in the first place
(maps' primary key is the composite `(server, id)`); and a counter attached to
`maps` would track the millions of *mirrored* rows, so it would sit at ~5M and
hand out ids that future real beatmaps will also use.

Allocation is a plain insert, which is what makes it race-free: InnoDB never
hands the same `auto_increment` value to two concurrent transactions, and never
rolls one back. A failed upload therefore burns an id. Gaps are expected and
harmless; reuse would not be.

The floor is asserted on every allocation rather than trusted, because the seed
lives only in the schema: a database restored from a data-only dump comes back
with the counter at 1, and the very first allocation would then target a real
beatmap id. Failing loudly there is the difference between a rejected upload and
an unnoticed hole in the beatmap table.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Integer
from sqlalchemy import func
from sqlalchemy import insert

from app.adapters.database import Database
from app.repositories import Base

# the first id handed out for privately-hosted content. Real osu! beatmap ids are
# ~5M as of 2026; signed int32 (the `maps.id` column type) tops out at
# 2,147,483,647, so this leaves ~147M ids of headroom. Kept in sync with the
# `auto_increment` seed in migrations/base.sql (pinned by a test).
PRIVATE_BEATMAP_ID_FLOOR = 2_000_000_000


class BeatmapIdAllocationError(RuntimeError):
    """An allocated id fell outside the range reserved for private content.

    Raised rather than returned: there is no safe way to continue, since using
    the id would let a later `REPLACE INTO maps` overwrite a mirrored beatmap.
    """


class MapIdKind(StrEnum):
    """What an allocated id identifies. Recorded for auditing only."""

    MAP = "map"
    SET = "set"


class MapIdSequenceTable(Base):
    __tablename__ = "map_id_sequence"

    id = Column(Integer, nullable=False, primary_key=True, autoincrement=True)
    kind = Column(Enum(MapIdKind, name="kind"), nullable=False)
    allocated_by = Column(Integer, nullable=False)
    allocated_at = Column(DateTime, nullable=False, server_default=func.now())


class MapIdSequenceRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def allocate(self, *, kind: MapIdKind, allocated_by: int) -> int:
        """Reserve a single id for privately-hosted content.

        Raises :class:`BeatmapIdAllocationError` if the id is below the private
        floor, which means the table's `auto_increment` seed is missing.
        """
        insert_stmt = insert(MapIdSequenceTable).values(
            kind=kind,
            allocated_by=allocated_by,
        )
        allocated_id = await self._database.execute(insert_stmt)

        if allocated_id < PRIVATE_BEATMAP_ID_FLOOR:
            raise BeatmapIdAllocationError(
                f"allocated id {allocated_id} is below the private floor "
                f"{PRIVATE_BEATMAP_ID_FLOOR}: the map_id_sequence auto_increment "
                "seed is missing (was the database restored from a data-only "
                "dump?). Refusing to allocate, as this id may belong to a real "
                "osu! beatmap.",
            )

        return allocated_id

    async def allocate_many(
        self,
        *,
        kind: MapIdKind,
        count: int,
        allocated_by: int,
    ) -> list[int]:
        """Reserve ``count`` ids, oldest first.

        Deliberately one insert per id rather than a single multi-row insert:
        whether a bulk insert reserves a *contiguous* block depends on
        `innodb_autoinc_lock_mode`, and nothing here needs contiguity. Uploads
        are not a hot path -- a 10-difficulty set costs 11 tiny inserts.
        """
        return [
            await self.allocate(kind=kind, allocated_by=allocated_by)
            for _ in range(count)
        ]
