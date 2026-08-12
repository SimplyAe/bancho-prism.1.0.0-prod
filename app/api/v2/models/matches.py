from __future__ import annotations

from datetime import datetime

from . import BaseModel

# output models


class MatchModel(BaseModel):
    """One multiplayer lobby's durable record, mirroring the repository's
    :class:`app.repositories.mp_matches.MpMatch`.

    ``disbanded_at`` is null while the lobby is still open (or was still open
    when the server last ran); a non-null value marks a lobby that has torn
    down. ``has_public_history`` is the ``//private`` flag -- private lobbies are
    absent from the public listing entirely, so a client only sees this ``true``
    there, but it is surfaced for the host/staff views that can read a private
    match directly.
    """

    id: int
    name: str
    host_id: int
    has_public_history: bool
    created_at: datetime
    disbanded_at: datetime | None


class MatchGameModel(BaseModel):
    """One completed game within a lobby, mirroring the repository's
    :class:`app.repositories.mp_matches.MpMatchGame`.

    The map / mode / mods / win-condition / team-type are the snapshot taken at
    MATCH_COMPLETE, so the record stays faithful even after the lobby moved on
    to another map. ``participants`` is the list of user ids that played, with
    ``participant_count`` denormalised alongside so a client need not count.
    ``ended_at`` is null only for a game whose completion went unrecorded.
    """

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
