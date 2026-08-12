"""Guards for the spectator-history read service.

Unlike the match-history reader, this service enforces no visibility rule:
spectating is an inherently public relationship in osu! (host and watchers see
each other in real time), so a spectate session carries no privacy flag. What
the service owns is the *shape* of the reads, and that is what these tests pin:

- ``fetch_sessions`` threads its two independently-composable filters
  (``host_id`` / ``spectator_id``) and the keyset paging args to the repository
  untouched -- no viewer identity is invented or required;
- ``fetch_session`` passes an id straight through and answers ``None`` for an
  unknown one, so the HTTP layer can 404 rather than conflate states.

No database: a fake repository returns canned rows and records how it was
queried, matching the injected-edge style the services layer tests use.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.repositories.spectator_sessions import SpectatorSession
from app.services.spectator.spectator_history_reader import SpectatorHistoryService

_NOW = datetime(2026, 8, 12, 20, 0, 0)


def _session(
    session_id: int, *, host_id: int = 3, spectator_id: int = 7
) -> SpectatorSession:
    return SpectatorSession(
        id=session_id,
        host_id=host_id,
        spectator_id=spectator_id,
        started_at=_NOW,
        ended_at=None,
    )


class _FakeSessionsRepository:
    """Returns canned session rows and records the queries it received."""

    def __init__(self) -> None:
        self.sessions_by_id: dict[int, SpectatorSession] = {}
        self.fetch_calls: list[SimpleNamespace] = []
        self.fetch_result: list[SpectatorSession] = []

    async def fetch_sessions(
        self,
        *,
        host_id: int | None = None,
        spectator_id: int | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[SpectatorSession]:
        self.fetch_calls.append(
            SimpleNamespace(
                host_id=host_id,
                spectator_id=spectator_id,
                before_id=before_id,
                limit=limit,
            ),
        )
        return self.fetch_result

    async def fetch_session(self, session_id: int) -> SpectatorSession | None:
        return self.sessions_by_id.get(session_id)


def _service() -> tuple[SpectatorHistoryService, _FakeSessionsRepository]:
    repository = _FakeSessionsRepository()
    return SpectatorHistoryService(sessions=repository), repository  # type: ignore[arg-type]


# --- listing: filters + cursor threaded, no visibility gate ----------------


async def test_fetch_sessions_threads_filters_and_paging_through() -> None:
    service, repository = _service()
    repository.fetch_result = [_session(3), _session(2)]

    result = await service.fetch_sessions(
        host_id=3,
        spectator_id=7,
        before_id=10,
        limit=25,
    )

    assert [s.id for s in result] == [3, 2]
    # every argument is threaded to the repository untouched -- the reader adds
    # no filtering of its own (spectating is public).
    call = repository.fetch_calls[0]
    assert (call.host_id, call.spectator_id) == (3, 7)
    assert (call.before_id, call.limit) == (10, 25)


async def test_fetch_sessions_defaults_to_the_global_feed() -> None:
    service, repository = _service()

    await service.fetch_sessions()

    # no filters -> both are None (the whole feed), with the default page size.
    call = repository.fetch_calls[0]
    assert (call.host_id, call.spectator_id, call.before_id) == (None, None, None)
    assert call.limit == 50


# --- single session: passthrough, None on unknown --------------------------


async def test_fetch_session_returns_a_known_session() -> None:
    service, repository = _service()
    repository.sessions_by_id[5] = _session(5)

    session = await service.fetch_session(5)

    assert session is not None
    assert session.id == 5


async def test_fetch_session_returns_none_for_an_unknown_id() -> None:
    service, _repository = _service()

    assert await service.fetch_session(404) is None
