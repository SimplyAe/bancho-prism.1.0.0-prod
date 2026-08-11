from __future__ import annotations

from datetime import datetime
from typing import Any

from . import BaseModel

# output models


class ActivityEventModel(BaseModel):
    """A single feed entry, mirroring the repository's ``ActivityEvent``.

    ``event_type`` is surfaced as a plain string and ``data`` as an open object:
    the closed vocabulary and each payload's shape live in the service layer
    (:class:`app.services.social.activity_feed.ActivityEventType`), so a client
    renders by ``event_type`` without this model being coupled to that enum. A
    new event kind therefore needs no change here. ``mode`` and ``data`` are
    null when the event carries neither.
    """

    id: int
    user_id: int
    event_type: str
    mode: int | None
    data: dict[str, Any] | None
    created_at: datetime
