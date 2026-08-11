"""Guards for the replay-analysis orchestration service.

This service is the seam between the pure anticheat pipeline
(``parse_replay_frame_data -> extract_features -> run_detectors``) and durable
storage. It owns the mapping from "what happened to one replay" to a terminal
``score_replay_stats`` state, and these tests pin exactly that mapping -- the
three outcomes and the one deliberate *non*-outcome:

- a replay that is absent on disk becomes ``replay_missing`` (terminal), so the
  queue stops retrying a file that will never appear;
- a replay present but undecodable becomes ``error`` (re-runnable), so a
  transient/corrupt file can be retried later and never kills the worker loop;
- a decodable replay is ``analyzed`` with the feature document and the extractor
  version recorded, and the advisory detector report attached for the caller;
- an *empty* stored blob is analysed (zero frames), not treated as missing --
  absent and empty are distinct;
- a failure of the recorder itself (not the replay) *propagates*, because that
  is the supervised loop's job to back off on, not something to swallow.

The two impure edges are injected, so there is no filesystem and no database:
a closure supplies the bytes, and a recording fake captures the writes. The
pipeline in between runs for real against genuinely LZMA-compressed frames.
"""

from __future__ import annotations

import lzma

from app.services.anticheat.features import EXTRACTOR_VERSION
from app.services.anticheat.replay_analysis import AnalysisOutcome
from app.services.anticheat.replay_analysis import ReplayAnalysisService


def _compress_frames(frame_tokens: list[str]) -> bytes:
    """LZMA1 alone-format frame stream -- exactly what lands on disk."""
    raw = ",".join(frame_tokens).encode("ascii")
    filters = [{"id": lzma.FILTER_LZMA1, "preset": 6}]
    return lzma.compress(raw, format=lzma.FORMAT_ALONE, filters=filters)


def _moving_replay_bytes() -> bytes:
    # a modest cursor path with periodic taps -- enough for the extractor to form
    # vertices and the detectors to run; the exact values do not matter here.
    tokens = [
        f"16|{float(i * 4)}|{float(i * 3)}|{1 if i % 4 == 0 else 0}"
        for i in range(64)
    ]
    return _compress_frames(tokens)


class _RecordingStats:
    """Captures which terminal state was written, standing in for the repo."""

    def __init__(self) -> None:
        self.analyzed: list[tuple[int, int, int, dict[str, object]]] = []
        self.missing: list[tuple[int, int]] = []
        self.errors: list[tuple[int, int, str]] = []

    async def mark_analyzed(
        self,
        score_id: int,
        *,
        mode: int,
        extractor_version: int,
        features: dict[str, object],
    ) -> object:
        self.analyzed.append((score_id, mode, extractor_version, features))
        return None

    async def mark_replay_missing(self, score_id: int, *, mode: int) -> object:
        self.missing.append((score_id, mode))
        return None

    async def mark_error(
        self,
        score_id: int,
        *,
        mode: int,
        error_detail: str,
    ) -> object:
        self.errors.append((score_id, mode, error_detail))
        return None


def _service(
    stats: _RecordingStats,
    replay_bytes: bytes | None,
) -> ReplayAnalysisService:
    async def read_replay_bytes(score_id: int) -> bytes | None:
        return replay_bytes

    return ReplayAnalysisService(read_replay_bytes=read_replay_bytes, stats=stats)


async def test_missing_replay_is_marked_replay_missing() -> None:
    stats = _RecordingStats()
    service = _service(stats, replay_bytes=None)

    result = await service.analyze_score(42, mode=0)

    assert result.outcome is AnalysisOutcome.REPLAY_MISSING
    assert result.report is None
    assert stats.missing == [(42, 0)]
    assert stats.analyzed == [] and stats.errors == []


async def test_undecodable_replay_is_marked_error_and_stays_rerunnable() -> None:
    stats = _RecordingStats()
    service = _service(stats, replay_bytes=b"\xff\xff\xff not lzma")

    result = await service.analyze_score(7, mode=4)

    assert result.outcome is AnalysisOutcome.ERROR
    assert result.error_detail is not None
    assert "decode failed" in result.error_detail
    # recorded as error (re-runnable), never as analysed.
    assert [e[:2] for e in stats.errors] == [(7, 4)]
    assert stats.analyzed == [] and stats.missing == []


async def test_decodable_replay_is_analyzed_and_recorded() -> None:
    stats = _RecordingStats()
    service = _service(stats, replay_bytes=_moving_replay_bytes())

    result = await service.analyze_score(11, mode=0)

    assert result.outcome is AnalysisOutcome.ANALYZED
    # the advisory report is attached for the caller (flag, never ban).
    assert result.report is not None
    # exactly one analysed row, stamped with the extractor version and a feature
    # document carrying the promoted scalars.
    assert len(stats.analyzed) == 1
    score_id, mode, version, features = stats.analyzed[0]
    assert (score_id, mode, version) == (11, 0, EXTRACTOR_VERSION)
    assert "tap_count" in features
    assert stats.missing == [] and stats.errors == []


async def test_empty_blob_is_analyzed_not_treated_as_missing() -> None:
    stats = _RecordingStats()
    # empty bytes (a zero-length stored blob) are distinct from a missing file:
    # they decode to zero frames and are analysed, not marked replay_missing.
    service = _service(stats, replay_bytes=b"")

    result = await service.analyze_score(5, mode=0)

    assert result.outcome is AnalysisOutcome.ANALYZED
    assert len(stats.analyzed) == 1
    assert stats.missing == []


async def test_recorder_failure_propagates_to_the_supervised_loop() -> None:
    class _FailingStats(_RecordingStats):
        async def mark_analyzed(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("db down")

    service = _service(_FailingStats(), replay_bytes=_moving_replay_bytes())

    # a store failure is not a replay problem: it must surface, so the loop backs
    # off, rather than being swallowed as an "error" outcome.
    raised = False
    try:
        await service.analyze_score(11, mode=0)
    except RuntimeError:
        raised = True
    assert raised
