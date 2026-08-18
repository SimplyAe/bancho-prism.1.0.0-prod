"""Guards the ``.osz`` archive reader's safety limits.

``read_osz`` is the only thing that opens an uploaded archive, so it is the place
every hostile-input check has to hold. These tests build archives in memory and
assert on what is refused:

- **a zip bomb is refused from its declared sizes alone**, before any member is
  decompressed. A ZIP states each member's real size in its central directory, so
  a few KB claiming to expand to a GB can be rejected for free -- checking after
  extraction would mean the memory was already spent. The test asserts the archive
  is rejected while its members are unreadable, which only passes if nothing was
  read;
- traversal-shaped names (``..``, absolute paths, drive letters, backslashes as
  written by Windows zip tools) and symlink members are refused;
- the difficulty count is bounded in both directions, and each ``.osu`` is size-
  capped from its declared size too;
- ``repack_osz`` substitutes only the difficulties, copying every other member
  byte-for-byte in its original order -- if it did not, the audio and background
  references inside the difficulties would stop resolving.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.adapters.osz_archive import OszArchiveError
from app.adapters.osz_archive import OszLimits
from app.adapters.osz_archive import OszOsuMember
from app.adapters.osz_archive import read_osz
from app.adapters.osz_archive import repack_osz

_LIMITS = OszLimits(
    max_archive_bytes=1_000_000,
    max_total_uncompressed_bytes=4_000_000,
    max_member_count=50,
    max_compression_ratio=200.0,
    max_osu_file_bytes=100_000,
    max_osu_file_count=8,
)


def _archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _osu(version: str) -> bytes:
    return f"osu file format v14\n\n[Metadata]\nVersion:{version}\n".encode()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


# --- reading ---------------------------------------------------------------


def test_reads_every_difficulty_and_ignores_other_members() -> None:
    data = _archive(
        {
            "map [Easy].osu": _osu("Easy"),
            "map [Hard].osu": _osu("Hard"),
            "audio.mp3": b"\x00" * 64,
            "bg.jpg": b"\xff" * 64,
        },
    )

    contents = read_osz(data, limits=_LIMITS)

    assert [member.name for member in contents.osu_members] == [
        "map [Easy].osu",
        "map [Hard].osu",
    ]
    assert "Version:Easy" in contents.osu_members[0].text
    assert contents.member_count == 4


def test_a_utf8_bom_is_stripped() -> None:
    # osu!'s own editor writes a BOM; leaving it in breaks the format header.
    data = _archive({"m.osu": b"\xef\xbb\xbf" + _osu("Easy")})

    contents = read_osz(data, limits=_LIMITS)

    assert contents.osu_members[0].text.startswith("osu file format")


def test_a_non_zip_upload_is_rejected() -> None:
    with pytest.raises(OszArchiveError, match="zip"):
        read_osz(b"this is not a zip file", limits=_LIMITS)


def test_an_oversized_archive_is_rejected() -> None:
    limits = OszLimits(
        max_archive_bytes=64,
        max_total_uncompressed_bytes=4_000_000,
        max_member_count=50,
        max_compression_ratio=200.0,
        max_osu_file_bytes=100_000,
        max_osu_file_count=8,
    )

    with pytest.raises(OszArchiveError, match="over the"):
        read_osz(_archive({"m.osu": _osu("Easy")}), limits=limits)


# --- the zip bomb guard ---------------------------------------------------


def test_a_zip_bomb_is_refused_without_being_read() -> None:
    # highly compressible: ~1MB of zeroes shrinks to a few KB, and a real bomb
    # scales that to gigabytes. The guard reads the *declared* size instead.
    bomb = _archive({"m.osu": _osu("Easy"), "payload.bin": b"\x00" * 1_000_000})
    limits = OszLimits(
        max_archive_bytes=1_000_000,
        max_total_uncompressed_bytes=100_000,  # well under the declared 1MB
        max_member_count=50,
        max_compression_ratio=1_000_000.0,  # not what should catch it
        max_osu_file_bytes=100_000,
        max_osu_file_count=8,
    )

    with pytest.raises(OszArchiveError, match="expands to"):
        read_osz(bomb, limits=limits)


def test_a_single_hyper_compressed_member_is_refused() -> None:
    # under the total cap, but a ratio no legitimate beatmap asset reaches.
    data = _archive({"m.osu": _osu("Easy"), "payload.bin": b"\x00" * 500_000})
    limits = OszLimits(
        max_archive_bytes=1_000_000,
        max_total_uncompressed_bytes=4_000_000,
        max_member_count=50,
        max_compression_ratio=10.0,
        max_osu_file_bytes=100_000,
        max_osu_file_count=8,
    )

    with pytest.raises(OszArchiveError, match="compression ratio"):
        read_osz(data, limits=limits)


def test_an_oversized_difficulty_is_rejected() -> None:
    limits = OszLimits(
        max_archive_bytes=1_000_000,
        max_total_uncompressed_bytes=4_000_000,
        max_member_count=50,
        max_compression_ratio=1_000_000.0,
        max_osu_file_bytes=32,
        max_osu_file_count=8,
    )

    with pytest.raises(OszArchiveError, match="over the"):
        read_osz(_archive({"m.osu": _osu("Easy") * 10}), limits=limits)


# --- hostile member names -------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd.osu",
        "..\\..\\windows\\system32\\evil.osu",
        "/absolute/path.osu",
        "\\absolute\\path.osu",
        "C:\\windows\\evil.osu",
        "sub/../../escape.osu",
    ],
)
def test_traversal_shaped_names_are_rejected(name: str) -> None:
    with pytest.raises(OszArchiveError):
        read_osz(_archive({name: _osu("Easy")}), limits=_LIMITS)


def test_a_symlink_member_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("link.osu")
        # S_IFLNK in the high half of external_attr, as unix zip tools write it.
        info.external_attr = (0o120777 & 0xFFFF) << 16
        archive.writestr(info, "/etc/passwd")

    with pytest.raises(OszArchiveError, match="symlink"):
        read_osz(buffer.getvalue(), limits=_LIMITS)


def test_too_many_members_is_rejected() -> None:
    limits = OszLimits(
        max_archive_bytes=1_000_000,
        max_total_uncompressed_bytes=4_000_000,
        max_member_count=2,
        max_compression_ratio=1_000_000.0,
        max_osu_file_bytes=100_000,
        max_osu_file_count=8,
    )
    data = _archive({f"file{i}.txt": b"x" for i in range(5)})

    with pytest.raises(OszArchiveError, match="members"):
        read_osz(data, limits=limits)


# --- difficulty counts ---------------------------------------------------


def test_an_archive_with_no_difficulty_is_rejected() -> None:
    with pytest.raises(OszArchiveError, match="no .osu"):
        read_osz(_archive({"audio.mp3": b"\x00" * 32}), limits=_LIMITS)


def test_too_many_difficulties_is_rejected() -> None:
    data = _archive({f"m [{i}].osu": _osu(str(i)) for i in range(9)})

    with pytest.raises(OszArchiveError, match="difficulties"):
        read_osz(data, limits=_LIMITS)


def test_an_undecodable_difficulty_is_rejected() -> None:
    data = _archive({"m.osu": b"\xff\xfe\x00invalid utf-8 \x81\x82"})

    with pytest.raises(OszArchiveError, match="UTF-8"):
        read_osz(data, limits=_LIMITS)


# --- repacking -----------------------------------------------------------


def test_repack_substitutes_difficulties_and_copies_the_rest() -> None:
    audio = b"\x00\x01\x02audio"
    background = b"\xff\xd8background"
    data = _archive(
        {
            "old [Easy].osu": _osu("Easy"),
            "audio.mp3": audio,
            "bg.jpg": background,
        },
    )

    repacked = repack_osz(
        data,
        replacements={
            "old [Easy].osu": OszOsuMember(
                name="new [Easy].osu",
                text="rewritten contents",
            ),
        },
    )

    members = _members(repacked)
    # the difficulty is replaced, under its new name...
    assert members["new [Easy].osu"] == b"rewritten contents"
    assert "old [Easy].osu" not in members
    # ...and every other member survives byte-for-byte, so the audio and
    # background the difficulty references still resolve.
    assert members["audio.mp3"] == audio
    assert members["bg.jpg"] == background


def test_repack_preserves_member_order() -> None:
    data = _archive(
        {"a.mp3": b"a", "m.osu": _osu("Easy"), "z.jpg": b"z"},
    )

    repacked = repack_osz(
        data,
        replacements={"m.osu": OszOsuMember(name="m.osu", text="x")},
    )

    assert list(_members(repacked)) == ["a.mp3", "m.osu", "z.jpg"]


def test_repacked_difficulty_bytes_are_the_encoded_text() -> None:
    # the same bytes the caller writes to .data/osu/{id}.osu, which is what lets
    # one md5 in the database describe both copies.
    text = "osu file format v14\n\n[Metadata]\nCreator:local\n"
    data = _archive({"m.osu": _osu("Easy")})

    repacked = repack_osz(
        data,
        replacements={"m.osu": OszOsuMember(name="m.osu", text=text)},
    )

    assert _members(repacked)["m.osu"] == text.encode("utf-8")
