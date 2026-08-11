"""Tests for the replay movement-feature extractor.

Frames are built directly (not via the ``.osr`` encoder -- that's exercised in
``osr_replay_test``) so each test pins one behaviour on a known geometry.

Two granularities are covered separately:

- **tap-aligned** segment features (arc/vel/overshoot/dt) key off key-press
  edges, so these tests hold a key down at each "note" and place the cursor
  where the note is;
- **frame-level** mechanical signatures (frozen/jitter/straight/const-v/
  tortuosity) key off the raw cursor stream regardless of keys.
"""

from __future__ import annotations

from app.adapters.osr_replay import Replay
from app.adapters.osr_replay import ReplayFrame
from app.adapters.osr_replay import ReplayKeys
from app.services.anticheat.features import Distribution
from app.services.anticheat.features import SegmentKind
from app.services.anticheat.features import _percentile
from app.services.anticheat.features import _summarize
from app.services.anticheat.features import extract_features
from app.services.anticheat.features import features_from_dict
from app.services.anticheat.features import features_to_dict
from app.services.anticheat.features import max_robotic_run_taps

_K1 = ReplayKeys.KEY_1
_K2 = ReplayKeys.KEY_2


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


def _frame(dt: int, x: float, y: float, keys: ReplayKeys = ReplayKeys.NONE) -> ReplayFrame:
    return ReplayFrame(time_delta=dt, x=x, y=y, keys=keys)


def _taps(positions: list[tuple[int, float, float]]) -> list[ReplayFrame]:
    """Build frames that register one tap per (dt, x, y), alternating K1/K2.

    Each tap is a down-frame followed by an up-frame (keys released) so the
    next down is a fresh rising edge. dt is charged to the down-frame.
    """
    frames: list[ReplayFrame] = []
    for i, (dt, x, y) in enumerate(positions):
        key = _K1 if i % 2 == 0 else _K2
        frames.append(_frame(dt, x, y, key))
        frames.append(_frame(0, x, y, ReplayKeys.NONE))
    return frames


# --- distribution primitives ------------------------------------------------


def test_percentile_uses_linear_interpolation() -> None:
    ordered = [float(v) for v in range(1, 11)]  # 1..10
    assert _percentile(ordered, 50) == 5.5  # rank 4.5 -> between 5 and 6
    assert _percentile(ordered, 0) == 1.0
    assert _percentile(ordered, 100) == 10.0


def test_summarize_empty_is_all_zero() -> None:
    assert _summarize([]) == Distribution(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_summarize_reports_mean_std_and_max() -> None:
    dist = _summarize([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    assert dist.n == 8
    assert dist.mean == 5.0
    assert abs(dist.std - 2.0) < 1e-9  # known population std of this set
    assert dist.max == 9.0


# --- tap detection & segmentation -------------------------------------------


def test_taps_counted_once_per_rising_edge_on_keyboard() -> None:
    feats = extract_features(_replay(_taps([(100, 0.0, 0.0)] * 5)))
    assert feats.tap_count == 5
    assert feats.uses_keyboard is True


def test_keyboard_press_does_not_double_count_via_mouse_bit() -> None:
    # K1 also sets M1 in the real bitmask; still one tap.
    frames = [
        _frame(100, 0.0, 0.0, ReplayKeys.KEY_1 | ReplayKeys.MOUSE_1),
        _frame(0, 0.0, 0.0, ReplayKeys.NONE),
    ]
    assert extract_features(_replay(frames)).tap_count == 1


def test_mouse_only_replay_uses_mouse_channels() -> None:
    frames = [
        _frame(100, 0.0, 0.0, ReplayKeys.MOUSE_1),
        _frame(0, 0.0, 0.0, ReplayKeys.NONE),
        _frame(100, 5.0, 5.0, ReplayKeys.MOUSE_2),
        _frame(0, 5.0, 5.0, ReplayKeys.NONE),
    ]
    feats = extract_features(_replay(frames))
    assert feats.uses_keyboard is False
    assert feats.tap_count == 2


def test_tap_dt_buckets_into_the_right_segment() -> None:
    # 10 taps at each spacing (diagonal march so legs are non-degenerate).
    def march(dt: int) -> list[tuple[int, float, float]]:
        return [(dt, float(i * 5), float(i * 3)) for i in range(10)]

    stream = extract_features(_replay(_taps(march(100))))
    jump = extract_features(_replay(_taps(march(250))))
    idle = extract_features(_replay(_taps(march(600))))

    # 10 taps -> interior vertices at i=1..8 -> 8 counted legs.
    assert (stream.stream.n, stream.jump.n, stream.idle.n) == (8, 0, 0)
    assert (jump.jump.n, jump.stream.n, jump.idle.n) == (8, 0, 0)
    assert (idle.idle.n, idle.stream.n, idle.jump.n) == (8, 0, 0)


def test_segment_cutoffs_are_inclusive_at_the_boundary() -> None:
    def march(dt: int) -> list[tuple[int, float, float]]:
        return [(dt, float(i * 5), float(i * 3)) for i in range(6)]

    assert extract_features(_replay(_taps(march(150)))).stream.n == 4
    assert extract_features(_replay(_taps(march(400)))).jump.n == 4


# --- tap-aligned geometry ---------------------------------------------------


def test_right_angle_tap_turn_is_ninety_degrees() -> None:
    feats = extract_features(_replay(_taps([
        (100, 0.0, 0.0),
        (100, 10.0, 0.0),
        (100, 10.0, 10.0),
        (100, 20.0, 10.0),
    ])))
    assert feats.stream.arc.n == 2  # two interior vertices
    assert abs(feats.stream.arc.mean - 90.0) < 1e-6


def test_reversal_produces_overshoot_smooth_turn_does_not() -> None:
    reversal = extract_features(_replay(_taps([
        (100, 0.0, 0.0),
        (100, 10.0, 0.0),
        (100, 2.0, 0.0),
    ])))
    assert reversal.stream.overshoot.n == 1
    assert abs(reversal.stream.overshoot.max - 8.0) < 1e-6  # 8u backtrack

    smooth = extract_features(_replay(_taps([
        (100, 0.0, 0.0),
        (100, 10.0, 0.0),
        (100, 10.0, 10.0),
    ])))
    assert smooth.stream.overshoot.max == 0.0


def test_velocity_is_field_units_per_millisecond() -> None:
    feats = extract_features(_replay(_taps(
        [(100, float(i * 5), 0.0) for i in range(5)],
    )))
    assert abs(feats.stream.vel.mean - 0.05) < 1e-9  # 5u / 100ms


# --- hold durations ---------------------------------------------------------


def test_hold_duration_measures_press_to_release_span() -> None:
    # K1 down at t=0, released at t=40; K2 down at t=100, released at t=175.
    frames = [
        _frame(0, 0.0, 0.0, _K1),
        _frame(40, 0.0, 0.0, ReplayKeys.NONE),
        _frame(60, 5.0, 5.0, _K2),
        _frame(75, 5.0, 5.0, ReplayKeys.NONE),
    ]
    hold = extract_features(_replay(frames)).hold
    assert hold.n == 2
    assert hold.max == 75.0
    assert abs(hold.mean - 57.5) < 1e-9


def test_hold_still_down_at_replay_end_is_dropped() -> None:
    # one clean press->release, then a press never released -> only the first
    # contributes a known duration.
    frames = [
        _frame(0, 0.0, 0.0, _K1),
        _frame(30, 0.0, 0.0, ReplayKeys.NONE),
        _frame(50, 5.0, 5.0, _K2),
    ]
    hold = extract_features(_replay(frames)).hold
    assert hold.n == 1
    assert hold.max == 30.0


# --- robotic tapping --------------------------------------------------------

def test_identical_tap_intervals_flag_a_robotic_run() -> None:
    feats = extract_features(_replay(_taps(
        [(100, float(i * 6), float(i)) for i in range(8)],
    )))
    assert len(feats.robotic_tap_runs) == 1
    run = feats.robotic_tap_runs[0]
    assert run.dt_ms == 100
    assert run.tap_count == 8  # 7 identical intervals bound 8 taps


def test_human_like_tap_jitter_is_not_robotic() -> None:
    positions = [(0, 0.0, 0.0)]
    positions += [(98 if i % 2 else 102, float(i * 6), float(i)) for i in range(1, 12)]
    assert extract_features(_replay(_taps(positions))).robotic_tap_runs == ()


# --- frame-level signatures -------------------------------------------------


def test_collinear_uniform_path_is_straight_and_constant_velocity() -> None:
    frames = [_frame(100, float(i * 10), 0.0) for i in range(8)]
    feats = extract_features(_replay(frames))
    assert len(feats.straight_runs) >= 1
    assert len(feats.constant_velocity_runs) >= 1
    assert abs(feats.tortuosity - 1.0) < 1e-9  # perfectly straight


def test_frozen_cursor_span_is_detected() -> None:
    frames = [
        _frame(100, 0.0, 0.0),
        _frame(100, 50.0, 50.0),
        _frame(100, 50.0, 50.0),
        _frame(100, 50.0, 50.0),  # 3 identical -> frozen
        _frame(100, 90.0, 20.0),
    ]
    spans = extract_features(_replay(frames)).frozen_spans
    assert len(spans) == 1
    assert spans[0].frame_count == 3
    assert (spans[0].x, spans[0].y) == (50.0, 50.0)


def test_zigzag_path_has_high_tortuosity() -> None:
    frames = [
        _frame(100, 0.0, 0.0),
        _frame(100, 10.0, 40.0),
        _frame(100, 20.0, 0.0),
        _frame(100, 30.0, 40.0),
        _frame(100, 40.0, 0.0),
    ]
    assert extract_features(_replay(frames)).tortuosity > 1.5


# --- robustness -------------------------------------------------------------


def test_too_short_replay_yields_empty_features() -> None:
    feats = extract_features(_replay([_frame(0, 1.0, 1.0), _frame(100, 2.0, 2.0)]))
    for seg in (feats.jump, feats.stream, feats.idle):
        assert seg.n == 0
    assert feats.robotic_tap_runs == ()
    assert feats.straight_runs == ()
    assert feats.frozen_spans == ()


def test_empty_replay_does_not_raise() -> None:
    feats = extract_features(_replay(_taps([])))
    assert feats.frame_count == 0
    assert feats.duration_ms == 0
    assert feats.tap_count == 0
    assert feats.tortuosity == 0.0


# --- serialization ----------------------------------------------------------


def test_features_round_trip_through_dict_is_a_fixed_point() -> None:
    # a replay exercising taps (segments + robotic run) and a frozen span, so
    # every branch of the serializer carries a non-empty payload.
    frames = _taps([(100, float(i * 6), float(i)) for i in range(8)])
    frames += [
        _frame(100, 300.0, 300.0),
        _frame(100, 300.0, 300.0),
        _frame(100, 300.0, 300.0),
    ]
    feats = extract_features(_replay(frames))

    assert feats.robotic_tap_runs != ()  # guard: the run list is actually tested
    assert feats.frozen_spans != ()

    assert features_from_dict(features_to_dict(feats)) == feats


def test_features_to_dict_promotes_run_counts_at_the_top_level() -> None:
    frames = _taps([(100, float(i * 6), float(i)) for i in range(8)])
    frames += [_frame(100, 300.0, 300.0)] * 3
    feats = extract_features(_replay(frames))

    data = features_to_dict(feats)
    assert data["robotic_tap_run_count"] == len(feats.robotic_tap_runs)
    assert data["frozen_span_count"] == len(feats.frozen_spans)
    assert data["max_robotic_run_taps"] == max_robotic_run_taps(feats)
    assert data["tap_count"] == feats.tap_count


def test_max_robotic_run_taps_is_zero_without_any_run() -> None:
    feats = extract_features(_replay(_taps([(100, 0.0, 0.0), (100, 5.0, 5.0)])))
    assert feats.robotic_tap_runs == ()
    assert max_robotic_run_taps(feats) == 0

