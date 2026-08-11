"""Tests for the movement anticheat detectors.

Features are constructed directly (not via ``extract_features``) so each test
pins one detector on an exact distribution -- the extractor is covered in its
own suite. A ``_feats`` builder supplies a plausible *human* baseline (real
overshoot tail, dispersed arc, varied hold widths, no robotic runs); each test
overrides only the fields whose detector it exercises.

The provisional thresholds mean these tests assert *behaviour relative to the
config*, not magic numbers: they use ``DEFAULT_CONFIG`` and craft inputs clearly
on one side of each documented boundary.
"""

from __future__ import annotations

from app.services.anticheat.detectors import DEFAULT_CONFIG
from app.services.anticheat.detectors import Severity
from app.services.anticheat.detectors import detect_aim_controller
from app.services.anticheat.detectors import detect_hold_uniformity
from app.services.anticheat.detectors import detect_robotic_timing
from app.services.anticheat.detectors import detect_timing_aim_independence
from app.services.anticheat.detectors import max_severity
from app.services.anticheat.detectors import run_detectors
from app.services.anticheat.features import ConstantVelocityRun
from app.services.anticheat.features import Distribution
from app.services.anticheat.features import ReplayFeatures
from app.services.anticheat.features import RoboticTapRun
from app.services.anticheat.features import SegmentKind
from app.services.anticheat.features import SegmentStats
from app.services.anticheat.features import StraightRun


def _dist(
    *,
    n: int = 0,
    mean: float = 0.0,
    std: float = 0.0,
    p90: float = 0.0,
    mx: float = 0.0,
) -> Distribution:
    # only the fields the detectors read are meaningful; the rest stay zero.
    return Distribution(
        n=n, mean=mean, std=std,
        p10=0.0, p25=0.0, p50=0.0, p75=0.0, p90=p90, p99=0.0, max=mx,
    )


def _seg(
    kind: SegmentKind,
    *,
    n: int = 0,
    arc: Distribution | None = None,
    vel: Distribution | None = None,
    overshoot: Distribution | None = None,
) -> SegmentStats:
    return SegmentStats(
        kind=kind,
        n=n,
        arc=arc or _dist(),
        vel=vel or _dist(),
        overshoot=overshoot or _dist(),
        dt=_dist(),
    )


def _human_jump() -> SegmentStats:
    # mirrors the reference scale: arc std ~31 deg, real overshoot p90 ~2.7,
    # velocity CoV ~0.38 (mean 2.19 / std 0.84).
    return _seg(
        SegmentKind.JUMP,
        n=300,
        arc=_dist(n=300, mean=30.0, std=31.0),
        vel=_dist(n=300, mean=2.19, std=0.84),
        overshoot=_dist(n=300, mean=0.87, p90=2.7),
    )


def _feats(
    *,
    hold: Distribution | None = None,
    jump: SegmentStats | None = None,
    tap_count: int = 500,
    robotic_tap_runs: tuple[RoboticTapRun, ...] = (),
    straight_runs: tuple[StraightRun, ...] = (),
    constant_velocity_runs: tuple[ConstantVelocityRun, ...] = (),
) -> ReplayFeatures:
    return ReplayFeatures(
        frame_count=10_000,
        duration_ms=120_000,
        tap_count=tap_count,
        uses_keyboard=True,
        hold=hold or _dist(n=200, mean=45.0, std=18.0, p90=70.0),  # human spread
        jump=jump or _human_jump(),
        stream=_seg(SegmentKind.STREAM),
        idle=_seg(SegmentKind.IDLE),
        robotic_tap_runs=robotic_tap_runs,
        straight_runs=straight_runs,
        constant_velocity_runs=constant_velocity_runs,
        frozen_spans=(),
        total_path=0.0,
        straight_line_displacement=0.0,
        tortuosity=1.5,
        jitter_mean=0.0,
        jitter_std=0.0,
        jitter_threshold=0.0,
        jitter_spike_count=0,
    )


def _run(start_taps: int, dt_ms: int) -> RoboticTapRun:
    return RoboticTapRun(start_ms=0, end_ms=dt_ms * start_taps, tap_count=start_taps, dt_ms=dt_ms)


def _straight(k: int) -> tuple[StraightRun, ...]:
    return tuple(StraightRun(start_ms=i, frame_count=5, distance=100.0) for i in range(k))


def _const_v(k: int) -> tuple[ConstantVelocityRun, ...]:
    return tuple(ConstantVelocityRun(start_ms=i, frame_count=5, velocity=1.0) for i in range(k))


# --- severity helper --------------------------------------------------------


def test_max_severity_orders_and_defaults_to_none() -> None:
    assert max_severity(()) is Severity.NONE
    assert max_severity((Severity.LOW, Severity.HIGH, Severity.MEDIUM)) is Severity.HIGH
    assert max_severity((Severity.NONE, Severity.LOW)) is Severity.LOW


# --- B1 hold-duration -------------------------------------------------------


def test_hold_human_spread_is_not_flagged() -> None:
    sig = detect_hold_uniformity(_feats())
    assert sig.severity is Severity.NONE
    assert sig.flagged is False


def test_hold_too_few_samples_is_none() -> None:
    sig = detect_hold_uniformity(_feats(hold=_dist(n=10, mean=45.0, std=18.0, p90=70.0)))
    assert sig.severity is Severity.NONE


def test_hold_zero_width_is_injected_high() -> None:
    sig = detect_hold_uniformity(_feats(hold=_dist(n=200, mean=0.0, std=0.0, p90=0.0)))
    assert sig.severity is Severity.HIGH
    assert sig.flagged is True


def test_hold_near_constant_width_flags_medium() -> None:
    # cov 1/30 ~= 0.033 <= 0.08, but p90 70ms is not instant -> uniform only.
    sig = detect_hold_uniformity(_feats(hold=_dist(n=200, mean=30.0, std=1.0, p90=70.0)))
    assert sig.severity is Severity.MEDIUM
    assert sig.flagged is True


def test_hold_uniform_and_instant_is_autoclicker_high() -> None:
    sig = detect_hold_uniformity(_feats(hold=_dist(n=200, mean=12.0, std=0.4, p90=14.0)))
    assert sig.severity is Severity.HIGH
    assert sig.flagged is True


# --- B3 aim-controller ------------------------------------------------------


def test_aim_human_correction_tail_is_not_flagged() -> None:
    sig = detect_aim_controller(_feats())
    assert sig.severity is Severity.NONE
    assert sig.flagged is False


def test_aim_too_few_legs_is_none() -> None:
    jump = _seg(SegmentKind.JUMP, n=50, arc=_dist(n=50, std=2.0), vel=_dist(n=50, mean=2.0, std=0.05))
    assert detect_aim_controller(_feats(jump=jump)).severity is Severity.NONE


def test_aim_all_three_clean_conditions_is_high() -> None:
    jump = _seg(
        SegmentKind.JUMP,
        n=300,
        arc=_dist(n=300, mean=8.0, std=5.0),          # low dispersion
        vel=_dist(n=300, mean=2.0, std=0.1),           # cov 0.05 near-constant
        overshoot=_dist(n=300, mean=0.0, p90=0.0),     # no overshoot tail
    )
    sig = detect_aim_controller(_feats(jump=jump))
    assert sig.severity is Severity.HIGH
    assert sig.flagged is True


def test_aim_two_clean_conditions_is_medium() -> None:
    jump = _seg(
        SegmentKind.JUMP,
        n=300,
        arc=_dist(n=300, mean=8.0, std=5.0),           # clean
        vel=_dist(n=300, mean=2.19, std=0.84),          # human cov ~0.38
        overshoot=_dist(n=300, mean=0.0, p90=0.0),      # clean
    )
    sig = detect_aim_controller(_feats(jump=jump))
    assert sig.severity is Severity.MEDIUM
    assert sig.flagged is True


def test_aim_single_clean_condition_is_low_not_flagged() -> None:
    jump = _seg(
        SegmentKind.JUMP,
        n=300,
        arc=_dist(n=300, mean=30.0, std=31.0),          # human
        vel=_dist(n=300, mean=2.19, std=0.84),           # human
        overshoot=_dist(n=300, mean=0.0, p90=0.0),       # only this is clean
    )
    sig = detect_aim_controller(_feats(jump=jump))
    assert sig.severity is Severity.LOW
    assert sig.flagged is False


# --- B4 robotic timing ------------------------------------------------------


def test_robotic_no_runs_is_none() -> None:
    assert detect_robotic_timing(_feats()).severity is Severity.NONE


def test_robotic_long_comb_alone_is_medium() -> None:
    sig = detect_robotic_timing(_feats(robotic_tap_runs=(_run(15, 100),)))
    assert sig.severity is Severity.MEDIUM
    assert sig.flagged is True


def test_robotic_comb_with_mechanical_runs_is_high() -> None:
    sig = detect_robotic_timing(
        _feats(
            robotic_tap_runs=(_run(15, 100),),
            straight_runs=_straight(4),
            constant_velocity_runs=_const_v(3),
        ),
    )
    assert sig.severity is Severity.HIGH
    assert sig.flagged is True


def test_robotic_mechanical_runs_alone_is_low_not_flagged() -> None:
    sig = detect_robotic_timing(
        _feats(straight_runs=_straight(3), constant_velocity_runs=_const_v(3)),
    )
    assert sig.severity is Severity.LOW
    assert sig.flagged is False


# --- C1 timing/aim independence ---------------------------------------------


def _clean_jump() -> SegmentStats:
    return _seg(
        SegmentKind.JUMP,
        n=300,
        arc=_dist(n=300, mean=8.0, std=5.0),
        vel=_dist(n=300, mean=2.0, std=0.1),
        overshoot=_dist(n=300, mean=0.0, p90=0.0),
    )


def test_independence_both_human_is_none() -> None:
    assert detect_timing_aim_independence(_feats()).severity is Severity.NONE


def test_independence_insufficient_data_is_none() -> None:
    jump = _seg(SegmentKind.JUMP, n=50)
    assert detect_timing_aim_independence(_feats(jump=jump)).severity is Severity.NONE


def test_independence_clean_aim_human_timing_flags() -> None:
    # machine-clean aim, ordinary human tapping -> the two are decoupled.
    sig = detect_timing_aim_independence(_feats(jump=_clean_jump()))
    assert sig.severity is Severity.MEDIUM
    assert sig.flagged is True
    assert sig.evidence["aim_machineness"] > sig.evidence["timing_machineness"]


def test_independence_clean_timing_human_aim_flags() -> None:
    # near-constant hold width (machine timing) with human aim.
    sig = detect_timing_aim_independence(
        _feats(hold=_dist(n=200, mean=30.0, std=0.5, p90=60.0)),
    )
    assert sig.severity is Severity.MEDIUM
    assert sig.flagged is True
    assert sig.evidence["timing_machineness"] > sig.evidence["aim_machineness"]


def test_independence_both_machine_is_none() -> None:
    # a full bot is clean on BOTH channels -> low divergence -> C1 stays quiet
    # (B1/B3/B4 are what catch that case); C1 targets the partial cheat only.
    sig = detect_timing_aim_independence(
        _feats(jump=_clean_jump(), hold=_dist(n=200, mean=30.0, std=0.5, p90=60.0)),
    )
    assert sig.severity is Severity.NONE
    assert sig.flagged is False


# --- aggregation ------------------------------------------------------------


def test_report_on_clean_replay_is_not_flagged() -> None:
    report = run_detectors(_feats())
    assert report.flagged is False
    assert report.severity is Severity.NONE
    assert report.triggered == ()


def test_report_flags_and_ranks_when_a_detector_hits() -> None:
    report = run_detectors(_feats(jump=_clean_jump()))
    assert report.flagged is True
    assert report.severity is Severity.HIGH  # B3 all-three-clean
    codes = [s.code for s in report.triggered]
    assert "B3_AIM_CONTROLLER" in codes
    # strongest-first ordering: the HIGH signal leads.
    assert report.triggered[0].severity is Severity.HIGH


def test_report_covers_all_four_detectors() -> None:
    report = run_detectors(_feats())
    codes = {s.code for s in report.signals}
    assert codes == {
        "B1_HOLD_DURATION",
        "B3_AIM_CONTROLLER",
        "B4_ROBOTIC_TIMING",
        "C1_TIMING_AIM_INDEPENDENCE",
    }
