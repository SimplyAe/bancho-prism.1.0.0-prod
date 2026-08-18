"""Hosting beatmaps uploaded to this server.

A player uploads an ``.osz``; we validate it, allocate ids that cannot collide
with osu!'s, stamp our identity into each difficulty, write the files, and record
``maps`` rows marked ``server='private'`` so the game treats them like any other
beatmap. This module owns that whole transaction and the moderation rules around
it.

Three decisions here carry the weight:

**A submitter can never give their own map a pp-awarding status.** ``Ranked`` and
``Approved`` are exactly what ``Beatmap.awards_ranked_pp`` covers, and exactly the
filter the global pp recalculation reads, so self-assigning either would let anyone
mint arbitrary pp by uploading a trivial map. Those are staff-only. ``Loved`` grants
a leaderboard without pp -- the right ceiling for a submitter -- but it also enables
the server-wide #1 announcement, so it waits behind a human ``review_state``.

**Files are written before the rows are committed.** The inverse of the replay
path (which renames after commit, because a score id does not exist until its
insert): ids are allocated up front here, so the final paths are known. The
invariant that matters is *a ``maps`` row never exists without its ``.osu``* --
that is the case that breaks score submission for everyone who plays the map. An
``.osu`` with no row is an unreferenced file, invisible and reclaimable.

**The md5 and filename collision checks run against the whole ``maps`` table, not
just the private rows.** ``maps.md5`` is globally unique and the osu!api refresh
writes with ``REPLACE INTO``; a byte-identical copy of a ranked map's ``.osu``
would collide on md5 and let the next refresh of the *real* set overwrite our row
-- or, with a colliding filename, shadow a real map in every filename lookup.

Everything with a side effect is injected, so the whole flow tests with no
filesystem, no database and no calculator.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app._typing import UNSET
from app.adapters.database import Database
from app.adapters.osu_beatmap_file import OsuFileMetadata
from app.adapters.osu_beatmap_file import OsuFileParseError
from app.adapters.osu_beatmap_file import canonical_osu_filename
from app.adapters.osu_beatmap_file import parse_osu_file
from app.adapters.osu_beatmap_file import rewrite_osu_file_identity
from app.adapters.osz_archive import OszArchiveError
from app.adapters.osz_archive import OszLimits
from app.adapters.osz_archive import OszOsuMember
from app.adapters.osz_archive import read_osz
from app.adapters.osz_archive import repack_osz
from app.constants.beatmap_statuses import RankedStatus
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log
from app.repositories.map_id_sequence import MapIdKind
from app.repositories.map_id_sequence import MapIdSequenceRepository
from app.repositories.map_submissions import MapSubmission
from app.repositories.map_submissions import MapSubmissionReviewState
from app.repositories.map_submissions import MapSubmissionsRepository
from app.repositories.maps import Map
from app.repositories.maps import MapServer
from app.repositories.maps import MapsRepository
from app.repositories.mapsets import MapsetsRepository
from app.repositories.scores import ScoresRepository
from app.repositories.users import User
from app.services.performance import BeatmapDifficultyAttributes
from app.services.performance import BeatmapDifficultyError

# A submitter may move their own map between these two. `Pending` is inert;
# `Loved` grants a leaderboard but never pp, and is gated on staff approval.
_OWNER_ASSIGNABLE_STATUSES = (RankedStatus.Pending, RankedStatus.Loved)

# Only staff may reach these: Ranked/Approved award pp, and Qualified is a
# pipeline state that belongs to review, not to the author.
_STAFF_ONLY_STATUSES = (
    RankedStatus.Ranked,
    RankedStatus.Approved,
    RankedStatus.Qualified,
)


class BeatmapSubmissionResultCode(StrEnum):
    """Every way a submission operation can end.

    One value per outcome, so the HTTP layer maps each to its own status rather
    than collapsing distinct failures into a generic 400.
    """

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    FOUND = "found"

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    NOT_ELIGIBLE = "not_eligible"
    QUOTA_EXCEEDED = "quota_exceeded"
    ARCHIVE_TOO_LARGE = "archive_too_large"
    INVALID_ARCHIVE = "invalid_archive"
    INVALID_DIFFICULTY_FILE = "invalid_difficulty_file"
    DUPLICATE_DIFFICULTY_NAME = "duplicate_difficulty_name"
    INCONSISTENT_METADATA = "inconsistent_metadata"
    CHECKSUM_ALREADY_EXISTS = "checksum_already_exists"
    FILENAME_ALREADY_EXISTS = "filename_already_exists"
    STATUS_NOT_PERMITTED = "status_not_permitted"
    HAS_FOREIGN_SCORES = "has_foreign_scores"
    RATING_FAILED = "rating_failed"


@dataclass(frozen=True, slots=True)
class SubmittedDifficulty:
    """One hosted difficulty, as it was written to ``maps``."""

    beatmap_id: int
    md5: str
    filename: str
    version: str
    mode: int
    status: int
    total_length: int
    max_combo: int
    bpm: float
    cs: float
    ar: float
    od: float
    hp: float
    diff: float


@dataclass(frozen=True)
class BeatmapSubmissionResult:
    code: BeatmapSubmissionResultCode
    submission: MapSubmission | None = None
    difficulties: tuple[SubmittedDifficulty, ...] = ()
    detail: str | None = None


@dataclass(frozen=True)
class BeatmapSubmissionListing:
    submissions: list[MapSubmission]
    total: int


class AtomicFileWriter(Protocol):
    def __call__(self, path: Path, data: bytes) -> Awaitable[None]: ...


class FileRemover(Protocol):
    def __call__(self, path: Path) -> None: ...


class DifficultyCalculator(Protocol):
    def __call__(
        self,
        *,
        osu_file_content: str,
        mode: int,
    ) -> BeatmapDifficultyAttributes: ...


class BeatmapCacheEvictor(Protocol):
    def __call__(
        self,
        *,
        set_id: int,
        map_ids: Sequence[int],
        map_md5s: Sequence[str],
    ) -> None: ...


@dataclass(frozen=True)
class _PreparedDifficulty:
    """A difficulty validated and stamped, but not yet written anywhere."""

    beatmap_id: int
    original_member_name: str
    filename: str
    text: str
    data: bytes
    md5: str
    metadata: OsuFileMetadata
    attributes: BeatmapDifficultyAttributes


@dataclass(frozen=True)
class BeatmapSubmissionService:
    maps: MapsRepository
    mapsets: MapsetsRepository
    submissions: MapSubmissionsRepository
    scores: ScoresRepository
    id_sequence: MapIdSequenceRepository
    database: Database
    beatmaps_path: Path
    archives_path: Path
    limits: OszLimits
    max_submissions_per_user: int
    write_file: AtomicFileWriter
    remove_file: FileRemover
    calculate_difficulty: DifficultyCalculator
    evict_beatmap_cache: BeatmapCacheEvictor
    now: Callable[[], datetime]

    # --- submitting -------------------------------------------------------

    async def submit_beatmap_archive(
        self,
        *,
        submitter: User,
        osz_data: bytes,
    ) -> BeatmapSubmissionResult:
        """Host an uploaded ``.osz`` as a new privately-owned beatmap set."""
        eligibility = self._check_eligibility(submitter)
        if eligibility is not None:
            return eligibility

        existing_count = await self.submissions.fetch_count(
            submitter_user_id=submitter.id,
        )
        if existing_count >= self.max_submissions_per_user:
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.QUOTA_EXCEEDED,
                detail=(
                    f"you already host {existing_count} beatmap sets "
                    f"(limit {self.max_submissions_per_user})"
                ),
            )

        try:
            contents = read_osz(osz_data, limits=self.limits)
        except OszArchiveError as exc:
            code = (
                BeatmapSubmissionResultCode.ARCHIVE_TOO_LARGE
                if "over the" in str(exc) or "expands to" in str(exc)
                else BeatmapSubmissionResultCode.INVALID_ARCHIVE
            )
            return BeatmapSubmissionResult(code=code, detail=str(exc))

        parsed: list[tuple[OszOsuMember, OsuFileMetadata]] = []
        for member in contents.osu_members:
            try:
                parsed.append((member, parse_osu_file(member.text)))
            except OsuFileParseError as exc:
                return BeatmapSubmissionResult(
                    code=BeatmapSubmissionResultCode.INVALID_DIFFICULTY_FILE,
                    detail=f"{member.name}: {exc}",
                )

        consistency = _check_cross_difficulty_consistency(parsed)
        if consistency is not None:
            return consistency

        # ids first: the final file paths depend on them, and the write has to
        # happen before the commit (see the module docstring).
        set_id = await self.id_sequence.allocate(
            kind=MapIdKind.SET,
            allocated_by=submitter.id,
        )
        beatmap_ids = await self.id_sequence.allocate_many(
            kind=MapIdKind.MAP,
            count=len(parsed),
            allocated_by=submitter.id,
        )

        prepared: list[_PreparedDifficulty] = []
        for beatmap_id, (member, metadata) in zip(beatmap_ids, parsed, strict=True):
            try:
                difficulty = self._prepare_difficulty(
                    beatmap_id=beatmap_id,
                    set_id=set_id,
                    member=member,
                    metadata=metadata,
                    creator=submitter.name,
                )
            except OsuFileParseError as exc:
                return BeatmapSubmissionResult(
                    code=BeatmapSubmissionResultCode.INVALID_DIFFICULTY_FILE,
                    detail=f"{member.name}: {exc}",
                )
            except BeatmapDifficultyError as exc:
                return BeatmapSubmissionResult(
                    code=BeatmapSubmissionResultCode.RATING_FAILED,
                    detail=f"{member.name}: {exc}",
                )
            prepared.append(difficulty)

        collision = await self._check_collisions(prepared)
        if collision is not None:
            return collision

        written: list[Path] = []
        try:
            for difficulty in prepared:
                path = self.beatmaps_path / f"{difficulty.beatmap_id}.osu"
                await self.write_file(path, difficulty.data)
                written.append(path)

            archive = repack_osz(
                osz_data,
                replacements={
                    difficulty.original_member_name: OszOsuMember(
                        name=difficulty.filename,
                        text=difficulty.text,
                    )
                    for difficulty in prepared
                },
            )
            archive_path = self.archives_path / f"{set_id}.osz"
            await self.write_file(archive_path, archive)
            written.append(archive_path)

            submission = await self._commit_submission(
                submitter=submitter,
                set_id=set_id,
                prepared=prepared,
                archive=archive,
                parsed=parsed,
            )
        except Exception:
            # a row without its .osu breaks score submission for everyone who
            # plays the map, so unwind the files we wrote rather than leave a
            # half-hosted set behind.
            for path in written:
                try:
                    self.remove_file(path)
                except Exception:  # pragma: no cover - best effort cleanup
                    log(f"failed to clean up {path}", Ansi.LYELLOW)
            raise

        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.CREATED,
            submission=submission,
            difficulties=tuple(
                _as_submitted_difficulty(difficulty) for difficulty in prepared
            ),
        )

    def _check_eligibility(self, submitter: User) -> BeatmapSubmissionResult | None:
        required = Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value
        if submitter.priv & required != required:
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.NOT_ELIGIBLE,
                detail="your account is not eligible to submit beatmaps",
            )
        if submitter.silence_end > int(self.now().timestamp()):
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.NOT_ELIGIBLE,
                detail="you cannot submit beatmaps while silenced",
            )
        return None

    def _prepare_difficulty(
        self,
        *,
        beatmap_id: int,
        set_id: int,
        member: OszOsuMember,
        metadata: OsuFileMetadata,
        creator: str,
    ) -> _PreparedDifficulty:
        # our ids and our creator, not whatever the upload claimed.
        text = rewrite_osu_file_identity(
            member.text,
            beatmap_id=beatmap_id,
            beatmap_set_id=set_id,
            creator=creator,
        )
        data = text.encode("utf-8")

        return _PreparedDifficulty(
            beatmap_id=beatmap_id,
            original_member_name=member.name,
            filename=canonical_osu_filename(
                artist=metadata.artist,
                title=metadata.title,
                creator=creator,
                version=metadata.version,
            ),
            text=text,
            data=data,
            # hashed *after* the rewrite, so the md5 describes the bytes we host.
            md5=hashlib.md5(data).hexdigest(),
            metadata=metadata,
            attributes=self.calculate_difficulty(
                osu_file_content=text,
                mode=metadata.mode,
            ),
        )

    async def _check_collisions(
        self,
        prepared: Sequence[_PreparedDifficulty],
    ) -> BeatmapSubmissionResult | None:
        """Refuse anything that would shadow or hijack an existing beatmap."""
        for difficulty in prepared:
            # NOT scoped to private rows: `maps.md5` is globally unique and the
            # osu!api refresh writes with REPLACE INTO, so a collision with a
            # mirrored map lets that refresh overwrite our row.
            if await self.maps.fetch_one(md5=difficulty.md5) is not None:
                return BeatmapSubmissionResult(
                    code=BeatmapSubmissionResultCode.CHECKSUM_ALREADY_EXISTS,
                    detail=(
                        f"{difficulty.filename}: this beatmap already exists on "
                        "this server"
                    ),
                )
            # likewise unscoped: filename lookups have no server filter, so a
            # duplicate would shadow a real map in osu!'s own requests.
            if await self.maps.fetch_one(filename=difficulty.filename) is not None:
                return BeatmapSubmissionResult(
                    code=BeatmapSubmissionResultCode.FILENAME_ALREADY_EXISTS,
                    detail=(
                        f"{difficulty.filename}: a beatmap with this name already "
                        "exists; change the title or difficulty name"
                    ),
                )
        return None

    async def _commit_submission(
        self,
        *,
        submitter: User,
        set_id: int,
        prepared: Sequence[_PreparedDifficulty],
        archive: bytes,
        parsed: Sequence[tuple[OszOsuMember, OsuFileMetadata]],
    ) -> MapSubmission:
        submitted_at = self.now()

        async with self.database.transaction():
            await self.mapsets.upsert(
                id=set_id,
                server=MapServer.PRIVATE,
                # meaningless for a private set (there is no upstream to have
                # checked) but the column is not null.
                last_osuapi_check=submitted_at,
            )

            for difficulty in prepared:
                metadata = difficulty.metadata
                await self.maps.create(
                    id=difficulty.beatmap_id,
                    server=MapServer.PRIVATE,
                    set_id=set_id,
                    status=RankedStatus.Pending,
                    md5=difficulty.md5,
                    artist=metadata.artist,
                    title=metadata.title,
                    version=metadata.version,
                    # the submitting account, never the file's claim.
                    creator=submitter.name,
                    filename=difficulty.filename,
                    last_update=submitted_at,
                    total_length=metadata.total_length,
                    max_combo=difficulty.attributes.max_combo,
                    # a human owns this status; the osu!api must never touch it.
                    frozen=True,
                    plays=0,
                    passes=0,
                    mode=metadata.mode,
                    bpm=metadata.bpm,
                    cs=metadata.cs,
                    ar=metadata.ar,
                    od=metadata.od,
                    hp=metadata.hp,
                    diff=difficulty.attributes.stars,
                )

            return await self.submissions.create(
                set_id=set_id,
                submitter_user_id=submitter.id,
                # kept only for review context; it is attacker-controlled.
                declared_creator=parsed[0][1].declared_creator,
                difficulty_count=len(prepared),
                osz_size_bytes=len(archive),
                osz_sha256=hashlib.sha256(archive).hexdigest(),
            )

    # --- reading ----------------------------------------------------------

    async def fetch_submissions(
        self,
        *,
        actor: User,
        owner_user_id: int | None = None,
        review_state: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> BeatmapSubmissionListing:
        """List submissions the actor may see.

        Staff see everything. Everyone else sees their own submissions in full,
        and other players' only once approved.
        """
        if _is_staff(actor):
            effective_owner = owner_user_id
            effective_state = review_state
        elif owner_user_id == actor.id:
            effective_owner = actor.id
            effective_state = review_state
        else:
            effective_owner = owner_user_id
            effective_state = MapSubmissionReviewState.APPROVED.value

        submissions = await self.submissions.fetch_many(
            submitter_user_id=effective_owner,
            review_state=effective_state,
            page=page,
            page_size=page_size,
        )
        total = await self.submissions.fetch_count(
            submitter_user_id=effective_owner,
            review_state=effective_state,
        )
        return BeatmapSubmissionListing(submissions=submissions, total=total)

    async def fetch_submission(
        self,
        *,
        actor: User | None,
        set_id: int,
    ) -> BeatmapSubmissionResult:
        """One submission with its difficulties, if the actor may see it."""
        submission = await self.submissions.fetch_one(set_id)
        if submission is None or not _may_view(actor, submission):
            # identical answer for "no such set" and "not yours": the API must
            # not confirm a pending submission exists to a stranger.
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.NOT_FOUND,
            )

        maps = await self.maps.fetch_many(
            set_id=set_id,
            server=MapServer.PRIVATE,
        )
        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.FOUND,
            submission=submission,
            difficulties=tuple(_map_to_difficulty(row) for row in maps),
        )

    # --- moderation -------------------------------------------------------

    async def update_submission(
        self,
        *,
        actor: User,
        set_id: int,
        status: int | None = None,
        review_state: str | None = None,
        review_note: str | None = None,
    ) -> BeatmapSubmissionResult:
        """Change a submission's ranked status and/or its review state."""
        submission = await self.submissions.fetch_one(set_id)
        if submission is None or not _may_view(actor, submission):
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.NOT_FOUND,
            )

        is_staff = _is_staff(actor) or _is_nominator(actor)
        is_owner = submission.submitter_user_id == actor.id

        if not (is_staff or is_owner):
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.FORBIDDEN,
            )

        if (review_state is not None or review_note is not None) and not is_staff:
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.FORBIDDEN,
                detail="only staff may review submissions",
            )

        if status is not None:
            denied = _check_status_permitted(
                status=status,
                submission=submission,
                is_staff=is_staff,
            )
            if denied is not None:
                return denied

        # a rejection must not leave a leaderboard standing.
        effective_status = status
        if review_state == MapSubmissionReviewState.REJECTED.value:
            effective_status = RankedStatus.Pending.value

        updated = submission
        if review_state is not None or review_note is not None:
            result = await self.submissions.partial_update(
                set_id,
                review_state=review_state if review_state is not None else UNSET,
                reviewed_by=actor.id,
                review_note=review_note,
            )
            if result is not None:
                updated = result

        maps = await self.maps.fetch_many(set_id=set_id, server=MapServer.PRIVATE)
        if effective_status is not None:
            for row in maps:
                await self.maps.partial_update(row.id, status=effective_status)
            # `app.state.cache` holds live Beatmap objects whose `.status` the
            # leaderboard reads, so a stale entry would keep the old status.
            self.evict_beatmap_cache(
                set_id=set_id,
                map_ids=[row.id for row in maps],
                map_md5s=[row.md5 for row in maps],
            )
            maps = await self.maps.fetch_many(set_id=set_id, server=MapServer.PRIVATE)

        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.UPDATED,
            submission=updated,
            difficulties=tuple(_map_to_difficulty(row) for row in maps),
        )

    # --- deleting ---------------------------------------------------------

    async def delete_submission(
        self,
        *,
        actor: User,
        set_id: int,
    ) -> BeatmapSubmissionResult:
        """Take a hosted set down, with its scores and files."""
        submission = await self.submissions.fetch_one(set_id)
        if submission is None or not _may_view(actor, submission):
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.NOT_FOUND,
            )

        is_staff = _is_staff(actor)
        is_owner = submission.submitter_user_id == actor.id
        if not (is_staff or is_owner):
            return BeatmapSubmissionResult(
                code=BeatmapSubmissionResultCode.FORBIDDEN,
            )

        maps = await self.maps.fetch_many(set_id=set_id, server=MapServer.PRIVATE)
        map_md5s = [row.md5 for row in maps]

        if not is_staff:
            foreign = await self._has_foreign_scores(
                map_md5s=map_md5s,
                owner_user_id=submission.submitter_user_id,
            )
            if foreign:
                # other players' scores are not the submitter's to erase.
                return BeatmapSubmissionResult(
                    code=BeatmapSubmissionResultCode.HAS_FOREIGN_SCORES,
                    detail=(
                        "other players have set scores on this beatmap; ask staff "
                        "to remove it"
                    ),
                )

        async with self.database.transaction():
            await self.scores.delete_many_by_map_md5s(map_md5s)
            await self.maps.delete_many_by_set_id(
                set_id=set_id,
                server=MapServer.PRIVATE,
            )
            await self.mapsets.delete_one(id=set_id, server=MapServer.PRIVATE)
            await self.submissions.delete_one(set_id)

        # rows first, files after: the safe residue here is a file with no row,
        # which is the opposite ordering to creation and for the same reason.
        for row in maps:
            self.remove_file(self.beatmaps_path / f"{row.id}.osu")
        self.remove_file(self.archives_path / f"{set_id}.osz")

        self.evict_beatmap_cache(
            set_id=set_id,
            map_ids=[row.id for row in maps],
            map_md5s=map_md5s,
        )

        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.DELETED,
            submission=submission,
        )

    async def _has_foreign_scores(
        self,
        *,
        map_md5s: Sequence[str],
        owner_user_id: int,
    ) -> bool:
        """Whether anyone other than the owner has played this set.

        Composed from two counts rather than one query: the repository has no
        "scores by anyone but X" read, and adding one for this is not worth it.
        ``include_hidden_players`` is True on purpose -- a restricted player's
        score still belongs to them, and the point is that the submitter is not
        the only person whose play history would be erased.
        """
        for md5 in map_md5s:
            total = await self.scores.fetch_count(
                map_md5=md5,
                include_hidden_players=True,
            )
            owned = await self.scores.fetch_count(
                map_md5=md5,
                user_id=owner_user_id,
                include_hidden_players=True,
            )
            if total > owned:
                return True
        return False


def _is_staff(actor: User) -> bool:
    return actor.priv & Privileges.STAFF.value != 0


def _is_nominator(actor: User) -> bool:
    return actor.priv & Privileges.NOMINATOR.value != 0


def _may_view(actor: User | None, submission: MapSubmission) -> bool:
    if submission.review_state == MapSubmissionReviewState.APPROVED.value:
        return True
    if actor is None:
        return False
    if _is_staff(actor) or _is_nominator(actor):
        return True
    return submission.submitter_user_id == actor.id


def _check_status_permitted(
    *,
    status: int,
    submission: MapSubmission,
    is_staff: bool,
) -> BeatmapSubmissionResult | None:
    """Whether this actor may move the set to ``status``."""
    if is_staff:
        return None

    if status in [s.value for s in _STAFF_ONLY_STATUSES]:
        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED,
            detail="only staff may rank beatmaps",
        )

    if status not in [s.value for s in _OWNER_ASSIGNABLE_STATUSES]:
        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED,
            detail="that status cannot be set directly",
        )

    if (
        status == RankedStatus.Loved.value
        and submission.review_state != MapSubmissionReviewState.APPROVED.value
    ):
        # Loved grants a leaderboard *and* the server-wide #1 announcement, so a
        # human has to have looked at the map first.
        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED,
            detail="this beatmap has not been approved by staff yet",
        )

    return None


def _check_cross_difficulty_consistency(
    parsed: Sequence[tuple[OszOsuMember, OsuFileMetadata]],
) -> BeatmapSubmissionResult | None:
    versions = [metadata.version for _, metadata in parsed]
    if len(set(versions)) != len(versions):
        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.DUPLICATE_DIFFICULTY_NAME,
            detail="two difficulties share the same name",
        )

    # one artist/title per set: osu! keys a set on them, and differing values
    # would render as unrelated maps in song select.
    songs = {(metadata.artist, metadata.title) for _, metadata in parsed}
    if len(songs) > 1:
        return BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.INCONSISTENT_METADATA,
            detail="every difficulty must share the same artist and title",
        )

    return None


def _as_submitted_difficulty(
    difficulty: _PreparedDifficulty,
) -> SubmittedDifficulty:
    metadata = difficulty.metadata
    return SubmittedDifficulty(
        beatmap_id=difficulty.beatmap_id,
        md5=difficulty.md5,
        filename=difficulty.filename,
        version=metadata.version,
        mode=metadata.mode,
        status=RankedStatus.Pending.value,
        total_length=metadata.total_length,
        max_combo=difficulty.attributes.max_combo,
        bpm=metadata.bpm,
        cs=metadata.cs,
        ar=metadata.ar,
        od=metadata.od,
        hp=metadata.hp,
        diff=difficulty.attributes.stars,
    )


def _map_to_difficulty(row: Map) -> SubmittedDifficulty:
    return SubmittedDifficulty(
        beatmap_id=row.id,
        md5=row.md5,
        filename=row.filename,
        version=row.version,
        mode=row.mode,
        status=row.status,
        total_length=row.total_length,
        max_combo=row.max_combo,
        bpm=row.bpm,
        cs=row.cs,
        ar=row.ar,
        od=row.od,
        hp=row.hp,
        diff=row.diff,
    )
