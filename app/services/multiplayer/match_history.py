"""Turning the multiplayer match lifecycle into durable history rows.

The live ``Match`` object (``app.objects.match``) is memory-only: it is created
on a lobby open, mutated through the match's life, and dropped when the last
player leaves. These producers sit on that lifecycle and write the durable
record (``mp_matches`` / ``mp_match_games``) as the events happen, so history
survives a restart and outlives the lobby.

Three moments, mirroring the packet handlers that call them:

- **created** -- a lobby opened. We insert an ``mp_matches`` row and hand the
  durable id back through ``on_persisted`` so the live match can stash it
  (``Match.db_id``) for its game rows to reference.
- **game completed** -- a map ran to MATCH_COMPLETE. We insert an
  ``mp_match_games`` row snapshotting the map/mode/mods and who played.
- **disbanded** -- the lobby tore down. We stamp ``disbanded_at``.

Every entry point is fire-and-forget and defensive, exactly like the snapshot
producers: it runs off a packet handler, never raises into it, and swallows (with
a log) any persistence failure -- multiplayer must keep working whether or not
the history write succeeds. The ``match_db_id is None`` guard lives here, in one
tested place, rather than in each handler: if the create row never landed (it
failed, or a game somehow completed in the instant before the insert returned),
the dependent game/disband writes are skipped rather than pointed at a missing
match.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.logging import Ansi
from app.logging import log
from app.packets import decode_scoreframe
from app.packets import scoreframe_accuracy
from app.repositories.mp_matches import MpMatch
from app.repositories.mp_matches import MpMatchGame
from app.repositories.mp_matches import MpMatchGameScoreInput

# Called with the freshly-created durable match id so the live ``Match`` can
# stash it. A Callable side effect injected per the services-layer convention;
# defaults to a no-op at the wiring site when the caller does not care.
SetMatchDbId = Callable[[int], None]


@dataclass(frozen=True, slots=True)
class ParticipantScoreFrame:
    """A single participant's raw final frame, captured at MATCH_COMPLETE.

    The packet handler stashes the last MATCH_SCORE_UPDATE frame verbatim on the
    slot (unparsed, on the hot path) and snapshots one of these per participant
    when the game completes -- raw bytes plus the slot facts the frame itself
    does not carry (who, which team, effective mods, whether they passed). The
    producer decodes ``raw_frame`` once, off the handler, to build the scoreboard
    row; a frame that fails to decode drops that participant rather than raising.
    """

    user_id: int
    team: int
    mods: int
    passed: bool
    raw_frame: bytes


def _decode_participant_scores(
    frames: list[ParticipantScoreFrame],
    *,
    mode_vn: int,
) -> list[MpMatchGameScoreInput]:
    """Decode captured raw frames into scoreboard inputs, dropping bad frames.

    Accuracy is computed here from the decoded frame's hit counts (a multiplayer
    frame is not a submitted ``scores`` row, so it has no accuracy of its own).
    A frame that fails to decode -- truncated or malformed client bytes -- is
    silently omitted; its participant simply does not appear on the scoreboard.
    """
    inputs: list[MpMatchGameScoreInput] = []
    for frame in frames:
        sf = decode_scoreframe(frame.raw_frame)
        if sf is None:
            continue
        inputs.append(
            MpMatchGameScoreInput(
                user_id=frame.user_id,
                team=frame.team,
                mods=frame.mods,
                score=sf.total_score,
                max_combo=sf.max_combo,
                num300=sf.num300,
                num100=sf.num100,
                num50=sf.num50,
                num_geki=sf.num_geki,
                num_katu=sf.num_katu,
                num_miss=sf.num_miss,
                acc=scoreframe_accuracy(sf, mode_vn),
                perfect=sf.perfect,
                passed=frame.passed,
            ),
        )
    return inputs


class MpMatchHistoryStore(Protocol):
    """Structural type of the ``mp_matches`` repository's write surface.

    Declared as a Protocol so these producers need no database to test, matching
    the injected-edge style the rest of the services layer uses.
    """

    async def create_match(
        self,
        *,
        name: str,
        host_id: int,
        has_public_history: bool,
    ) -> MpMatch: ...

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
    ) -> MpMatchGame: ...

    async def record_game_scores(
        self,
        *,
        game_id: int,
        win_condition: int,
        scores: list[MpMatchGameScoreInput],
    ) -> int: ...

    async def mark_disbanded(self, match_id: int) -> None: ...


async def persist_match_created(
    store: MpMatchHistoryStore,
    on_persisted: SetMatchDbId,
    *,
    name: str,
    host_id: int,
    has_public_history: bool,
) -> None:
    """Record a newly-opened lobby, handing the durable id back via ``on_persisted``.

    Fire-and-forget off the CREATE_MATCH handler: a failed write is logged and
    swallowed, and ``on_persisted`` is simply never called, so the live match's
    ``db_id`` stays ``None`` and its later game/disband writes skip themselves
    rather than target a match row that does not exist.
    """
    try:
        created = await store.create_match(
            name=name,
            host_id=host_id,
            has_public_history=has_public_history,
        )
        on_persisted(created.id)
    except Exception as exc:  # never let a history write escape into the handler.
        log(f"Failed to persist multiplayer match {name!r}: {exc!r}", Ansi.LYELLOW)


async def persist_game_completed(
    store: MpMatchHistoryStore,
    *,
    match_db_id: int | None,
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
    score_frames: list[ParticipantScoreFrame] | None = None,
) -> None:
    """Record one completed game within a lobby, and its per-player scoreboard.

    Skips silently when ``match_db_id`` is ``None`` (the parent match row never
    landed): a game with nowhere to attach is dropped rather than orphaned. Any
    write failure is logged and swallowed.

    ``score_frames`` are the raw final frames captured off the slots at
    completion; each is decoded once here and written as a child scoreboard row
    keyed to the game just recorded. The scoreboard write is nested in the same
    try as the game write, so a scoreboard failure is logged and swallowed too --
    a game with no scoreboard is still a valid history row, never a lost game.
    """
    if match_db_id is None:
        return

    try:
        game = await store.record_game(
            match_id=match_db_id,
            map_md5=map_md5,
            map_id=map_id,
            map_name=map_name,
            mode=mode,
            mods=mods,
            win_condition=win_condition,
            team_type=team_type,
            freemods=freemods,
            scrim=scrim,
            participants=participants,
            started_at=started_at,
            ended_at=ended_at,
        )

        if score_frames:
            scores = _decode_participant_scores(
                score_frames,
                mode_vn=mode % 4,
            )
            if scores:
                await store.record_game_scores(
                    game_id=game.id,
                    win_condition=win_condition,
                    scores=scores,
                )
    except Exception as exc:  # never let a history write escape into the handler.
        log(
            f"Failed to persist multiplayer game for match {match_db_id}: {exc!r}",
            Ansi.LYELLOW,
        )


async def persist_match_disbanded(
    store: MpMatchHistoryStore,
    *,
    match_db_id: int | None,
) -> None:
    """Stamp a lobby's teardown time.

    Skips silently when ``match_db_id`` is ``None`` (nothing was ever recorded
    for this lobby). Any write failure is logged and swallowed.
    """
    if match_db_id is None:
        return

    try:
        await store.mark_disbanded(match_db_id)
    except Exception as exc:  # never let a history write escape into the handler.
        log(
            f"Failed to mark multiplayer match {match_db_id} disbanded: {exc!r}",
            Ansi.LYELLOW,
        )
