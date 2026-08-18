"""Pure parsing and rewriting of osu! beatmap (``.osu``) files.

Everything here is deterministic text handling with no I/O, so the submission
pipeline's understanding of an uploaded difficulty is testable without a database,
a filesystem, or a real beatmap. The format is osu!'s own: a ``osu file format
vN`` header followed by ``[Section]`` blocks of ``Key: value`` pairs, then two
comma-separated data sections (``[TimingPoints]`` and ``[HitObjects]``).

Three jobs:

- :func:`parse_osu_file` pulls out the metadata the ``maps`` table needs, and
  **derives** the two values the file does not state directly (``bpm`` and
  ``total_length``). It is strict: a missing required field raises rather than
  defaulting, because a half-parsed beatmap would be written to the database and
  served to clients as if it were complete.
- :func:`rewrite_osu_file_identity` stamps our own ids and creator into the file.
  An uploaded ``.osz`` is very often derived from an existing map and still
  carries *that* map's ``BeatmapID``/``BeatmapSetID``; leaving those in place
  would make the client associate the local file with a real beatmap. Rewriting
  ``Creator`` likewise keeps song-select consistent with ``maps.creator``, so
  attribution cannot be spoofed in one place and not the other.
- :func:`canonical_osu_filename` builds the on-disk/DB filename using the exact
  convention ``Beatmap._parse_from_osuapi_resp`` uses for mirrored maps, so a
  privately-hosted row is indistinguishable to every existing filename lookup.

Version quirks that matter, both handled below: ``ApproachRate`` only exists from
format v8 -- before that osu! reused ``OverallDifficulty``, so an absent AR must
fall back to OD rather than to zero; and ``[TimingPoints]`` lines may carry only
``time,beatLength`` in old files, where the ``uninherited`` flag is implied.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

# stripped from filenames, matching `app.objects.beatmap.IGNORED_BEATMAP_CHARS`
# so private and mirrored maps produce byte-identical names for equal metadata.
IGNORED_BEATMAP_CHARS: Final = dict.fromkeys(map(ord, r':\/*<>?"|'), None)

# osu! caps usernames/metadata well below this; the cap exists so a hostile file
# cannot produce a filename the filesystem rejects.
_MAX_FILENAME_LENGTH: Final = 200

# the `maps` columns these land in are varchar(128).
_MAX_METADATA_LENGTH: Final = 128

# format versions we will parse. v3 is the oldest with the sections we need; the
# upper bound is a sanity check against a malformed header, not a real limit.
_MIN_FORMAT_VERSION: Final = 3
_MAX_FORMAT_VERSION: Final = 128

_HEADER_PATTERN: Final = re.compile(r"^﻿?osu file format v(\d+)\s*$")
_SECTION_PATTERN: Final = re.compile(r"^\[([^\]]+)\]\s*$")

# hit-object type bitmask (`x,y,time,type,hitSound,...`). Sliders are `2`, but
# their end time is not stated in the file -- see `_hit_object_end_time`.
_HIT_OBJECT_SPINNER: Final = 1 << 3
_HIT_OBJECT_MANIA_HOLD: Final = 1 << 7

_REQUIRED_SECTIONS: Final = (
    "General",
    "Metadata",
    "Difficulty",
    "TimingPoints",
    "HitObjects",
)


class OsuFileParseError(ValueError):
    """An uploaded ``.osu`` file is not one we can host.

    A ``ValueError`` subclass so the caller can catch it precisely: every failure
    here is "this upload is bad", never "the server is broken".
    """


@dataclass(frozen=True, slots=True)
class OsuFileMetadata:
    """The slice of a ``.osu`` file the ``maps`` table records.

    ``declared_creator`` is the *file's* claim about who made the map. It is kept
    separate from anything authoritative because it is attacker-controlled: the
    ``maps.creator`` column is always filled from the submitting account instead.
    """

    format_version: int
    artist: str
    title: str
    version: str
    declared_creator: str
    mode: int
    hp: float
    cs: float
    od: float
    ar: float
    bpm: float
    total_length: int
    hit_object_count: int


def parse_osu_file(text: str) -> OsuFileMetadata:
    """Parse a ``.osu`` file's text into the metadata we store.

    Raises :class:`OsuFileParseError` if the file is malformed, is missing a
    section or field we need, or states a value outside osu!'s own ranges.
    """
    lines = text.splitlines()
    format_version = _parse_format_version(lines)
    sections = _split_sections(lines)

    for section in _REQUIRED_SECTIONS:
        if section not in sections:
            raise OsuFileParseError(f"missing [{section}] section")

    general = _parse_key_values(sections["General"])
    metadata = _parse_key_values(sections["Metadata"])
    difficulty = _parse_key_values(sections["Difficulty"])

    artist = _require_text(metadata, "Artist")
    title = _require_text(metadata, "Title")
    version = _require_text(metadata, "Version")
    # a missing Creator is tolerated: we never trust this value anyway.
    declared_creator = (metadata.get("Creator") or "").strip()[:_MAX_METADATA_LENGTH]

    mode = _parse_mode(general.get("Mode"))
    hp = _require_difficulty_value(difficulty, "HPDrainRate", maximum=10.0)
    od = _require_difficulty_value(difficulty, "OverallDifficulty", maximum=10.0)
    # mania's CircleSize is the key count (1..18), not a 0..10 difficulty stat.
    cs = _require_difficulty_value(
        difficulty,
        "CircleSize",
        maximum=18.0 if mode == 3 else 10.0,
    )
    # ApproachRate arrived in format v8; before that osu! used OverallDifficulty.
    ar = (
        _parse_difficulty_value(
            difficulty["ApproachRate"],
            "ApproachRate",
            maximum=10.0,
        )
        if "ApproachRate" in difficulty
        else od
    )

    # the hit objects come first: the last timing point runs to the end of the
    # map, so weighting the tempos needs to know where that is.
    total_length, hit_object_count, map_end_time = _parse_hit_object_span(
        sections["HitObjects"],
    )
    bpm = _parse_bpm(sections["TimingPoints"], map_end_time=map_end_time)

    return OsuFileMetadata(
        format_version=format_version,
        artist=artist,
        title=title,
        version=version,
        declared_creator=declared_creator,
        mode=mode,
        hp=hp,
        cs=cs,
        od=od,
        ar=ar,
        bpm=bpm,
        total_length=total_length,
        hit_object_count=hit_object_count,
    )


def rewrite_osu_file_identity(
    text: str,
    *,
    beatmap_id: int,
    beatmap_set_id: int,
    creator: str,
) -> str:
    """Stamp our ids and creator into a ``.osu`` file's ``[Metadata]``.

    Replaces ``BeatmapID``, ``BeatmapSetID`` and ``Creator`` where present and
    appends them where absent, leaving every other byte -- including the file's
    line-ending style -- untouched. The client keys its local beatmap database off
    these ids, so a file still carrying the ids of whatever map it was derived
    from would be filed under that map.
    """
    replacements = {
        "BeatmapID": str(beatmap_id),
        "BeatmapSetID": str(beatmap_set_id),
        "Creator": creator,
    }
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    metadata_bounds = _section_bounds(lines, "Metadata")
    if metadata_bounds is None:
        raise OsuFileParseError("missing [Metadata] section")
    start, end = metadata_bounds

    written: set[str] = set()
    rewritten: list[str] = []
    for index, line in enumerate(lines):
        if start <= index < end:
            key = line.split(":", 1)[0].strip() if ":" in line else ""
            if key in replacements:
                # `[Metadata]` uses `Key:value` with no space after the colon,
                # unlike [General]/[Difficulty]; match the section's own style.
                rewritten.append(f"{key}:{replacements[key]}")
                written.add(key)
                continue
        rewritten.append(line)

    missing = [key for key in replacements if key not in written]
    if missing:
        # append to the end of [Metadata], not the end of the file, or the keys
        # would land in whatever section happens to follow.
        insert_at = end
        for key in reversed(missing):
            rewritten.insert(insert_at, f"{key}:{replacements[key]}")

    trailing = newline if text.endswith(("\n", "\r")) else ""
    return newline.join(rewritten) + trailing


def canonical_osu_filename(
    *,
    artist: str,
    title: str,
    creator: str,
    version: str,
) -> str:
    """Build the ``maps.filename`` value for a difficulty.

    Byte-identical to what ``Beatmap._parse_from_osuapi_resp`` produces for a
    mirrored map with the same metadata, so existing filename lookups (osu!'s
    ``osu-getbeatmapinfo`` and the leaderboard's unsubmitted check) treat a
    privately-hosted map exactly like any other.
    """
    filename = f"{artist} - {title} ({creator}) [{version}].osu".translate(
        IGNORED_BEATMAP_CHARS,
    )
    # C0 controls and collapsed whitespace: not osu!'s doing, but a hostile
    # upload's. Trailing dots/spaces are rejected outright by Windows.
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    filename = re.sub(r"\s+", " ", filename).strip()

    if len(filename) > _MAX_FILENAME_LENGTH:
        stem = filename[: _MAX_FILENAME_LENGTH - len(".osu")].rstrip(" .")
        filename = f"{stem}.osu"

    # The scaffolding (" - ", "()", "[]", ".osu") always survives sanitising, so a
    # non-empty result proves nothing -- metadata of all-stripped characters yields
    # " -  () [].osu". Check that some real metadata is left instead.
    stem = filename.removesuffix(".osu")
    if not re.sub(r"[\s\-()\[\].]", "", stem):
        raise OsuFileParseError("beatmap metadata produces an empty filename")

    if filename.startswith("."):
        raise OsuFileParseError("beatmap metadata produces a hidden filename")

    return filename


def _parse_format_version(lines: list[str]) -> int:
    for line in lines:
        if not line.strip():
            continue
        match = _HEADER_PATTERN.match(line.strip())
        if match is None:
            raise OsuFileParseError("missing or malformed 'osu file format' header")
        version = int(match.group(1))
        if not _MIN_FORMAT_VERSION <= version <= _MAX_FORMAT_VERSION:
            raise OsuFileParseError(f"unsupported osu file format v{version}")
        return version
    raise OsuFileParseError("file is empty")


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Group the file's lines by section, dropping comments and blanks."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        match = _SECTION_PATTERN.match(stripped)
        if match is not None:
            # unknown sections are kept rather than rejected: osu! adds them over
            # time and none of them affect what we read.
            current = sections.setdefault(match.group(1), [])
            continue

        if current is not None:
            current.append(stripped)

    return sections


def _section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """The half-open line range of a section's *body*, or None if absent."""
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped == f"[{name}]":
                start = index + 1
        elif _SECTION_PATTERN.match(stripped) is not None:
            return start, index

    if start is None:
        return None
    return start, len(lines)


def _parse_key_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def _require_text(values: dict[str, str], key: str) -> str:
    value = (values.get(key) or "").strip()
    if not value:
        raise OsuFileParseError(f"missing required field '{key}'")
    return value[:_MAX_METADATA_LENGTH]


def _parse_mode(raw_mode: str | None) -> int:
    if raw_mode is None or not raw_mode.strip():
        return 0  # absent means osu!standard
    try:
        mode = int(float(raw_mode))
    except ValueError:
        raise OsuFileParseError(f"invalid Mode value {raw_mode!r}") from None
    if not 0 <= mode <= 3:
        raise OsuFileParseError(f"unsupported Mode {mode}")
    return mode


def _require_difficulty_value(
    values: dict[str, str],
    key: str,
    *,
    maximum: float,
) -> float:
    if key not in values:
        raise OsuFileParseError(f"missing required field '{key}'")
    return _parse_difficulty_value(values[key], key, maximum=maximum)


def _parse_difficulty_value(raw: str, key: str, *, maximum: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise OsuFileParseError(f"invalid {key} value {raw!r}") from None
    if math.isnan(value) or math.isinf(value) or not 0.0 <= value <= maximum:
        raise OsuFileParseError(f"{key} {raw!r} is out of range (0..{maximum:g})")
    return value


def _parse_bpm(timing_point_lines: list[str], *, map_end_time: float) -> float:
    """The map's headline BPM: the *duration-weighted* uninherited timing point.

    A map may change tempo many times; osu! reports the tempo in effect for the
    longest stretch, so picking the first (or fastest) point would disagree with
    what players see. Inherited points carry a negative ``beatLength`` -- they are
    slider-velocity multipliers, not tempo -- and are skipped entirely.

    ``map_end_time`` bounds the final point's stretch (see the loop below).
    """
    points: list[tuple[float, float]] = []  # (time, beat_length)

    for line in timing_point_lines:
        fields = line.split(",")
        if len(fields) < 2:
            continue
        try:
            time = float(fields[0])
            beat_length = float(fields[1])
        except ValueError:
            continue

        # pre-v6 files omit `uninherited`; a positive beatLength implies it.
        uninherited = True
        if len(fields) >= 7:
            uninherited = fields[6].strip() not in ("0", "")

        if uninherited and beat_length > 0:
            points.append((time, beat_length))

    if not points:
        raise OsuFileParseError("no uninherited timing points")

    points.sort(key=lambda point: point[0])

    # Weight each tempo by how long it is actually in effect, with the final point
    # running to the end of the map. Using a nominal span for that last point
    # instead would let a short fast intro outweigh a long slow body -- the exact
    # case this is here to get right.
    durations: dict[float, float] = {}
    first_seen: dict[float, int] = {}
    for index, (time, beat_length) in enumerate(points):
        if index + 1 < len(points):
            end = points[index + 1][0]
        else:
            end = max(map_end_time, time)
        durations[beat_length] = durations.get(beat_length, 0.0) + max(end - time, 0.0)
        first_seen.setdefault(beat_length, index)

    # `points` is time-sorted, so on a tie the tempo that appears first wins --
    # what a player would call the map's BPM. Without a tiebreak the answer would
    # depend on dict ordering.
    dominant_beat_length = max(
        durations,
        key=lambda length: (durations[length], -first_seen[length]),
    )
    return round(60_000 / dominant_beat_length, 2)


def _parse_hit_object_span(hit_object_lines: list[str]) -> tuple[int, int, float]:
    """``(total_length_seconds, hit_object_count, map_end_time_ms)``.

    ``total_length`` is first note to last note, matching osu!api's own
    definition (breaks included, audio lead-in excluded). The raw end time is
    returned alongside it because the BPM weighting needs it.
    """
    first_time: float | None = None
    last_end: float = 0.0
    count = 0

    for line in hit_object_lines:
        fields = line.split(",")
        if len(fields) < 4:
            continue
        try:
            time = float(fields[2])
            object_type = int(fields[3])
        except ValueError:
            continue

        count += 1
        if first_time is None or time < first_time:
            first_time = time
        last_end = max(last_end, _hit_object_end_time(fields, time, object_type))

    if first_time is None:
        raise OsuFileParseError("no hit objects")

    return (
        math.ceil(max(last_end - first_time, 0.0) / 1000),
        count,
        last_end,
    )


def _hit_object_end_time(fields: list[str], time: float, object_type: int) -> float:
    """When an object finishes.

    Spinners and mania holds state an explicit end time. A slider's duration is
    *not* in the file -- deriving it needs SliderMultiplier, the inherited SV in
    effect, pixel length and repeat count. We approximate a slider by its start
    time, so `total_length` can under-report by at most the final slider's
    duration (typically <3s). That value only drives the `!np` completion percent
    and the multiplayer auto-start timer, neither of which needs exactness.
    """
    if object_type & (_HIT_OBJECT_SPINNER | _HIT_OBJECT_MANIA_HOLD):
        # spinner: `...,endTime,hitSample`; mania hold: `...,endTime:hitSample`
        if len(fields) >= 6:
            raw_end = fields[5].split(":", 1)[0].strip()
            try:
                return max(time, float(raw_end))
            except ValueError:
                return time
    return time
