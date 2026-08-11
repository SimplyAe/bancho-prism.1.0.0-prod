from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from . import BaseModel

# output models


class AnticheatFlagModel(BaseModel):
    """A staff-facing view of one flagged score.

    Mirrors the repository's ``AnticheatFlag`` one-for-one. ``status`` and
    ``severity`` are surfaced as plain strings (the repository's ``severity`` is
    already an opaque string, and ``AnticheatFlagStatus`` is a ``StrEnum`` that
    validates cleanly against ``str``), so a reviewer's client is not coupled to
    the detector layer's enums. ``evidence`` is the raw numbers behind the top
    signal, or null if none were recorded.
    """

    score_id: int
    user_id: int
    mode: int
    status: str
    severity: str
    top_signal_code: str
    top_signal_title: str
    confidence: float
    triggered_count: int
    detail: str
    evidence: dict[str, Any] | None
    first_flagged_at: datetime
    last_flagged_at: datetime
    resolved_by: int | None
    resolved_at: datetime | None
    resolution_note: str | None


# input models


class ResolveFlagRequest(BaseModel):
    """A staff member's decision on a flag.

    ``status`` deliberately excludes ``"open"``: reopening is not a decision a
    reviewer records here (a fresh flag is already ``OPEN``), so the only
    transitions offered are claiming it (``reviewing``) or a terminal verdict
    (``actioned`` / ``dismissed``). Rejecting ``"open"`` at parse time keeps the
    invalid transition out of the service entirely.
    """

    status: Literal["reviewing", "actioned", "dismissed"]
    note: str | None = None
