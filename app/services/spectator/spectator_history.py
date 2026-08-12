"""Turning the spectator relationship into durable history rows.

Spectating in stock bancho.py is memory-only: when a viewer starts watching a
host, ``Player.add_spectator`` appends them to ``self.spectators`` and sets
``player.spectating = self`` (``app.objects.player``); ``remove_spectator``
undoes it. Both sides are live attributes that exist only for the connection
and are gone on logout or restart. These producers sit on that lifecycle and
write the durable record (``spectator_sessions``) as the events happen, so the
history of who watched whom survives the session.

Two moments, mirroring the ``Player`` methods that call them:

- **started** -- a viewer began spectating a host. We insert a
  ``spectator_sessions`` row and hand the durable id back through
  ``on_persisted`` so the live spectator can stash it (on
  ``Player.spectating_session_db_id``) for the matching close.
- **ended** -- the viewer stopped (or logged out, which stops it for them). We
  stamp ``ended_at`` on the stashed session id.

Every entry point is fire-and-forget and defensive, exactly like the match and
snapshot producers: it runs off a (synchronous) ``Player`` method via
``spawn_background_task``, never raises into it, and swallows (with a log) any
persistence failure -- spectating must keep working whether or not the history
write succeeds. The ``session_db_id is None`` guard lives here, in one tested
place, rather than in the caller: if the open row never landed (it failed, or a
stop somehow arrived in the instant before the insert returned), the dependent
close is skipped rather than pointed at a session that does not exist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.logging import Ansi
from app.logging import log
from app.repositories.spectator_sessions import SpectatorSession

# Called with the freshly-created durable session id so the live spectator can
# stash it. A Callable side effect injected per the services-layer convention;
# defaults to a no-op at the wiring site when the caller does not care.
SetSessionDbId = Callable[[int], None]


class SpectatorHistoryStore(Protocol):
    """Structural type of the ``spectator_sessions`` repository's write surface.

    Declared as a Protocol so these producers need no database to test, matching
    the injected-edge style the rest of the services layer uses.
    """

    async def start_session(
        self,
        *,
        host_id: int,
        spectator_id: int,
    ) -> SpectatorSession: ...

    async def end_session(self, session_id: int) -> None: ...


async def persist_spectator_started(
    store: SpectatorHistoryStore,
    on_persisted: SetSessionDbId,
    *,
    host_id: int,
    spectator_id: int,
) -> None:
    """Record a spectate session opening, handing the durable id back via ``on_persisted``.

    Fire-and-forget off ``Player.add_spectator``: a failed write is logged and
    swallowed, and ``on_persisted`` is simply never called, so the live
    spectator's ``spectating_session_db_id`` stays ``None`` and its later close
    skips itself rather than targeting a session row that does not exist.
    """
    try:
        session = await store.start_session(
            host_id=host_id,
            spectator_id=spectator_id,
        )
        on_persisted(session.id)
    except Exception as exc:  # never let a history write escape into the handler.
        log(
            f"Failed to persist spectator session "
            f"({spectator_id} watching {host_id}): {exc!r}",
            Ansi.LYELLOW,
        )


async def persist_spectator_ended(
    store: SpectatorHistoryStore,
    *,
    session_db_id: int | None,
) -> None:
    """Stamp a spectate session's end time.

    Skips silently when ``session_db_id`` is ``None`` (the open row never landed,
    so there is nothing to close). Any write failure is logged and swallowed.
    """
    if session_db_id is None:
        return

    try:
        await store.end_session(session_db_id)
    except Exception as exc:  # never let a history write escape into the handler.
        log(
            f"Failed to close spectator session {session_db_id}: {exc!r}",
            Ansi.LYELLOW,
        )
