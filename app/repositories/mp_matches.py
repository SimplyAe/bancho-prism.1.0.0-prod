"""Durable multiplayer match history.

Stock bancho.py keeps every multiplayer match purely in memory
(``app.state.sessions.matches``): the lobby, its slots, the map, the scrim
state -- all of it is a live object that vanishes when the last player leaves,
and the whole collection is gone on restart. Nothing is ever written down, so
"what happened in that lobby?" has no answer once the lobby closes. These two
tables are that record.

The in-memory ``Match.id`` is a recycled 0-15 lobby *slot* index, not a stable
identity, so the durable rows carry their own auto-increment ids. The live
match stashes the ``mp_matches.id`` it was assigned (see
``Match.db_id``) so the game rows written during its lifetime can point back at
it without threading the id through every packet handler.

Shape mirrors the in-memory model:

- ``mp_matches`` -- one row per lobby lifetime. Created when the lobby opens;
  ``disbanded_at`` is stamped when it tears down. ``has_public_history`` carries
  the ``//private`` privacy flag so a private lobby's history is marked private.
- ``mp_match_games`` -- one row per *completed* game (a map that ran to
  MATCH_COMPLETE). The map / mode / mods / win-condition / team-type snapshot is
  copied at completion, so the record is faithful even after the lobby moves on
  to the next map. Participants are stored as a JSON array of user ids (exactly
  as ``activity_events.data`` stores per-type detail) rather than a third join
  table, with a denormalised ``participant_count`` so "how many played" needs no
  parse.
- ``mp_match_game_scores`` -- one row per participant per completed game (a child
  of ``mp_match_games``). osu! streams each player's live score to the server via
  MATCH_SCORE_UPDATE; stock bancho.py only rebroadcasts those frames and throws
  them away. Prism keeps the final frame per player, so a completed game has an
  actual scoreboard: score, combo, accuracy components, mods, team, pass/fail,
  and a precomputed 1-based ``placement`` under the game's win condition.

Reads page by ``id`` (keyset, ``id < before_id``), never ``OFFSET``: match
history only ever scrolls backwards from "now", and keyset paging stays O(page)
as the history grows. As with the rest of the schema the foreign keys
(``host_id`` -> users, ``match_id`` -> mp_matches) are enforced in application
logic, not the DB, so a purged player or match orphans rather than cascades.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.mysql import FLOAT
from sqlalchemy.dialects.mysql import TINYINT

from app.adapters.database import Database
from app.adapters.database import MySQLParams
from app.adapters.database import MySQLRow
from app.repositories import Base

# osu! caps a multiplayer match name at 50 chars (MAX_MATCH_NAME_LENGTH); the
# column matches, and writes are truncated to it defensively.
_MATCH_NAME_MAX_LEN = 50
# "Artist - Title [Version]" can be long; each of artist/title/version is 128 in
# the maps table, so the assembled name needs comfortable headroom.
_MAP_NAME_MAX_LEN = 256


class MpMatchesTable(Base):
    __tablename__ = "mp_matches"

    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    name = Column("name", String(_MATCH_NAME_MAX_LEN), nullable=False)
    host_id = Column("host_id", Integer, nullable=False)
    has_public_history = Column(
        "has_public_history",
        TINYINT(1),
        nullable=False,
        server_default="1",
    )
    created_at = Column(
        "created_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    disbanded_at = Column("disbanded_at", DateTime, nullable=True)

    __table_args__ = (
        Index("mp_matches_host_id_index", host_id),
        Index("mp_matches_created_at_index", created_at),
    )


class MpMatchGamesTable(Base):
    __tablename__ = "mp_match_games"

    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    match_id = Column("match_id", BigInteger, nullable=False)
    map_md5 = Column("map_md5", String(32), nullable=False)
    map_id = Column("map_id", Integer, nullable=False, server_default="0")
    map_name = Column(
        "map_name",
        String(_MAP_NAME_MAX_LEN),
        nullable=False,
        server_default="",
    )
    mode = Column("mode", TINYINT(1), nullable=False, server_default="0")
    mods = Column("mods", Integer, nullable=False, server_default="0")
    win_condition = Column(
        "win_condition",
        TINYINT(1),
        nullable=False,
        server_default="0",
    )
    team_type = Column("team_type", TINYINT(1), nullable=False, server_default="0")
    freemods = Column("freemods", TINYINT(1), nullable=False, server_default="0")
    scrim = Column("scrim", TINYINT(1), nullable=False, server_default="0")
    participant_count = Column(
        "participant_count",
        Integer,
        nullable=False,
        server_default="0",
    )
    participants = Column("participants", Text, nullable=True)
    started_at = Column(
        "started_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    ended_at = Column("ended_at", DateTime, nullable=True)

    __table_args__ = (
        # games of a match, newest-first keyset scan.
        Index("mp_match_games_match_id_id_index", match_id, id),
        # "every game played on this map".
        Index("mp_match_games_map_md5_index", map_md5),
        # time-window scans and retention pruning.
        Index("mp_match_games_started_at_index", started_at),
    )


class MpMatchGameScoresTable(Base):
    __tablename__ = "mp_match_game_scores"

    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    game_id = Column("game_id", BigInteger, nullable=False)
    user_id = Column("user_id", Integer, nullable=False)
    team = Column("team", TINYINT(1), nullable=False, server_default="0")
    mods = Column("mods", Integer, nullable=False, server_default="0")
    score = Column("score", Integer, nullable=False, server_default="0")
    max_combo = Column("max_combo", Integer, nullable=False, server_default="0")
    num300 = Column("num300", Integer, nullable=False, server_default="0")
    num100 = Column("num100", Integer, nullable=False, server_default="0")
    num50 = Column("num50", Integer, nullable=False, server_default="0")
    num_geki = Column("num_geki", Integer, nullable=False, server_default="0")
    num_katu = Column("num_katu", Integer, nullable=False, server_default="0")
    num_miss = Column("num_miss", Integer, nullable=False, server_default="0")
    acc = Column(
        "acc",
        FLOAT(precision=6, scale=3),
        nullable=False,
        server_default="0",
    )
    perfect = Column("perfect", TINYINT(1), nullable=False, server_default="0")
    passed = Column("passed", TINYINT(1), nullable=False, server_default="1")
    placement = Column("placement", Integer, nullable=False, server_default="0")
    created_at = Column(
        "created_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # a game's scoreboard, already ordered by finishing placement.
        Index(
            "mp_match_game_scores_game_id_placement_index",
            game_id,
            placement,
        ),
        # "every multiplayer score this player has recorded".
        Index("mp_match_game_scores_user_id_index", user_id),
    )


MATCH_READ_PARAMS = (
    MpMatchesTable.id,
    MpMatchesTable.name,
    MpMatchesTable.host_id,
    MpMatchesTable.has_public_history,
    MpMatchesTable.created_at,
    MpMatchesTable.disbanded_at,
)

GAME_READ_PARAMS = (
    MpMatchGamesTable.id,
    MpMatchGamesTable.match_id,
    MpMatchGamesTable.map_md5,
    MpMatchGamesTable.map_id,
    MpMatchGamesTable.map_name,
    MpMatchGamesTable.mode,
    MpMatchGamesTable.mods,
    MpMatchGamesTable.win_condition,
    MpMatchGamesTable.team_type,
    MpMatchGamesTable.freemods,
    MpMatchGamesTable.scrim,
    MpMatchGamesTable.participant_count,
    MpMatchGamesTable.participants,
    MpMatchGamesTable.started_at,
    MpMatchGamesTable.ended_at,
)

GAME_SCORE_READ_PARAMS = (
    MpMatchGameScoresTable.id,
    MpMatchGameScoresTable.game_id,
    MpMatchGameScoresTable.user_id,
    MpMatchGameScoresTable.team,
    MpMatchGameScoresTable.mods,
    MpMatchGameScoresTable.score,
    MpMatchGameScoresTable.max_combo,
    MpMatchGameScoresTable.num300,
    MpMatchGameScoresTable.num100,
    MpMatchGameScoresTable.num50,
    MpMatchGameScoresTable.num_geki,
    MpMatchGameScoresTable.num_katu,
    MpMatchGameScoresTable.num_miss,
    MpMatchGameScoresTable.acc,
    MpMatchGameScoresTable.perfect,
    MpMatchGameScoresTable.passed,
    MpMatchGameScoresTable.placement,
    MpMatchGameScoresTable.created_at,
)


@dataclass(frozen=True, slots=True)
class MpMatch:
    id: int
    name: str
    host_id: int
    has_public_history: bool
    created_at: datetime
    disbanded_at: datetime | None


@dataclass(frozen=True, slots=True)
class MpMatchGame:
    id: int
    match_id: int
    map_md5: str
    map_id: int
    map_name: str
    mode: int
    mods: int
    win_condition: int
    team_type: int
    freemods: bool
    scrim: bool
    participant_count: int
    participants: list[int]
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class MpMatchGameScore:
    """One participant's final scoreboard row in a completed game."""

    id: int
    game_id: int
    user_id: int
    team: int
    mods: int
    score: int
    max_combo: int
    num300: int
    num100: int
    num50: int
    num_geki: int
    num_katu: int
    num_miss: int
    acc: float
    perfect: bool
    passed: bool
    placement: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MpMatchGameScoreInput:
    """A single participant's result to persist, before it has a game id.

    The producer builds one of these per participant from a decoded scoreframe;
    ``record_game_scores`` computes ``placement`` across the set and writes them
    as the game's child rows. ``placement`` is intentionally absent here -- it is
    a property of the whole scoreboard, not of one row, so it is assigned at
    write time rather than by the caller.
    """

    user_id: int
    team: int
    mods: int
    score: int
    max_combo: int
    num300: int
    num100: int
    num50: int
    num_geki: int
    num_katu: int
    num_miss: int
    acc: float
    perfect: bool
    passed: bool


class MpMatchesRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _deserialize_match(self, row: MySQLRow) -> MpMatch:
        return MpMatch(
            id=row["id"],
            name=row["name"],
            host_id=row["host_id"],
            has_public_history=bool(row["has_public_history"]),
            created_at=row["created_at"],
            disbanded_at=row["disbanded_at"],
        )

    def _deserialize_game(self, row: MySQLRow) -> MpMatchGame:
        participants_raw = row["participants"]
        return MpMatchGame(
            id=row["id"],
            match_id=row["match_id"],
            map_md5=row["map_md5"],
            map_id=row["map_id"],
            map_name=row["map_name"],
            mode=row["mode"],
            mods=row["mods"],
            win_condition=row["win_condition"],
            team_type=row["team_type"],
            freemods=bool(row["freemods"]),
            scrim=bool(row["scrim"]),
            participant_count=row["participant_count"],
            participants=(
                json.loads(participants_raw) if participants_raw is not None else []
            ),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )

    async def create_match(
        self,
        *,
        name: str,
        host_id: int,
        has_public_history: bool,
    ) -> MpMatch:
        """Record a newly-opened lobby and return the stored row.

        The returned ``id`` is the durable match id the live ``Match`` stashes so
        its game rows can reference it.
        """
        insert_stmt = insert(MpMatchesTable).values(
            name=name[:_MATCH_NAME_MAX_LEN],
            host_id=host_id,
            has_public_history=has_public_history,
            created_at=func.now(),
        )
        rec_id = await self._database.execute(insert_stmt)
        return await self._fetch_match(rec_id)

    async def _fetch_match(self, match_id: int) -> MpMatch:
        select_stmt = select(*MATCH_READ_PARAMS).where(MpMatchesTable.id == match_id)
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # inserted immediately before every call site.
        return self._deserialize_match(row)

    async def mark_disbanded(self, match_id: int) -> None:
        """Stamp ``disbanded_at`` when a lobby tears down.

        Guarded by ``disbanded_at IS NULL`` so a match is only ever closed once:
        a duplicate teardown (or a late-arriving event) cannot rewrite the
        original disband time.
        """
        update_stmt = (
            update(MpMatchesTable)
            .where(MpMatchesTable.id == match_id)
            .where(MpMatchesTable.disbanded_at.is_(None))
            .values(disbanded_at=func.now())
        )
        await self._database.execute(update_stmt)

    async def record_game(
        self,
        *,
        match_id: int,
        map_md5: str,
        map_id: int,
        map_name: str,
        mode: int,
        mods: int,
        win_condition: int,
        team_type: int,
        freemods: bool,
        scrim: bool,
        participants: list[int],
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> MpMatchGame:
        """Record one completed game within a lobby and return the stored row.

        ``participants`` is stored as a compact JSON array of user ids, with
        ``participant_count`` denormalised alongside. A missing ``ended_at``
        defaults to now (the game just completed); a missing ``started_at`` also
        defaults to now, so a game whose start went unrecorded still lands with a
        sane, non-null timestamp rather than being dropped.
        """
        participants_json = json.dumps(participants, separators=(",", ":"))
        insert_stmt = insert(MpMatchGamesTable).values(
            match_id=match_id,
            map_md5=map_md5,
            map_id=map_id,
            map_name=map_name[:_MAP_NAME_MAX_LEN],
            mode=mode,
            mods=mods,
            win_condition=win_condition,
            team_type=team_type,
            freemods=freemods,
            scrim=scrim,
            participant_count=len(participants),
            participants=participants_json,
            started_at=started_at if started_at is not None else func.now(),
            ended_at=ended_at if ended_at is not None else func.now(),
        )
        rec_id = await self._database.execute(insert_stmt)
        return await self._fetch_game(rec_id)

    async def _fetch_game(self, game_id: int) -> MpMatchGame:
        select_stmt = select(*GAME_READ_PARAMS).where(MpMatchGamesTable.id == game_id)
        row = await self._database.fetch_one(select_stmt)
        assert row is not None  # inserted immediately before every call site.
        return self._deserialize_game(row)

    async def fetch_game(self, game_id: int) -> MpMatchGame | None:
        """One game by its durable id, or ``None`` if unknown.

        Distinct from the private ``_fetch_game``, which asserts the row exists
        because it is only ever called right after inserting it. This one serves
        a client-supplied id, so a miss is an ordinary ``None`` rather than a
        crash. The read side uses it to confirm a game belongs to the match it
        was requested under before disclosing that game's scoreboard.
        """
        select_stmt = select(*GAME_READ_PARAMS).where(MpMatchGamesTable.id == game_id)
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize_game(row) if row is not None else None

    async def fetch_match(self, match_id: int) -> MpMatch | None:
        """One match by its durable id, or ``None`` if unknown."""
        select_stmt = select(*MATCH_READ_PARAMS).where(MpMatchesTable.id == match_id)
        row = await self._database.fetch_one(select_stmt)
        return self._deserialize_match(row) if row is not None else None

    async def fetch_recent_matches(
        self,
        *,
        before_id: int | None = None,
        limit: int = 50,
        public_only: bool = False,
    ) -> list[MpMatch]:
        """Recent matches, newest first, keyset-paged by id.

        ``before_id`` scrolls backwards: pass the id of the oldest match already
        seen to get the next older page. Paging by ``id < before_id`` (not
        ``OFFSET``) keeps each page O(limit) as history grows.

        ``public_only`` restricts the result to matches whose history is public,
        for the browsable listing: applying the filter in the query (rather than
        after paging) means a page is never silently short because private
        lobbies were dropped from it, which would corrupt the keyset cursor.
        """
        select_stmt = select(*MATCH_READ_PARAMS)
        if public_only:
            select_stmt = select_stmt.where(
                MpMatchesTable.has_public_history == True,
            )
        if before_id is not None:
            select_stmt = select_stmt.where(MpMatchesTable.id < before_id)

        select_stmt = select_stmt.order_by(MpMatchesTable.id.desc()).limit(limit)
        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize_match(row) for row in rows]

    async def fetch_games_for_match(
        self,
        match_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[MpMatchGame]:
        """The games played in one match, newest first, keyset-paged by id.

        The composite ``(match_id, id)`` index serves both the filter and the
        keyset order in one scan.
        """
        select_stmt = select(*GAME_READ_PARAMS).where(
            MpMatchGamesTable.match_id == match_id,
        )
        if before_id is not None:
            select_stmt = select_stmt.where(MpMatchGamesTable.id < before_id)

        select_stmt = select_stmt.order_by(MpMatchGamesTable.id.desc()).limit(limit)
        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize_game(row) for row in rows]

    def _deserialize_game_score(self, row: MySQLRow) -> MpMatchGameScore:
        return MpMatchGameScore(
            id=row["id"],
            game_id=row["game_id"],
            user_id=row["user_id"],
            team=row["team"],
            mods=row["mods"],
            score=row["score"],
            max_combo=row["max_combo"],
            num300=row["num300"],
            num100=row["num100"],
            num50=row["num50"],
            num_geki=row["num_geki"],
            num_katu=row["num_katu"],
            num_miss=row["num_miss"],
            acc=row["acc"],
            perfect=bool(row["perfect"]),
            passed=bool(row["passed"]),
            placement=row["placement"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _ranked_scores(
        scores: list[MpMatchGameScoreInput],
        *,
        win_condition: int,
    ) -> list[tuple[int, MpMatchGameScoreInput]]:
        """Assign each score a 1-based placement under the game's win condition.

        A player who passed always outranks one who failed; within each group the
        ordering is by the win-condition metric, highest first (accuracy for the
        accuracy condition, combo for combo, otherwise raw score -- score_v2 is
        still ranked on the score field the client sends). Ties share neither a
        placement nor an order beyond this; the sort is stable, so equal scores
        keep their input order and each still gets a distinct, increasing number.
        """

        def metric(s: MpMatchGameScoreInput) -> float:
            if win_condition == 1:  # accuracy
                return s.acc
            elif win_condition == 2:  # combo
                return s.max_combo
            else:  # score / scorev2
                return s.score

        ordered = sorted(scores, key=lambda s: (s.passed, metric(s)), reverse=True)
        return [(i + 1, s) for i, s in enumerate(ordered)]

    async def record_game_scores(
        self,
        *,
        game_id: int,
        win_condition: int,
        scores: list[MpMatchGameScoreInput],
    ) -> int:
        """Persist a completed game's per-player scoreboard; return rows written.

        Placement is computed here, across the whole set, so a scoreboard is
        stored already ranked and needs no re-sort on read. An empty ``scores``
        list is a no-op (no participant sent a usable frame) and writes nothing.
        """
        if not scores:
            return 0

        rows: list[MySQLParams] = [
            {
                "game_id": game_id,
                "user_id": s.user_id,
                "team": s.team,
                "mods": s.mods,
                "score": s.score,
                "max_combo": s.max_combo,
                "num300": s.num300,
                "num100": s.num100,
                "num50": s.num50,
                "num_geki": s.num_geki,
                "num_katu": s.num_katu,
                "num_miss": s.num_miss,
                "acc": s.acc,
                "perfect": s.perfect,
                "passed": s.passed,
                "placement": placement,
            }
            for placement, s in self._ranked_scores(
                scores,
                win_condition=win_condition,
            )
        ]
        await self._database.execute_many(
            "INSERT INTO mp_match_game_scores ("
            "game_id, user_id, team, mods, score, max_combo, "
            "num300, num100, num50, num_geki, num_katu, num_miss, "
            "acc, perfect, passed, placement"
            ") VALUES ("
            ":game_id, :user_id, :team, :mods, :score, :max_combo, "
            ":num300, :num100, :num50, :num_geki, :num_katu, :num_miss, "
            ":acc, :perfect, :passed, :placement"
            ")",
            rows,
        )
        return len(rows)

    async def fetch_scores_for_game(self, game_id: int) -> list[MpMatchGameScore]:
        """A completed game's scoreboard, ordered by finishing placement.

        Served by the ``(game_id, placement)`` index in one scan. Returns ``[]``
        for a game with no recorded scores (older games, or a game where no
        participant sent a usable frame) -- an empty scoreboard, not an error.
        """
        select_stmt = (
            select(*GAME_SCORE_READ_PARAMS)
            .where(MpMatchGameScoresTable.game_id == game_id)
            .order_by(MpMatchGameScoresTable.placement.asc())
        )
        rows = await self._database.fetch_all(select_stmt)
        return [self._deserialize_game_score(row) for row in rows]
