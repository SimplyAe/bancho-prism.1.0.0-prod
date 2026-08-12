"""Guards for the multiplayer match-history read service.

The repository is dumb about the ``//private`` flag -- it stores and returns the
bit. This service is where the bit acquires meaning, and that visibility rule is
the whole reason the service exists, so it is what these tests pin:

- the public *listing* always asks the repository for public matches only (the
  filter is pushed into the query, so a private lobby never even reaches paging);
- a *specific* match, and its games, is visible when it is public, when the
  viewer is its host, or when the viewer is staff -- and to nobody else;
- "unknown" and "not visible to you" are reported identically (both ``None``),
  so the HTTP layer cannot leak a private lobby's existence;
- ``fetch_games`` distinguishes a missing/inaccessible match (``None``) from a
  visible match with no games yet (``[]``).

No database: a fake repository returns canned rows and records how it was
queried, matching the injected-edge style the services layer tests use.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from app.repositories.mp_matches import MpMatch
from app.repositories.mp_matches import MpMatchGame
from app.repositories.mp_matches import MpMatchGameScore
from app.services.multiplayer.match_history_reader import MatchHistoryService

_NOW = datetime(2026, 8, 12, 20, 0, 0)

# a handful of stable actor identities used across the visibility matrix.
_HOST_ID = 3
_STRANGER_ID = 99


def _match(
    match_id: int, *, has_public_history: bool = True, host_id: int = _HOST_ID
) -> MpMatch:
    return MpMatch(
        id=match_id,
        name=f"lobby {match_id}",
        host_id=host_id,
        has_public_history=has_public_history,
        created_at=_NOW,
        disbanded_at=None,
    )


def _game(game_id: int, *, match_id: int) -> MpMatchGame:
    return MpMatchGame(
        id=game_id,
        match_id=match_id,
        map_md5="a" * 32,
        map_id=1,
        map_name="Artist - Title [Insane]",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participant_count=1,
        participants=[4],
        started_at=_NOW,
        ended_at=_NOW,
    )


def _score(game_id: int, user_id: int, *, placement: int) -> MpMatchGameScore:
    return MpMatchGameScore(
        id=user_id,
        game_id=game_id,
        user_id=user_id,
        team=0,
        mods=0,
        score=1_000_000 - placement,
        max_combo=0,
        num300=0,
        num100=0,
        num50=0,
        num_geki=0,
        num_katu=0,
        num_miss=0,
        acc=100.0,
        perfect=False,
        passed=True,
        placement=placement,
        created_at=_NOW,
    )


class _FakeMatchesRepository:
    """Returns canned match/game rows and records the queries it received."""

    def __init__(self) -> None:
        self.matches_by_id: dict[int, MpMatch] = {}
        self.games_by_match: dict[int, list[MpMatchGame]] = {}
        self.games_by_id: dict[int, MpMatchGame] = {}
        self.scores_by_game: dict[int, list[MpMatchGameScore]] = {}
        self.recent_calls: list[SimpleNamespace] = []
        self.games_calls: list[SimpleNamespace] = []
        self.scores_calls: list[int] = []
        self.recent_result: list[MpMatch] = []

    async def fetch_recent_matches(
        self,
        *,
        before_id: int | None = None,
        limit: int = 50,
        public_only: bool = False,
    ) -> list[MpMatch]:
        self.recent_calls.append(
            SimpleNamespace(before_id=before_id, limit=limit, public_only=public_only),
        )
        return self.recent_result

    async def fetch_match(self, match_id: int) -> MpMatch | None:
        return self.matches_by_id.get(match_id)

    async def fetch_game(self, game_id: int) -> MpMatchGame | None:
        return self.games_by_id.get(game_id)

    async def fetch_games_for_match(
        self,
        match_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[MpMatchGame]:
        self.games_calls.append(
            SimpleNamespace(match_id=match_id, before_id=before_id, limit=limit),
        )
        return self.games_by_match.get(match_id, [])

    async def fetch_scores_for_game(self, game_id: int) -> list[MpMatchGameScore]:
        self.scores_calls.append(game_id)
        return self.scores_by_game.get(game_id, [])


def _service() -> tuple[MatchHistoryService, _FakeMatchesRepository]:
    repository = _FakeMatchesRepository()
    return MatchHistoryService(matches=repository), repository  # type: ignore[arg-type]


# --- listing: always public-only, cursor threaded --------------------------


async def test_fetch_recent_matches_asks_the_repository_for_public_only() -> None:
    service, repository = _service()
    repository.recent_result = [_match(3), _match(2)]

    result = await service.fetch_recent_matches(before_id=10, limit=25)

    assert [m.id for m in result] == [3, 2]
    # the public/private filter is the service's job and is pushed into the
    # query, with the paging args threaded through untouched.
    call = repository.recent_calls[0]
    assert call.public_only is True
    assert (call.before_id, call.limit) == (10, 25)


# --- single match: the visibility matrix -----------------------------------


async def test_fetch_match_public_is_visible_to_anyone() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=True)

    match = await service.fetch_match(5, viewer_id=None, viewer_is_staff=False)

    assert match is not None
    assert match.id == 5


async def test_fetch_match_unknown_is_none() -> None:
    service, _repository = _service()

    assert await service.fetch_match(404, viewer_id=_HOST_ID) is None


async def test_fetch_match_private_is_hidden_from_anonymous_and_strangers() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False)

    # both an anonymous caller and a signed-in non-host, non-staff caller are
    # told "not found" -- the private lobby's existence is never disclosed.
    assert await service.fetch_match(5, viewer_id=None) is None
    assert await service.fetch_match(5, viewer_id=_STRANGER_ID) is None


async def test_fetch_match_private_is_visible_to_its_host() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False, host_id=_HOST_ID)

    match = await service.fetch_match(5, viewer_id=_HOST_ID, viewer_is_staff=False)

    assert match is not None
    assert match.id == 5


async def test_fetch_match_private_is_visible_to_staff() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False, host_id=_HOST_ID)

    # a stranger, but staff -> may audit any lobby.
    match = await service.fetch_match(5, viewer_id=_STRANGER_ID, viewer_is_staff=True)

    assert match is not None
    assert match.id == 5


# --- games: visibility gates the read, empty != missing --------------------


async def test_fetch_games_returns_games_for_a_visible_match() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=True)
    repository.games_by_match[5] = [_game(2, match_id=5), _game(1, match_id=5)]

    games = await service.fetch_games(5, before_id=10, limit=25, viewer_id=None)

    assert games is not None
    assert [g.id for g in games] == [2, 1]
    # the keyset args are threaded to the repository unchanged.
    call = repository.games_calls[0]
    assert (call.match_id, call.before_id, call.limit) == (5, 10, 25)


async def test_fetch_games_returns_none_for_an_inaccessible_match() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False)

    # not visible -> None (so HTTP -> 404), and the games read never happened.
    games = await service.fetch_games(5, viewer_id=_STRANGER_ID)

    assert games is None
    assert repository.games_calls == []


async def test_fetch_games_returns_none_for_an_unknown_match() -> None:
    service, _repository = _service()

    assert await service.fetch_games(404, viewer_id=_HOST_ID) is None


async def test_fetch_games_distinguishes_an_empty_history_from_a_missing_match() -> (
    None
):
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=True)
    # visible match, but no games recorded yet.

    games = await service.fetch_games(5, viewer_id=None)

    # an empty list, NOT None -- an empty history is not a missing match.
    assert games == []


async def test_fetch_games_lets_the_host_read_their_private_lobby() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False, host_id=_HOST_ID)
    repository.games_by_match[5] = [_game(1, match_id=5)]

    games = await service.fetch_games(5, viewer_id=_HOST_ID)

    assert games is not None
    assert [g.id for g in games] == [1]


# --- game scores: match visibility + game-ownership gate -------------------


async def test_fetch_game_scores_returns_the_scoreboard_for_a_visible_game() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=True)
    repository.games_by_id[8] = _game(8, match_id=5)
    repository.scores_by_game[8] = [
        _score(8, 20, placement=1),
        _score(8, 10, placement=2),
    ]

    scores = await service.fetch_game_scores(5, 8, viewer_id=None)

    assert scores is not None
    assert [(s.user_id, s.placement) for s in scores] == [(20, 1), (10, 2)]


async def test_fetch_game_scores_returns_none_for_an_inaccessible_match() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False)
    repository.games_by_id[8] = _game(8, match_id=5)
    repository.scores_by_game[8] = [_score(8, 20, placement=1)]

    # the parent match is not visible -> None, and the game/scores are never read.
    scores = await service.fetch_game_scores(5, 8, viewer_id=_STRANGER_ID)

    assert scores is None
    assert repository.scores_calls == []


async def test_fetch_game_scores_returns_none_when_the_game_belongs_to_another_match() -> (
    None
):
    service, repository = _service()
    # both matches are public and readable...
    repository.matches_by_id[5] = _match(5, has_public_history=True)
    repository.matches_by_id[6] = _match(6, has_public_history=True)
    # ...but game 8 belongs to match 6, not the match 5 it is requested under.
    repository.games_by_id[8] = _game(8, match_id=6)
    repository.scores_by_game[8] = [_score(8, 20, placement=1)]

    scores = await service.fetch_game_scores(5, 8, viewer_id=None)

    # a game cannot be read through the wrong match, so its scores stay hidden.
    assert scores is None
    assert repository.scores_calls == []


async def test_fetch_game_scores_returns_none_for_an_unknown_game() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=True)
    # no game 8 recorded.

    scores = await service.fetch_game_scores(5, 8, viewer_id=None)

    assert scores is None
    assert repository.scores_calls == []


async def test_fetch_game_scores_returns_none_for_an_unknown_match() -> None:
    service, repository = _service()
    repository.games_by_id[8] = _game(8, match_id=5)

    assert await service.fetch_game_scores(404, 8, viewer_id=_HOST_ID) is None


async def test_fetch_game_scores_distinguishes_an_empty_scoreboard_from_a_missing_game() -> (
    None
):
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=True)
    repository.games_by_id[8] = _game(8, match_id=5)
    # a real, visible game with no recorded scores yet.

    scores = await service.fetch_game_scores(5, 8, viewer_id=None)

    # an empty list, NOT None -- an empty scoreboard is not a missing game.
    assert scores == []
    assert repository.scores_calls == [8]


async def test_fetch_game_scores_lets_the_host_read_their_private_games_board() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False, host_id=_HOST_ID)
    repository.games_by_id[8] = _game(8, match_id=5)
    repository.scores_by_game[8] = [_score(8, 4, placement=1)]

    scores = await service.fetch_game_scores(5, 8, viewer_id=_HOST_ID)

    assert scores is not None
    assert [s.user_id for s in scores] == [4]


async def test_fetch_game_scores_lets_staff_read_any_private_games_board() -> None:
    service, repository = _service()
    repository.matches_by_id[5] = _match(5, has_public_history=False, host_id=_HOST_ID)
    repository.games_by_id[8] = _game(8, match_id=5)
    repository.scores_by_game[8] = [_score(8, 4, placement=1)]

    scores = await service.fetch_game_scores(
        5, 8, viewer_id=_STRANGER_ID, viewer_is_staff=True
    )

    assert scores is not None
    assert [s.user_id for s in scores] == [4]


# a small guard that the fake matches the real repository's keyword surface.
def test_fake_repository_matches_the_real_signature() -> None:
    from inspect import signature

    from app.repositories.mp_matches import MpMatchesRepository

    real: Any = signature(MpMatchesRepository.fetch_recent_matches).parameters
    assert "public_only" in real
