"""Guards for the flag-review seam.

``FlagReviewService`` is the one place the worker's per-result hook turns into a
durable review-queue entry, so it is where the **flag, never auto-ban** policy
is easiest to violate. These tests pin the mapping and the two deliberate
non-events:

- an *unflagged* (or reportless) result writes nothing and fires no metric --
  the overwhelming-majority path must be a cheap no-op;
- a flagged result records exactly the *strongest* signal (``triggered[0]``),
  carrying its code/title/confidence/detail/evidence, tagged with the report's
  rolled-up severity and how many detectors agreed, and fires the metric once;
- a flagged score whose owner can no longer be resolved (purged between analysis
  and now) is a benign skip -- ``None``, no write, no metric -- not an error.

Everything impure is injected, so there is no database and no Redis: a recording
fake stands in for the repository's ``record``, a closure resolves the owner, and
a counter captures the metric.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from app.repositories.anticheat_flags import AnticheatFlag
from app.repositories.anticheat_flags import AnticheatFlagStatus
from app.services.anticheat.detectors import DetectionReport
from app.services.anticheat.detectors import DetectionSignal
from app.services.anticheat.detectors import Severity
from app.services.anticheat.flag_review import FlagReviewService
from app.services.anticheat.replay_analysis import AnalysisOutcome
from app.services.anticheat.replay_analysis import AnalysisResult

_FIXED_TIME = datetime(2026, 8, 11, 12, 0, 0)


def _signal(
    *,
    code: str,
    severity: Severity,
    confidence: float,
    flagged: bool,
    title: str = "signal",
    detail: str = "detail",
    evidence: dict[str, float] | None = None,
) -> DetectionSignal:
    return DetectionSignal(
        code=code,
        title=title,
        severity=severity,
        confidence=confidence,
        flagged=flagged,
        detail=detail,
        evidence=evidence if evidence is not None else {},
    )


def _report(signals: tuple[DetectionSignal, ...]) -> DetectionReport:
    flagged = any(s.flagged for s in signals)
    severity = max((s.severity for s in signals), key=list(Severity).index, default=Severity.NONE)
    return DetectionReport(signals=signals, flagged=flagged, severity=severity)


def _result(report: DetectionReport | None, *, score_id: int = 5, mode: int = 0) -> AnalysisResult:
    return AnalysisResult(
        score_id=score_id,
        mode=mode,
        outcome=AnalysisOutcome.ANALYZED,
        report=report,
    )


class _RecordingRecorder:
    """Captures a `record` call and returns a canned persisted flag."""

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []

    async def __call__(
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
    ) -> AnticheatFlag:
        self.calls.append(
            SimpleNamespace(
                score_id=score_id,
                user_id=user_id,
                mode=mode,
                severity=severity,
                top_signal_code=top_signal_code,
                top_signal_title=top_signal_title,
                confidence=confidence,
                triggered_count=triggered_count,
                detail=detail,
                evidence=evidence,
            ),
        )
        return AnticheatFlag(
            score_id=score_id,
            user_id=user_id,
            mode=mode,
            status=AnticheatFlagStatus.OPEN,
            severity=severity,
            top_signal_code=top_signal_code,
            top_signal_title=top_signal_title,
            confidence=confidence,
            triggered_count=triggered_count,
            detail=detail,
            evidence=dict(evidence),
            first_flagged_at=_FIXED_TIME,
            last_flagged_at=_FIXED_TIME,
            resolved_by=None,
            resolved_at=None,
            resolution_note=None,
        )


class _Counter:
    def __init__(self) -> None:
        self.severities: list[str] = []

    def __call__(self, severity: str) -> None:
        self.severities.append(severity)


def _resolver(user_id: int | None) -> Any:
    async def _resolve(score_id: int) -> int | None:
        return user_id

    return _resolve


def _service(
    *,
    recorder: _RecordingRecorder,
    owner: int | None,
    counter: _Counter,
) -> FlagReviewService:
    return FlagReviewService(
        record_flag=recorder,  # type: ignore[arg-type]
        fetch_score_user=_resolver(owner),  # type: ignore[arg-type]
        on_flag_recorded=counter,  # type: ignore[arg-type]
    )


async def test_unflagged_result_records_nothing() -> None:
    recorder, counter = _RecordingRecorder(), _Counter()
    service = _service(recorder=recorder, owner=42, counter=counter)

    report = _report((_signal(code="B1", severity=Severity.LOW, confidence=0.4, flagged=False),))
    outcome = await service.observe(_result(report))

    assert outcome is None
    assert recorder.calls == []
    assert counter.severities == []


async def test_result_without_a_report_records_nothing() -> None:
    recorder, counter = _RecordingRecorder(), _Counter()
    service = _service(recorder=recorder, owner=42, counter=counter)

    outcome = await service.observe(_result(None))

    assert outcome is None
    assert recorder.calls == []
    assert counter.severities == []


async def test_flagged_result_records_the_strongest_signal_and_counts_it() -> None:
    recorder, counter = _RecordingRecorder(), _Counter()
    service = _service(recorder=recorder, owner=42, counter=counter)

    # two flagged signals; the HIGH one must be the persisted top signal, and the
    # count must reflect that two detectors agreed.
    weaker = _signal(
        code="B1_HOLD_DURATION",
        severity=Severity.MEDIUM,
        confidence=0.7,
        flagged=True,
        title="Hold-duration uniformity",
        detail="holds barely vary",
        evidence={"hold_cov": 0.02},
    )
    stronger = _signal(
        code="B3_AIM_CONTROLLER",
        severity=Severity.HIGH,
        confidence=0.9,
        flagged=True,
        title="Aim-controller cleanliness",
        detail="jump aim is too clean",
        evidence={"aim_machineness": 0.95},
    )
    report = _report((weaker, stronger))

    flag = await service.observe(_result(report, score_id=77, mode=2))

    assert flag is not None
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call.score_id == 77
    assert call.user_id == 42  # from the resolver, not the report
    assert call.mode == 2
    assert call.severity == "high"  # the report's rolled-up severity
    assert call.top_signal_code == "B3_AIM_CONTROLLER"  # strongest, not first
    assert call.top_signal_title == "Aim-controller cleanliness"
    assert call.confidence == 0.9
    assert call.triggered_count == 2  # both flagged signals counted
    assert call.detail == "jump aim is too clean"
    assert call.evidence == {"aim_machineness": 0.95}
    assert counter.severities == ["high"]  # metric fired once, keyed by severity


async def test_flagged_result_for_a_purged_owner_is_a_benign_skip() -> None:
    recorder, counter = _RecordingRecorder(), _Counter()
    # resolver returns None: the score was purged between analysis and now.
    service = _service(recorder=recorder, owner=None, counter=counter)

    report = _report((_signal(code="B3", severity=Severity.HIGH, confidence=0.9, flagged=True),))
    outcome = await service.observe(_result(report))

    assert outcome is None  # skipped, not raised
    assert recorder.calls == []  # nothing written for a gone account
    assert counter.severities == []
