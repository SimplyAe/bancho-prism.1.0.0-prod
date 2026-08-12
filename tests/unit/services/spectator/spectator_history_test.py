"""Guards for the spectator-history producers.

These sit on the in-memory spectate lifecycle (``Player.add_spectator`` /
``Player.remove_spectator``) and write the durable ``spectator_sessions`` rows,
fire-and-forget. Two properties matter and are pinned here:

- **the id handoff**: a successful open hands the durable session id back through
  ``on_persisted`` so the live spectator can stash it; a failed open simply never
  calls it, leaving ``spectating_session_db_id`` ``None``;
- **defensiveness**: every entry point swallows persistence failures (a history
  write must never break spectating), and the ``session_db_id is None`` guard --
  the single place an open failure is absorbed by the close -- skips the close
  rather than pointing it at a session that was never recorded.

No database: a fake store records the calls it receives (and can be told to
raise), matching the injected-edge style the services layer tests use.
"""

from __future__ import annotations

from datetime import datetime

from app.repositories.spectator_sessions import SpectatorSession
from app.services.spectator import spectator_history

_NOW = datetime(2026, 8, 12, 20, 0, 0)


class _FakeStore:
    """Records the spectator-session writes it receives; optionally raises."""

    def __init__(
        self,
        *,
        raise_on_start: bool = False,
        raise_on_end: bool = False,
    ) -> None:
        self._raise_on_start = raise_on_start
        self._raise_on_end = raise_on_end
        self.started: list[dict[str, int]] = []
        self.ended: list[int] = []
        self._next_id = 100

    async def start_session(
        self,
        *,
        host_id: int,
        spectator_id: int,
    ) -> SpectatorSession:
        if self._raise_on_start:
            raise RuntimeError("db down")
        self.started.append({"host_id": host_id, "spectator_id": spectator_id})
        session_id = self._next_id
        self._next_id += 1
        return SpectatorSession(
            id=session_id,
            host_id=host_id,
            spectator_id=spectator_id,
            started_at=_NOW,
            ended_at=None,
        )

    async def end_session(self, session_id: int) -> None:
        if self._raise_on_end:
            raise RuntimeError("db down")
        self.ended.append(session_id)


def _collector() -> tuple[list[int], spectator_history.SetSessionDbId]:
    stashed: list[int] = []
    return stashed, stashed.append


# --- started ---------------------------------------------------------------


async def test_persist_spectator_started_hands_the_durable_id_back() -> None:
    store = _FakeStore()
    stashed, on_persisted = _collector()

    await spectator_history.persist_spectator_started(
        store,  # type: ignore[arg-type]
        on_persisted,
        host_id=3,
        spectator_id=7,
    )

    assert store.started == [{"host_id": 3, "spectator_id": 7}]
    # the durable id was handed back for the live spectator to stash.
    assert stashed == [100]


async def test_persist_spectator_started_swallows_failure_and_stashes_nothing() -> None:
    store = _FakeStore(raise_on_start=True)
    stashed, on_persisted = _collector()

    # a failed history write must not raise into add_spectator...
    await spectator_history.persist_spectator_started(
        store,  # type: ignore[arg-type]
        on_persisted,
        host_id=3,
        spectator_id=7,
    )

    # ...and leaves the stashed id unset, so the later close skips itself.
    assert stashed == []


# --- ended -----------------------------------------------------------------


async def test_persist_spectator_ended_closes_the_session() -> None:
    store = _FakeStore()

    await spectator_history.persist_spectator_ended(
        store,  # type: ignore[arg-type]
        session_db_id=100,
    )

    assert store.ended == [100]


async def test_persist_spectator_ended_skips_when_session_was_never_recorded() -> None:
    store = _FakeStore()

    # the open row never landed -> nothing to close.
    await spectator_history.persist_spectator_ended(
        store,  # type: ignore[arg-type]
        session_db_id=None,
    )

    assert store.ended == []


async def test_persist_spectator_ended_swallows_a_write_failure() -> None:
    store = _FakeStore(raise_on_end=True)

    # a failed close must not raise into remove_spectator's teardown path.
    await spectator_history.persist_spectator_ended(
        store,  # type: ignore[arg-type]
        session_db_id=100,
    )

    assert store.ended == []
