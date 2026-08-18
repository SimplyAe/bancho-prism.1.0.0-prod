"""Safe reading and repacking of osu! beatmap archives (``.osz``).

An ``.osz`` is a plain ZIP holding a beatmap set: one ``.osu`` file per difficulty
plus the audio, backgrounds and (optionally) storyboard they reference. This module
is the only thing that opens one, and it treats every archive as hostile input.

The checks are ordered cheapest-first, and one of them matters more than the rest:
**the uncompressed size is summed from the ZIP's central directory before a single
member is read.** A ZIP records each member's real size in its metadata, so a
"zip bomb" -- a few kilobytes that expand to gigabytes -- can be rejected on the
strength of its own declared sizes, without ever being decompressed. Checking
after extraction is not a check at all; by then the memory is already gone.

Member *names* are validated too (no absolute paths, drive letters, ``..``
segments, or symlinks). Nothing here extracts by member name -- only ``.osu``
members are read, into memory, and the caller writes them to paths built from
ids it allocated -- so these are not guarding an extraction. They keep a hostile
name from surviving into the archive we then serve to other players.

:func:`repack_osz` rewrites the ``.osu`` members with the identity we stamped
(see ``app.adapters.osu_beatmap_file``) and copies every other member through
byte-for-byte, so the audio and background references inside the difficulties
keep resolving.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Final

_OSU_EXTENSION: Final = ".osu"

# stat(2) mode bits live in the high half of a ZIP entry's external_attr when the
# archive was produced by a unix tool; 0o120000 is S_IFLNK.
_UNIX_MODE_SHIFT: Final = 16
_FILE_TYPE_MASK: Final = 0o170000
_SYMLINK_TYPE: Final = 0o120000


class OszArchiveError(ValueError):
    """An uploaded ``.osz`` is not one we are willing to open or host.

    A ``ValueError`` subclass so callers can catch it precisely: every failure
    here means "reject this upload", never "the server is broken".
    """


@dataclass(frozen=True, slots=True)
class OszLimits:
    """Bounds on what an archive may contain.

    Injected rather than hardcoded so the API layer can source them from config,
    and so tests can use small values without building large archives.
    """

    max_archive_bytes: int
    max_total_uncompressed_bytes: int
    max_member_count: int
    max_compression_ratio: float
    max_osu_file_bytes: int
    max_osu_file_count: int


@dataclass(frozen=True, slots=True)
class OszOsuMember:
    """A single difficulty file inside an archive."""

    name: str
    text: str


@dataclass(frozen=True, slots=True)
class OszContents:
    """What we read out of an archive: the difficulties, plus what it weighed."""

    member_count: int
    total_uncompressed_bytes: int
    osu_members: tuple[OszOsuMember, ...]


def read_osz(data: bytes, *, limits: OszLimits) -> OszContents:
    """Validate an ``.osz`` and decode its difficulty files.

    Returns only the ``.osu`` members -- everything else is carried through by
    :func:`repack_osz` without ever being decompressed here.

    Raises :class:`OszArchiveError` for anything malformed, oversized, or unsafe.
    """
    if len(data) > limits.max_archive_bytes:
        raise OszArchiveError(
            f"archive is {len(data)} bytes, over the "
            f"{limits.max_archive_bytes} byte limit",
        )

    with _open_archive(data) as archive:
        infos = archive.infolist()

        if len(infos) > limits.max_member_count:
            raise OszArchiveError(
                f"archive holds {len(infos)} members, over the "
                f"{limits.max_member_count} limit",
            )

        # read from the central directory, before decompressing anything: this is
        # what makes a zip bomb cheap to refuse.
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > limits.max_total_uncompressed_bytes:
            raise OszArchiveError(
                f"archive expands to {total_uncompressed} bytes, over the "
                f"{limits.max_total_uncompressed_bytes} byte limit",
            )

        osu_infos: list[zipfile.ZipInfo] = []
        for info in infos:
            _validate_member(info, limits=limits)
            if info.is_dir():
                continue
            if info.filename.lower().endswith(_OSU_EXTENSION):
                osu_infos.append(info)

        if not osu_infos:
            raise OszArchiveError("archive holds no .osu difficulty files")
        if len(osu_infos) > limits.max_osu_file_count:
            raise OszArchiveError(
                f"archive holds {len(osu_infos)} difficulties, over the "
                f"{limits.max_osu_file_count} limit",
            )

        osu_members: list[OszOsuMember] = []
        for info in osu_infos:
            # the per-file cap is checked against the declared size, again before
            # the read, for the same reason as the total above.
            if info.file_size > limits.max_osu_file_bytes:
                raise OszArchiveError(
                    f"difficulty {info.filename!r} is {info.file_size} bytes, "
                    f"over the {limits.max_osu_file_bytes} byte limit",
                )
            osu_members.append(
                OszOsuMember(
                    name=info.filename,
                    text=_decode_osu_member(archive.read(info), info.filename),
                ),
            )

    return OszContents(
        member_count=len(infos),
        total_uncompressed_bytes=total_uncompressed,
        osu_members=tuple(osu_members),
    )


def repack_osz(data: bytes, *, replacements: Mapping[str, OszOsuMember]) -> bytes:
    """Rebuild an archive with its ``.osu`` members replaced.

    ``replacements`` maps an original member name to its replacement (which may
    carry a new name and new text). Every other member is copied through as raw
    bytes in its original order, so audio/background/storyboard references inside
    the difficulties keep resolving.

    The replacement bytes are exactly ``text.encode("utf-8")`` -- the same bytes
    the caller writes to ``.data/osu/{id}.osu`` -- which is what lets the md5 in
    the database describe both copies.
    """
    buffer = io.BytesIO()

    with _open_archive(data) as source:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                if info.is_dir():
                    continue

                replacement = replacements.get(info.filename)
                if replacement is not None:
                    target.writestr(replacement.name, replacement.text.encode("utf-8"))
                else:
                    target.writestr(info.filename, source.read(info))

    return buffer.getvalue()


def _open_archive(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise OszArchiveError(f"not a readable zip archive: {exc}") from exc


def _validate_member(info: zipfile.ZipInfo, *, limits: OszLimits) -> None:
    name = info.filename

    if not name:
        raise OszArchiveError("archive holds a member with an empty name")
    if "\x00" in name:
        raise OszArchiveError("archive member name contains a NUL byte")

    # zip writers on Windows emit backslashes, so normalise both separators
    # before looking for traversal.
    pure = PureWindowsPath(name)
    if pure.is_absolute() or name.startswith(("/", "\\")):
        raise OszArchiveError(f"archive member {name!r} is an absolute path")
    if pure.drive:
        raise OszArchiveError(f"archive member {name!r} carries a drive letter")
    if ".." in pure.parts:
        raise OszArchiveError(f"archive member {name!r} escapes the archive root")

    if (info.external_attr >> _UNIX_MODE_SHIFT) & _FILE_TYPE_MASK == _SYMLINK_TYPE:
        raise OszArchiveError(f"archive member {name!r} is a symlink")

    # a single member may be under the total cap and still be a bomb on its own.
    if info.compress_size > 0:
        ratio = info.file_size / info.compress_size
        if ratio > limits.max_compression_ratio:
            raise OszArchiveError(
                f"archive member {name!r} has a compression ratio of {ratio:.0f}:1, "
                f"over the {limits.max_compression_ratio:.0f}:1 limit",
            )


def _decode_osu_member(raw: bytes, name: str) -> str:
    # strict UTF-8: a difficulty we cannot decode losslessly is one we cannot
    # rewrite and re-serve without corrupting it.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OszArchiveError(f"difficulty {name!r} is not valid UTF-8") from exc
    return text
