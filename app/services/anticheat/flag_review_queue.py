"""The staff-facing side of the review queue.

``app/services/anticheat/flag_review.py`` is the *worker* seam: it turns a
flagged ``AnalysisResult`` into a durable row. This module is the other end --
the seam a staff member reaches through when they list what has been flagged and
record a decision on it. It is deliberately thin: a service over the
``AnticheatFlagsRepository`` that mirrors ``WebSessionsService`` and
``PlayersService`` (concrete repository injected, not ``Protocol`` callables --
that pattern is reserved for the pure detector pipeline), so the HTTP layer never
touches the repository directly and the paging/counting decision lives in one
place.

Nothing here can ban a player. ``resolve_flag`` records *a human's* verdict
(``resolved_by`` is always the acting staff member's id) and never mutates the
player, exactly per **flag, never auto-ban**. A resolve on a score that was never
flagged -- or was purged since -- surfaces as ``None`` rather than conjuring a
row, so the HTTP layer can answer 404 instead of silently creating a flag with no
detection behind it.

A *terminal* verdict (``ACTIONED`` / ``DISMISSED``, not the interim
``REVIEWING`` claim) is also written to the server's ``logs`` table -- the same
moderation trail a restrict or silence lands in. This is the "audit law" wiring:
an anticheat decision becomes visible next to every other staff action, so a
review is auditable after the fact. It records the decision only; it does not
touch privileges (restriction remains a separate, explicit human action). The
log is written *after* a successful resolve, mirroring ``Player.restrict``'s
mutate-then-log order, so a resolution that did not happen never leaves a
phantom audit entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.repositories.anticheat_flags import AnticheatFlag
from app.repositories.anticheat_flags import AnticheatFlagStatus
from app.repositories.anticheat_flags import AnticheatFlagsRepository
from app.repositories.logs import LogsRepository

# the statuses that represent a decision worth auditing. `REVIEWING` is a claim,
# not a verdict, so it is deliberately excluded -- claiming a flag should not
# spam the moderation log.
_TERMINAL_STATUSES = frozenset(
    {AnticheatFlagStatus.ACTIONED, AnticheatFlagStatus.DISMISSED},
)

# `logs.msg` is varchar(2048); never let a long note fail the audit write.
_AUDIT_MSG_MAX_LEN = 2048


@dataclass(frozen=True)
class FlagListing:
    """One page of the review queue plus the full filtered count.

    ``total`` is the count of the *same filtered set*, not just this page, so the
    UI can render "page 2 of N" without a second round-trip -- the same shape as
    ``PlayersListing``.
    """

    flags: list[AnticheatFlag]
    total: int


@dataclass(frozen=True)
class AnticheatReviewService:
    flags: AnticheatFlagsRepository
    logs: LogsRepository

    async def list_flags(
        self,
        *,
        status: AnticheatFlagStatus | None = None,
        mode: int | None = None,
        user_id: int | None = None,
        page: int,
        page_size: int,
    ) -> FlagListing:
        """A page of flags (newest re-flag first) and the total for the filter."""
        flags = await self.flags.fetch_many(
            status=status,
            mode=mode,
            user_id=user_id,
            page=page,
            page_size=page_size,
        )
        total = await self.flags.fetch_count(
            status=status,
            mode=mode,
            user_id=user_id,
        )
        return FlagListing(flags=flags, total=total)

    async def fetch_flag(self, score_id: int) -> AnticheatFlag | None:
        """One flag by score id, or None if the score was never flagged."""
        return await self.flags.fetch_one(score_id)

    async def resolve_flag(
        self,
        score_id: int,
        *,
        status: AnticheatFlagStatus,
        resolved_by: int,
        note: str | None = None,
    ) -> AnticheatFlag | None:
        """Record a staff decision; None when there is no such flag.

        ``resolved_by`` is the acting staff member -- this is the only path that
        writes a resolution, and it writes one a human chose. A terminal verdict
        is additionally written to the moderation ``logs`` trail (see module
        docstring); the log is written only after a resolution actually happened,
        and never restricts the player.
        """
        flag = await self.flags.resolve(
            score_id,
            status=status,
            resolved_by=resolved_by,
            note=note,
        )
        if flag is None:
            return None

        if status in _TERMINAL_STATUSES:
            await self.logs.create(
                _from=resolved_by,
                to=flag.user_id,
                action=f"ac_flag_{status.value}",
                msg=_audit_message(flag, note),
            )

        return flag


def _audit_message(flag: AnticheatFlag, note: str | None) -> str:
    """A one-line audit summary of the decided flag, plus the staff note.

    Kept human-readable and self-contained (score id, severity, top signal) so a
    reviewer scanning the moderation log sees what was decided without opening
    the flag; truncated to the ``logs.msg`` width so it can never fail the write.
    """
    summary = f"score {flag.score_id} [{flag.severity}/{flag.top_signal_code}]"
    message = f"{summary}: {note}" if note else summary
    return message[:_AUDIT_MSG_MAX_LEN]

