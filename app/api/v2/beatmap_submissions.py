"""bancho.py's v2 apis for beatmaps hosted on this server.

Players upload an ``.osz`` here and the server hosts it: allocating ids of its
own, storing the files, and registering ``maps`` rows the game treats like any
other beatmap. Five endpoints over
:class:`app.services.beatmap_submissions.BeatmapSubmissionService`:

- ``POST /beatmaps/submissions`` uploads an archive and hosts it.
- ``GET /beatmaps/submissions`` lists submissions the caller may see.
- ``GET /beatmaps/submissions/{set_id}`` reads one, with its difficulties.
- ``PATCH /beatmaps/submissions/{set_id}`` changes the ranked status (owner, within
  limits) and/or the review state (staff only).
- ``DELETE /beatmaps/submissions/{set_id}`` takes a set down.

Every endpoint needs a session, checked in-handler rather than through a raising
dependency so an unauthorized caller gets the same ``{"status": ...}`` envelope as
every other error here. Authorization *beyond* "is signed in" belongs to the
service: it owns the rule that a submitter can never give their own beatmap a
pp-awarding status, and this layer only maps its outcomes onto HTTP.

The whole feature is behind ``BEATMAP_SUBMISSION_ENABLED`` and off by default, so
a server that has not opted in exposes no upload surface at all.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Query
from fastapi import UploadFile
from fastapi import status

import app.settings
from app.api import dependencies as api_dependencies
from app.api.v2.common import responses
from app.api.v2.common.actors import get_optional_actor
from app.api.v2.common.responses import Failure
from app.api.v2.common.responses import Success
from app.api.v2.models.beatmap_submissions import BeatmapSubmissionDifficultyModel
from app.api.v2.models.beatmap_submissions import BeatmapSubmissionModel
from app.api.v2.models.beatmap_submissions import UpdateBeatmapSubmissionRequest
from app.repositories.users import User
from app.services.beatmap_submissions import BeatmapSubmissionResult
from app.services.beatmap_submissions import BeatmapSubmissionResultCode
from app.services.beatmap_submissions import BeatmapSubmissionService
from app.services.beatmap_submissions import SubmittedDifficulty

router = APIRouter()

# Each failure the service can report, and the status it earns. Distinct codes get
# distinct statuses on purpose: "your account cannot submit" (403) and "you are
# over your quota" (429) are very different things to a client.
_FAILURE_STATUSES: dict[BeatmapSubmissionResultCode, int] = {
    BeatmapSubmissionResultCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    BeatmapSubmissionResultCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    BeatmapSubmissionResultCode.NOT_ELIGIBLE: status.HTTP_403_FORBIDDEN,
    BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED: status.HTTP_403_FORBIDDEN,
    BeatmapSubmissionResultCode.QUOTA_EXCEEDED: status.HTTP_429_TOO_MANY_REQUESTS,
    BeatmapSubmissionResultCode.ARCHIVE_TOO_LARGE: (status.HTTP_413_CONTENT_TOO_LARGE),
    BeatmapSubmissionResultCode.CHECKSUM_ALREADY_EXISTS: status.HTTP_409_CONFLICT,
    BeatmapSubmissionResultCode.FILENAME_ALREADY_EXISTS: status.HTTP_409_CONFLICT,
    BeatmapSubmissionResultCode.HAS_FOREIGN_SCORES: status.HTTP_409_CONFLICT,
    BeatmapSubmissionResultCode.INVALID_ARCHIVE: status.HTTP_400_BAD_REQUEST,
    BeatmapSubmissionResultCode.INVALID_DIFFICULTY_FILE: status.HTTP_400_BAD_REQUEST,
    BeatmapSubmissionResultCode.DUPLICATE_DIFFICULTY_NAME: status.HTTP_400_BAD_REQUEST,
    BeatmapSubmissionResultCode.INCONSISTENT_METADATA: status.HTTP_400_BAD_REQUEST,
    BeatmapSubmissionResultCode.RATING_FAILED: status.HTTP_400_BAD_REQUEST,
}

_DEFAULT_FAILURE_MESSAGES: dict[BeatmapSubmissionResultCode, str] = {
    BeatmapSubmissionResultCode.NOT_FOUND: "Beatmap submission not found.",
    BeatmapSubmissionResultCode.FORBIDDEN: ("You do not have permission to do that."),
    BeatmapSubmissionResultCode.NOT_ELIGIBLE: (
        "Your account is not eligible to submit beatmaps."
    ),
    BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED: (
        "You cannot set that ranked status."
    ),
    BeatmapSubmissionResultCode.QUOTA_EXCEEDED: (
        "You have reached your beatmap submission limit."
    ),
    BeatmapSubmissionResultCode.ARCHIVE_TOO_LARGE: "That .osz is too large.",
    BeatmapSubmissionResultCode.CHECKSUM_ALREADY_EXISTS: (
        "That beatmap already exists on this server."
    ),
    BeatmapSubmissionResultCode.FILENAME_ALREADY_EXISTS: (
        "A beatmap with that filename already exists."
    ),
    BeatmapSubmissionResultCode.HAS_FOREIGN_SCORES: (
        "Other players have set scores on this beatmap."
    ),
    BeatmapSubmissionResultCode.INVALID_ARCHIVE: "That file is not a valid .osz.",
    BeatmapSubmissionResultCode.INVALID_DIFFICULTY_FILE: (
        "One of the .osu files could not be read."
    ),
    BeatmapSubmissionResultCode.DUPLICATE_DIFFICULTY_NAME: (
        "Two difficulties share the same name."
    ),
    BeatmapSubmissionResultCode.INCONSISTENT_METADATA: (
        "Every difficulty must share the same artist and title."
    ),
    BeatmapSubmissionResultCode.RATING_FAILED: (
        "One of the difficulties could not be rated."
    ),
}


def _require_actor(actor: User | None) -> Failure | None:
    """Every endpoint here needs a session; returns a Failure to send, or None."""
    if actor is None:
        return responses.failure(
            message="Authentication required.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return None


def _feature_disabled() -> Failure:
    # 404 rather than 503: a server that has not enabled submissions does not have
    # this endpoint, and saying so invites no further attempts.
    return responses.failure(
        message="Beatmap submission is not enabled on this server.",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _failure(result: BeatmapSubmissionResult) -> Failure:
    """Turn a service failure into its HTTP answer."""
    status_code = _FAILURE_STATUSES.get(
        result.code,
        status.HTTP_400_BAD_REQUEST,
    )
    # the service's `detail` names the offending file or limit where it can, which
    # is the difference between a usable error and "invalid archive".
    message = result.detail or _DEFAULT_FAILURE_MESSAGES.get(
        result.code,
        "Your beatmap submission could not be processed.",
    )
    return responses.failure(message=message, status_code=status_code)


def _difficulty_model(
    difficulty: SubmittedDifficulty,
) -> BeatmapSubmissionDifficultyModel:
    return BeatmapSubmissionDifficultyModel(
        id=difficulty.beatmap_id,
        md5=difficulty.md5,
        filename=difficulty.filename,
        version=difficulty.version,
        mode=difficulty.mode,
        status=difficulty.status,
        total_length=difficulty.total_length,
        max_combo=difficulty.max_combo,
        bpm=difficulty.bpm,
        cs=difficulty.cs,
        ar=difficulty.ar,
        od=difficulty.od,
        hp=difficulty.hp,
        diff=difficulty.diff,
    )


def _submission_model(result: BeatmapSubmissionResult) -> BeatmapSubmissionModel:
    assert result.submission is not None  # every success carries its submission.
    model = BeatmapSubmissionModel.model_validate(result.submission)
    model.difficulties = [
        _difficulty_model(difficulty) for difficulty in result.difficulties
    ]
    return model


@router.post("/beatmaps/submissions")
async def submit_beatmap(
    osz_file: UploadFile = File(..., alias="osz"),
    *,
    actor: Annotated[User | None, Depends(get_optional_actor)],
    beatmap_submission_service: Annotated[
        BeatmapSubmissionService,
        Depends(api_dependencies.get_beatmap_submission_service),
    ],
) -> Success[BeatmapSubmissionModel] | Failure:
    if not app.settings.BEATMAP_SUBMISSION_ENABLED:
        return _feature_disabled()

    denied = _require_actor(actor)
    if denied is not None:
        return denied
    assert actor is not None

    result = await beatmap_submission_service.submit_beatmap_archive(
        submitter=actor,
        osz_data=await osz_file.read(),
    )
    if result.code is not BeatmapSubmissionResultCode.CREATED:
        return _failure(result)

    return responses.success(
        _submission_model(result),
        status_code=status.HTTP_201_CREATED,
    )


@router.get("/beatmaps/submissions")
async def get_beatmap_submissions(
    mine: bool = Query(False),
    submitter_user_id: int | None = Query(None, ge=1),
    review_state: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    *,
    actor: Annotated[User | None, Depends(get_optional_actor)],
    beatmap_submission_service: Annotated[
        BeatmapSubmissionService,
        Depends(api_dependencies.get_beatmap_submission_service),
    ],
) -> Success[list[BeatmapSubmissionModel]] | Failure:
    if not app.settings.BEATMAP_SUBMISSION_ENABLED:
        return _feature_disabled()

    denied = _require_actor(actor)
    if denied is not None:
        return denied
    assert actor is not None

    listing = await beatmap_submission_service.fetch_submissions(
        actor=actor,
        # `mine` is a convenience for the common case; it wins over an explicit id
        # so a client cannot accidentally ask for both.
        owner_user_id=actor.id if mine else submitter_user_id,
        review_state=review_state,
        page=page,
        page_size=page_size,
    )

    return responses.success(
        content=[
            BeatmapSubmissionModel.model_validate(submission)
            for submission in listing.submissions
        ],
        meta={
            "total": listing.total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/beatmaps/submissions/{set_id}")
async def get_beatmap_submission(
    set_id: int,
    *,
    actor: Annotated[User | None, Depends(get_optional_actor)],
    beatmap_submission_service: Annotated[
        BeatmapSubmissionService,
        Depends(api_dependencies.get_beatmap_submission_service),
    ],
) -> Success[BeatmapSubmissionModel] | Failure:
    if not app.settings.BEATMAP_SUBMISSION_ENABLED:
        return _feature_disabled()

    denied = _require_actor(actor)
    if denied is not None:
        return denied

    result = await beatmap_submission_service.fetch_submission(
        actor=actor,
        set_id=set_id,
    )
    if result.code is not BeatmapSubmissionResultCode.FOUND:
        return _failure(result)

    return responses.success(_submission_model(result))


@router.patch("/beatmaps/submissions/{set_id}")
async def update_beatmap_submission(
    set_id: int,
    args: UpdateBeatmapSubmissionRequest,
    *,
    actor: Annotated[User | None, Depends(get_optional_actor)],
    beatmap_submission_service: Annotated[
        BeatmapSubmissionService,
        Depends(api_dependencies.get_beatmap_submission_service),
    ],
) -> Success[BeatmapSubmissionModel] | Failure:
    if not app.settings.BEATMAP_SUBMISSION_ENABLED:
        return _feature_disabled()

    denied = _require_actor(actor)
    if denied is not None:
        return denied
    assert actor is not None

    result = await beatmap_submission_service.update_submission(
        actor=actor,
        set_id=set_id,
        status=args.status,
        review_state=args.review_state,
        review_note=args.review_note,
    )
    if result.code is not BeatmapSubmissionResultCode.UPDATED:
        return _failure(result)

    return responses.success(_submission_model(result))


@router.delete("/beatmaps/submissions/{set_id}")
async def delete_beatmap_submission(
    set_id: int,
    *,
    actor: Annotated[User | None, Depends(get_optional_actor)],
    beatmap_submission_service: Annotated[
        BeatmapSubmissionService,
        Depends(api_dependencies.get_beatmap_submission_service),
    ],
) -> Success[None] | Failure:
    if not app.settings.BEATMAP_SUBMISSION_ENABLED:
        return _feature_disabled()

    denied = _require_actor(actor)
    if denied is not None:
        return denied
    assert actor is not None

    result = await beatmap_submission_service.delete_submission(
        actor=actor,
        set_id=set_id,
    )
    if result.code is not BeatmapSubmissionResultCode.DELETED:
        return _failure(result)

    return responses.success(None)
