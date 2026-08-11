"""Behavioural feature extraction from a parsed ``.osr`` replay.

This turns the raw cursor+key stream (see ``app/adapters/osr_replay.py``) into
the movement statistics the anticheat detectors reason about. It is pure and
side-effect free: same frames in, same features out, no thresholds baked in.
The detectors in ``app/services/anticheat`` decide what a value *means*, and the
policy is flag-not-ban until precision is backtested.

Two granularities, because the signals live at different ones:

1. **Tap-aligned aim features** -- ``jump`` / ``stream`` / ``idle`` segment
   summaries of turn-angle, velocity, overshoot and interval, measured
   *between consecutive key presses* (i.e. note to note). This matches the
   labelled baseline in ``calibration_v2.json``: its segment dt bands
   (stream <=150 ms, jump 150-400 ms, idle >400 ms) are tap-to-tap intervals,
   not the ~7-16 ms frame sampling of a modern client. Aim assistance shows up
   in the geometry of movement *between* the notes a player is hitting, which
   is what these capture.

2. **Frame-level mechanical signatures** -- frozen cursor, acceleration-jitter
   spikes, path tortuosity, and perfectly-straight / constant-velocity runs,
   computed over the raw cursor stream. These catch interpolation and injection
   tells that are independent of how the player is tapping, so they work for
   relax/autopilot replays too.

IMPORTANT -- the bundled ``calibration_v2.json`` is only three legit players'
vanilla plays, produced by an external offline tool. Treat it as a *reference*
for the shape and rough scale of features, not as production thresholds. Real
thresholds must be re-derived by running *this* extractor over a baseline of
verified replays the server itself has collected; until then, detectors flag
for review rather than punish. The generator's ``arc_half`` feature is
deliberately not reproduced -- its definition isn't recoverable from the tools
on hand, and a fabricated feature the detectors key on is worse than one fewer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.adapters.osr_replay import Replay
from app.adapters.osr_replay import ReplayFrame
from app.adapters.osr_replay import ReplayKeys

# Bumped whenever this extractor's output changes meaning (a new/renamed feature,
# a changed cutoff, a fixed bug). Stored alongside every persisted feature row so
# the analysis queue can tell a row produced by an older extractor from a current
# one and re-analyse it, rather than trusting a stale vector against fresh
# thresholds. This is load-bearing for the flag-not-ban policy: thresholds are
# re-derived from features, so features and thresholds must be the same vintage.
EXTRACTOR_VERSION = 2

# --- tap-aligned segmentation cutoffs (ms), matching calibration_v2.json ----
_STREAM_MAX_DT_MS = 150
_JUMP_MAX_DT_MS = 400

# a movement leg shorter than this (field units) has no reliable direction, so
# the vertex is skipped rather than contributing a noisy near-zero-length turn.
_MIN_LEG_LENGTH = 0.01

# tap channels: a keyboard press (K1/K2) also sets its mouse bit, so if the
# replay ever uses the keyboard we count K1/K2 edges, else M1/M2, to avoid
# double-counting one physical tap.
_KEYBOARD_KEYS = ReplayKeys.KEY_1 | ReplayKeys.KEY_2
_KEYBOARD_CHANNELS = (ReplayKeys.KEY_1, ReplayKeys.KEY_2)
_MOUSE_CHANNELS = (ReplayKeys.MOUSE_1, ReplayKeys.MOUSE_2)

# robotic tapping: >= this many consecutive tap intervals sharing one exact dt,
# within the human-stream band. People jitter by a millisecond or two even at a
# steady rate; pixel-identical intervals are an auto-clicker / timing assist.
_ROBOTIC_MIN_TAPS = 6
_ROBOTIC_MIN_DT_MS = 25
_ROBOTIC_MAX_DT_MS = _STREAM_MAX_DT_MS

# frame-level "straight" run: >= this many consecutive turns below the angle.
_STRAIGHT_MIN_FRAMES = 4
_STRAIGHT_MAX_DEG = 1.0

# frame-level constant-velocity run: >= this many consecutive steps whose speed
# varies by less than the ratio, ignoring near-stationary steps.
_CONST_V_MIN_FRAMES = 4
_CONST_V_MAX_RATIO = 0.01
_CONST_V_MIN_SPEED = 0.05

# frozen cursor: >= this many consecutive frames at the exact same pixel.
_FROZEN_MIN_FRAMES = 3

# jitter spike: acceleration above mean + this many population std devs.
_JITTER_SIGMA = 3.0


class SegmentKind(StrEnum):
    JUMP = "jump"
    STREAM = "stream"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class Distribution:
    """Summary of one feature over one segment; fields mirror the baseline."""

    n: int
    mean: float
    std: float
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    p99: float
    max: float


@dataclass(frozen=True, slots=True)
class SegmentStats:
    """Tap-aligned aim features for one movement regime."""

    kind: SegmentKind
    n: int
    arc: Distribution
    vel: Distribution
    overshoot: Distribution
    dt: Distribution


@dataclass(frozen=True, slots=True)
class RoboticTapRun:
    """A run of consecutive taps with an identical inter-tap interval."""

    start_ms: int
    end_ms: int
    tap_count: int
    dt_ms: int


@dataclass(frozen=True, slots=True)
class StraightRun:
    start_ms: int
    frame_count: int
    distance: float


@dataclass(frozen=True, slots=True)
class ConstantVelocityRun:
    start_ms: int
    frame_count: int
    velocity: float


@dataclass(frozen=True, slots=True)
class FrozenSpan:
    start_ms: int
    x: float
    y: float
    frame_count: int


@dataclass(frozen=True, slots=True)
class ReplayFeatures:
    """Everything the detectors read off one replay.

    ``jump``/``stream``/``idle`` are tap-aligned and directly comparable to a
    baseline of the same shape; the run lists, tortuosity and jitter fields are
    the frame-level mechanical-signature aggregates.
    """

    frame_count: int
    duration_ms: int
    tap_count: int
    uses_keyboard: bool

    hold: Distribution

    jump: SegmentStats
    stream: SegmentStats
    idle: SegmentStats

    robotic_tap_runs: tuple[RoboticTapRun, ...]
    straight_runs: tuple[StraightRun, ...]
    constant_velocity_runs: tuple[ConstantVelocityRun, ...]
    frozen_spans: tuple[FrozenSpan, ...]

    total_path: float
    straight_line_displacement: float
    tortuosity: float

    jitter_mean: float
    jitter_std: float
    jitter_threshold: float
    jitter_spike_count: int


# --- distribution helpers ---------------------------------------------------


def _percentile(ordered: list[float], q: float) -> float:
    """Linear-interpolation percentile (numpy default), ``q`` in [0, 100].

    ``ordered`` must be sorted ascending and non-empty.
    """
    if len(ordered) == 1:
        return ordered[0]
    rank = (q / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] + frac * (ordered[hi] - ordered[lo])


def _summarize(values: list[float]) -> Distribution:
    """Reduce a feature's per-vertex values to a baseline-shaped summary.

    Empty input yields an all-zero distribution with ``n == 0`` rather than
    raising, so a segment with no vertices is still describable.
    """
    n = len(values)
    if n == 0:
        return Distribution(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    mean = math.fsum(values) / n
    variance = math.fsum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(variance)
    ordered = sorted(values)
    return Distribution(
        n=n,
        mean=mean,
        std=std,
        p10=_percentile(ordered, 10),
        p25=_percentile(ordered, 25),
        p50=_percentile(ordered, 50),
        p75=_percentile(ordered, 75),
        p90=_percentile(ordered, 90),
        p99=_percentile(ordered, 99),
        max=ordered[-1],
    )


def _segment_of(dt_ms: int) -> SegmentKind:
    if dt_ms <= _STREAM_MAX_DT_MS:
        return SegmentKind.STREAM
    if dt_ms <= _JUMP_MAX_DT_MS:
        return SegmentKind.JUMP
    return SegmentKind.IDLE


# --- geometry ---------------------------------------------------------------


def _turn_angle_deg(inx: float, iny: float, outx: float, outy: float) -> float:
    """Angle (degrees) between the arriving and leaving legs at a vertex."""
    in_len = math.hypot(inx, iny)
    out_len = math.hypot(outx, outy)
    if in_len < _MIN_LEG_LENGTH or out_len < _MIN_LEG_LENGTH:
        return 0.0
    cos = (inx * outx + iny * outy) / (in_len * out_len)
    cos = max(-1.0, min(1.0, cos))  # clamp for float error before acos
    return math.degrees(math.acos(cos))


def _overshoot(inx: float, iny: float, outx: float, outy: float) -> float:
    """Field-unit backtrack of the leaving leg against the arriving direction.

    Zero unless the leaving leg has a component *opposite* the arriving one
    (the cursor reversed past the vertex), in which case it's the length of
    that opposing component. Beatmap-free -- no hit-object positions needed.
    """
    in_len = math.hypot(inx, iny)
    if in_len < _MIN_LEG_LENGTH:
        return 0.0
    projection = (outx * inx + outy * iny) / in_len
    return -projection if projection < 0.0 else 0.0


# --- sample building --------------------------------------------------------


def _absolute_samples(frames: tuple[ReplayFrame, ...]) -> list[tuple[int, float, float]]:
    """Convert delta-timed frames into cumulative-time (t, x, y) samples."""
    samples: list[tuple[int, float, float]] = []
    t = 0
    for frame in frames:
        t += frame.time_delta
        samples.append((t, frame.x, frame.y))
    return samples


def _active_channels(
    frames: tuple[ReplayFrame, ...],
) -> tuple[bool, tuple[ReplayKeys, ...]]:
    """(uses_keyboard, the key channels to count edges on).

    A keyboard press (K1/K2) also sets its mouse bit, so if the replay ever
    uses the keyboard we key off K1/K2, else M1/M2 -- otherwise one physical
    tap would be counted on both channels.
    """
    uses_keyboard = any(frame.keys & _KEYBOARD_KEYS for frame in frames)
    channels = _KEYBOARD_CHANNELS if uses_keyboard else _MOUSE_CHANNELS
    return uses_keyboard, channels


def _tap_samples(
    frames: tuple[ReplayFrame, ...],
) -> tuple[bool, list[tuple[int, float, float]]]:
    """(uses_keyboard, tap positions), one per key-down edge on active channels.

    A tap is the rising edge of a key on the active channel pair; the cursor
    position at that instant is where the note was hit.
    """
    uses_keyboard, channels = _active_channels(frames)

    taps: list[tuple[int, float, float]] = []
    held = {channel: False for channel in channels}
    t = 0
    for frame in frames:
        t += frame.time_delta
        for channel in channels:
            now = bool(frame.keys & channel)
            if now and not held[channel]:
                taps.append((t, frame.x, frame.y))
            held[channel] = now
    return uses_keyboard, taps


def _hold_durations(frames: tuple[ReplayFrame, ...]) -> list[float]:
    """Milliseconds each key was held, one value per completed press->release.

    Measured on the same active channels as taps. A press still held at the end
    of the replay has no known duration and is dropped rather than closed at an
    arbitrary time, so the distribution isn't biased by a truncated final hold.
    Auto-tap and replay-bot inputs collapse this distribution -- either to a
    near-constant width or to ultra-short one-to-two-frame presses -- where a
    human's hold durations spread out.
    """
    _, channels = _active_channels(frames)

    durations: list[float] = []
    press_time: dict[ReplayKeys, int] = {}
    held = {channel: False for channel in channels}
    t = 0
    for frame in frames:
        t += frame.time_delta
        for channel in channels:
            now = bool(frame.keys & channel)
            if now and not held[channel]:
                press_time[channel] = t
            elif not now and held[channel]:
                start = press_time.pop(channel, None)
                if start is not None:
                    durations.append(float(t - start))
            held[channel] = now
    return durations


# --- top-level extraction ---------------------------------------------------


def extract_features(replay: Replay) -> ReplayFeatures:
    """Extract movement features from a parsed replay.

    Pure: identical frames in, identical features out. Safe on replays too
    short to form any vertex -- every segment reports ``n == 0`` and the run
    lists come back empty.
    """
    samples = _absolute_samples(replay.frames)
    uses_keyboard, taps = _tap_samples(replay.frames)

    hold = _summarize(_hold_durations(replay.frames))
    segments = _segment_features(taps)
    velocities = _step_velocities(samples)
    jitter_mean, jitter_std, jitter_threshold, jitter_spikes = _jitter(velocities)
    total_path, straight_line, tortuosity = _path_shape(samples)

    return ReplayFeatures(
        frame_count=len(samples),
        duration_ms=samples[-1][0] - samples[0][0] if len(samples) >= 2 else 0,
        tap_count=len(taps),
        uses_keyboard=uses_keyboard,
        hold=hold,
        jump=segments[SegmentKind.JUMP],
        stream=segments[SegmentKind.STREAM],
        idle=segments[SegmentKind.IDLE],
        robotic_tap_runs=_robotic_tap_runs(taps),
        straight_runs=_straight_runs(samples),
        constant_velocity_runs=_constant_velocity_runs(samples, velocities),
        frozen_spans=_frozen_spans(samples),
        total_path=total_path,
        straight_line_displacement=straight_line,
        tortuosity=tortuosity,
        jitter_mean=jitter_mean,
        jitter_std=jitter_std,
        jitter_threshold=jitter_threshold,
        jitter_spike_count=jitter_spikes,
    )


def _segment_features(
    taps: list[tuple[int, float, float]],
) -> dict[SegmentKind, SegmentStats]:
    """Tap-aligned arc/vel/overshoot/dt per movement regime.

    One value per interior tap vertex; degenerate legs (zero length or
    non-positive dt) are dropped so arc/vel/overshoot/dt share a count, exactly
    as the baseline is shaped.
    """
    arc: dict[SegmentKind, list[float]] = {k: [] for k in SegmentKind}
    vel: dict[SegmentKind, list[float]] = {k: [] for k in SegmentKind}
    overshoot: dict[SegmentKind, list[float]] = {k: [] for k in SegmentKind}
    dt_by_seg: dict[SegmentKind, list[float]] = {k: [] for k in SegmentKind}

    for i in range(1, len(taps) - 1):
        t_prev, x_prev, y_prev = taps[i - 1]
        t_cur, x_cur, y_cur = taps[i]
        _, x_next, y_next = taps[i + 1]

        dt = t_cur - t_prev
        if dt <= 0:
            continue

        inx, iny = x_cur - x_prev, y_cur - y_prev
        outx, outy = x_next - x_cur, y_next - y_cur
        in_len = math.hypot(inx, iny)
        out_len = math.hypot(outx, outy)
        if in_len < _MIN_LEG_LENGTH or out_len < _MIN_LEG_LENGTH:
            continue

        segment = _segment_of(dt)
        arc[segment].append(_turn_angle_deg(inx, iny, outx, outy))
        vel[segment].append(in_len / dt)
        overshoot[segment].append(_overshoot(inx, iny, outx, outy))
        dt_by_seg[segment].append(float(dt))

    return {
        kind: SegmentStats(
            kind=kind,
            n=len(vel[kind]),
            arc=_summarize(arc[kind]),
            vel=_summarize(vel[kind]),
            overshoot=_summarize(overshoot[kind]),
            dt=_summarize(dt_by_seg[kind]),
        )
        for kind in SegmentKind
    }


def _robotic_tap_runs(
    taps: list[tuple[int, float, float]],
) -> tuple[RoboticTapRun, ...]:
    """Runs of >= 6 consecutive taps with an identical interval in the band."""
    runs: list[RoboticTapRun] = []
    intervals = [taps[i][0] - taps[i - 1][0] for i in range(1, len(taps))]
    n = len(intervals)
    i = 0
    while i < n:
        dt = intervals[i]
        if not (_ROBOTIC_MIN_DT_MS <= dt <= _ROBOTIC_MAX_DT_MS):
            i += 1
            continue
        j = i
        while j < n and intervals[j] == dt:
            j += 1
        run_len = j - i  # number of identical intervals
        if run_len >= _ROBOTIC_MIN_TAPS:
            runs.append(
                RoboticTapRun(
                    start_ms=taps[i][0],
                    end_ms=taps[j][0],
                    tap_count=run_len + 1,  # intervals -> taps that bound them
                    dt_ms=dt,
                ),
            )
        i = j if j > i else i + 1
    return tuple(runs)


# --- frame-level mechanical signatures --------------------------------------


def _step_velocities(samples: list[tuple[int, float, float]]) -> list[float]:
    """Per-step speed (field units / ms); 0 where dt <= 0. Index j == step j+1."""
    velocities: list[float] = []
    for i in range(1, len(samples)):
        dt = samples[i][0] - samples[i - 1][0]
        dx = samples[i][1] - samples[i - 1][1]
        dy = samples[i][2] - samples[i - 1][2]
        velocities.append(math.hypot(dx, dy) / dt if dt > 0 else 0.0)
    return velocities


def _jitter(velocities: list[float]) -> tuple[float, float, float, int]:
    """Acceleration-spike stats: (mean, std, threshold, spike_count).

    Acceleration is the absolute change in step speed; a spike is any value
    above mean + 3 population std devs, the reference's aim-jitter signal.
    """
    accel = [abs(velocities[i] - velocities[i - 1]) for i in range(1, len(velocities))]
    if not accel:
        return 0.0, 0.0, 0.0, 0
    mean = math.fsum(accel) / len(accel)
    variance = math.fsum((a - mean) ** 2 for a in accel) / len(accel)
    std = math.sqrt(variance)
    threshold = mean + _JITTER_SIGMA * std
    spikes = sum(1 for a in accel if a > threshold)
    return mean, std, threshold, spikes


def _path_shape(samples: list[tuple[int, float, float]]) -> tuple[float, float, float]:
    """(total_path, straight_line_displacement, tortuosity).

    Tortuosity is total path length over start-to-end displacement (1.0 for a
    perfectly straight sweep); 0.0 when there's no displacement to divide by.
    """
    if len(samples) < 2:
        return 0.0, 0.0, 0.0
    total = math.fsum(
        math.hypot(samples[i][1] - samples[i - 1][1], samples[i][2] - samples[i - 1][2])
        for i in range(1, len(samples))
    )
    straight = math.hypot(samples[-1][1] - samples[0][1], samples[-1][2] - samples[0][2])
    tortuosity = total / straight if straight > 0 else 0.0
    return total, straight, tortuosity


def _straight_runs(samples: list[tuple[int, float, float]]) -> tuple[StraightRun, ...]:
    """Runs of >= 4 consecutive vertices turning < 1 degree -- linear paths."""
    runs: list[StraightRun] = []
    n = len(samples)
    start: int | None = None
    for i in range(1, n - 1):
        inx, iny = samples[i][1] - samples[i - 1][1], samples[i][2] - samples[i - 1][2]
        outx, outy = samples[i + 1][1] - samples[i][1], samples[i + 1][2] - samples[i][2]
        straight = (
            math.hypot(inx, iny) >= _MIN_LEG_LENGTH
            and math.hypot(outx, outy) >= _MIN_LEG_LENGTH
            and _turn_angle_deg(inx, iny, outx, outy) < _STRAIGHT_MAX_DEG
        )
        if straight:
            if start is None:
                start = i
        else:
            _flush_straight_run(runs, samples, start, i)
            start = None
    _flush_straight_run(runs, samples, start, n - 1)
    return tuple(runs)


def _flush_straight_run(
    runs: list[StraightRun],
    samples: list[tuple[int, float, float]],
    start: int | None,
    end: int,
) -> None:
    if start is None or (end - start) < _STRAIGHT_MIN_FRAMES:
        return
    dist = math.hypot(
        samples[end][1] - samples[start][1],
        samples[end][2] - samples[start][2],
    )
    runs.append(
        StraightRun(start_ms=samples[start][0], frame_count=end - start, distance=dist),
    )


def _constant_velocity_runs(
    samples: list[tuple[int, float, float]],
    velocities: list[float],
) -> tuple[ConstantVelocityRun, ...]:
    """Runs of >= 4 steps whose speed varies < 1% -- mechanical interpolation."""
    runs: list[ConstantVelocityRun] = []
    start: int | None = None
    for i in range(1, len(velocities)):
        cur, prev = velocities[i], velocities[i - 1]
        steady = (
            cur > _CONST_V_MIN_SPEED
            and prev > _CONST_V_MIN_SPEED
            and abs(cur - prev) / max(cur, 0.001) < _CONST_V_MAX_RATIO
        )
        if steady:
            if start is None:
                start = i
        else:
            _flush_const_v_run(runs, samples, velocities, start, i)
            start = None
    _flush_const_v_run(runs, samples, velocities, start, len(velocities))
    return tuple(runs)


def _flush_const_v_run(
    runs: list[ConstantVelocityRun],
    samples: list[tuple[int, float, float]],
    velocities: list[float],
    start: int | None,
    end: int,
) -> None:
    if start is None or (end - start) < _CONST_V_MIN_FRAMES:
        return
    # velocity index j corresponds to sample j+1; report the run's start time.
    runs.append(
        ConstantVelocityRun(
            start_ms=samples[start + 1][0],
            frame_count=end - start,
            velocity=velocities[start],
        ),
    )


def _frozen_spans(samples: list[tuple[int, float, float]]) -> tuple[FrozenSpan, ...]:
    """Spans of >= 3 consecutive frames at the exact same pixel."""
    spans: list[FrozenSpan] = []
    n = len(samples)
    i = 0
    while i < n - 1:
        j = i
        while j < n and samples[j][1] == samples[i][1] and samples[j][2] == samples[i][2]:
            j += 1
        if (j - i) >= _FROZEN_MIN_FRAMES:
            spans.append(
                FrozenSpan(
                    start_ms=samples[i][0],
                    x=samples[i][1],
                    y=samples[i][2],
                    frame_count=j - i,
                ),
            )
        i = j if j > i else i + 1
    return tuple(spans)


# --- serialization ----------------------------------------------------------
#
# ``ReplayFeatures`` is persisted per score (see
# ``app/repositories/score_replay_stats.py``) as a JSON document so the
# detectors can be re-run -- and the baseline re-derived -- without re-parsing
# every ``.osr``. The dict is deliberately flat at the top for the handful of
# scalars the repository promotes to indexed columns, then nests the full
# per-segment distributions and run lists underneath. ``features_from_dict`` is
# the exact inverse of ``features_to_dict``; the round-trip is a fixed point.


def _distribution_to_dict(dist: Distribution) -> dict[str, Any]:
    return {
        "n": dist.n,
        "mean": dist.mean,
        "std": dist.std,
        "p10": dist.p10,
        "p25": dist.p25,
        "p50": dist.p50,
        "p75": dist.p75,
        "p90": dist.p90,
        "p99": dist.p99,
        "max": dist.max,
    }


def _distribution_from_dict(data: dict[str, Any]) -> Distribution:
    return Distribution(
        n=data["n"],
        mean=data["mean"],
        std=data["std"],
        p10=data["p10"],
        p25=data["p25"],
        p50=data["p50"],
        p75=data["p75"],
        p90=data["p90"],
        p99=data["p99"],
        max=data["max"],
    )


def _segment_to_dict(segment: SegmentStats) -> dict[str, Any]:
    return {
        "kind": segment.kind.value,
        "n": segment.n,
        "arc": _distribution_to_dict(segment.arc),
        "vel": _distribution_to_dict(segment.vel),
        "overshoot": _distribution_to_dict(segment.overshoot),
        "dt": _distribution_to_dict(segment.dt),
    }


def _segment_from_dict(data: dict[str, Any]) -> SegmentStats:
    return SegmentStats(
        kind=SegmentKind(data["kind"]),
        n=data["n"],
        arc=_distribution_from_dict(data["arc"]),
        vel=_distribution_from_dict(data["vel"]),
        overshoot=_distribution_from_dict(data["overshoot"]),
        dt=_distribution_from_dict(data["dt"]),
    )


def max_robotic_run_taps(feats: ReplayFeatures) -> int:
    """Longest single robotic tap run, in taps; 0 when there are none.

    Exposed (not just inlined) because it is the strongest single spectral-comb
    signal, and both the repository's promoted column and any future detector
    should read it the same way.
    """
    return max((run.tap_count for run in feats.robotic_tap_runs), default=0)


def features_to_dict(feats: ReplayFeatures) -> dict[str, Any]:
    """Serialize extracted features to a JSON-ready dict.

    Top-level keys mirror the scalar columns the repository indexes; the nested
    ``segments`` and ``*_runs`` / ``*_spans`` keys carry the full detail the
    detectors and baseline re-derivation consume.
    """
    return {
        # promoted scalars -- also mapped straight onto indexed columns.
        "frame_count": feats.frame_count,
        "duration_ms": feats.duration_ms,
        "tap_count": feats.tap_count,
        "uses_keyboard": feats.uses_keyboard,
        "tortuosity": feats.tortuosity,
        "jitter_spike_count": feats.jitter_spike_count,
        "robotic_tap_run_count": len(feats.robotic_tap_runs),
        "max_robotic_run_taps": max_robotic_run_taps(feats),
        "frozen_span_count": len(feats.frozen_spans),
        "straight_run_count": len(feats.straight_runs),
        "constant_velocity_run_count": len(feats.constant_velocity_runs),
        # full detail.
        "total_path": feats.total_path,
        "straight_line_displacement": feats.straight_line_displacement,
        "jitter_mean": feats.jitter_mean,
        "jitter_std": feats.jitter_std,
        "jitter_threshold": feats.jitter_threshold,
        "hold": _distribution_to_dict(feats.hold),
        "segments": {
            "jump": _segment_to_dict(feats.jump),
            "stream": _segment_to_dict(feats.stream),
            "idle": _segment_to_dict(feats.idle),
        },
        "robotic_tap_runs": [
            {
                "start_ms": run.start_ms,
                "end_ms": run.end_ms,
                "tap_count": run.tap_count,
                "dt_ms": run.dt_ms,
            }
            for run in feats.robotic_tap_runs
        ],
        "straight_runs": [
            {
                "start_ms": run.start_ms,
                "frame_count": run.frame_count,
                "distance": run.distance,
            }
            for run in feats.straight_runs
        ],
        "constant_velocity_runs": [
            {
                "start_ms": run.start_ms,
                "frame_count": run.frame_count,
                "velocity": run.velocity,
            }
            for run in feats.constant_velocity_runs
        ],
        "frozen_spans": [
            {
                "start_ms": span.start_ms,
                "x": span.x,
                "y": span.y,
                "frame_count": span.frame_count,
            }
            for span in feats.frozen_spans
        ],
    }


def features_from_dict(data: dict[str, Any]) -> ReplayFeatures:
    """Reconstruct ``ReplayFeatures`` from ``features_to_dict`` output."""
    segments = data["segments"]
    return ReplayFeatures(
        frame_count=data["frame_count"],
        duration_ms=data["duration_ms"],
        tap_count=data["tap_count"],
        uses_keyboard=data["uses_keyboard"],
        hold=_distribution_from_dict(data["hold"]),
        jump=_segment_from_dict(segments["jump"]),
        stream=_segment_from_dict(segments["stream"]),
        idle=_segment_from_dict(segments["idle"]),
        robotic_tap_runs=tuple(
            RoboticTapRun(
                start_ms=run["start_ms"],
                end_ms=run["end_ms"],
                tap_count=run["tap_count"],
                dt_ms=run["dt_ms"],
            )
            for run in data["robotic_tap_runs"]
        ),
        straight_runs=tuple(
            StraightRun(
                start_ms=run["start_ms"],
                frame_count=run["frame_count"],
                distance=run["distance"],
            )
            for run in data["straight_runs"]
        ),
        constant_velocity_runs=tuple(
            ConstantVelocityRun(
                start_ms=run["start_ms"],
                frame_count=run["frame_count"],
                velocity=run["velocity"],
            )
            for run in data["constant_velocity_runs"]
        ),
        frozen_spans=tuple(
            FrozenSpan(
                start_ms=span["start_ms"],
                x=span["x"],
                y=span["y"],
                frame_count=span["frame_count"],
            )
            for span in data["frozen_spans"]
        ),
        total_path=data["total_path"],
        straight_line_displacement=data["straight_line_displacement"],
        tortuosity=data["tortuosity"],
        jitter_mean=data["jitter_mean"],
        jitter_std=data["jitter_std"],
        jitter_threshold=data["jitter_threshold"],
        jitter_spike_count=data["jitter_spike_count"],
    )
