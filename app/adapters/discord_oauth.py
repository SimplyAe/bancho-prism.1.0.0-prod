"""Pure building blocks for the Discord OAuth2 account-linking flow.

Everything here is deterministic and does no I/O: URL construction and response
parsing only. The two network steps of the flow (exchanging the ``code`` for an
access token, and fetching the account identity with it) are injected into
``app.services.social.discord_linking.DiscordLinkingService`` as callables, so
the service stays unit-testable with no live HTTP -- exactly as ``CaptchaService``
injects its ``post_siteverify``. This module is what those callables target and
what the service parses their results with.

The parsers are intentionally forgiving in one direction only: they return
``None`` when a required field is missing or the wrong shape, so a malformed or
error response from Discord becomes a clean "linking failed" rather than a
``KeyError`` deep in the flow. They never guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

# Discord's OAuth2 + API endpoints. The authorize URL is where the player is sent
# to approve the link; the token URL trades the returned code for an access
# token; the identity URL ("@me") returns the account behind that token.
DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_IDENTITY_URL = "https://discord.com/api/users/@me"

# "identify" is the least-privilege scope that still returns id + username; we do
# not ask for email, guilds, or anything we would not store.
DEFAULT_SCOPE = "identify"


@dataclass(frozen=True, slots=True)
class DiscordIdentity:
    """The slice of a Discord account we keep: its snowflake id and username.

    ``id`` is Discord's snowflake, handled as a string (it is a 64-bit value we
    only ever compare and store, never do arithmetic on). ``username`` is the
    account's unique handle under Discord's current username system.
    """

    id: str
    username: str


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str = DEFAULT_SCOPE,
) -> str:
    """Build the URL the player is redirected to in order to approve the link.

    ``state`` is the CSRF token bound to the requesting account (see
    ``app.repositories.discord_oauth_state``); Discord echoes it back on the
    callback so we can prove the callback answers a flow we started.
    """
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
        },
    )
    return f"{DISCORD_AUTHORIZE_URL}?{query}"


def build_token_exchange_data(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> dict[str, str]:
    """The form body that trades an authorization ``code`` for an access token.

    ``redirect_uri`` must match the one used in the authorize step; Discord
    rejects the exchange otherwise. Kept here (not in the service) so the exact
    field set is a pure, testable value.
    """
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }


def build_identity_headers(access_token: str) -> dict[str, str]:
    """The bearer-auth header for the ``/users/@me`` identity request."""
    return {"Authorization": f"Bearer {access_token}"}


def parse_access_token(payload: dict[str, Any]) -> str | None:
    """Pull the access token out of a token-exchange response.

    Returns ``None`` if the field is absent or not a non-empty string, which is
    how an error response (``{"error": "invalid_grant"}``) turns into a failed
    link instead of an exception.
    """
    token = payload.get("access_token")
    if isinstance(token, str) and token:
        return token
    return None


def parse_identity(payload: dict[str, Any]) -> DiscordIdentity | None:
    """Parse a ``/users/@me`` response into a ``DiscordIdentity``.

    Both the id and the username must be present and non-empty; anything else
    (an error body, a partial response) returns ``None``. The id is coerced to a
    string because Discord may render a snowflake as a JSON number.
    """
    raw_id = payload.get("id")
    username = payload.get("username")
    if raw_id is None or not isinstance(username, str) or not username:
        return None
    discord_id = str(raw_id)
    if not discord_id:
        return None
    return DiscordIdentity(id=discord_id, username=username)
