from __future__ import annotations

from datetime import datetime

from . import BaseModel

# output models


class SpectatorSessionModel(BaseModel):
    """One spectate session's durable record, mirroring the repository's
    :class:`app.repositories.spectator_sessions.SpectatorSession`.

    ``host_id`` is the player who was being watched, ``spectator_id`` the one
    watching. ``ended_at`` is null while the session is still open (or was still
    open when the server last ran -- a session whose close went unrecorded, e.g.
    the process died mid-spectate); a non-null value marks a session that closed
    cleanly.
    """

    id: int
    host_id: int
    spectator_id: int
    started_at: datetime
    ended_at: datetime | None
