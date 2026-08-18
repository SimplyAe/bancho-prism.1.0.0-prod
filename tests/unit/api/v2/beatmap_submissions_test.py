"""Guards the beatmap-submission HTTP surface.

The handlers are called directly with a fake service, so these tests pin what this
layer actually owns -- the translation between the service's outcomes and HTTP --
without a database, a filesystem, or an ASGI stack.

What matters here:

- **the feature is off by default**, and while off every endpoint answers 404: a
  server that has not opted in exposes no upload surface to probe;
- **every endpoint needs a session**, answered 401 *in the shared envelope* rather
  than by a raising dependency, so an unauthorized caller gets the same body shape
  as every other error;
- **each result code earns its own status.** A single parametrised test covers all
  of them, so adding a code to the service without deciding its status here fails
  loudly instead of silently returning 200;
- **``status`` is constrained at parse time** to the values that are meaningful to
  set by hand: ``NotSubmitted (-1)`` and ``UpdateAvailable (1)`` are internal
  sentinels the osu!api refresh owns, and they never reach the service.

Authorization beyond "is signed in" is the service's job (a submitter can never
give their own beatmap a pp-awarding status); that is tested against the service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

import app.settings
from app.api.v2 import beatmap_submissions as api
from app.api.v2.models.beatmap_submissions import UpdateBeatmapSubmissionRequest
from app.constants.privileges import Privileges
from app.repositories.map_submissions import MapSubmission
from app.repositories.users import User
from app.services.beatmap_submissions import BeatmapSubmissionListing
from app.services.beatmap_submissions import BeatmapSubmissionResult
from app.services.beatmap_submissions import BeatmapSubmissionResultCode
from app.services.beatmap_submissions import SubmittedDifficulty

_NOW = datetime(2026, 8, 18, 12, 0, 0)
_SET_ID = 2_000_000_000


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # every test but the disabled ones assumes the operator opted in.
    monkeypatch.setattr(app.settings, "BEATMAP_SUBMISSION_ENABLED", True)


def _user(*, user_id: int = 6) -> User:
    return User(
        id=user_id,
        name="submitter",
        safe_name="submitter",
        email="submitter@example.com",
        priv=Privileges.UNRESTRICTED.value | Privileges.VERIFIED.value,
        country="us",
        silence_end=0,
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


def _submission(*, set_id: int = _SET_ID) -> MapSubmission:
    return MapSubmission(
        set_id=set_id,
        submitter_user_id=6,
        review_state="pending",
        declared_creator="someone-else",
        difficulty_count=1,
        osz_size_bytes=2048,
        osz_sha256="0" * 64,
        submitted_at=_NOW,
        updated_at=_NOW,
        reviewed_by=None,
        reviewed_at=None,
        review_note=None,
    )


def _difficulty() -> SubmittedDifficulty:
    return SubmittedDifficulty(
        beatmap_id=_SET_ID + 1,
        md5="a" * 32,
        filename="a - t (submitter) [Easy].osu",
        version="Easy",
        mode=0,
        status=0,
        total_length=90,
        max_combo=250,
        bpm=180.0,
        cs=4.0,
        ar=9.0,
        od=8.0,
        hp=5.0,
        diff=5.5,
    )


class _FakeUploadFile:
    """Just enough of ``UploadFile`` for the handler: an awaitable ``read``."""

    def __init__(self, data: bytes = b"fake-osz") -> None:
        self._data = data
        self.reads = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        return self._data


class _FakeSubmissionService:
    def __init__(
        self,
        *,
        result: BeatmapSubmissionResult | None = None,
        listing: BeatmapSubmissionListing | None = None,
    ) -> None:
        self._result = result or BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.CREATED,
            submission=_submission(),
            difficulties=(_difficulty(),),
        )
        self._listing = listing or BeatmapSubmissionListing(
            submissions=[_submission()],
            total=1,
        )
        self.calls: list[dict[str, Any]] = []

    async def submit_beatmap_archive(
        self,
        *,
        submitter: User,
        osz_data: bytes,
    ) -> BeatmapSubmissionResult:
        self.calls.append(
            {
                "method": "submit",
                "submitter": submitter,
                "osz_size": len(osz_data),
            },
        )
        return self._result

    async def fetch_submissions(self, **kwargs: Any) -> BeatmapSubmissionListing:
        self.calls.append({"method": "list", **kwargs})
        return self._listing

    async def fetch_submission(self, **kwargs: Any) -> BeatmapSubmissionResult:
        self.calls.append({"method": "fetch", **kwargs})
        return self._result

    async def update_submission(self, **kwargs: Any) -> BeatmapSubmissionResult:
        self.calls.append({"method": "update", **kwargs})
        return self._result

    async def delete_submission(self, **kwargs: Any) -> BeatmapSubmissionResult:
        self.calls.append({"method": "delete", **kwargs})
        return self._result


def _body(response: Any) -> dict[str, Any]:
    return orjson.loads(response.body)


# --- the feature switch ----------------------------------------------------


async def test_every_endpoint_is_absent_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.settings, "BEATMAP_SUBMISSION_ENABLED", False)
    service = _FakeSubmissionService()

    responses = [
        await api.submit_beatmap(
            osz_file=_FakeUploadFile(),  # type: ignore[arg-type]
            actor=_user(),
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.get_beatmap_submissions(
            actor=_user(),
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.get_beatmap_submission(
            _SET_ID,
            actor=_user(),
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.update_beatmap_submission(
            _SET_ID,
            UpdateBeatmapSubmissionRequest(status=0),
            actor=_user(),
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.delete_beatmap_submission(
            _SET_ID,
            actor=_user(),
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
    ]

    # 404, not 503: a server without submissions enabled does not have these.
    assert [r.status_code for r in responses] == [404] * 5
    # and nothing reached the service.
    assert service.calls == []


# --- authentication --------------------------------------------------------


async def test_every_endpoint_requires_a_session() -> None:
    service = _FakeSubmissionService()

    responses = [
        await api.submit_beatmap(
            osz_file=_FakeUploadFile(),  # type: ignore[arg-type]
            actor=None,
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.get_beatmap_submissions(
            actor=None,
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.get_beatmap_submission(
            _SET_ID,
            actor=None,
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.update_beatmap_submission(
            _SET_ID,
            UpdateBeatmapSubmissionRequest(status=0),
            actor=None,
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
        await api.delete_beatmap_submission(
            _SET_ID,
            actor=None,
            beatmap_submission_service=service,  # type: ignore[arg-type]
        ),
    ]

    assert [r.status_code for r in responses] == [401] * 5
    # the envelope holds -- this is not FastAPI's own error shape.
    assert all(_body(r)["status"] == "error" for r in responses)
    assert service.calls == []


# --- uploading -------------------------------------------------------------


async def test_a_successful_upload_is_201_with_its_difficulties() -> None:
    service = _FakeSubmissionService()
    upload = _FakeUploadFile(b"x" * 1234)

    response = await api.submit_beatmap(
        osz_file=upload,  # type: ignore[arg-type]
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 201
    data = _body(response)["data"]
    assert data["set_id"] == _SET_ID
    assert [d["id"] for d in data["difficulties"]] == [_SET_ID + 1]
    # the archive bytes were read once and handed to the service whole.
    assert upload.reads == 1
    assert service.calls[0]["osz_size"] == 1234
    assert service.calls[0]["submitter"].id == 6


async def test_the_service_detail_is_surfaced_to_the_caller() -> None:
    # "invalid archive" alone is not actionable; the service names the file.
    service = _FakeSubmissionService(
        result=BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.INVALID_DIFFICULTY_FILE,
            detail="song [Hard].osu: missing [TimingPoints] section",
        ),
    )

    response = await api.submit_beatmap(
        osz_file=_FakeUploadFile(),  # type: ignore[arg-type]
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 400
    assert "song [Hard].osu" in _body(response)["error"]


# --- the code-to-status map -----------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (BeatmapSubmissionResultCode.NOT_FOUND, 404),
        (BeatmapSubmissionResultCode.FORBIDDEN, 403),
        (BeatmapSubmissionResultCode.NOT_ELIGIBLE, 403),
        (BeatmapSubmissionResultCode.STATUS_NOT_PERMITTED, 403),
        (BeatmapSubmissionResultCode.QUOTA_EXCEEDED, 429),
        (BeatmapSubmissionResultCode.ARCHIVE_TOO_LARGE, 413),
        (BeatmapSubmissionResultCode.CHECKSUM_ALREADY_EXISTS, 409),
        (BeatmapSubmissionResultCode.FILENAME_ALREADY_EXISTS, 409),
        (BeatmapSubmissionResultCode.HAS_FOREIGN_SCORES, 409),
        (BeatmapSubmissionResultCode.INVALID_ARCHIVE, 400),
        (BeatmapSubmissionResultCode.INVALID_DIFFICULTY_FILE, 400),
        (BeatmapSubmissionResultCode.DUPLICATE_DIFFICULTY_NAME, 400),
        (BeatmapSubmissionResultCode.INCONSISTENT_METADATA, 400),
        (BeatmapSubmissionResultCode.RATING_FAILED, 400),
    ],
)
async def test_each_failure_code_maps_to_its_status(
    code: BeatmapSubmissionResultCode,
    expected_status: int,
) -> None:
    service = _FakeSubmissionService(result=BeatmapSubmissionResult(code=code))

    response = await api.submit_beatmap(
        osz_file=_FakeUploadFile(),  # type: ignore[arg-type]
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == expected_status, code
    body = _body(response)
    assert body["status"] == "error"
    # every code carries a human message, not an empty string.
    assert body["error"]


async def test_every_failure_code_has_a_declared_status() -> None:
    # the guard against adding a code to the service and forgetting it here: an
    # unmapped failure would fall through to a generic 400 unnoticed.
    successes = {
        BeatmapSubmissionResultCode.CREATED,
        BeatmapSubmissionResultCode.UPDATED,
        BeatmapSubmissionResultCode.DELETED,
        BeatmapSubmissionResultCode.FOUND,
    }
    failures = set(BeatmapSubmissionResultCode) - successes

    assert failures == set(api._FAILURE_STATUSES)
    assert failures == set(api._DEFAULT_FAILURE_MESSAGES)


# --- listing ---------------------------------------------------------------


async def test_listing_returns_paging_metadata() -> None:
    service = _FakeSubmissionService()

    response = await api.get_beatmap_submissions(
        page=2,
        page_size=10,
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert _body(response)["meta"] == {"total": 1, "page": 2, "page_size": 10}


async def test_mine_scopes_the_listing_to_the_caller() -> None:
    service = _FakeSubmissionService()

    await api.get_beatmap_submissions(
        mine=True,
        submitter_user_id=999,  # ignored: `mine` wins
        # explicit: calling the handler directly leaves FastAPI's Query defaults
        # unresolved, so they would otherwise reach the response as Query objects.
        page=1,
        page_size=50,
        actor=_user(user_id=6),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert service.calls[0]["owner_user_id"] == 6


async def test_listing_without_mine_passes_the_requested_owner() -> None:
    service = _FakeSubmissionService()

    await api.get_beatmap_submissions(
        mine=False,
        submitter_user_id=42,
        page=1,
        page_size=50,
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert service.calls[0]["owner_user_id"] == 42


# --- reading one ---------------------------------------------------------


async def test_reading_one_returns_its_difficulties() -> None:
    service = _FakeSubmissionService(
        result=BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.FOUND,
            submission=_submission(),
            difficulties=(_difficulty(),),
        ),
    )

    response = await api.get_beatmap_submission(
        _SET_ID,
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    data = _body(response)["data"]
    assert data["difficulties"][0]["md5"] == "a" * 32
    assert data["declared_creator"] == "someone-else"


async def test_an_invisible_submission_reads_as_missing() -> None:
    service = _FakeSubmissionService(
        result=BeatmapSubmissionResult(code=BeatmapSubmissionResultCode.NOT_FOUND),
    )

    response = await api.get_beatmap_submission(
        _SET_ID,
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 404


# --- updating ------------------------------------------------------------


async def test_updating_forwards_every_field() -> None:
    service = _FakeSubmissionService(
        result=BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.UPDATED,
            submission=_submission(),
        ),
    )

    response = await api.update_beatmap_submission(
        _SET_ID,
        UpdateBeatmapSubmissionRequest(
            status=5,
            review_state="approved",
            review_note="looks good",
        ),
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    call = service.calls[0]
    assert call["set_id"] == _SET_ID
    assert call["status"] == 5
    assert call["review_state"] == "approved"
    assert call["review_note"] == "looks good"


@pytest.mark.parametrize("status", [-1, 1, 7, 99])
def test_internal_ranked_statuses_are_rejected_at_parse_time(status: int) -> None:
    # NotSubmitted (-1) and UpdateAvailable (1) belong to the osu!api refresh;
    # rejecting them here keeps them out of the service entirely.
    with pytest.raises(ValidationError):
        UpdateBeatmapSubmissionRequest(status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [0, 2, 3, 4, 5])
def test_settable_ranked_statuses_are_accepted(status: int) -> None:
    # whether a *given caller* may use each of these is the service's call.
    request = UpdateBeatmapSubmissionRequest(status=status)  # type: ignore[arg-type]
    assert request.status == status


def test_an_unknown_review_state_is_rejected() -> None:
    with pytest.raises(ValidationError):
        UpdateBeatmapSubmissionRequest(review_state="ranked")  # type: ignore[arg-type]


# --- deleting ------------------------------------------------------------


async def test_deleting_returns_no_content_payload() -> None:
    service = _FakeSubmissionService(
        result=BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.DELETED,
            submission=_submission(),
        ),
    )

    response = await api.delete_beatmap_submission(
        _SET_ID,
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert _body(response)["data"] is None
    assert service.calls[0]["set_id"] == _SET_ID


async def test_deleting_a_played_set_is_a_conflict() -> None:
    service = _FakeSubmissionService(
        result=BeatmapSubmissionResult(
            code=BeatmapSubmissionResultCode.HAS_FOREIGN_SCORES,
            detail="other players have set scores on this beatmap",
        ),
    )

    response = await api.delete_beatmap_submission(
        _SET_ID,
        actor=_user(),
        beatmap_submission_service=service,  # type: ignore[arg-type]
    )

    assert response.status_code == 409
