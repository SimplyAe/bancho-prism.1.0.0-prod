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

from app.repositories.mp_matches import MpMatch
from app.services.multiplayer import match_history


class _FakeStore:
    """Records the history writes it receives; optionally raises on each."""

    def __init__(
        self,
        *,
        raise_on_create: bool = False,
        raise_on_game: bool = False,
        raise_on_disband: bool = False,
    ) -> None:
        self._raise_on_create = raise_on_create
        self._raise_on_game = raise_on_game
        self._raise_on_disband = raise_on_disband
        self.created: list[dict[str, Any]] = []
        self.games: list[dict[str, Any]] = []
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
            {"name": name, "host_id": host_id, "has_public_history": has_public_history},
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
        return SimpleNamespace(id=1, **kwargs)

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
