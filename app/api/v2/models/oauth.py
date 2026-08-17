from __future__ import annotations

from datetime import datetime

from . import BaseModel

# output models


class DiscordAuthorizeModel(BaseModel):
    """Where to send the player to approve a Discord link.

    ``begin_link`` mints a state and hands back Discord's authorize URL; the
    caller (a frontend, or the client) redirects the browser there. Returned as
    JSON rather than a 302 so the v2 ``{"status": ...}`` envelope holds and the
    endpoint unit-tests without following redirects.
    """

    authorize_url: str


class DiscordLinkModel(BaseModel):
    """A player's Discord link, mirroring the repository's ``DiscordLink``.

    ``from_attributes`` (set on the shared base) lets this validate straight off
    the frozen DTO, so the handler never hand-copies fields.
    """

    user_id: int
    discord_id: str
    discord_username: str
    linked_at: datetime
