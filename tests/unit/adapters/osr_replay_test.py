"""Tests for the ``.osr`` replay parser.

The parser is exercised two ways:

- against replays built by a small in-test encoder, so every field and edge
  case (absent strings, the RNG-seed sentinel, truncation, an old replay with
  no trailing score id) is checked deterministically;
- against structural invariants only, never against magic offsets, so the test
  stays readable as the format's own documentation.
"""

from __future__ import annotations

import lzma
import struct

import pytest

from app.adapters.osr_replay import ReplayKeys
from app.adapters.osr_replay import ReplayParseError
from app.adapters.osr_replay import parse_osr
from app.adapters.osr_replay import parse_replay_frame_data


# --- a minimal .osr encoder, test-only -------------------------------------
# mirrors the format the parser decodes, so a round-trip proves the two agree.
# kept here rather than in the app: the server never writes raw .osr from
# scratch (app/services/replays.py rebuilds from DB rows), so this is scaffolding.


def _write_uleb128(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _write_string(text: str | None) -> bytes:
    if not text:
        return b"\x00"
    body = text.encode("utf-8")
    return b"\x0b" + _write_uleb128(len(body)) + body


def _compress_frames(frame_tokens: list[str]) -> bytes:
    raw = ",".join(frame_tokens).encode("ascii")
    # LZMA1 alone-format, exactly what the osu! client emits.
    filters = [{"id": lzma.FILTER_LZMA1, "preset": 6}]
    return lzma.compress(raw, format=lzma.FORMAT_ALONE, filters=filters)


def _build_osr(
    *,
    mode: int = 0,
    version: int = 20200207,
    beatmap_md5: str = "a" * 32,
    player_name: str = "cmyui",
    replay_md5: str = "b" * 32,
    counts: tuple[int, int, int, int, int, int] = (300, 10, 5, 0, 0, 2),
    score: int = 123456,
    max_combo: int = 640,
    perfect: int = 0,
    mods: int = 0,
    life_bar: str = "",
    timestamp: int = 0,
    frame_tokens: list[str] | None = None,
    include_score_id: bool = True,
    online_score_id: int = 999,
) -> bytes:
    if frame_tokens is None:
        frame_tokens = ["0|256|192|0", "16|260|196|5", "-12345|0|0|42"]

    out = bytearray()
    out += struct.pack("<Bi", mode, version)
    out += _write_string(beatmap_md5)
    out += _write_string(player_name)
    out += _write_string(replay_md5)
    out += struct.pack("<hhhhhh", *counts)
    out += struct.pack("<i", score)
    out += struct.pack("<h", max_combo)
    out += struct.pack("<B", perfect)
    out += struct.pack("<i", mods)
    out += _write_string(life_bar)
    out += struct.pack("<q", timestamp)

    compressed = _compress_frames(frame_tokens)
    out += struct.pack("<i", len(compressed))
    out += compressed

    if include_score_id:
        out += struct.pack("<q", online_score_id)
    return bytes(out)


# --- header round-trip ------------------------------------------------------


def test_parses_header_fields() -> None:
    replay = parse_osr(
        _build_osr(
            mode=0,
            player_name="cmyui",
            beatmap_md5="c" * 32,
            counts=(300, 10, 5, 1, 2, 3),
            score=987654,
            max_combo=512,
            perfect=1,
            mods=88,
            online_score_id=777,
        ),
    )

    assert replay.mode == 0
    assert replay.player_name == "cmyui"
    assert replay.beatmap_md5 == "c" * 32
    assert (replay.n300, replay.n100, replay.n50) == (300, 10, 5)
    assert (replay.ngeki, replay.nkatu, replay.nmiss) == (1, 2, 3)
    assert replay.score == 987654
    assert replay.max_combo == 512
    assert replay.perfect is True
    assert replay.mods == 88
    assert replay.online_score_id == 777


def test_absent_optional_string_is_empty() -> None:
    replay = parse_osr(_build_osr(life_bar=""))
    assert replay.life_bar_graph == ""


# --- frame stream -----------------------------------------------------------


def test_parses_frames_and_excludes_the_rng_seed_frame() -> None:
    replay = parse_osr(
        _build_osr(
            frame_tokens=["0|256|192|0", "16|300|200|5", "-12345|0|0|1337"],
        ),
    )

    # the seed sentinel is surfaced separately, not left in the frame list.
    assert replay.rng_seed == 1337
    assert len(replay.frames) == 2

    first, second = replay.frames
    assert (first.time_delta, first.x, first.y) == (0, 256.0, 192.0)
    assert first.keys is ReplayKeys.NONE

    assert second.time_delta == 16
    # keys=5 -> MOUSE_1 (1) | KEY_1 (4)
    assert second.keys is ReplayKeys.MOUSE_1 | ReplayKeys.KEY_1


def test_malformed_frame_tokens_are_skipped_not_fatal() -> None:
    replay = parse_osr(
        _build_osr(
            frame_tokens=["0|256|192|0", "garbage", "16|1|2", "16|300|200|1"],
        ),
    )
    # only the two well-formed, non-seed tokens survive.
    assert len(replay.frames) == 2
    assert replay.rng_seed is None


def test_empty_frame_stream_yields_no_frames() -> None:
    replay = parse_osr(_build_osr(frame_tokens=[]))
    assert replay.frames == ()
    assert replay.rng_seed is None


# --- optional trailing score id --------------------------------------------


def test_missing_trailing_score_id_defaults_to_zero() -> None:
    # some very old replays end right after the frame stream.
    replay = parse_osr(_build_osr(include_score_id=False))
    assert replay.online_score_id == 0


# --- error handling ---------------------------------------------------------


def test_truncated_replay_raises_replay_parse_error() -> None:
    full = _build_osr()
    with pytest.raises(ReplayParseError):
        parse_osr(full[:10])


def test_bad_string_flag_raises_replay_parse_error() -> None:
    data = bytearray(_build_osr())
    # the byte right after mode(1) + version(4) is the beatmap-md5 string flag.
    data[5] = 0x07  # neither 0x00 nor 0x0b
    with pytest.raises(ReplayParseError):
        parse_osr(bytes(data))


def test_undecompressable_frame_block_raises_replay_parse_error() -> None:
    out = bytearray()
    out += struct.pack("<Bi", 0, 20200207)
    out += _write_string("a" * 32)
    out += _write_string("player")
    out += _write_string("b" * 32)
    out += struct.pack("<hhhhhh", 1, 0, 0, 0, 0, 0)
    out += struct.pack("<i", 1)
    out += struct.pack("<h", 1)
    out += struct.pack("<B", 0)
    out += struct.pack("<i", 0)
    out += _write_string("")
    out += struct.pack("<q", 0)
    garbage = b"\xff\xff\xff\xff\xff\xff"
    out += struct.pack("<i", len(garbage))
    out += garbage
    out += struct.pack("<q", 0)
    with pytest.raises(ReplayParseError):
        parse_osr(bytes(out))


# -- parse_replay_frame_data: the bare frame block the worker reads off disk --
#
# The files under .data/osr/{score_id}.osr are only the LZMA frame stream, not a
# full .osr; the header lives in the scores row. These pin that the worker's
# frame-only entry decodes that stream and leaves the header at neutral defaults.


def test_frame_data_decodes_frames_and_rng_seed() -> None:
    replay = parse_replay_frame_data(
        _compress_frames(["0|256|192|0", "16|300|200|5", "-12345|0|0|9001"]),
    )

    # the two real frames survive; the trailing -12345 token is the RNG seed.
    assert len(replay.frames) == 2
    assert replay.rng_seed == 9001
    assert replay.frames[0].x == 256.0
    assert replay.frames[1].keys == ReplayKeys(5)


def test_frame_data_fills_mode_and_leaves_header_neutral() -> None:
    replay = parse_replay_frame_data(_compress_frames(["0|1|2|0"]), mode=3)

    # mode comes from the score row (the caller); the rest of the header is not
    # in the stored bytes, so it is left neutral rather than invented.
    assert replay.mode == 3
    assert replay.version == 0
    assert replay.beatmap_md5 == ""
    assert replay.player_name == ""


def test_frame_data_empty_stream_yields_no_frames() -> None:
    # an empty stored blob decodes to zero frames (extract_features tolerates it)
    # rather than raising -- distinct from a missing file, which never reaches here.
    replay = parse_replay_frame_data(b"")

    assert replay.frames == ()
    assert replay.rng_seed is None


def test_frame_data_undecompressable_raises_replay_parse_error() -> None:
    with pytest.raises(ReplayParseError):
        parse_replay_frame_data(b"\xff\xff\xff\xff\xff\xff")
