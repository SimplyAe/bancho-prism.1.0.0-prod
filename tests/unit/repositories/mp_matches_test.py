"""Guards for the multiplayer match-history repository.

``mp_matches`` / ``mp_match_games`` are the durable record of a thing that is
otherwise memory-only: a multiplayer lobby and the games played in it. These
tests pin the behaviour the history readers depend on:

- ``create_match`` round-trips a match back through ``_fetch_match``, returning
  the durable id the live match stashes;
- ``record_game`` stores participants as a JSON array (round-tripping to a real
  ``list[int]``) with ``participant_count`` denormalised alongside, and stamps a
  provided ``started_at`` while defaulting ``ended_at`` to now;
- ``mark_disbanded`` only ever closes a match once -- its ``UPDATE`` is guarded
  by ``disbanded_at IS NULL``, asserted at the SQL level since the guard is the
  whole point;
- both keyset reads (``fetch_recent_matches``, ``fetch_games_for_match``) walk
  ``id`` backwards from newest and honour ``before_id``.

No MySQL is involved: a fake ``Database`` stores rows and interprets the Core
statements it is handed. Inserts pull their values from the statement's bound
columns (a fixed clock stands in for the ``now()`` server defaults); reads walk
the ``Select``'s ``WHERE``/``ORDER BY``/``LIMIT``; the disband ``UPDATE`` is
compiled against the MySQL dialect so its guard is asserted for real.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Insert
from sqlalchemy import Select
from sqlalchemy import Update
from sqlalchemy.dialects import mysql

from app.repositories.mp_matches import MpMatchesRepository
from app.repositories.mp_matches import MpMatchGameScoreInput

# a fixed clock standing in for the `now()` server defaults, so ordering ties
# and default timestamps never hinge on wall-clock time.
_NOW = datetime(2026, 8, 12, 20, 0, 0)


class _FakeDatabase:
    """Stores match/game rows and answers by interpreting the statement.

    ``execute`` handles both the inserts (pulling values out of the statement's
    bound columns, assigning an autoincrement id, defaulting ``now()`` columns to
    a fixed clock) and the disband ``UPDATE`` (walking its ``WHERE`` so the
    ``disbanded_at IS NULL`` guard actually decides whether the row changes).
    ``fetch_one`` / ``fetch_all`` walk the ``Select`` so the repository's own
    filtering and ordering decide the result.
    """

    def __init__(self) -> None:
        self.matches: list[dict[str, Any]] = []
        self.games: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self._next_match_id = 1
        self._next_game_id = 1
        self._next_score_id = 1
        # the last compiled disband UPDATE, for SQL-shape assertions.
        self.last_update_sql: str | None = None

    def _rows_for(self, table_name: str) -> list[dict[str, Any]]:
        if table_name == "mp_matches":
            return self.matches
        if table_name == "mp_match_game_scores":
            return self.scores
        return self.games

    async def execute(self, statement: Any) -> int:
        if isinstance(statement, Insert):
            return self._execute_insert(statement)
        if isinstance(statement, Update):
            return self._execute_update(statement)
        raise AssertionError(f"unhandled statement in fake: {type(statement).__name__}")

    def _execute_insert(self, statement: Insert) -> int:
        table_name = statement.table.name
        values = {col.name: _literal(val) for col, val in statement._values.items()}

        if table_name == "mp_matches":
            row: dict[str, Any] = {
                "id": self._next_match_id,
                "has_public_history": 1,
                "created_at": _NOW,
                "disbanded_at": None,
            }
            row.update(values)
            # `now()` server defaults are SQL functions, not binds; resolve them.
            row["created_at"] = _resolve_now(row.get("created_at"))
            self.matches.append(row)
            self._next_match_id += 1
            return row["id"]

        row = {
            "id": self._next_game_id,
            "map_id": 0,
            "map_name": "",
            "mode": 0,
            "mods": 0,
            "win_condition": 0,
            "team_type": 0,
            "freemods": 0,
            "scrim": 0,
            "participant_count": 0,
            "participants": None,
            "started_at": _NOW,
            "ended_at": None,
        }
        row.update(values)
        row["started_at"] = _resolve_now(row.get("started_at"))
        row["ended_at"] = _resolve_now(row.get("ended_at"))
        self.games.append(row)
        self._next_game_id += 1
        return row["id"]

    def _execute_update(self, statement: Update) -> int:
        self.last_update_sql = str(
            statement.compile(
                dialect=mysql.dialect(),
                compile_kwargs={"literal_binds": True},
            ),
        )
        predicates = _where_predicates(statement.whereclause)
        set_values = {
            col.name: _resolve_now(_literal(val))
            for col, val in statement._values.items()
        }
        changed = 0
        for row in self.matches:
            if all(p(row) for p in predicates):
                row.update(set_values)
                changed += 1
        return changed

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        matched = self._query(statement)
        return matched[0] if matched else None

    async def fetch_all(self, statement: Any) -> list[dict[str, Any]]:
        return self._query(statement)

    async def execute_many(self, query: str, params: list[dict[str, Any]]) -> None:
        """Persist a batch of raw-SQL rows (the scoreboard insert).

        ``record_game_scores`` uses raw SQL + a list of param dicts (matching the
        codebase's bulk-insert idiom), so this stores each param dict as a row,
        assigning the autoincrement id and default ``created_at`` the column would
        get. The table is inferred from the query text, keeping the fake honest
        about which store the batch lands in.
        """
        table_name = "mp_match_game_scores" if "mp_match_game_scores" in query else ""
        assert table_name, f"unhandled execute_many target: {query!r}"
        for param in params:
            row = {"id": self._next_score_id, "created_at": _NOW}
            row.update(param)
            self.scores.append(row)
            self._next_score_id += 1

    def _query(self, statement: Select) -> list[dict[str, Any]]:
        table_name = list(statement.selected_columns)[0].table.name
        rows = self._rows_for(table_name)

        predicates = _where_predicates(statement.whereclause)
        matched = [row for row in rows if all(p(row) for p in predicates)]

        for column_name, descending in reversed(_order_specs(statement)):
            matched.sort(key=lambda r: r[column_name], reverse=descending)

        limit = _limit_value(statement)
        if limit is not None:
            matched = matched[:limit]
        return matched


def _literal(value: Any) -> Any:
    """Unwrap a bound literal to its Python value; leave SQL functions intact."""
    return getattr(value, "value", value)


def _resolve_now(value: Any) -> Any:
    """A ``func.now()`` node stands in as the fixed clock; pass through reals."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # anything else (a `now()` function element) resolves to the fixed clock.
    return _NOW


def _where_predicates(whereclause: Any) -> list[Any]:
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None) or [whereclause]

    predicates = []
    for clause in clauses:
        column_name = clause.left.name
        operator = clause.operator.__name__
        if operator == "eq":
            predicates.append(_eq(column_name, _clause_value(clause.right)))
        elif operator == "lt":
            predicates.append(_lt(column_name, clause.right.value))
        elif operator == "is_":  # `col.is_(None)` -> right is a Null node
            predicates.append(_is_none(column_name))
        else:  # pragma: no cover - the repository only uses the three above
            raise AssertionError(f"unhandled operator in fake: {operator}")
    return predicates


def _clause_value(node: Any) -> Any:
    """Pull the Python value out of a clause's right-hand side.

    A bound literal (``col == 5``) carries it on ``.value``; ``col == True`` /
    ``col == False`` compile to bare ``True_`` / ``False_`` SQL literals with no
    ``.value``, so resolve those by node type.
    """
    type_name = type(node).__name__
    if type_name == "True_":
        return True
    if type_name == "False_":
        return False
    return node.value


def _eq(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] == value


def _lt(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] < value


def _is_none(column_name: str) -> Any:
    return lambda row: row[column_name] is None


def _order_specs(statement: Select) -> list[tuple[str, bool]]:
    specs: list[tuple[str, bool]] = []
    for element in statement._order_by_clauses:
        descending = element.modifier.__name__ == "desc_op"
        specs.append((element.element.name, descending))
    return specs


def _limit_value(statement: Select) -> int | None:
    limit_clause = getattr(statement, "_limit_clause", None)
    return getattr(limit_clause, "value", None) if limit_clause is not None else None


def _repo() -> tuple[MpMatchesRepository, _FakeDatabase]:
    database = _FakeDatabase()
    return MpMatchesRepository(database), database  # type: ignore[arg-type]


# --- matches ---------------------------------------------------------------


async def test_create_match_round_trips_and_returns_the_durable_id() -> None:
    repository, _database = _repo()

    match = await repository.create_match(
        name="OWC2024 Finals",
        host_id=3,
        has_public_history=True,
    )

    assert match.id == 1
    assert match.name == "OWC2024 Finals"
    assert match.host_id == 3
    assert match.has_public_history is True
    assert match.disbanded_at is None

    # the durable id is fetchable back for the game rows to reference.
    fetched = await repository.fetch_match(match.id)
    assert fetched is not None
    assert fetched.id == match.id


async def test_create_match_persists_the_private_history_flag() -> None:
    repository, _database = _repo()

    match = await repository.create_match(
        name="private lobby",
        host_id=3,
        has_public_history=False,
    )

    assert match.has_public_history is False
    fetched = await repository.fetch_match(match.id)
    assert fetched is not None
    assert fetched.has_public_history is False


async def test_fetch_match_returns_none_for_an_unknown_id() -> None:
    repository, _database = _repo()

    assert await repository.fetch_match(999) is None


async def test_mark_disbanded_stamps_and_is_guarded_by_null() -> None:
    repository, database = _repo()
    match = await repository.create_match(
        name="lobby",
        host_id=3,
        has_public_history=True,
    )

    await repository.mark_disbanded(match.id)

    disbanded = await repository.fetch_match(match.id)
    assert disbanded is not None
    assert disbanded.disbanded_at is not None

    # the UPDATE only ever closes an open match: the NULL guard is in the SQL, so
    # a duplicate teardown cannot rewrite the original disband time.
    sql = (database.last_update_sql or "").lower()
    assert "update mp_matches" in sql
    assert "set disbanded_at=now()" in sql
    assert "disbanded_at is null" in sql


async def test_mark_disbanded_does_not_reclose_an_already_disbanded_match() -> None:
    repository, database = _repo()
    match = await repository.create_match(
        name="lobby",
        host_id=3,
        has_public_history=True,
    )
    await repository.mark_disbanded(match.id)
    first = await repository.fetch_match(match.id)
    assert first is not None
    first_time = first.disbanded_at

    # pin a later clock so a (wrongly) re-applied UPDATE would be observable.
    database.matches[0]["disbanded_at"] = first_time
    await repository.mark_disbanded(match.id)

    second = await repository.fetch_match(match.id)
    assert second is not None
    assert second.disbanded_at == first_time


async def test_fetch_recent_matches_pages_newest_first_by_id() -> None:
    repository, _database = _repo()
    for name in ("a", "b", "c"):
        await repository.create_match(
            name=name,
            host_id=3,
            has_public_history=True,
        )

    newest = await repository.fetch_recent_matches(limit=2)
    assert [m.name for m in newest] == ["c", "b"]

    older = await repository.fetch_recent_matches(before_id=newest[-1].id, limit=2)
    assert [m.name for m in older] == ["a"]


async def test_fetch_recent_matches_public_only_hides_private_lobbies() -> None:
    repository, _database = _repo()
    await repository.create_match(name="pub1", host_id=3, has_public_history=True)
    await repository.create_match(name="priv", host_id=3, has_public_history=False)
    await repository.create_match(name="pub2", host_id=3, has_public_history=True)

    # the browsable index asks for public matches only -> the private lobby is
    # filtered in the query, so it never occupies a slot in the page.
    public = await repository.fetch_recent_matches(public_only=True)
    assert [m.name for m in public] == ["pub2", "pub1"]

    # the default (unfiltered) read still returns everything, newest first.
    everything = await repository.fetch_recent_matches()
    assert [m.name for m in everything] == ["pub2", "priv", "pub1"]


# --- games -----------------------------------------------------------------


async def test_record_game_round_trips_participants_as_a_list() -> None:
    repository, _database = _repo()
    match = await repository.create_match(
        name="lobby",
        host_id=3,
        has_public_history=True,
    )
    started = datetime(2026, 8, 12, 19, 30, 0)

    game = await repository.record_game(
        match_id=match.id,
        map_md5="a" * 32,
        map_id=42,
        map_name="Artist - Title [Insane]",
        mode=0,
        mods=64,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=True,
        participants=[4, 5, 6],
        started_at=started,
    )

    assert game.match_id == match.id
    assert game.participants == [4, 5, 6]
    assert game.participant_count == 3
    assert game.scrim is True
    assert game.mods == 64
    # a provided started_at is honoured; ended_at defaults to the completion now.
    assert game.started_at == started
    assert game.ended_at is not None


async def test_record_game_stores_empty_participants_as_empty_list() -> None:
    repository, _database = _repo()
    match = await repository.create_match(
        name="lobby",
        host_id=3,
        has_public_history=True,
    )

    game = await repository.record_game(
        match_id=match.id,
        map_md5="b" * 32,
        map_id=0,
        map_name="",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[],
    )

    assert game.participants == []
    assert game.participant_count == 0


async def test_fetch_games_for_match_pages_newest_first_and_scopes_to_match() -> None:
    repository, _database = _repo()
    match_a = await repository.create_match(
        name="a",
        host_id=3,
        has_public_history=True,
    )
    match_b = await repository.create_match(
        name="b",
        host_id=3,
        has_public_history=True,
    )
    # two games in A, one in B (which must not leak into A's history).
    await repository.record_game(
        match_id=match_a.id,
        map_md5="a" * 32,
        map_id=1,
        map_name="map1",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4],
    )
    await repository.record_game(
        match_id=match_a.id,
        map_md5="c" * 32,
        map_id=2,
        map_name="map2",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4, 5],
    )
    await repository.record_game(
        match_id=match_b.id,
        map_md5="d" * 32,
        map_id=3,
        map_name="map3",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[9],
    )

    games = await repository.fetch_games_for_match(match_a.id)
    assert [g.map_name for g in games] == ["map2", "map1"]
    assert all(g.match_id == match_a.id for g in games)

    older = await repository.fetch_games_for_match(match_a.id, before_id=games[-1].id)
    assert older == []


# --- per-player scoreboards ------------------------------------------------


def _score(
    user_id: int,
    *,
    score: int = 0,
    acc: float = 0.0,
    max_combo: int = 0,
    passed: bool = True,
    team: int = 0,
    mods: int = 0,
) -> MpMatchGameScoreInput:
    return MpMatchGameScoreInput(
        user_id=user_id,
        team=team,
        mods=mods,
        score=score,
        max_combo=max_combo,
        num300=0,
        num100=0,
        num50=0,
        num_geki=0,
        num_katu=0,
        num_miss=0,
        acc=acc,
        perfect=False,
        passed=passed,
    )


async def test_record_game_scores_ranks_by_score_and_round_trips() -> None:
    repository, _database = _repo()

    written = await repository.record_game_scores(
        game_id=7,
        win_condition=0,  # score
        scores=[
            _score(10, score=500_000),
            _score(20, score=900_000),
            _score(30, score=700_000),
        ],
    )

    assert written == 3
    board = await repository.fetch_scores_for_game(7)
    # returned already ordered by placement; the highest score is 1st.
    assert [(s.user_id, s.placement) for s in board] == [(20, 1), (30, 2), (10, 3)]
    assert all(s.game_id == 7 for s in board)


async def test_record_game_scores_ranks_by_accuracy_when_that_is_the_condition() -> (
    None
):
    repository, _database = _repo()

    await repository.record_game_scores(
        game_id=7,
        win_condition=1,  # accuracy
        scores=[
            _score(10, score=999_999, acc=90.0),  # top score, lower acc
            _score(20, score=100_000, acc=99.5),  # low score, top acc -> 1st
        ],
    )

    board = await repository.fetch_scores_for_game(7)
    assert [(s.user_id, s.placement) for s in board] == [(20, 1), (10, 2)]


async def test_record_game_scores_ranks_by_combo_when_that_is_the_condition() -> None:
    repository, _database = _repo()

    await repository.record_game_scores(
        game_id=7,
        win_condition=2,  # combo
        scores=[
            _score(10, score=999_999, max_combo=120),
            _score(20, score=100_000, max_combo=340),  # top combo -> 1st
        ],
    )

    board = await repository.fetch_scores_for_game(7)
    assert [(s.user_id, s.placement) for s in board] == [(20, 1), (10, 2)]


async def test_record_game_scores_ranks_passers_above_failers() -> None:
    repository, _database = _repo()

    await repository.record_game_scores(
        game_id=7,
        win_condition=0,
        scores=[
            _score(10, score=900_000, passed=False),  # highest score, but failed
            _score(20, score=200_000, passed=True),  # passed -> outranks the failer
        ],
    )

    board = await repository.fetch_scores_for_game(7)
    assert [(s.user_id, s.placement) for s in board] == [(20, 1), (10, 2)]
    # the fail flag round-trips as a real bool.
    failer = next(s for s in board if s.user_id == 10)
    assert failer.passed is False


async def test_record_game_scores_no_op_on_empty() -> None:
    repository, database = _repo()

    written = await repository.record_game_scores(
        game_id=7,
        win_condition=0,
        scores=[],
    )

    assert written == 0
    assert database.scores == []
    assert await repository.fetch_scores_for_game(7) == []


async def test_fetch_scores_for_game_is_empty_for_an_unrecorded_game() -> None:
    repository, _database = _repo()

    assert await repository.fetch_scores_for_game(999) == []
