"""Guards for the multiplayer match-history producers.

These sit on the in-memory match lifecycle and write the durable history rows,
fire-and-forget, off the packet handlers. Two properties matter and are pinned
here:

- **the id handoff**: a successful create hands the durable id back through
  ``on_persisted`` so the live match can stash it; a failed create simply never
  calls it, leaving the match's ``db_id`` ``None``;
- **defensiveness**: every entry point swallows persistence failures (a history
  write must never break multiplayer), and the ``match_db_id is None`` guard --
  the single place a create failure is absorbed by the dependent writes -- skips
  the game/disband write rather than pointing it at a match that was never
  recorded.

No database: a fake store records the calls it receives (and can be told to
raise), matching the injected-edge style the services layer tests use.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import app.packets
from app.packets import ScoreFrame
from app.repositories.mp_matches import MpMatch
from app.repositories.mp_matches import MpMatchGameScoreInput
from app.services.multiplayer import match_history
from app.services.multiplayer.match_history import ParticipantScoreFrame


def _raw_frame(
    *,
    total_score: int = 0,
    max_combo: int = 0,
    num300: int = 0,
    num100: int = 0,
    num50: int = 0,
    num_miss: int = 0,
    score_v2: bool = False,
) -> bytes:
    """A raw MATCH_SCORE_UPDATE frame body, as stashed on a slot."""
    sf = ScoreFrame(
        time=0,
        id=0,
        num300=num300,
        num100=num100,
        num50=num50,
        num_geki=0,
        num_katu=0,
        num_miss=num_miss,
        total_score=total_score,
        current_combo=0,
        max_combo=max_combo,
        perfect=False,
        current_hp=0,
        tag_byte=0,
        score_v2=score_v2,
    )
    return app.packets.write_scoreframe(sf)


class _FakeStore:
    """Records the history writes it receives; optionally raises on each."""

    def __init__(
        self,
        *,
        raise_on_create: bool = False,
        raise_on_game: bool = False,
        raise_on_disband: bool = False,
        raise_on_scores: bool = False,
    ) -> None:
        self._raise_on_create = raise_on_create
        self._raise_on_game = raise_on_game
        self._raise_on_disband = raise_on_disband
        self._raise_on_scores = raise_on_scores
        self.created: list[dict[str, Any]] = []
        self.games: list[dict[str, Any]] = []
        self.scoreboards: list[dict[str, Any]] = []
        self.disbanded: list[int] = []
        self._next_id = 100

    async def create_match(
        self,
        *,
        name: str,
        host_id: int,
        has_public_history: bool,
    ) -> MpMatch:
        if self._raise_on_create:
            raise RuntimeError("db down")
        self.created.append(
            {
                "name": name,
                "host_id": host_id,
                "has_public_history": has_public_history,
            },
        )
        match_id = self._next_id
        self._next_id += 1
        return MpMatch(
            id=match_id,
            name=name,
            host_id=host_id,
            has_public_history=has_public_history,
            created_at=datetime(2026, 8, 12, 20, 0, 0),
            disbanded_at=None,
        )

    async def record_game(self, **kwargs: Any) -> Any:
        if self._raise_on_game:
            raise RuntimeError("db down")
        self.games.append(kwargs)
        # a stable game id so the scoreboard write has something to key to.
        return SimpleNamespace(id=555, **kwargs)

    async def record_game_scores(
        self,
        *,
        game_id: int,
        win_condition: int,
        scores: list[MpMatchGameScoreInput],
    ) -> int:
        if self._raise_on_scores:
            raise RuntimeError("db down")
        self.scoreboards.append(
            {"game_id": game_id, "win_condition": win_condition, "scores": scores},
        )
        return len(scores)

    async def mark_disbanded(self, match_id: int) -> None:
        if self._raise_on_disband:
            raise RuntimeError("db down")
        self.disbanded.append(match_id)


def _collector() -> tuple[list[int], match_history.SetMatchDbId]:
    stashed: list[int] = []
    return stashed, stashed.append


# --- created ---------------------------------------------------------------


async def test_persist_match_created_hands_the_durable_id_back() -> None:
    store = _FakeStore()
    stashed, on_persisted = _collector()

    await match_history.persist_match_created(
        store,  # type: ignore[arg-type]
        on_persisted,
        name="lobby",
        host_id=3,
        has_public_history=True,
    )

    assert store.created == [
        {"name": "lobby", "host_id": 3, "has_public_history": True},
    ]
    # the durable id was handed back for the live match to stash.
    assert stashed == [100]


async def test_persist_match_created_swallows_failure_and_stashes_nothing() -> None:
    store = _FakeStore(raise_on_create=True)
    stashed, on_persisted = _collector()

    # a failed history write must not raise into the packet handler...
    await match_history.persist_match_created(
        store,  # type: ignore[arg-type]
        on_persisted,
        name="lobby",
        host_id=3,
        has_public_history=True,
    )

    # ...and leaves db_id unset, so dependent writes will skip themselves.
    assert stashed == []


# --- game completed --------------------------------------------------------


async def test_persist_game_completed_records_the_game() -> None:
    store = _FakeStore()
    started = datetime(2026, 8, 12, 19, 30, 0)

    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=100,
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

    assert len(store.games) == 1
    game = store.games[0]
    assert game["match_id"] == 100
    assert game["participants"] == [4, 5, 6]
    assert game["scrim"] is True
    assert game["started_at"] == started


async def test_persist_game_completed_skips_when_match_was_never_recorded() -> None:
    store = _FakeStore()

    # the parent create row never landed -> nothing to attach the game to.
    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=None,
        map_md5="a" * 32,
        map_id=42,
        map_name="map",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4],
    )

    assert store.games == []


async def test_persist_game_completed_swallows_a_write_failure() -> None:
    store = _FakeStore(raise_on_game=True)

    # must not raise into the completion handler the clients already heard about.
    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=100,
        map_md5="a" * 32,
        map_id=42,
        map_name="map",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4],
    )

    assert store.games == []


# --- game completed: per-player scoreboard ---------------------------------


async def test_persist_game_completed_records_the_scoreboard() -> None:
    store = _FakeStore()

    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=100,
        map_md5="a" * 32,
        map_id=42,
        map_name="map",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4, 5],
        score_frames=[
            ParticipantScoreFrame(
                user_id=4,
                team=0,
                mods=0,
                passed=True,
                raw_frame=_raw_frame(total_score=800_000, num300=300),
            ),
            ParticipantScoreFrame(
                user_id=5,
                team=0,
                mods=64,
                passed=True,
                raw_frame=_raw_frame(total_score=500_000, num300=200, num100=50),
            ),
        ],
    )

    assert len(store.scoreboards) == 1
    board = store.scoreboards[0]
    # the scoreboard is keyed to the game the store just recorded, and carries
    # the game's win condition through for placement.
    assert board["game_id"] == 555
    assert board["win_condition"] == 0
    decoded = {s.user_id: s for s in board["scores"]}
    assert decoded[4].score == 800_000
    assert decoded[5].mods == 64
    # accuracy is computed from the decoded frame (all 300s -> 100%).
    assert decoded[4].acc == 100.0


async def test_persist_game_completed_without_frames_writes_no_scoreboard() -> None:
    store = _FakeStore()

    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=100,
        map_md5="a" * 32,
        map_id=42,
        map_name="map",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4],
    )

    # the game still lands; there is simply no scoreboard to write.
    assert len(store.games) == 1
    assert store.scoreboards == []


async def test_persist_game_completed_drops_undecodable_frames() -> None:
    store = _FakeStore()

    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=100,
        map_md5="a" * 32,
        map_id=42,
        map_name="map",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4, 5],
        score_frames=[
            ParticipantScoreFrame(
                user_id=4,
                team=0,
                mods=0,
                passed=True,
                raw_frame=_raw_frame(total_score=800_000),
            ),
            # a truncated frame decodes to None -> that participant is dropped.
            ParticipantScoreFrame(
                user_id=5,
                team=0,
                mods=0,
                passed=True,
                raw_frame=b"\x00\x00\x00",
            ),
        ],
    )

    board = store.scoreboards[0]
    assert [s.user_id for s in board["scores"]] == [4]


async def test_persist_game_completed_all_frames_undecodable_writes_no_scoreboard() -> (
    None
):
    store = _FakeStore()

    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=100,
        map_md5="a" * 32,
        map_id=42,
        map_name="map",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[5],
        score_frames=[
            ParticipantScoreFrame(
                user_id=5,
                team=0,
                mods=0,
                passed=True,
                raw_frame=b"\x00",
            ),
        ],
    )

    # nothing decodable -> the game lands, but no empty scoreboard is written.
    assert len(store.games) == 1
    assert store.scoreboards == []


async def test_persist_game_completed_swallows_a_scoreboard_write_failure() -> None:
    store = _FakeStore(raise_on_scores=True)

    # a scoreboard write failure must not raise into the completion handler; the
    # game write already succeeded and the clients have been told.
    await match_history.persist_game_completed(
        store,  # type: ignore[arg-type]
        match_db_id=100,
        map_md5="a" * 32,
        map_id=42,
        map_name="map",
        mode=0,
        mods=0,
        win_condition=0,
        team_type=0,
        freemods=False,
        scrim=False,
        participants=[4],
        score_frames=[
            ParticipantScoreFrame(
                user_id=4,
                team=0,
                mods=0,
                passed=True,
                raw_frame=_raw_frame(total_score=800_000),
            ),
        ],
    )

    assert len(store.games) == 1
    assert store.scoreboards == []


# --- disbanded -------------------------------------------------------------


async def test_persist_match_disbanded_marks_the_match() -> None:
    store = _FakeStore()

    await match_history.persist_match_disbanded(store, match_db_id=100)  # type: ignore[arg-type]

    assert store.disbanded == [100]


async def test_persist_match_disbanded_skips_an_unrecorded_match() -> None:
    store = _FakeStore()

    await match_history.persist_match_disbanded(store, match_db_id=None)  # type: ignore[arg-type]

    assert store.disbanded == []


async def test_persist_match_disbanded_swallows_a_write_failure() -> None:
    store = _FakeStore(raise_on_disband=True)

    # a failed disband stamp must not raise into leave_match's teardown path.
    await match_history.persist_match_disbanded(store, match_db_id=100)  # type: ignore[arg-type]

    assert store.disbanded == []
