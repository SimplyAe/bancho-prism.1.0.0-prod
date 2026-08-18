"""Beatmap set rows -- the per-set record behind ``maps``.

``mapsets`` holds one row per beatmap set, carrying the timestamp of the last
osu!api refresh for that set. Historically nothing in the codebase owned this
table: ``app.objects.beatmap`` reached into it with raw SQL, and there was no
repository at all.

Beatmap submission needs to write it (a privately-hosted set must have a row, or
``BeatmapSet._from_bsid_sql`` returns ``None`` and the maps never resolve), so
rather than add more raw SQL this gives the table a home.

``server`` is the load-bearing column: it is what tells the osu!api refresh path
that a set is hosted here and must be left alone. ``last_osuapi_check`` is
meaningless for a private set -- there is no upstream to have checked -- but it is
``not null``, so it is written as the submission time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Integer
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.mysql import Insert as MysqlInsert
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.repositories import Base
from app.repositories.maps import MapServer


class MapsetsTable(Base):
    __tablename__ = "mapsets"

    # (server, id) is the primary key: the same numeric id may in principle exist
    # under either server, and `server` is what the refresh path filters on.
    server = Column(
        Enum(MapServer, name="server"),
        nullable=False,
        server_default=MapServer.OSU.value,
        primary_key=True,
    )
    id = Column(Integer, nullable=False, primary_key=True, autoincrement=False)
    last_osuapi_check = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


READ_PARAMS = (
    MapsetsTable.server,
    MapsetsTable.id,
    MapsetsTable.last_osuapi_check,
)


@dataclass(frozen=True, slots=True)
class Mapset:
    id: int
    server: str
    last_osuapi_check: datetime


class MapsetsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> Mapset:
        return Mapset(
            id=row["id"],
            server=str(row["server"]),
            last_osuapi_check=row["last_osuapi_check"],
        )

    async def upsert(
        self,
        *,
        id: int,
        server: str,
        last_osuapi_check: datetime,
    ) -> Mapset:
        """Create or refresh a set row.

        Keyed on ``(server, id)``, so re-submitting the same set id refreshes the
        row rather than erroring.
        """
        insert_stmt: MysqlInsert = (
            mysql_insert(MapsetsTable)
            .values(
                id=id,
                server=server,
                last_osuapi_check=last_osuapi_check,
            )
            .on_duplicate_key_update(last_osuapi_check=last_osuapi_check)
        )
        await self._database.execute(insert_stmt)

        mapset = await self.fetch_one(id=id, server=server)
        assert mapset is not None  # written immediately above.
        return mapset

    async def fetch_one(self, *, id: int, server: str) -> Mapset | None:
        """A single set row, or None if it does not exist for that server."""
        select_stmt = select(*READ_PARAMS).where(
            MapsetsTable.id == id,
            MapsetsTable.server == server,
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def delete_one(self, *, id: int, server: str) -> None:
        """Remove a set row.

        ``server`` is required rather than optional: this is a destructive call on
        a table that holds both mirrored and privately-hosted sets, and an
        unscoped delete here would take out an osu! set with the same id.
        """
        delete_stmt = delete(MapsetsTable).where(
            MapsetsTable.id == id,
            MapsetsTable.server == server,
        )
        await self._database.execute(delete_stmt)
