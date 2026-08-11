"""Orchestration for one replay's analysis, end to end.

This is the seam that ties the pure anticheat pipeline to durable storage. The
pipeline itself -- ``parse_replay_frame_data -> extract_features ->
run_detectors`` -- is pure and lives in ``app/adapters`` and
``app/services/anticheat``; this service supplies the two impure edges (reading
the ``.osr`` off disk, writing the outcome to ``score_replay_stats``) as
injected callables, so the whole thing is unit-testable with no filesystem and
no database, in the frozen-dataclass style the rest of the codebase uses.

What one analysis does, and why each terminal state exists:

- **replay on disk missing** -> ``mark_replay_missing``. The durability reorder
  in score submission means a ``scores`` row can outlive a replay that never
  landed. Marking it terminal-but-missing stops the queue retrying a file that
  will never appear, while leaving the fact recorded for a reviewer.
- **replay present but undecodable / analysis raises** -> ``mark_error``. This
  is a transient class (a truncated file that a re-upload could fix, a bug in a
  new extractor): the row stays re-runnable so a later pass can retry it.
- **analysis succeeds** -> ``mark_analyzed`` with the full feature document and
  the extractor version, so detectors can be re-run and thresholds re-derived
  without re-reading the ``.osr``.

The detector report is computed and attached to the result for the caller (the
worker logs a flag; a later staff-review-queue track consumes it), but the
policy remains **flag, never auto-ban** -- nothing here restricts a player.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.adapters.osr_replay import ReplayParseError
from app.adapters.osr_replay import parse_replay_frame_data
from app.services.anticheat.detectors import DEFAULT_CONFIG
from app.services.anticheat.detectors import DetectionReport
from app.services.anticheat.detectors import DetectorConfig
from app.services.anticheat.detectors import run_detectors
from app.services.anticheat.features import EXTRACTOR_VERSION
from app.services.anticheat.features import extract_features
from app.services.anticheat.features import features_to_dict


class ReplayBytesReader(Protocol):
    """Read a stored replay's raw frame bytes, or ``None`` if absent.

    Absent (``None``) and empty are distinct: a missing file is the
    ``replay_missing`` terminal state, whereas empty bytes are decoded like any
    other (short) replay.
    """

    def __call__(self, score_id: int) -> Awaitable[bytes | None]: ...


class AnalysisRecorder(Protocol):
    """Persist a terminal analysis outcome for one score (upsert)."""

    async def mark_analyzed(
        self,
        score_id: int,
        *,
        mode: int,
        extractor_version: int,
        features: dict[str, object],
    ) -> object: ...

    async def mark_replay_missing(
        self,
        score_id: int,
        *,
        mode: int,
    ) -> object: ...

    async def mark_error(
        self,
        score_id: int,
        *,
        mode: int,
        error_detail: str,
    ) -> object: ...


class AnalysisOutcome(StrEnum):
    """Which terminal state one analysis reached; mirrors the stored status."""

    ANALYZED = "analyzed"
    REPLAY_MISSING = "replay_missing"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """What one ``analyze_score`` call did, for the worker to log/act on.

    ``report`` is present only when the replay was analysed; it is the advisory
    detector output (``report.flagged`` means "surface to staff", never "ban").
    """

    score_id: int
    mode: int
    outcome: AnalysisOutcome
    report: DetectionReport | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class ReplayAnalysisService:
    read_replay_bytes: ReplayBytesReader
    stats: AnalysisRecorder
    config: DetectorConfig = DEFAULT_CONFIG

    async def analyze_score(self, score_id: int, *, mode: int) -> AnalysisResult:
        """Run the full pipeline for one score and persist the outcome.

        Never raises for a bad replay: a decode failure is caught and recorded
        as ``error`` (re-runnable) so one corrupt file cannot kill the worker
        loop. A genuinely unexpected error (e.g. the recorder itself failing)
        does propagate -- that is the supervised loop's job to back off on.
        """
        replay_bytes = await self.read_replay_bytes(score_id)
        if replay_bytes is None:
            await self.stats.mark_replay_missing(score_id, mode=mode)
            return AnalysisResult(
                score_id=score_id,
                mode=mode,
                outcome=AnalysisOutcome.REPLAY_MISSING,
            )

        try:
            replay = parse_replay_frame_data(replay_bytes, mode=mode)
            features = extract_features(replay)
        except ReplayParseError as exc:
            detail = f"replay decode failed: {exc}"
            await self.stats.mark_error(score_id, mode=mode, error_detail=detail)
            return AnalysisResult(
                score_id=score_id,
                mode=mode,
                outcome=AnalysisOutcome.ERROR,
                error_detail=detail,
            )

        report = run_detectors(features, self.config)
        await self.stats.mark_analyzed(
            score_id,
            mode=mode,
            extractor_version=EXTRACTOR_VERSION,
            features=features_to_dict(features),
        )
        return AnalysisResult(
            score_id=score_id,
            mode=mode,
            outcome=AnalysisOutcome.ANALYZED,
            report=report,
        )
