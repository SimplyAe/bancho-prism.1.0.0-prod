"""Guards for the per-score replay-analysis repository.

The repository is the durable record of *whether* a score's replay has been
analysed; the Redis queue (Track 2.5) is only the ephemeral work list on top of
it. These tests pin the three properties that make that split safe to rely on:

- the terminal ``replay_missing`` status is written idempotently, so a score
  whose ``.osr`` never landed is skipped by the queue rather than retried
  forever, and re-marking it doesn't churn or clobber anything;
- ``mark_analyzed`` promotes exactly the scalar columns the schema indexes from
  the serialized feature document, so triage SQL and the stored blob can never
  disagree about the same score;
- a persisted row round-trips back through the extractor's ``features_from_dict``
  unchanged, so re-running a detector off the DB is identical to running it off
  the freshly-parsed replay.

No DB is involved: a fake stands in for ``Database``, recording the statements
it is handed and answering the read-back from whatever the write stored, so the
tests exercise the repository's own SQL and (de)serialization, not MySQL.
"""

from __future__ import annotations

import json
from typing import Any

from app.repositories.score_replay_stats import PROMOTED_FEATURE_COLUMNS
from app.repositories.score_replay_stats import ReplayAnalysisStatus
from app.repositories.score_replay_stats import ScoreReplayStatsRepository
from app.services.anticheat.features import EXTRACTOR_VERSION
from app.services.anticheat.features import extract_features
from app.services.anticheat.features import features_from_dict
from app.services.anticheat.features import features_to_dict
from app.adapters.osr_replay import Replay
from app.adapters.osr_replay import ReplayFrame
from app.adapters.osr_replay import ReplayKeys


class _FakeDatabase:
    """Records executed statements and serves reads from the last write.

    ``execute`` compiles the insert/upsert it is handed and keeps the resolved
    parameters keyed by ``score_id``; ``fetch_one`` compiles the select, pulls
    the queried ``score_id`` out of the ``WHERE``, and returns the stored params
    as a row. Together they make the read-back reflect exactly what the
    repository wrote -- which is the behaviour under test.
    """

    def __init__(self) -> None:
        self.rows_by_score_id: dict[int, dict[str, Any]] = {}
        self.execute_call_count = 0

    async def execute(self, statement: Any) -> int:
        self.execute_call_count += 1
        compiled = statement.compile()
        params = dict(compiled.params)
        score_id = params["score_id"]
        # on-duplicate-key upsert: merge over any existing row, as MySQL would.
        existing = self.rows_by_score_id.get(score_id, {})
        merged = {**existing, **params}
        # func.now() compiles to a parameterless SQL function, not a bind param;
        # the read path only needs a value present, so normalise it to None.
        merged.setdefault("analyzed_at", None)
        self.rows_by_score_id[score_id] = merged
        return 0

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        compiled = statement.compile()
        params = compiled.params
        score_id = next(iter(params.values()))
        stored = self.rows_by_score_id.get(score_id)
        if stored is None:
            return None
        return self._as_row(stored)

    @staticmethod
    def _as_row(stored: dict[str, Any]) -> dict[str, Any]:
        # server_default columns are absent from an INSERT's bound params; the
        # DB would materialise them, so fill the ones the deserializer reads.
        row: dict[str, Any] = {
            "score_id": stored["score_id"],
            "mode": stored.get("mode", 0),
            "status": stored.get("status", ReplayAnalysisStatus.PENDING.value),
            "extractor_version": stored.get("extractor_version", 0),
            "error_detail": stored.get("error_detail"),
            "frame_count": stored.get("frame_count", 0),
            "duration_ms": stored.get("duration_ms", 0),
            "tap_count": stored.get("tap_count", 0),
            "uses_keyboard": stored.get("uses_keyboard", 0),
            "tortuosity": stored.get("tortuosity", 0.0),
            "jitter_spike_count": stored.get("jitter_spike_count", 0),
            "robotic_tap_run_count": stored.get("robotic_tap_run_count", 0),
            "max_robotic_run_taps": stored.get("max_robotic_run_taps", 0),
            "frozen_span_count": stored.get("frozen_span_count", 0),
            "straight_run_count": stored.get("straight_run_count", 0),
            "constant_velocity_run_count": stored.get(
                "constant_velocity_run_count",
                0,
            ),
            "features": stored.get("features"),
            "analyzed_at": stored.get("analyzed_at"),
        }
        return row


def _repo() -> tuple[ScoreReplayStatsRepository, _FakeDatabase]:
    database = _FakeDatabase()
    repository = ScoreReplayStatsRepository(database)  # type: ignore[arg-type]
    return repository, database


def _taps(positions: list[tuple[int, float, float]]) -> list[ReplayFrame]:
    frames: list[ReplayFrame] = []
    for i, (dt, x, y) in enumerate(positions):
        key = ReplayKeys.KEY_1 if i % 2 == 0 else ReplayKeys.KEY_2
        frames.append(ReplayFrame(time_delta=dt, x=x, y=y, keys=key))
        frames.append(ReplayFrame(time_delta=0, x=x, y=y, keys=ReplayKeys.NONE))
    return frames


def _replay(frames: list[ReplayFrame]) -> Replay:
    return Replay(
        mode=0,
        version=20200207,
        beatmap_md5="a" * 32,
        player_name="tester",
        replay_md5="b" * 32,
        n300=0,
        n100=0,
        n50=0,
        ngeki=0,
        nkatu=0,
        nmiss=0,
        score=0,
        max_combo=0,
        perfect=False,
        mods=0,
        life_bar_graph="",
        timestamp=0,
        frames=tuple(frames),
        rng_seed=None,
        online_score_id=0,
    )


async def test_fetch_one_returns_none_when_never_enqueued() -> None:
    repository, _ = _repo()
    assert await repository.fetch_one(999) is None


async def test_mark_replay_missing_is_the_queue_skip_signal() -> None:
    repository, _ = _repo()

    stats = await repository.mark_replay_missing(42, mode=0)

    assert stats.score_id == 42
    assert stats.status is ReplayAnalysisStatus.REPLAY_MISSING
    assert stats.features is None


async def test_mark_replay_missing_is_idempotent() -> None:
    repository, database = _repo()

    await repository.mark_replay_missing(42, mode=0)
    again = await repository.mark_replay_missing(42, mode=0)

    # still exactly one stored row, still missing -- no churn, no duplicate.
    assert again.status is ReplayAnalysisStatus.REPLAY_MISSING
    assert list(database.rows_by_score_id) == [42]


async def test_mark_error_keeps_the_score_rerunnable_and_truncates_detail() -> None:
    repository, _ = _repo()

    stats = await repository.mark_error(7, mode=4, error_detail="x" * 400)

    assert stats.status is ReplayAnalysisStatus.ERROR
    assert stats.error_detail is not None
    assert len(stats.error_detail) == 255


async def test_mark_analyzed_promotes_exactly_the_indexed_scalars() -> None:
    repository, _ = _repo()
    feats = extract_features(
        _replay(_taps([(100, float(i * 6), float(i)) for i in range(8)])),
    )
    features = features_to_dict(feats)

    stats = await repository.mark_analyzed(
        11,
        mode=0,
        extractor_version=EXTRACTOR_VERSION,
        features=features,
    )

    assert stats.status is ReplayAnalysisStatus.ANALYZED
    assert stats.extractor_version == EXTRACTOR_VERSION
    # every promoted column equals the value in the serialized document.
    for name in PROMOTED_FEATURE_COLUMNS:
        expected = features[name]
        actual = getattr(stats, name)
        if isinstance(expected, bool):
            assert actual is expected
        else:
            assert actual == expected


async def test_stored_features_round_trip_back_to_the_extractor_output() -> None:
    repository, _ = _repo()
    feats = extract_features(
        _replay(_taps([(100, float(i * 6), float(i)) for i in range(8)])),
    )

    await repository.mark_analyzed(
        11,
        mode=0,
        extractor_version=EXTRACTOR_VERSION,
        features=features_to_dict(feats),
    )
    reloaded = await repository.fetch_one(11)

    assert reloaded is not None
    assert reloaded.features is not None
    assert features_from_dict(reloaded.features) == feats


async def test_analyzed_features_are_stored_as_valid_json_text() -> None:
    repository, database = _repo()
    feats = extract_features(
        _replay(_taps([(100, float(i * 6), float(i)) for i in range(8)])),
    )

    await repository.mark_analyzed(
        11,
        mode=0,
        extractor_version=EXTRACTOR_VERSION,
        features=features_to_dict(feats),
    )

    stored = database.rows_by_score_id[11]["features"]
    assert isinstance(stored, str)  # persisted as text, not a dict
    assert json.loads(stored)["tap_count"] == feats.tap_count


async def test_re_analysis_overwrites_the_previous_row() -> None:
    repository, database = _repo()
    feats = extract_features(
        _replay(_taps([(100, float(i * 6), float(i)) for i in range(8)])),
    )

    await repository.mark_error(11, mode=0, error_detail="transient parse fail")
    stats = await repository.mark_analyzed(
        11,
        mode=0,
        extractor_version=EXTRACTOR_VERSION,
        features=features_to_dict(feats),
    )

    assert stats.status is ReplayAnalysisStatus.ANALYZED
    assert stats.error_detail is None
    assert list(database.rows_by_score_id) == [11]
