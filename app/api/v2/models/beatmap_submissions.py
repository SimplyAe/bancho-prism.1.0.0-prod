from __future__ import annotations

from datetime import datetime
from typing import Literal

from . import BaseModel

# output models


class BeatmapSubmissionDifficultyModel(BaseModel):
    """One hosted difficulty.

    ``id`` is a privately-allocated beatmap id (2,000,000,000+), deliberately far
    above osu!'s own id space so the two can never be confused. ``status`` is the
    ``RankedStatus`` value the difficulty currently carries.
    """

    id: int
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


class BeatmapSubmissionModel(BaseModel):
    """A submitted beatmap set and its review state.

    ``declared_creator`` is what the uploaded files *claimed* about their author.
    It is surfaced for review context only -- it is attacker-controlled, and the
    authoritative creator is the submitting account (see ``maps.creator``).

    ``difficulties`` is populated on the single-set read and empty in listings.
    """

    set_id: int
    submitter_user_id: int
    review_state: str
    declared_creator: str
    difficulty_count: int
    osz_size_bytes: int
    osz_sha256: str
    submitted_at: datetime
    updated_at: datetime
    reviewed_by: int | None
    reviewed_at: datetime | None
    review_note: str | None
    difficulties: list[BeatmapSubmissionDifficultyModel] = []


# input models


class UpdateBeatmapSubmissionRequest(BaseModel):
    """A change to a submission's ranked status and/or its review state.

    ``status`` is restricted to the values that are meaningful to set by hand:
    ``NotSubmitted (-1)`` and ``UpdateAvailable (1)`` are internal sentinels the
    osu!api refresh owns, so rejecting them at parse time keeps them out of the
    service entirely. Which of the remaining values a given caller may actually
    use is a permission question, answered in the service -- a submitter can never
    reach a pp-awarding status.

    ``review_state`` and ``review_note`` are staff-only, also enforced there.
    """

    status: Literal[0, 2, 3, 4, 5] | None = None
    review_state: Literal["pending", "approved", "rejected"] | None = None
    review_note: str | None = None
