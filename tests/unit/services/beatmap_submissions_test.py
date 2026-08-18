"""Guards hosting an uploaded beatmap.

``BeatmapSubmissionService`` is where the upload becomes a real, playable beatmap,
so it is where the rules that keep that safe have to hold. Fakes stand in for every
repository, the filesystem, and the difficulty calculator, so the whole flow runs
in-process.

The properties that matter most, and why:

- **the md5 in the database describes the bytes we host.** The row, the file on
  disk, and the ``.osu`` inside the served ``.osz`` must all agree, or the client
  refuses the map (and score submission cannot rate it);
- **our identity is stamped into every difficulty**, whatever the upload claimed.
  An ``.osz`` derived from a ranked map still carries that map's ids, which would
  make the client file ours under the real one;
- **files are written before the rows are committed**, and a failed commit removes
  them. A ``maps`` row without its ``.osu`` breaks score submission for everyone
  who plays the map; an ``.osu`` without a row is invisible;
- **a submitter can never reach a pp-awarding status.** Ranked/Approved are exactly
  what the global pp recalculation reads, so self-assignment would let anyone mint
  pp from a trivial map. Loved (leaderboard, no pp) waits on staff approval because
  it also triggers the server-wide #1 announcement;
- **a pending submission is invisible to strangers**, reported as missing rather
  than forbidden so its existence is not confirmed.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app._typing import UNSET
from app._typing import _UnsetSentinel
from app.adapters.osz_archive import OszLimits
from app.constants.beatmap_statuses import RankedStatus
from app.constants.privileges import Privileges
from app.repositories.map_submissions import MapSubmission
from app.repositories.map_submissions import MapSubmissionReviewState
from app.repositories.maps import Map
from app.repositories.maps import MapServer
from app.repositories.users import User
from app.services.beatmap_submissions import BeatmapSubmissionResultCode
from app.services.beatmap_submissions import BeatmapSubmissionService
from app.services.performance import BeatmapDifficultyAttributes
from app.services.performance import BeatmapDifficultyError

_NOW = datetime(2026, 8, 18, 12, 0, 0)
_FLOOR = 2_000_000_000

_PENDING = MapSubmissionReviewState.PENDING.value
_APPROVED = MapSubmissionReviewState.APPROVED.value
_REJECTED = MapSubmissionReviewState.REJECTED.value

_LIMITS = OszLimits(
    max_archive_bytes=1_000_000,
    max_total_uncompressed_bytes=4_000_000,
    max_member_count=50,
    max_compression_ratio=1_000.0,
    max_osu_file_bytes=100_000,
    max_osu_file_count=8,
)


# --- fixtures --------------------------------------------------------------


def _user(
    *,
    user_id: int = 6,
    name: str = "submitter",
    priv: int = Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value,
    silence_end: int = 0,
) -> User:
    return User(
        id=user_id,
        name=name,
        safe_name=name,
        email=f"{name}@example.com",
        priv=priv,
        country="us",
        silence_end=silence_end,
        donor_end=0,
        creation_time=0,
        latest_activity=0,
        clan_id=0,
        clan_priv=0,
        preferred_mode=0,
        play_style=0,
        custom_badge_name=None,
        custom_badge_icon=None,
        userpage_content=None,
        api_key=None,
    )


def _staff() -> User:
    return _user(
        user_id=1,
        name="mod",
        priv=(
            Privileges.UNRESTRICTED.value
            | Privileges.VERIFIED.value
            | Privileges.STAFF.value
        ),
    )


def _nominator() -> User:
    return _user(
        user_id=2,
        name="nom",
        priv=(
            Privileges.UNRESTRICTED.value
            | Privileges.VERIFIED.value
            | Privileges.NOMINATOR.value
        ),
    )


def _osu_text(
    *,
    version: str = "Easy",
    artist: str = "Camellia",
    title: str = "Ghost",
    beatmap_id: int = 55555,
    set_id: int = 99999,
    creator: str = "someone-else",
) -> str:
    return "\n".join(
        [
            "osu file format v14",
            "",
            "[General]",
            "Mode: 0",
            "",
            "[Metadata]",
            f"Artist:{artist}",
            f"Title:{title}",
            f"Creator:{creator}",
            f"Version:{version}",
            f"BeatmapID:{beatmap_id}",
            f"BeatmapSetID:{set_id}",
            "",
            "[Difficulty]",
            "HPDrainRate:5",
            "CircleSize:4",
            "OverallDifficulty:8",
            "ApproachRate:9",
            "",
            "[TimingPoints]",
            "0,300,4,2,0,60,1,0",
            "",
            "[HitObjects]",
            "256,192,1000,1,0",
            "256,192,4000,1,0",
            "",
        ],
    )


def _archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _one_difficulty_osz() -> bytes:
    return _archive(
        {
            "song [Easy].osu": _osu_text(version="Easy").encode(),
            "audio.mp3": b"\x00" * 128,
        },
    )


def _two_difficulty_osz() -> bytes:
    return _archive(
        {
            "song [Easy].osu": _osu_text(version="Easy").encode(),
            "song [Hard].osu": _osu_text(version="Hard").encode(),
            "audio.mp3": b"\x00" * 128,
        },
    )


# --- fakes -----------------------------------------------------------------


class _FakeMapsRepository:
    def __init__(self, *, existing: list[Map] | None = None) -> None:
        self.rows: list[Map] = list(existing or [])
        self.deleted_set_ids: list[tuple[int, str]] = []

    async def fetch_one(
        self,
        id: int | None = None,
        md5: str | None = None,
        filename: str | None = None,
    ) -> Map | None:
        for row in self.rows:
            if id is not None and row.id == id:
                return row
            if md5 is not None and row.md5 == md5:
                return row
            if filename is not None and row.filename == filename:
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

    async def create(self, **values: Any) -> Map:
        row = Map(**values)
        self.rows.append(row)
        return row

    async def partial_update(self, id: int, **values: Any) -> Map | None:
        for index, row in enumerate(self.rows):
            if row.id == id:
                updated = Map(
                    **{
                        **{f: getattr(row, f) for f in row.__slots__},
                        **values,
                    },
                )
                self.rows[index] = updated
                return updated
        return None

    async def delete_many_by_set_id(self, *, set_id: int, server: str) -> int:
        self.deleted_set_ids.append((set_id, server))
        before = len(self.rows)
        self.rows = [
            row
            for row in self.rows
            if not (row.set_id == set_id and row.server == server)
        ]
        return before - len(self.rows)


class _FakeMapsetsRepository:
    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []
        self.deletes: list[tuple[int, str]] = []

    async def upsert(self, *, id: int, server: str, last_osuapi_check: Any) -> Any:
        self.upserts.append({"id": id, "server": server})
        return None

    async def delete_one(self, *, id: int, server: str) -> None:
        self.deletes.append((id, server))


class _FakeSubmissionsRepository:
    def __init__(self, *, existing: list[MapSubmission] | None = None) -> None:
        self.rows: list[MapSubmission] = list(existing or [])
        self.deletes: list[int] = []

    async def create(self, **values: Any) -> MapSubmission:
        row = MapSubmission(
            review_state=_PENDING,
            submitted_at=_NOW,
            updated_at=_NOW,
            reviewed_by=None,
            reviewed_at=None,
            review_note=None,
            **values,
        )
        self.rows.append(row)
        return row

    async def fetch_one(self, set_id: int) -> MapSubmission | None:
        return next((row for row in self.rows if row.set_id == set_id), None)

    async def fetch_many(self, **filters: Any) -> list[MapSubmission]:
        return [row for row in self.rows if self._matches(row, filters)]

    async def fetch_count(self, **filters: Any) -> int:
        return len([row for row in self.rows if self._matches(row, filters)])

    def _matches(self, row: MapSubmission, filters: dict[str, Any]) -> bool:
        owner = filters.get("submitter_user_id")
        state = filters.get("review_state")
        if owner is not None and row.submitter_user_id != owner:
            return False
        if state is not None and row.review_state != state:
            return False
        return True

    async def partial_update(
        self,
        set_id: int,
        *,
        review_state: str | _UnsetSentinel = UNSET,
        reviewed_by: int | None | _UnsetSentinel = UNSET,
        review_note: str | None | _UnsetSentinel = UNSET,
    ) -> MapSubmission | None:
        for index, row in enumerate(self.rows):
            if row.set_id != set_id:
                continue
            values = {f: getattr(row, f) for f in row.__slots__}
            if not isinstance(review_state, _UnsetSentinel):
                values["review_state"] = review_state
            if not isinstance(reviewed_by, _UnsetSentinel):
                values["reviewed_by"] = reviewed_by
                values["reviewed_at"] = _NOW
            if not isinstance(review_note, _UnsetSentinel):
                values["review_note"] = review_note
            updated = MapSubmission(**values)
            self.rows[index] = updated
            return updated
        return None

    async def delete_one(self, set_id: int) -> None:
        self.deletes.append(set_id)
        self.rows = [row for row in self.rows if row.set_id != set_id]


class _FakeScoresRepository:
    def __init__(
        self,
        *,
        counts: dict[tuple[str, int | None], int] | None = None,
    ) -> None:
        self._counts = counts or {}
        self.deleted_md5s: list[list[str]] = []

    async def fetch_count(
        self,
        map_md5: str | None = None,
        user_id: int | None = None,
        **_: Any,
    ) -> int:
        assert map_md5 is not None
        return self._counts.get((map_md5, user_id), 0)

    async def delete_many_by_map_md5s(self, map_md5s: Any) -> int:
        self.deleted_md5s.append(list(map_md5s))
        return len(list(map_md5s))


class _FakeIdSequence:
    def __init__(self, *, start: int = _FLOOR) -> None:
        self._next = start

    async def allocate(self, *, kind: Any, allocated_by: int) -> int:
        allocated = self._next
        self._next += 1
        return allocated

    async def allocate_many(
        self,
        *,
        kind: Any,
        count: int,
        allocated_by: int,
    ) -> list[int]:
        return [
            await self.allocate(kind=kind, allocated_by=allocated_by)
            for _ in range(count)
        ]


class _FakeTransaction:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def __aenter__(self) -> _FakeTransaction:
        self._log.append("transaction:enter")
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._log.append("transaction:exit")


class _FakeDatabase:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._log)


class _RecordingFileWriter:
    def __init__(self, log: list[str]) -> None:
        self.written: dict[Path, bytes] = {}
        self._log = log

    async def __call__(self, path: Path, data: bytes) -> None:
        self.written[path] = data
        self._log.append(f"write:{path.name}")


class _RecordingFileRemover:
    def __init__(self, log: list[str]) -> None:
        self.removed: list[Path] = []
        self._log = log

    def __call__(self, path: Path) -> None:
        self.removed.append(path)
        self._log.append(f"remove:{path.name}")


class _FakeDifficultyCalculator:
    def __init__(self, *, stars: float = 5.5, max_combo: int = 2) -> None:
        self._stars = stars
        self._max_combo = max_combo
        self.calls: list[tuple[str, int]] = []

    def __call__(
        self,
        *,
        osu_file_content: str,
        mode: int,
    ) -> BeatmapDifficultyAttributes:
        self.calls.append((osu_file_content, mode))
        return BeatmapDifficultyAttributes(
            stars=self._stars,
            max_combo=self._max_combo,
        )


class _RecordingCacheEvictor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, set_id: int, map_ids: Any, map_md5s: Any) -> None:
        self.calls.append(
            {
                "set_id": set_id,
                "map_ids": list(map_ids),
                "map_md5s": list(map_md5s),
            },
        )


class _Harness:
    """Everything the service was built with, kept reachable for assertions."""

    def __init__(
        self,
        *,
        maps: _FakeMapsRepository | None = None,
        submissions: _FakeSubmissionsRepository | None = None,
        scores: _FakeScoresRepository | None = None,
        calculator: _FakeDifficultyCalculator | None = None,
        max_submissions_per_user: int = 20,
    ) -> None:
        self.log: list[str] = []
        self.maps = maps or _FakeMapsRepository()
        self.mapsets = _FakeMapsetsRepository()
        self.submissions = submissions or _FakeSubmissionsRepository()
        self.scores = scores or _FakeScoresRepository()
        self.id_sequence = _FakeIdSequence()
        self.writer = _RecordingFileWriter(self.log)
        self.remover = _RecordingFileRemover(self.log)
        self.calculator = calculator or _FakeDifficultyCalculator()
        self.evictor = _RecordingCacheEvictor()
        self.service = BeatmapSubmissionService(
            maps=self.maps,  # type: ignore[arg-type]
            mapsets=self.mapsets,  # type: ignore[arg-type]
            submissions=self.submissions,  # type: ignore[arg-type]
            scores=self.scores,  # type: ignore[arg-type]
            id_sequence=self.id_sequence,  # type: ignore[arg-type]
            database=_FakeDatabase(self.log),  # type: ignore[arg-type]
            beatmaps_path=Path(".data/osu"),
            archives_path=Path(".data/osz"),
            limits=_LIMITS,
            max_submissions_per_user=max_submissions_per_user,
            write_file=self.writer,
            remove_file=self.remover,
            calculate_difficulty=self.calculator,
            evict_beatmap_cache=self.evictor,
            now=lambda: _NOW,
        )


def _submission(
    *,
    set_id: int = _FLOOR,
    owner_id: int = 6,
    review_state: str = _PENDING,
) -> MapSubmission:
    return MapSubmission(
        set_id=set_id,
        submitter_user_id=owner_id,
        review_state=review_state,
        declared_creator="someone-else",
        difficulty_count=1,
        osz_size_bytes=100,
        osz_sha256="0" * 64,
        submitted_at=_NOW,
        updated_at=_NOW,
        reviewed_by=None,
        reviewed_at=None,
        review_note=None,
    )


def _hosted_map(
    *,
    map_id: int = _FLOOR + 1,
    set_id: int = _FLOOR,
    md5: str = "a" * 32,
    filename: str = "hosted.osu",
    status: int = RankedStatus.Pending.value,
    server: str = MapServer.PRIVATE.value,
) -> Map:
    return Map(
        id=map_id,
        server=server,
        set_id=set_id,
        status=status,
        md5=md5,
        artist="a",
        title="t",
        version="v",
        creator="submitter",
        filename=filename,
        last_update=_NOW,
        total_length=60,
        max_combo=100,
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


def _osz_members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


# --- hosting a set ---------------------------------------------------------


async def test_submitting_hosts_the_set_privately() -> None:
    harness = _Harness()

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_two_difficulty_osz(),
    )

    assert result.code is BeatmapSubmissionResultCode.CREATED
    # the set row and every map row is marked as ours, so the osu!api refresh
    # leaves them alone.
    assert harness.mapsets.upserts == [
        {"id": _FLOOR, "server": MapServer.PRIVATE},
    ]
    assert [row.server for row in harness.maps.rows] == [MapServer.PRIVATE] * 2
    assert all(row.set_id == _FLOOR for row in harness.maps.rows)
    # every id is above the private floor, and the set id is not reused as a map id.
    assert all(row.id > _FLOOR for row in harness.maps.rows)
    assert all(row.id != _FLOOR for row in harness.maps.rows)


async def test_hosted_maps_start_pending_and_frozen() -> None:
    harness = _Harness()

    await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )

    row = harness.maps.rows[0]
    # Pending gives no leaderboard and no pp; frozen says a human owns the status.
    assert row.status == RankedStatus.Pending
    assert row.frozen is True
    assert (row.plays, row.passes) == (0, 0)


async def test_the_creator_is_the_submitting_account_not_the_file() -> None:
    harness = _Harness()

    await harness.service.submit_beatmap_archive(
        submitter=_user(name="submitter"),
        osz_data=_one_difficulty_osz(),
    )

    # the upload claimed "someone-else"; attribution follows the account.
    assert harness.maps.rows[0].creator == "submitter"
    assert harness.submissions.rows[0].declared_creator == "someone-else"


async def test_the_written_file_matches_the_row_and_the_archive() -> None:
    harness = _Harness()

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )

    difficulty = result.difficulties[0]
    osu_path = Path(".data/osu") / f"{difficulty.beatmap_id}.osu"
    written = harness.writer.written[osu_path]

    # the md5 the database stores describes the bytes on disk...
    assert hashlib.md5(written).hexdigest() == difficulty.md5
    assert harness.maps.rows[0].md5 == difficulty.md5
    # ...and the copy inside the .osz we serve is byte-identical to it.
    archive = harness.writer.written[Path(".data/osz") / f"{_FLOOR}.osz"]
    assert _osz_members(archive)[difficulty.filename] == written


async def test_our_identity_replaces_whatever_the_upload_claimed() -> None:
    harness = _Harness()

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(name="submitter"),
        osz_data=_one_difficulty_osz(),
    )

    difficulty = result.difficulties[0]
    text = harness.writer.written[
        Path(".data/osu") / f"{difficulty.beatmap_id}.osu"
    ].decode()

    assert f"BeatmapID:{difficulty.beatmap_id}" in text
    assert f"BeatmapSetID:{_FLOOR}" in text
    assert "Creator:submitter" in text
    # the ids it arrived with are gone, or the client files this under the real map.
    assert "BeatmapID:55555" not in text
    assert "BeatmapSetID:99999" not in text


async def test_non_difficulty_members_survive_the_repack() -> None:
    harness = _Harness()

    await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )

    archive = harness.writer.written[Path(".data/osz") / f"{_FLOOR}.osz"]
    # the audio the difficulty references has to still be there, unchanged.
    assert _osz_members(archive)["audio.mp3"] == b"\x00" * 128


async def test_files_are_written_before_the_rows_are_committed() -> None:
    harness = _Harness()

    await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_two_difficulty_osz(),
    )

    # a maps row without its .osu breaks score submission for everyone who plays
    # the map, so the files must land first.
    first_commit = harness.log.index("transaction:enter")
    writes = [i for i, entry in enumerate(harness.log) if entry.startswith("write:")]
    assert writes
    assert max(writes) < first_commit


async def test_a_failed_commit_removes_every_written_file() -> None:
    class _FailingSubmissions(_FakeSubmissionsRepository):
        async def create(self, **values: Any) -> MapSubmission:
            raise RuntimeError("database went away")

    harness = _Harness(submissions=_FailingSubmissions())

    with pytest.raises(RuntimeError):
        await harness.service.submit_beatmap_archive(
            submitter=_user(),
            osz_data=_two_difficulty_osz(),
        )

    # no orphaned files left claiming to be a hosted set.
    assert set(harness.remover.removed) == set(harness.writer.written)


# --- refusing an upload ----------------------------------------------------


@pytest.mark.parametrize(
    "submitter",
    [
        _user(priv=Privileges.VERIFIED.value),  # restricted
        _user(priv=Privileges.UNRESTRICTED.value),  # unverified
    ],
)
async def test_an_ineligible_account_cannot_submit(submitter: User) -> None:
    harness = _Harness()

    result = await harness.service.submit_beatmap_archive(
        submitter=submitter,
        osz_data=_one_difficulty_osz(),
    )

    assert result.code is BeatmapSubmissionResultCode.NOT_ELIGIBLE
    assert harness.writer.written == {}


async def test_a_silenced_account_cannot_submit() -> None:
    harness = _Harness()
    silenced = _user(silence_end=int(_NOW.timestamp()) + 600)

    result = await harness.service.submit_beatmap_archive(
        submitter=silenced,
        osz_data=_one_difficulty_osz(),
    )

    assert result.code is BeatmapSubmissionResultCode.NOT_ELIGIBLE
    assert harness.writer.written == {}


async def test_the_per_user_quota_is_enforced() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        max_submissions_per_user=1,
    )

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )

    assert result.code is BeatmapSubmissionResultCode.QUOTA_EXCEEDED
    assert harness.writer.written == {}


async def test_a_broken_archive_is_refused() -> None:
    harness = _Harness()

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=b"not a zip at all",
    )

    assert result.code is BeatmapSubmissionResultCode.INVALID_ARCHIVE
    assert harness.writer.written == {}


async def test_an_unparseable_difficulty_names_the_file() -> None:
    harness = _Harness()
    osz = _archive({"broken [Easy].osu": b"this is not a beatmap"})

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=osz,
    )

    assert result.code is BeatmapSubmissionResultCode.INVALID_DIFFICULTY_FILE
    assert result.detail is not None
    assert "broken [Easy].osu" in result.detail
    assert harness.writer.written == {}


async def test_duplicate_difficulty_names_are_refused() -> None:
    harness = _Harness()
    osz = _archive(
        {
            "a.osu": _osu_text(version="Easy").encode(),
            "b.osu": _osu_text(version="Easy").encode(),
        },
    )

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=osz,
    )

    assert result.code is BeatmapSubmissionResultCode.DUPLICATE_DIFFICULTY_NAME
    assert harness.writer.written == {}


async def test_mismatched_song_metadata_is_refused() -> None:
    harness = _Harness()
    osz = _archive(
        {
            "a.osu": _osu_text(version="Easy", title="Ghost").encode(),
            "b.osu": _osu_text(version="Hard", title="Different Song").encode(),
        },
    )

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=osz,
    )

    assert result.code is BeatmapSubmissionResultCode.INCONSISTENT_METADATA
    assert harness.writer.written == {}


async def test_a_duplicate_checksum_is_refused() -> None:
    # the collision that matters: maps.md5 is globally unique and the osu!api
    # refresh writes with REPLACE INTO, so a copy of a mirrored map's .osu would
    # let that refresh overwrite our row.
    harness = _Harness()
    probe = await _Harness().service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )
    existing_md5 = probe.difficulties[0].md5
    harness.maps.rows.append(
        _hosted_map(md5=existing_md5, server=MapServer.OSU.value, filename="real.osu"),
    )

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )

    assert result.code is BeatmapSubmissionResultCode.CHECKSUM_ALREADY_EXISTS
    assert harness.writer.written == {}


async def test_a_duplicate_filename_is_refused() -> None:
    # filename lookups have no server filter, so a duplicate would shadow a real
    # map in osu!'s own requests.
    harness = _Harness()
    probe = await _Harness().service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )
    harness.maps.rows.append(
        _hosted_map(
            md5="f" * 32,
            filename=probe.difficulties[0].filename,
            server=MapServer.OSU.value,
        ),
    )

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )

    assert result.code is BeatmapSubmissionResultCode.FILENAME_ALREADY_EXISTS
    assert harness.writer.written == {}


async def test_an_unratable_difficulty_is_refused() -> None:
    class _FailingCalculator(_FakeDifficultyCalculator):
        def __call__(self, **kwargs: Any) -> BeatmapDifficultyAttributes:
            raise BeatmapDifficultyError("cannot rate")

    harness = _Harness(calculator=_FailingCalculator())

    result = await harness.service.submit_beatmap_archive(
        submitter=_user(),
        osz_data=_one_difficulty_osz(),
    )

    assert result.code is BeatmapSubmissionResultCode.RATING_FAILED
    assert harness.writer.written == {}


# --- the moderation matrix -------------------------------------------------


@pytest.mark.parametrize(
    ("status", "review_state", "expected"),
    [
        # a submitter may always park their own map as Pending.
        (
            RankedStatus.Pending.value,
            _PENDING,
            BeatmapSubmissionResultCode.UPDATED,
        ),
        # Loved grants a leaderboard and the #1 announcement: staff first.
        (
            RankedStatus.Loved.value,
            _PENDING,
            BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED,
        ),
        (
            RankedStatus.Loved.value,
            _APPROVED,
            BeatmapSubmissionResultCode.UPDATED,
        ),
        # Ranked/Approved award pp -- never self-assignable, at any review state.
        (
            RankedStatus.Ranked.value,
            _APPROVED,
            BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED,
        ),
        (
            RankedStatus.Approved.value,
            _APPROVED,
            BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED,
        ),
        (
            RankedStatus.Qualified.value,
            _APPROVED,
            BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED,
        ),
    ],
)
async def test_what_an_owner_may_set(
    status: int,
    review_state: str,
    expected: BeatmapSubmissionResultCode,
) -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(
            existing=[_submission(review_state=review_state)],
        ),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.update_submission(
        actor=_user(),
        set_id=_FLOOR,
        status=status,
    )

    assert result.code is expected


@pytest.mark.parametrize(
    "status",
    [
        RankedStatus.Ranked.value,
        RankedStatus.Approved.value,
        RankedStatus.Loved.value,
    ],
)
async def test_staff_may_set_any_ranked_status(status: int) -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.update_submission(
        actor=_staff(),
        set_id=_FLOOR,
        status=status,
    )

    assert result.code is BeatmapSubmissionResultCode.UPDATED
    assert harness.maps.rows[0].status == status


async def test_a_nominator_may_rank_a_map() -> None:
    # NOMINATOR is the existing "manage maps ranked status" bit; reuse, don't
    # invent a new privilege.
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.update_submission(
        actor=_nominator(),
        set_id=_FLOOR,
        status=RankedStatus.Ranked.value,
    )

    assert result.code is BeatmapSubmissionResultCode.UPDATED


async def test_an_owner_cannot_review_their_own_submission() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.update_submission(
        actor=_user(),
        set_id=_FLOOR,
        review_state=_APPROVED,
    )

    assert result.code is BeatmapSubmissionResultCode.FORBIDDEN
    assert harness.submissions.rows[0].review_state == _PENDING


async def test_a_stranger_cannot_touch_someone_else_s_submission() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission(owner_id=6)]),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.update_submission(
        actor=_user(user_id=99, name="stranger"),
        set_id=_FLOOR,
        status=RankedStatus.Pending.value,
    )

    # reported missing, not forbidden: a pending submission's existence is not
    # disclosed to someone who may not see it.
    assert result.code is BeatmapSubmissionResultCode.NOT_FOUND


async def test_rejecting_a_submission_strips_its_leaderboard() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(
            existing=[_submission(review_state=_APPROVED)],
        ),
        maps=_FakeMapsRepository(
            existing=[_hosted_map(status=RankedStatus.Loved.value)],
        ),
    )

    result = await harness.service.update_submission(
        actor=_staff(),
        set_id=_FLOOR,
        review_state=_REJECTED,
    )

    assert result.code is BeatmapSubmissionResultCode.UPDATED
    # a rejected map must not keep a live leaderboard.
    assert harness.maps.rows[0].status == RankedStatus.Pending.value


async def test_a_status_change_evicts_the_beatmap_cache() -> None:
    # `app.state.cache` holds live Beatmap objects whose `.status` the leaderboard
    # reads, so a stale entry would keep serving the old status.
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map(md5="b" * 32)]),
    )

    await harness.service.update_submission(
        actor=_staff(),
        set_id=_FLOOR,
        status=RankedStatus.Ranked.value,
    )

    assert harness.evictor.calls == [
        {
            "set_id": _FLOOR,
            "map_ids": [_FLOOR + 1],
            "map_md5s": ["b" * 32],
        },
    ]


async def test_a_status_change_applies_to_every_difficulty() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(
            existing=[
                _hosted_map(map_id=_FLOOR + 1, md5="1" * 32, filename="a.osu"),
                _hosted_map(map_id=_FLOOR + 2, md5="2" * 32, filename="b.osu"),
            ],
        ),
    )

    await harness.service.update_submission(
        actor=_staff(),
        set_id=_FLOOR,
        status=RankedStatus.Ranked.value,
    )

    assert [row.status for row in harness.maps.rows] == [
        RankedStatus.Ranked.value,
    ] * 2


# --- reading -------------------------------------------------------------


async def test_an_owner_sees_their_own_pending_submission() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.fetch_submission(actor=_user(), set_id=_FLOOR)

    assert result.code is BeatmapSubmissionResultCode.FOUND
    assert len(result.difficulties) == 1


async def test_a_stranger_cannot_see_a_pending_submission() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
    )

    result = await harness.service.fetch_submission(
        actor=_user(user_id=99, name="stranger"),
        set_id=_FLOOR,
    )

    assert result.code is BeatmapSubmissionResultCode.NOT_FOUND


async def test_anyone_can_see_an_approved_submission() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(
            existing=[_submission(review_state=_APPROVED)],
        ),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.fetch_submission(actor=None, set_id=_FLOOR)

    assert result.code is BeatmapSubmissionResultCode.FOUND


async def test_staff_can_see_a_pending_submission() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.fetch_submission(actor=_staff(), set_id=_FLOOR)

    assert result.code is BeatmapSubmissionResultCode.FOUND


async def test_an_unknown_set_is_missing() -> None:
    harness = _Harness()

    result = await harness.service.fetch_submission(actor=_staff(), set_id=12345)

    assert result.code is BeatmapSubmissionResultCode.NOT_FOUND


# --- taking a set down ---------------------------------------------------


async def test_an_owner_can_delete_their_untouched_set() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map(md5="c" * 32)]),
    )

    result = await harness.service.delete_submission(actor=_user(), set_id=_FLOOR)

    assert result.code is BeatmapSubmissionResultCode.DELETED
    # rows, scores, and files all go...
    assert harness.scores.deleted_md5s == [["c" * 32]]
    assert harness.maps.deleted_set_ids == [(_FLOOR, MapServer.PRIVATE)]
    assert harness.mapsets.deletes == [(_FLOOR, MapServer.PRIVATE)]
    assert harness.submissions.deletes == [_FLOOR]
    assert [path.name for path in harness.remover.removed] == [
        f"{_FLOOR + 1}.osu",
        f"{_FLOOR}.osz",
    ]


async def test_deletion_commits_the_rows_before_removing_the_files() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    await harness.service.delete_submission(actor=_user(), set_id=_FLOOR)

    # the opposite ordering to creation, for the same reason: here the safe
    # residue is a file with no row.
    commit_exit = harness.log.index("transaction:exit")
    removes = [i for i, entry in enumerate(harness.log) if entry.startswith("remove:")]
    assert removes
    assert min(removes) > commit_exit


async def test_an_owner_cannot_delete_a_set_others_have_played() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map(md5="d" * 32)]),
        # 3 scores in total, only 1 the owner's.
        scores=_FakeScoresRepository(counts={("d" * 32, None): 3, ("d" * 32, 6): 1}),
    )

    result = await harness.service.delete_submission(actor=_user(), set_id=_FLOOR)

    assert result.code is BeatmapSubmissionResultCode.HAS_FOREIGN_SCORES
    # other players' play history is not the submitter's to erase.
    assert harness.scores.deleted_md5s == []
    assert harness.remover.removed == []


async def test_an_owner_can_delete_a_set_only_they_have_played() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map(md5="e" * 32)]),
        scores=_FakeScoresRepository(counts={("e" * 32, None): 4, ("e" * 32, 6): 4}),
    )

    result = await harness.service.delete_submission(actor=_user(), set_id=_FLOOR)

    assert result.code is BeatmapSubmissionResultCode.DELETED


async def test_staff_can_delete_a_set_others_have_played() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(existing=[_submission()]),
        maps=_FakeMapsRepository(existing=[_hosted_map(md5="d" * 32)]),
        scores=_FakeScoresRepository(counts={("d" * 32, None): 3, ("d" * 32, 6): 1}),
    )

    result = await harness.service.delete_submission(actor=_staff(), set_id=_FLOOR)

    assert result.code is BeatmapSubmissionResultCode.DELETED


async def test_a_stranger_cannot_delete_a_set() -> None:
    harness = _Harness(
        submissions=_FakeSubmissionsRepository(
            existing=[_submission(review_state=_APPROVED)],
        ),
        maps=_FakeMapsRepository(existing=[_hosted_map()]),
    )

    result = await harness.service.delete_submission(
        actor=_user(user_id=99, name="stranger"),
        set_id=_FLOOR,
    )

    # visible (approved) but not theirs, so this one is a real refusal.
    assert result.code is BeatmapSubmissionResultCode.FORBIDDEN
    assert harness.submissions.deletes == []
