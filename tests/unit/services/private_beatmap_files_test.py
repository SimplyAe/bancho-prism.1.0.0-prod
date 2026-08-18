"""Guards the lookup behind serving hosted beatmap files to the osu! client.

The client asks for a difficulty by filename (``/web/maps/{filename}``) and a set
by id (``/d/{set_id}``). Stock bancho.py redirects both to osu! and to a mirror,
neither of which has ever heard of a map hosted only here. This service decides
whether *we* have the file, and the routes fall back to their redirect when it
says no -- so mirrored maps keep working untouched.

Three properties are load-bearing:

- **a mirrored map is never claimed as ours.** Returning a hosted file for an
  osu!-hosted row would serve the wrong bytes for a real beatmap;
- **a missing file falls back rather than failing.** The caller is the game client,
  so a row whose file vanished has to degrade to the redirect, not a 500 that
  leaves a player unable to load the map;
- **a download honours the submission's visibility.** The API refuses to show a
  pending or rejected submission, so the download endpoint must not become the way
  to read one.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.repositories.map_id_sequence import PRIVATE_BEATMAP_ID_FLOOR
from app.repositories.map_submissions import MapSubmission
from app.repositories.map_submissions import MapSubmissionReviewState
from app.repositories.maps import Map
from app.repositories.maps import MapServer
from app.services.private_beatmap_files import PrivateBeatmapFilesService

_NOW = datetime(2026, 8, 18, 12, 0, 0)
_SET_ID = PRIVATE_BEATMAP_ID_FLOOR
_MAP_ID = PRIVATE_BEATMAP_ID_FLOOR + 1

_HOSTED_FILENAME = "Camellia - Ghost (submitter) [Insane].osu"


def _map(
    *,
    map_id: int = _MAP_ID,
    set_id: int = _SET_ID,
    filename: str = _HOSTED_FILENAME,
    server: str = MapServer.PRIVATE.value,
    artist: str = "Camellia",
    title: str = "Ghost",
) -> Map:
    return Map(
        id=map_id,
        server=server,
        set_id=set_id,
        status=0,
        md5="a" * 32,
        artist=artist,
        title=title,
        version="Insane",
        creator="submitter",
        filename=filename,
        last_update=_NOW,
        total_length=90,
        max_combo=250,
        frozen=True,
        plays=0,
        passes=0,
        mode=0,
        bpm=180.0,
        cs=4.0,
        ar=9.0,
        od=8.0,
        hp=5.0,
        diff=5.5,
    )


def _submission(
    *,
    set_id: int = _SET_ID,
    review_state: str = MapSubmissionReviewState.APPROVED.value,
) -> MapSubmission:
    return MapSubmission(
        set_id=set_id,
        submitter_user_id=6,
        review_state=review_state,
        declared_creator="someone",
        difficulty_count=1,
        osz_size_bytes=2048,
        osz_sha256="0" * 64,
        submitted_at=_NOW,
        updated_at=_NOW,
        reviewed_by=None,
        reviewed_at=None,
        review_note=None,
    )


class _FakeMapsRepository:
    def __init__(self, rows: list[Map] | None = None) -> None:
        self.rows = rows or []

    async def fetch_one(
        self,
        id: int | None = None,
        md5: str | None = None,
        filename: str | None = None,
    ) -> Map | None:
        for row in self.rows:
            if filename is not None and row.filename == filename:
                return row
            if id is not None and row.id == id:
                return row
            if md5 is not None and row.md5 == md5:
                return row
        return None

    async def fetch_many(
        self,
        server: str | None = None,
        set_id: int | None = None,
        **_: Any,
    ) -> list[Map]:
        return [
            row
            for row in self.rows
            if (server is None or row.server == server)
            and (set_id is None or row.set_id == set_id)
        ]


class _FakeSubmissionsRepository:
    def __init__(self, rows: list[MapSubmission] | None = None) -> None:
        self.rows = rows or []

    async def fetch_one(self, set_id: int) -> MapSubmission | None:
        return next((row for row in self.rows if row.set_id == set_id), None)


def _service(
    tmp_path: Path,
    *,
    maps: list[Map] | None = None,
    submissions: list[MapSubmission] | None = None,
) -> PrivateBeatmapFilesService:
    beatmaps_path = tmp_path / "osu"
    archives_path = tmp_path / "osz"
    beatmaps_path.mkdir(exist_ok=True)
    archives_path.mkdir(exist_ok=True)
    return PrivateBeatmapFilesService(
        maps=_FakeMapsRepository(maps),  # type: ignore[arg-type]
        submissions=_FakeSubmissionsRepository(submissions),  # type: ignore[arg-type]
        beatmaps_path=beatmaps_path,
        archives_path=archives_path,
    )


# --- serving a difficulty --------------------------------------------------


async def test_a_hosted_difficulty_resolves_to_its_file(tmp_path: Path) -> None:
    service = _service(tmp_path, maps=[_map()])
    (tmp_path / "osu" / f"{_MAP_ID}.osu").write_text("osu file format v14")

    hosted = await service.fetch_osu_file(_HOSTED_FILENAME)

    assert hosted is not None
    assert hosted.path == tmp_path / "osu" / f"{_MAP_ID}.osu"


async def test_a_mirrored_map_is_not_served_as_ours(tmp_path: Path) -> None:
    # the row exists but belongs to osu!; serving our path would hand back the
    # wrong bytes for a real beatmap.
    service = _service(tmp_path, maps=[_map(server=MapServer.OSU.value)])
    (tmp_path / "osu" / f"{_MAP_ID}.osu").write_text("osu file format v14")

    assert await service.fetch_osu_file(_HOSTED_FILENAME) is None


async def test_an_unknown_filename_is_not_ours(tmp_path: Path) -> None:
    service = _service(tmp_path, maps=[_map()])

    assert await service.fetch_osu_file("something else.osu") is None


async def test_a_hosted_row_whose_file_vanished_falls_back(tmp_path: Path) -> None:
    # no file written: the caller must redirect rather than 500 at the client.
    service = _service(tmp_path, maps=[_map()])

    assert await service.fetch_osu_file(_HOSTED_FILENAME) is None


# --- serving an archive --------------------------------------------------


async def test_a_hosted_set_resolves_to_its_archive(tmp_path: Path) -> None:
    service = _service(tmp_path, maps=[_map()], submissions=[_submission()])
    (tmp_path / "osz" / f"{_SET_ID}.osz").write_bytes(b"PK\x03\x04")

    hosted = await service.fetch_osz_archive(_SET_ID)

    assert hosted is not None
    assert hosted.path == tmp_path / "osz" / f"{_SET_ID}.osz"
    # osu! shows this name in the download; match its usual convention.
    assert hosted.download_name == f"{_SET_ID} Camellia - Ghost.osz"


async def test_an_id_below_the_private_floor_is_never_ours(tmp_path: Path) -> None:
    # a real mirror id cannot reach the private range, so this needs no lookup.
    service = _service(
        tmp_path,
        maps=[_map(set_id=123456)],
        submissions=[_submission(set_id=123456)],
    )
    (tmp_path / "osz" / "123456.osz").write_bytes(b"PK\x03\x04")

    assert await service.fetch_osz_archive(123456) is None


async def test_an_unknown_set_is_not_ours(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert await service.fetch_osz_archive(_SET_ID) is None


async def test_a_pending_set_is_not_downloadable(tmp_path: Path) -> None:
    # the API refuses to show a pending submission; the download endpoint must not
    # become the way around that.
    service = _service(
        tmp_path,
        maps=[_map()],
        submissions=[
            _submission(review_state=MapSubmissionReviewState.PENDING.value),
        ],
    )
    (tmp_path / "osz" / f"{_SET_ID}.osz").write_bytes(b"PK\x03\x04")

    assert await service.fetch_osz_archive(_SET_ID) is None


async def test_a_rejected_set_is_not_downloadable(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        maps=[_map()],
        submissions=[
            _submission(review_state=MapSubmissionReviewState.REJECTED.value),
        ],
    )
    (tmp_path / "osz" / f"{_SET_ID}.osz").write_bytes(b"PK\x03\x04")

    assert await service.fetch_osz_archive(_SET_ID) is None


async def test_an_approved_set_whose_archive_vanished_falls_back(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, maps=[_map()], submissions=[_submission()])

    assert await service.fetch_osz_archive(_SET_ID) is None


async def test_a_set_with_no_maps_still_gets_a_download_name(
    tmp_path: Path,
) -> None:
    # defensive: the archive is what matters, and a set with no rows should not
    # crash on building a display name.
    service = _service(tmp_path, submissions=[_submission()])
    (tmp_path / "osz" / f"{_SET_ID}.osz").write_bytes(b"PK\x03\x04")

    hosted = await service.fetch_osz_archive(_SET_ID)

    assert hosted is not None
    assert hosted.download_name == f"{_SET_ID}.osz"
