"""Parser for osu! ``.osr`` replay files.

This is a pure decoder for osu!'s own replay export format -- the same bytes
the client uploads on score submission and downloads for playback. The backend
needs the decoded cursor stream to compute behavioural statistics for the
anticheat (see ``app/services/anticheat``); nothing here alters, produces, or
conceals a replay, it only reads what the client already sent.

Format reference (little-endian throughout):

    byte      game mode
    int32     client version (e.g. 20200207)
    string    beatmap md5           (ULEB128-prefixed, 0x0b flag)
    string    player name
    string    replay md5
    int16     n300, n100, n50, ngeki, nkatu, nmiss   (six values)
    int32     total score
    int16     max combo
    byte      perfect (1 = full combo)
    int32     mods
    string    life-bar graph  ("t|hp,..." or empty)
    int64     timestamp (.NET ticks)
    int32     compressed replay length
    bytes     LZMA stream  (frames, ASCII "t|x|y|keys" comma-separated)
    int64     online score id  (absent in some very old replays)

The frame stream's ``t`` values are *deltas* in milliseconds. The client
appends a trailing ``-12345|0|0|seed`` frame carrying the RNG seed, not a real
cursor sample; we surface that seed separately and keep it out of the frames.
"""

from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass
from enum import IntFlag


class ReplayParseError(ValueError):
    """Raised when a byte stream is not a well-formed ``.osr`` replay.

    A subclass of ``ValueError`` so callers can treat a malformed replay as
    bad input rather than an unexpected crash -- the analysis worker marks the
    score unanalysable and moves on instead of dying on one corrupt file.
    """


class ReplayKeys(IntFlag):
    """The key/button bitmask carried by each replay frame.

    osu! reports mouse and keyboard separately, but a keyboard press also sets
    its paired mouse bit (K1 implies M1, K2 implies M2), so tests that care
    about "was a click held" should mask on M1/M2.
    """

    NONE = 0
    MOUSE_1 = 1 << 0
    MOUSE_2 = 1 << 1
    KEY_1 = 1 << 2
    KEY_2 = 1 << 3
    SMOKE = 1 << 4


# the sentinel time on the trailing RNG-seed frame; never a real sample.
_SEED_FRAME_TIME = -12345

# osu! ULEB128 strings are prefixed with 0x00 (absent) or 0x0b (present).
_STRING_ABSENT = 0x00
_STRING_PRESENT = 0x0B


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    """A single cursor sample.

    ``time_delta`` is milliseconds since the previous frame (as stored);
    ``x``/``y`` are osu! playfield coordinates (0-512 x, 0-384 y, but the
    client may report slightly outside that during spinners/edges);
    ``keys`` is the button bitmask at this instant.
    """

    time_delta: int
    x: float
    y: float
    keys: ReplayKeys


@dataclass(frozen=True, slots=True)
class Replay:
    mode: int
    version: int
    beatmap_md5: str
    player_name: str
    replay_md5: str
    n300: int
    n100: int
    n50: int
    ngeki: int
    nkatu: int
    nmiss: int
    score: int
    max_combo: int
    perfect: bool
    mods: int
    life_bar_graph: str
    timestamp: int
    frames: tuple[ReplayFrame, ...]
    rng_seed: int | None
    online_score_id: int


class _Reader:
    """A cursor over the replay bytes with bounds-checked primitives.

    Every read validates it has the bytes it needs first, so a truncated file
    raises ``ReplayParseError`` at the exact field rather than an opaque
    ``struct.error`` or ``IndexError`` from deep in the parse.
    """

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _require(self, n: int, what: str) -> None:
        if self._pos + n > len(self._data):
            raise ReplayParseError(
                f"truncated replay: needed {n} byte(s) for {what} at offset "
                f"{self._pos}, only {len(self._data) - self._pos} remain",
            )

    def read_byte(self, what: str) -> int:
        self._require(1, what)
        value = self._data[self._pos]
        self._pos += 1
        return value

    def _unpack(self, fmt: str, size: int, what: str) -> int:
        self._require(size, what)
        (value,) = struct.unpack_from(fmt, self._data, self._pos)
        self._pos += size
        # every fmt used here is an integer code; struct types it as Any.
        return int(value)

    def read_int16(self, what: str) -> int:
        return self._unpack("<H", 2, what)

    def read_int32(self, what: str) -> int:
        return self._unpack("<I", 4, what)

    def read_int64(self, what: str) -> int:
        return self._unpack("<q", 8, what)

    def read_string(self, what: str) -> str:
        flag = self.read_byte(f"{what} string flag")
        if flag == _STRING_ABSENT:
            return ""
        if flag != _STRING_PRESENT:
            raise ReplayParseError(
                f"bad string flag {flag:#x} for {what} at offset {self._pos - 1}",
            )
        length = self._read_uleb128(what)
        self._require(length, f"{what} string body")
        raw = self._data[self._pos : self._pos + length]
        self._pos += length
        return raw.decode("utf-8", errors="replace")

    def _read_uleb128(self, what: str) -> int:
        result = shift = 0
        while True:
            byte = self.read_byte(f"{what} length")
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 63:  # a 10th continuation byte can't fit a 64-bit length
                raise ReplayParseError(f"overlong ULEB128 length for {what}")

    def read_bytes(self, n: int, what: str) -> bytes:
        self._require(n, what)
        raw = self._data[self._pos : self._pos + n]
        self._pos += n
        return raw

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos


def _decode_frames(raw: bytes) -> tuple[tuple[ReplayFrame, ...], int | None]:
    """Decode the ASCII frame stream into frames plus the RNG seed.

    Each token is ``time|x|y|keys``. The trailing ``-12345|0|0|seed`` token is
    the RNG seed, returned separately; malformed tokens are skipped rather than
    fatal, matching how the client itself tolerates them.
    """
    frames: list[ReplayFrame] = []
    rng_seed: int | None = None

    for token in raw.decode("ascii", errors="replace").split(","):
        token = token.strip()
        if not token:
            continue
        parts = token.split("|")
        if len(parts) != 4:
            continue

        try:
            time_delta = int(parts[0])
            x = float(parts[1])
            y = float(parts[2])
            keys = int(parts[3])
        except ValueError:
            continue

        if time_delta == _SEED_FRAME_TIME:
            rng_seed = keys
            continue

        frames.append(
            ReplayFrame(
                time_delta=time_delta,
                x=x,
                y=y,
                keys=ReplayKeys(keys & 0x1F),
            ),
        )

    return tuple(frames), rng_seed


def parse_osr(data: bytes) -> Replay:
    """Parse a complete ``.osr`` byte stream.

    Raises ``ReplayParseError`` on any structural problem (truncation, bad
    string flag, undecompressable frame stream).
    """
    reader = _Reader(data)

    mode = reader.read_byte("game mode")
    version = reader.read_int32("client version")
    beatmap_md5 = reader.read_string("beatmap md5")
    player_name = reader.read_string("player name")
    replay_md5 = reader.read_string("replay md5")

    n300 = reader.read_int16("n300")
    n100 = reader.read_int16("n100")
    n50 = reader.read_int16("n50")
    ngeki = reader.read_int16("ngeki")
    nkatu = reader.read_int16("nkatu")
    nmiss = reader.read_int16("nmiss")

    score = reader.read_int32("score")
    max_combo = reader.read_int16("max combo")
    perfect = reader.read_byte("perfect") == 1
    mods = reader.read_int32("mods")

    life_bar_graph = reader.read_string("life bar graph")
    timestamp = reader.read_int64("timestamp")

    compressed_length = reader.read_int32("compressed replay length")
    compressed = reader.read_bytes(compressed_length, "compressed replay data")

    # very old replays omit the trailing score id; default to 0 when absent.
    online_score_id = reader.read_int64("online score id") if reader.remaining >= 8 else 0

    frames, rng_seed = _decode_replay_frames(compressed)

    return Replay(
        mode=mode,
        version=version,
        beatmap_md5=beatmap_md5,
        player_name=player_name,
        replay_md5=replay_md5,
        n300=n300,
        n100=n100,
        n50=n50,
        ngeki=ngeki,
        nkatu=nkatu,
        nmiss=nmiss,
        score=score,
        max_combo=max_combo,
        perfect=perfect,
        mods=mods,
        life_bar_graph=life_bar_graph,
        timestamp=timestamp,
        frames=frames,
        rng_seed=rng_seed,
        online_score_id=online_score_id,
    )


def _decode_replay_frames(compressed: bytes) -> tuple[tuple[ReplayFrame, ...], int | None]:
    if not compressed:
        return (), None
    try:
        # osu! writes LZMA1 in the "alone" (.lzma) container; FORMAT_AUTO also
        # accepts the rare xz-wrapped replay without us having to guess.
        raw = lzma.decompress(compressed, format=lzma.FORMAT_AUTO)
    except lzma.LZMAError as exc:
        raise ReplayParseError(f"could not decompress replay frames: {exc}") from exc
    return _decode_frames(raw)


def parse_replay_frame_data(compressed: bytes, *, mode: int = 0) -> Replay:
    """Decode a stored replay's raw frame block (no ``.osr`` header).

    The bytes bancho.py writes to ``.data/osr/{score_id}.osr`` are *only* the
    LZMA frame stream the client uploaded, not a full ``.osr`` file -- the
    header (map, score, mods, ...) lives in the ``scores`` row and is stitched
    back on for download (see ``ReplayService.build_full_replay``). The
    anticheat worker analyses the stored file, so it needs to decode that bare
    stream directly rather than round-tripping through a reconstructed ``.osr``.

    Returns a ``Replay`` carrying the real decoded ``frames`` (and RNG seed);
    the header fields the caller already knows from the score row are left at
    neutral defaults, with ``mode`` filled from the argument. Only ``frames`` is
    read by the feature extractor. Raises ``ReplayParseError`` on an
    undecompressable stream, exactly like ``parse_osr``.
    """
    frames, rng_seed = _decode_replay_frames(compressed)
    return Replay(
        mode=mode,
        version=0,
        beatmap_md5="",
        player_name="",
        replay_md5="",
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
        frames=frames,
        rng_seed=rng_seed,
        online_score_id=0,
    )
