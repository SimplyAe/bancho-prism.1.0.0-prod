"""Guards the pure ``.osu`` file parsing, rewriting, and naming.

``app.adapters.osu_beatmap_file`` does no I/O, so these tests need nothing faked
-- they build ``.osu`` text and assert on the result. What they pin:

- the metadata that lands in the ``maps`` table, including the two values the
  file does not state outright: ``bpm`` (the *duration-weighted* tempo, since a
  map may change tempo and osu! reports the one in effect longest) and
  ``total_length`` (first note to last note);
- **``ApproachRate`` falls back to ``OverallDifficulty``**, not to zero, because
  AR only exists from format v8 and that is what osu! itself does -- getting this
  wrong silently publishes every old map as AR 0;
- strictness in one direction: a missing section or field raises rather than
  defaulting, so a half-parsed beatmap is never written to the database;
- ``rewrite_osu_file_identity`` changes exactly the three identity lines and
  nothing else, preserving the file's line endings -- the client files a beatmap
  by the ids inside it, so a leftover ``BeatmapSetID`` would attach an upload to
  a real map;
- ``canonical_osu_filename`` is byte-identical to the name
  ``Beatmap._parse_from_osuapi_resp`` builds for a mirrored map, which is what
  makes existing filename lookups treat a private map like any other.
"""

from __future__ import annotations

import pytest

from app.adapters.osu_beatmap_file import IGNORED_BEATMAP_CHARS
from app.adapters.osu_beatmap_file import OsuFileParseError
from app.adapters.osu_beatmap_file import canonical_osu_filename
from app.adapters.osu_beatmap_file import parse_osu_file
from app.adapters.osu_beatmap_file import rewrite_osu_file_identity


def _osu_file(
    *,
    format_version: int = 14,
    general: str = "Mode: 0",
    metadata: str = (
        "Artist:Camellia\nTitle:Ghost\nCreator:someone\nVersion:Insane\n"
        "BeatmapID:12345\nBeatmapSetID:999"
    ),
    difficulty: str = (
        "HPDrainRate:5\nCircleSize:4\nOverallDifficulty:8\nApproachRate:9"
    ),
    timing_points: str = "0,300,4,2,0,60,1,0",
    hit_objects: str = "256,192,1000,1,0\n256,192,4000,1,0",
    newline: str = "\n",
) -> str:
    return newline.join(
        [
            f"osu file format v{format_version}",
            "",
            "[General]",
            general,
            "",
            "[Metadata]",
            metadata,
            "",
            "[Difficulty]",
            difficulty,
            "",
            "[TimingPoints]",
            timing_points,
            "",
            "[HitObjects]",
            hit_objects,
            "",
        ],
    )


# --- metadata --------------------------------------------------------------


def test_parses_every_stored_field() -> None:
    parsed = parse_osu_file(_osu_file())

    assert parsed.format_version == 14
    assert parsed.artist == "Camellia"
    assert parsed.title == "Ghost"
    assert parsed.version == "Insane"
    assert parsed.declared_creator == "someone"
    assert parsed.mode == 0
    assert (parsed.hp, parsed.cs, parsed.od, parsed.ar) == (5.0, 4.0, 8.0, 9.0)
    assert parsed.bpm == 200.0  # 60000 / 300
    assert parsed.total_length == 3  # 1000ms -> 4000ms
    assert parsed.hit_object_count == 2


def test_absent_mode_is_osu_standard() -> None:
    parsed = parse_osu_file(_osu_file(general="AudioFilename: audio.mp3"))
    assert parsed.mode == 0


def test_comments_and_unknown_sections_are_ignored() -> None:
    text = _osu_file().replace(
        "[HitObjects]",
        "[Editor]\nBookmarks: 1,2\n\n// a comment\n[HitObjects]",
    )
    assert parse_osu_file(text).hit_object_count == 2


# --- the ApproachRate fallback (format v8) --------------------------------


def test_absent_approach_rate_falls_back_to_overall_difficulty() -> None:
    # pre-v8 files have no ApproachRate; osu! reuses OverallDifficulty. Reading
    # it as 0.0 would publish every old map as AR 0.
    parsed = parse_osu_file(
        _osu_file(
            format_version=7,
            difficulty="HPDrainRate:5\nCircleSize:4\nOverallDifficulty:7.5",
        ),
    )

    assert parsed.ar == 7.5
    assert parsed.od == 7.5


# --- bpm derivation --------------------------------------------------------


def test_bpm_is_the_tempo_in_effect_longest() -> None:
    # 200 BPM for the first 5s, then 120 BPM for a minute. The headline BPM is
    # the dominant one, not the first and not the fastest.
    parsed = parse_osu_file(
        _osu_file(
            timing_points="0,300,4,2,0,60,1,0\n5000,500,4,2,0,60,1,0",
            hit_objects="256,192,0,1,0\n256,192,65000,1,0",
        ),
    )

    assert parsed.bpm == 120.0


def test_inherited_timing_points_never_set_the_bpm() -> None:
    # an inherited point's negative "beatLength" is a slider-velocity percentage,
    # not a tempo; treating it as one yields a negative or nonsense BPM.
    parsed = parse_osu_file(
        _osu_file(timing_points="0,400,4,2,0,60,1,0\n100,-50,4,2,0,60,0,0"),
    )

    assert parsed.bpm == 150.0  # 60000 / 400


def test_old_timing_points_without_the_uninherited_flag_are_accepted() -> None:
    # pre-v6 lines carry only `time,beatLength`.
    parsed = parse_osu_file(_osu_file(timing_points="0,250"))
    assert parsed.bpm == 240.0


def test_no_uninherited_timing_point_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="uninherited"):
        parse_osu_file(_osu_file(timing_points="0,-50,4,2,0,60,0,0"))


# --- total_length ----------------------------------------------------------


def test_spinner_end_time_extends_the_length() -> None:
    # type 12 = spinner (8) + new combo (4); field 5 is its end time.
    parsed = parse_osu_file(
        _osu_file(hit_objects="256,192,1000,1,0\n256,192,2000,12,0,9000,0:0:0:0:"),
    )

    assert parsed.total_length == 8  # 1000ms -> 9000ms


def test_mania_hold_end_time_extends_the_length() -> None:
    # type 128 = mania hold; its end time is `endTime:hitSample`.
    parsed = parse_osu_file(
        _osu_file(
            general="Mode: 3",
            difficulty="HPDrainRate:5\nCircleSize:7\nOverallDifficulty:8",
            hit_objects="64,192,1000,1,0\n64,192,2000,128,0,7000:0:0:0:0:",
        ),
    )

    assert parsed.total_length == 6  # 1000ms -> 7000ms


def test_a_trailing_slider_is_measured_from_its_start() -> None:
    # documented divergence: a slider's duration is not in the file, so the map
    # can under-report by that one slider's length.
    parsed = parse_osu_file(
        _osu_file(hit_objects="256,192,0,1,0\n256,192,5000,2,0,L|320:192,1,140"),
    )

    assert parsed.total_length == 5


def test_no_hit_objects_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="hit objects"):
        parse_osu_file(_osu_file(hit_objects=""))


# --- mania key count ------------------------------------------------------


def test_mania_accepts_a_key_count_above_ten() -> None:
    # in mania CircleSize is the key count (up to 18), not a 0..10 stat.
    parsed = parse_osu_file(
        _osu_file(
            general="Mode: 3",
            difficulty="HPDrainRate:5\nCircleSize:7\nOverallDifficulty:8",
        ),
    )
    assert parsed.cs == 7.0


def test_an_impossible_key_count_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="CircleSize"):
        parse_osu_file(
            _osu_file(
                general="Mode: 3",
                difficulty="HPDrainRate:5\nCircleSize:19\nOverallDifficulty:8",
            ),
        )


def test_standard_rejects_a_circle_size_above_ten() -> None:
    with pytest.raises(OsuFileParseError, match="CircleSize"):
        parse_osu_file(
            _osu_file(difficulty="HPDrainRate:5\nCircleSize:11\nOverallDifficulty:8"),
        )


# --- malformed input ------------------------------------------------------


def test_a_missing_header_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="header"):
        parse_osu_file("[Metadata]\nArtist:x\n")


def test_an_unsupported_format_version_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="format"):
        parse_osu_file(_osu_file(format_version=1))


def test_an_empty_file_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="empty"):
        parse_osu_file("")


@pytest.mark.parametrize(
    "section",
    ["General", "Metadata", "Difficulty", "TimingPoints", "HitObjects"],
)
def test_every_required_section_is_required(section: str) -> None:
    text = _osu_file().replace(f"[{section}]", "[Unused]")

    with pytest.raises(OsuFileParseError, match=section):
        parse_osu_file(text)


@pytest.mark.parametrize("field", ["Artist", "Title", "Version"])
def test_required_metadata_fields_are_required(field: str) -> None:
    text = _osu_file().replace(f"{field}:", "Unused:")

    with pytest.raises(OsuFileParseError, match=field):
        parse_osu_file(text)


@pytest.mark.parametrize(
    "field",
    ["HPDrainRate", "CircleSize", "OverallDifficulty"],
)
def test_required_difficulty_fields_are_required(field: str) -> None:
    text = _osu_file().replace(f"{field}:", "Unused:")

    with pytest.raises(OsuFileParseError, match=field):
        parse_osu_file(text)


def test_a_non_numeric_difficulty_value_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="OverallDifficulty"):
        parse_osu_file(
            _osu_file(difficulty="HPDrainRate:5\nCircleSize:4\nOverallDifficulty:hard"),
        )


def test_an_unsupported_mode_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="Mode"):
        parse_osu_file(_osu_file(general="Mode: 7"))


# --- identity rewriting ---------------------------------------------------


def _metadata_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index("[Metadata]") + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("[")),
        len(lines),
    )
    return [line for line in lines[start:end] if line]


def test_rewrite_replaces_the_identity_fields() -> None:
    rewritten = rewrite_osu_file_identity(
        _osu_file(),
        beatmap_id=2_000_000_001,
        beatmap_set_id=2_000_000_000,
        creator="localplayer",
    )

    metadata = _metadata_lines(rewritten)
    assert "BeatmapID:2000000001" in metadata
    assert "BeatmapSetID:2000000000" in metadata
    assert "Creator:localplayer" in metadata
    # the ids the upload arrived with must be gone, or the client files this
    # beatmap under whatever real map it was derived from.
    assert "BeatmapID:12345" not in rewritten
    assert "BeatmapSetID:999" not in rewritten
    assert "Creator:someone" not in rewritten


def test_rewrite_appends_identity_fields_when_absent() -> None:
    rewritten = rewrite_osu_file_identity(
        _osu_file(metadata="Artist:a\nTitle:t\nVersion:v"),
        beatmap_id=7,
        beatmap_set_id=8,
        creator="c",
    )

    metadata = _metadata_lines(rewritten)
    assert "BeatmapID:7" in metadata
    assert "BeatmapSetID:8" in metadata
    assert "Creator:c" in metadata
    # appended inside [Metadata], not leaked into the next section.
    assert parse_osu_file(rewritten).declared_creator == "c"


def test_rewrite_changes_only_the_identity_lines() -> None:
    original = _osu_file()
    rewritten = rewrite_osu_file_identity(
        original,
        beatmap_id=7,
        beatmap_set_id=8,
        creator="c",
    )

    changed = set(original.splitlines()) - set(rewritten.splitlines())
    assert changed == {"BeatmapID:12345", "BeatmapSetID:999", "Creator:someone"}


def test_rewrite_preserves_crlf_line_endings() -> None:
    rewritten = rewrite_osu_file_identity(
        _osu_file(newline="\r\n"),
        beatmap_id=7,
        beatmap_set_id=8,
        creator="c",
    )

    assert "\r\n" in rewritten
    assert "\n" not in rewritten.replace("\r\n", "")


def test_a_rewritten_file_still_parses() -> None:
    rewritten = rewrite_osu_file_identity(
        _osu_file(),
        beatmap_id=7,
        beatmap_set_id=8,
        creator="c",
    )

    parsed = parse_osu_file(rewritten)
    assert parsed.artist == "Camellia"
    assert parsed.bpm == 200.0


# --- filenames ------------------------------------------------------------


def test_filename_matches_the_osuapi_convention() -> None:
    # the exact expression `Beatmap._parse_from_osuapi_resp` uses.
    expected = "{artist} - {title} ({creator}) [{version}].osu".format(
        artist="Camellia",
        title="Ghost",
        creator="localplayer",
        version="Insane",
    ).translate(IGNORED_BEATMAP_CHARS)

    assert (
        canonical_osu_filename(
            artist="Camellia",
            title="Ghost",
            creator="localplayer",
            version="Insane",
        )
        == expected
    )


def test_filename_strips_filesystem_hostile_characters() -> None:
    filename = canonical_osu_filename(
        artist="a/b",
        title='t:"x"',
        creator="c\\d",
        version="v?|*",
    )

    assert not any(char in filename for char in r':\/*<>?"|')
    assert filename.endswith(".osu")


def test_filename_is_truncated_but_keeps_its_extension() -> None:
    filename = canonical_osu_filename(
        artist="a" * 300,
        title="t",
        creator="c",
        version="v",
    )

    assert len(filename) <= 200
    assert filename.endswith(".osu")


def test_filename_that_would_be_empty_is_rejected() -> None:
    with pytest.raises(OsuFileParseError, match="filename"):
        canonical_osu_filename(artist="/", title="/", creator="/", version="/")
