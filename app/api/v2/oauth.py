"""bancho.py's v2 apis for linking an osu! account to a Discord account.

Four endpoints over :class:`app.services.social.discord_linking.DiscordLinkingService`:

- ``GET /oauth/discord/authorize`` (session-gated) starts a flow and hands back
  Discord's authorize URL for the caller to redirect to.
- ``GET /oauth/discord/callback`` finishes it. It is deliberately *not*
  session-gated: the ``state`` minted at authorize time is bound in Redis to the
  requesting player, so the callback links *that* account, not whoever's cookie
  happens to ride in. Each :class:`LinkOutcome` maps to a distinct status.
- ``GET`` / ``DELETE /oauth/discord/link`` (session-gated) read and remove the
  current player's link.

The authorize/callback pair returns JSON (an authorize URL, or the resulting
link) rather than issuing 302s, so the shared ``{"status": ...}`` envelope holds
and every path unit-tests without chasing redirects. Auth and configuration are
checked in-handler for the same reason -- a raising dependency would bypass the
envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status

from app.api import dependencies as api_dependencies
from app.api.v2.common import responses
from app.api.v2.common.parameters import SessionCookie
from app.api.v2.common.responses import Failure
from app.api.v2.common.responses import Success
from app.api.v2.models.oauth import DiscordAuthorizeModel
from app.api.v2.models.oauth import DiscordLinkModel
from app.services.social.discord_linking import DiscordLinkingService
from app.services.social.discord_linking import LinkOutcome
from app.services.web_sessions import WebSessionsService

router = APIRouter()

# each way a completed link can end, and the response it earns. LINKED is handled
# on its own (it carries the link to serialize); the rest are plain failures.
_FAILURE_RESPONSES: dict[LinkOutcome, tuple[int, str]] = {
    LinkOutcome.DISABLED: (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Discord account linking is not configured on this server.",
    ),
    LinkOutcome.INVALID_STATE: (
        status.HTTP_400_BAD_REQUEST,
        "The link request is invalid or has expired; start it again.",
    ),
    LinkOutcome.EXCHANGE_FAILED: (
        status.HTTP_502_BAD_GATEWAY,
        "Could not complete the exchange with Discord; try again.",
    ),
    LinkOutcome.IDENTITY_FAILED: (
        status.HTTP_502_BAD_GATEWAY,
        "Could not read your Discord account; try again.",
    ),
    LinkOutcome.ALREADY_LINKED_ELSEWHERE: (
        status.HTTP_409_CONFLICT,
        "That Discord account is already linked to another player.",
    ),
}


@router.get("/oauth/discord/authorize")
async def begin_discord_link(
    session_token: SessionCookie = None,
    *,
    discord_linking_service: Annotated[
        DiscordLinkingService,
        Depends(api_dependencies.get_discord_linking_service),
    ],
    web_sessions_service: Annotated[
        WebSessionsService,
        Depends(api_dependencies.get_web_sessions_service),
    ],
) -> Success[DiscordAuthorizeModel] | Failure:
    if session_token is None:
        return responses.failure(
            message="Authentication required.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    user = await web_sessions_service.fetch_session_user(session_token)
    if user is None:
        return responses.failure(
            message="Invalid or expired session.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    authorize_url = await discord_linking_service.begin_link(user.id)
    if authorize_url is None:
        return responses.failure(
            message="Discord account linking is not configured on this server.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return responses.success(DiscordAuthorizeModel(authorize_url=authorize_url))


@router.get("/oauth/discord/callback")
async def complete_discord_link(
    code: str | None = None,
    state: str | None = None,
    *,
    discord_linking_service: Annotated[
        DiscordLinkingService,
        Depends(api_dependencies.get_discord_linking_service),
    ],
) -> Success[DiscordLinkModel] | Failure:
    # Discord sends `?code=&state=` on approval and an `?error=` (no code) when
    # the player declines. A missing code or state is the same class of problem
    # as a bad state: the flow did not complete, so answer 400 in our envelope
    # rather than letting FastAPI 422 on a required-query-param and skip it.
    if code is None or state is None:
        return responses.failure(
            message="The link request is invalid or has expired; start it again.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    result = await discord_linking_service.complete_link(state=state, code=code)
    if result.outcome is LinkOutcome.LINKED:
        assert result.link is not None  # LINKED always carries the stored link.
        return responses.success(DiscordLinkModel.model_validate(result.link))

    status_code, message = _FAILURE_RESPONSES[result.outcome]
    return responses.failure(message=message, status_code=status_code)


@router.get("/oauth/discord/link")
async def get_discord_link(
    session_token: SessionCookie = None,
    *,
    discord_linking_service: Annotated[
        DiscordLinkingService,
        Depends(api_dependencies.get_discord_linking_service),
    ],
    web_sessions_service: Annotated[
        WebSessionsService,
        Depends(api_dependencies.get_web_sessions_service),
    ],
) -> Success[DiscordLinkModel] | Failure:
    if session_token is None:
        return responses.failure(
            message="Authentication required.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    user = await web_sessions_service.fetch_session_user(session_token)
    if user is None:
        return responses.failure(
            message="Invalid or expired session.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    link = await discord_linking_service.fetch_link(user.id)
    if link is None:
        return responses.failure(
            message="No Discord account is linked to this player.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return responses.success(DiscordLinkModel.model_validate(link))


@router.delete("/oauth/discord/link")
async def delete_discord_link(
    session_token: SessionCookie = None,
    *,
    discord_linking_service: Annotated[
        DiscordLinkingService,
        Depends(api_dependencies.get_discord_linking_service),
    ],
    web_sessions_service: Annotated[
        WebSessionsService,
        Depends(api_dependencies.get_web_sessions_service),
    ],
) -> Success[None] | Failure:
    if session_token is None:
        return responses.failure(
            message="Authentication required.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    user = await web_sessions_service.fetch_session_user(session_token)
    if user is None:
        return responses.failure(
            message="Invalid or expired session.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # a no-op when nothing was linked; unlinking is idempotent by design.
    await discord_linking_service.unlink(user.id)
    return responses.success(None)
