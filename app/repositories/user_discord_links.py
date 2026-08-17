"""Durable osu! <-> Discord account links.

Stock bancho.py has no concept of a linked Discord account: there is nowhere to
record that the person playing as osu! user 6 is the same person as Discord user
``1234...``. This table is that record, written once a player completes Discord's
OAuth2 flow and proves they control the Discord account.

One row per link, keyed on ``user_id`` (the osu! account, primary key), so a
player has at most one Discord account linked. ``discord_id`` carries a unique
index, so a single Discord account backs at most one osu! account: the service
refuses a second osu! account trying to claim an already-linked Discord rather
than silently stealing it. Re-linking the *same* osu! account to a new Discord
account is a clean replace, which is why ``upsert_link`` refreshes the columns on
a duplicate ``user_id`` instead of erroring.

As with the rest of the schema the foreign key (``user_id`` -> users) is enforced
in application logic, not the DB, so a purged player orphans its link rather than
cascading. ``discord_id`` / ``discord_username`` are stored as strings because a
Discord snowflake is a 64-bit id conventionally handled as a string (it can lose
precision as a JS number, and we never do arithmetic on it), and the username is
Discord's, not ours, so we cap it defensively at the documented length.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.mysql import Insert as MysqlInsert
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.adapters.database import Database
from app.adapters.database import MySQLRow
from app.repositories import Base

# Discord usernames are documented at 2..32 characters; the id is a snowflake
# that renders as up to 20 decimal digits. Both are Discord's values, so the caps
# are defensive: a write must never fail because Discord changed a length on us.
_DISCORD_ID_MAX_LEN = 20
_DISCORD_USERNAME_MAX_LEN = 32


class UserDiscordLinksTable(Base):
    __tablename__ = "user_discord_links"

    # users.id is Integer elsewhere; mirror it. autoincrement is disabled: the
    # value is always the supplied osu! account id, never generated.
    user_id = Column(
        "user_id",
        Integer,
        nullable=False,
        primary_key=True,
        autoincrement=False,
    )
    discord_id = Column("discord_id", String(_DISCORD_ID_MAX_LEN), nullable=False)
    discord_username = Column(
        "discord_username",
        String(_DISCORD_USERNAME_MAX_LEN),
        nullable=False,
    )
    linked_at = Column(
        "linked_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


READ_PARAMS = (
    UserDiscordLinksTable.user_id,
    UserDiscordLinksTable.discord_id,
    UserDiscordLinksTable.discord_username,
    UserDiscordLinksTable.linked_at,
)


@dataclass(frozen=True, slots=True)
class DiscordLink:
    user_id: int
    discord_id: str
    discord_username: str
    linked_at: datetime


class DiscordLinksRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize(self, row: MySQLRow) -> DiscordLink:
        return DiscordLink(
            user_id=row["user_id"],
            discord_id=row["discord_id"],
            discord_username=row["discord_username"],
            linked_at=row["linked_at"],
        )

    async def _fetch(self, user_id: int) -> DiscordLink:
        select_stmt = select(*READ_PARAMS).where(
            UserDiscordLinksTable.user_id == user_id,
        )
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # written immediately before every call site.
        return self._deserialize(row)

    async def fetch_by_user_id(self, user_id: int) -> DiscordLink | None:
        """The Discord link for an osu! account, or None if it has none."""
        select_stmt = select(*READ_PARAMS).where(
            UserDiscordLinksTable.user_id == user_id,
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def fetch_by_discord_id(self, discord_id: str) -> DiscordLink | None:
        """The link backing a Discord account, or None if it is unlinked.

        The conflict check: before linking a Discord account to an osu! account,
        the service asks this whether that Discord id is already claimed by a
        *different* osu! account.
        """
        select_stmt = select(*READ_PARAMS).where(
            UserDiscordLinksTable.discord_id == discord_id,
        )
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize(row) if row is not None else None

    async def upsert_link(
        self,
        *,
        user_id: int,
        discord_id: str,
        discord_username: str,
    ) -> DiscordLink:
        """Create or replace the Discord link for an osu! account.

        Keyed on ``user_id``: a first link inserts, and re-linking the same osu!
        account to a different Discord account refreshes the columns (and stamps
        a fresh ``linked_at``) rather than erroring. The caller is responsible for
        the cross-account conflict check -- this only enforces one-Discord-per-osu
        via the primary key; one-osu-per-Discord is the DB's unique index, which
        would raise here if the guard were skipped.
        """
        discord_username = discord_username[:_DISCORD_USERNAME_MAX_LEN]
        insert_stmt: MysqlInsert = (
            mysql_insert(UserDiscordLinksTable)
            .values(
                user_id=user_id,
                discord_id=discord_id,
                discord_username=discord_username,
                linked_at=func.now(),
            )
            .on_duplicate_key_update(
                discord_id=discord_id,
                discord_username=discord_username,
                linked_at=func.now(),
            )
        )
        await self._database.execute(insert_stmt)
        return await self._fetch(user_id)

    async def delete(self, user_id: int) -> None:
        """Remove an osu! account's Discord link; a no-op if it had none."""
        delete_stmt = delete(UserDiscordLinksTable).where(
            UserDiscordLinksTable.user_id == user_id,
        )
        await self._database.execute(delete_stmt)
