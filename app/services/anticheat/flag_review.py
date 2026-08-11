"""Turn a flagged analysis result into a durable review-queue entry.

This is the seam between the worker's per-result hook and the staff review
queue (``app/repositories/anticheat_flags.py``). The worker analyses a score and
hands the :class:`AnalysisResult` here; if the detector report is flagged, this
service maps its strongest signal onto a persisted flag record. Everything
impure is an injected callable, in the frozen-dataclass style the rest of the
codebase uses, so it is unit-testable with no database and no Redis.

The policy is unchanged and worth restating at the boundary where it would be
easiest to violate: **flag, never auto-ban**. Persisting a flag surfaces a score
to a human; it does not restrict, ban, or otherwise punish the player. Nothing
here reads or writes player privileges.

Two deliberate non-events:

- an *unflagged* result (the overwhelming majority) is a fast no-op -- there is
  nothing for a reviewer to see, so nothing is written;
- a flagged score whose owner can no longer be resolved (the score was purged
  between analysis and this call) is skipped rather than erroring. The flag
  table denormalises ``user_id`` and a flag for a deleted account is moot, so a
  missing owner is a benign skip, consistent with foreign keys being an
  application concern here rather than a DB constraint.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from app.repositories.anticheat_flags import AnticheatFlag
from app.services.anticheat.replay_analysis import AnalysisResult


class FlagRecorder(Protocol):
    """Persist (upsert) one flagged score; mirrors ``AnticheatFlagsRepository.record``."""

    def __call__(
        self,
        score_id: int,
        *,
        user_id: int,
        mode: int,
        severity: str,
        top_signal_code: str,
        top_signal_title: str,
        confidence: float,
        triggered_count: int,
        detail: str,
        evidence: dict[str, float],
    ) -> Awaitable[AnticheatFlag]: ...


class ScoreUserResolver(Protocol):
    """Resolve the owning user id for a score, or ``None`` if it is gone."""

    def __call__(self, score_id: int) -> Awaitable[int | None]: ...


class FlagCounter(Protocol):
    """Side-effect hook for the flag-recorded metric, keyed by severity."""

    def __call__(self, severity: str) -> None: ...


def _ignore_flag(severity: str) -> None:
    return None


@dataclass(frozen=True)
class FlagReviewService:
    record_flag: FlagRecorder
    fetch_score_user: ScoreUserResolver
    on_flag_recorded: FlagCounter = _ignore_flag

    async def observe(self, result: AnalysisResult) -> AnticheatFlag | None:
        """Persist a review-queue entry if this result is flagged.

        Returns the persisted flag, or ``None`` when there was nothing to record
        (the report was absent or unflagged, or the score's owner could not be
        resolved). Never raises for an unflagged result and never punishes a
        player -- see the module docstring.
        """
        report = result.report
        if report is None or not report.flagged or not report.triggered:
            return None

        user_id = await self.fetch_score_user(result.score_id)
        if user_id is None:
            # the score was purged between analysis and now; a flag for a gone
            # account is moot, so skip rather than fail the write.
            return None

        top = report.triggered[0]
        flag = await self.record_flag(
            result.score_id,
            user_id=user_id,
            mode=result.mode,
            severity=report.severity.value,
            top_signal_code=top.code,
            top_signal_title=top.title,
            confidence=top.confidence,
            triggered_count=len(report.triggered),
            detail=top.detail,
            evidence=dict(top.evidence),
        )
        self.on_flag_recorded(report.severity.value)
        return flag
