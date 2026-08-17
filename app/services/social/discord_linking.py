"""Linking an osu! account to a Discord account over OAuth2.

This is the flow behind "connect your Discord": a logged-in player is sent to
Discord to approve the link, Discord redirects back with a one-time ``code``, and
we trade that code for the player's Discord identity and store the pairing. Two
guarantees shape the whole thing:

- **the callback links the account that started the flow.** The ``state`` token
  minted at ``begin_link`` is bound in Redis to the requesting ``user_id`` (see
  ``app.repositories.discord_oauth_state``); ``complete_link`` trusts *that*, not
  whatever session rode in on the callback request. A forged or stale callback
  finds no state and is rejected.
- **one Discord account backs at most one osu! account.** Before writing the
  link we check whether the Discord id is already claimed by a *different* osu!
  account and refuse rather than steal it. Re-linking your own account (to a
  different Discord) is a clean replace.

Like ``CaptchaService``, this is a frozen service with its network steps injected
as callables (``exchange_code`` and ``fetch_identity``), so the flow unit-tests
with no live HTTP. The pure URL-building and response-parsing live in
``app.adapters.discord_oauth``. Every network step is wrapped: a Discord outage
or a malformed response becomes a typed failure outcome, never an exception that
escapes the flow. The service is gated on config -- absent OAuth credentials
leave it disabled, and ``begin_link`` returns ``None`` rather than sending a
player to a half-configured Discord.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.adapters import discord_oauth
from app.adapters.discord_oauth import DiscordIdentity
from app.logging import Ansi
from app.logging import log
from app.repositories.discord_oauth_state import DiscordOAuthStateRepository
from app.repositories.user_discord_links import DiscordLink
from app.repositories.user_discord_links import DiscordLinksRepository

# how long a player has between the redirect out to Discord and the redirect
# back before the CSRF state expires on its own. Long enough to approve, short
# enough that an abandoned flow leaves nothing lingering.
DISCORD_OAUTH_STATE_EXPIRY_SECONDS = 60 * 10  # 10 minutes

# POST a form body to a URL, return the decoded JSON (the token exchange).
# GET a URL with headers, return the decoded JSON (the identity fetch). Both are
# injected so the service needs no live HTTP client; the DI layer supplies real
# httpx-backed helpers, tests supply fakes.
ExchangeCode = Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]]
FetchIdentity = Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]]


class LinkOutcome(StrEnum):
    """Why a ``complete_link`` attempt ended the way it did.

    Exactly one is returned per attempt, and the API layer maps each to a
    response: ``LINKED`` is the success, the rest are the distinct ways a link
    can legitimately fail without anything being broken.
    """

    LINKED = "linked"
    DISABLED = "disabled"
    INVALID_STATE = "invalid_state"
    EXCHANGE_FAILED = "exchange_failed"
    IDENTITY_FAILED = "identity_failed"
    ALREADY_LINKED_ELSEWHERE = "already_linked_elsewhere"


@dataclass(frozen=True, slots=True)
class LinkResult:
    outcome: LinkOutcome
    link: DiscordLink | None = None


@dataclass(frozen=True)
class DiscordLinkingService:
    client_id: str
    client_secret: str
    redirect_uri: str
    links: DiscordLinksRepository
    oauth_state: DiscordOAuthStateRepository
    exchange_code: ExchangeCode
    fetch_identity: FetchIdentity
    generate_state: Callable[[], str]

    @property
    def enabled(self) -> bool:
        """True only when every OAuth credential is configured.

        A partially-configured integration is treated as off: better a disabled
        feature than a redirect to Discord that can never complete.
        """
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    async def begin_link(self, user_id: int) -> str | None:
        """Start a link for ``user_id``; return the Discord authorize URL.

        Mints a single-use ``state`` bound to ``user_id`` in Redis, then builds
        the URL to send the player to. Returns ``None`` if the integration is
        disabled, so the caller answers "not configured" instead of producing a
        dead redirect.
        """
        if not self.enabled:
            return None

        state = self.generate_state()
        await self.oauth_state.store(
            state,
            user_id,
            DISCORD_OAUTH_STATE_EXPIRY_SECONDS,
        )
        return discord_oauth.build_authorize_url(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            state=state,
        )

    async def complete_link(self, *, state: str, code: str) -> LinkResult:
        """Finish a link from the OAuth callback's ``state`` and ``code``.

        Consumes the state (single-use), exchanges the code for the player's
        Discord identity, refuses the link if that Discord account already backs
        a different osu! account, and otherwise stores the pairing. Every step
        that can fail maps to a distinct :class:`LinkOutcome`; nothing raises.
        """
        if not self.enabled:
            return LinkResult(LinkOutcome.DISABLED)

        # the state proves this callback answers a flow we started, and tells us
        # *which* account started it. Single-use: a replayed callback finds none.
        user_id = await self.oauth_state.consume(state)
        if user_id is None:
            return LinkResult(LinkOutcome.INVALID_STATE)

        access_token = await self._exchange_code_for_token(code)
        if access_token is None:
            return LinkResult(LinkOutcome.EXCHANGE_FAILED)

        identity = await self._fetch_discord_identity(access_token)
        if identity is None:
            return LinkResult(LinkOutcome.IDENTITY_FAILED)

        # one Discord per osu!: if this Discord already backs a *different*
        # account, refuse rather than silently reassign it.
        existing = await self.links.fetch_by_discord_id(identity.id)
        if existing is not None and existing.user_id != user_id:
            return LinkResult(LinkOutcome.ALREADY_LINKED_ELSEWHERE)

        link = await self.links.upsert_link(
            user_id=user_id,
            discord_id=identity.id,
            discord_username=identity.username,
        )
        return LinkResult(LinkOutcome.LINKED, link=link)

    async def fetch_link(self, user_id: int) -> DiscordLink | None:
        """The Discord link for an osu! account, or None if it has none."""
        return await self.links.fetch_by_user_id(user_id)

    async def unlink(self, user_id: int) -> None:
        """Remove an osu! account's Discord link; a no-op if it had none."""
        await self.links.delete(user_id)

    async def _exchange_code_for_token(self, code: str) -> str | None:
        """Trade an authorization code for an access token, or None on failure.

        Wraps the injected network call so a Discord error (a bad code, an
        outage, a non-JSON body) becomes a clean ``None`` the caller maps to
        ``EXCHANGE_FAILED`` rather than an exception escaping the flow.
        """
        data = discord_oauth.build_token_exchange_data(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            code=code,
        )
        try:
            payload = await self.exchange_code(discord_oauth.DISCORD_TOKEN_URL, data)
        except Exception as exc:
            log(f"Discord OAuth token exchange failed: {exc!r}", Ansi.LYELLOW)
            return None
        return discord_oauth.parse_access_token(payload)

    async def _fetch_discord_identity(
        self,
        access_token: str,
    ) -> DiscordIdentity | None:
        """Fetch the Discord account behind an access token, or None on failure."""
        headers = discord_oauth.build_identity_headers(access_token)
        try:
            payload = await self.fetch_identity(
                discord_oauth.DISCORD_IDENTITY_URL,
                headers,
            )
        except Exception as exc:
            log(f"Discord OAuth identity fetch failed: {exc!r}", Ansi.LYELLOW)
            return None
        return discord_oauth.parse_identity(payload)
