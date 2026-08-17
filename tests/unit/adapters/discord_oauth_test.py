"""Guards for the pure Discord OAuth building blocks.

Everything in ``app.adapters.discord_oauth`` is deterministic and does no I/O, so
these tests need nothing faked. They pin the two properties the flow leans on:

- the authorize URL and token-exchange body carry exactly the fields Discord
  requires (a missing ``redirect_uri`` or ``response_type`` silently breaks the
  real flow, so the shapes are asserted here where it is cheap);
- the parsers are forgiving in one direction only -- a missing or wrong-typed
  field returns ``None`` rather than raising, which is what turns a Discord error
  body into a clean "linking failed" instead of a ``KeyError`` in the service.
"""

from __future__ import annotations

from urllib.parse import parse_qs
from urllib.parse import urlsplit

from app.adapters.discord_oauth import DISCORD_AUTHORIZE_URL
from app.adapters.discord_oauth import DiscordIdentity
from app.adapters.discord_oauth import build_authorize_url
from app.adapters.discord_oauth import build_identity_headers
from app.adapters.discord_oauth import build_token_exchange_data
from app.adapters.discord_oauth import parse_access_token
from app.adapters.discord_oauth import parse_identity


def test_authorize_url_targets_discord_and_carries_the_flow_fields() -> None:
    url = build_authorize_url(
        client_id="my-client",
        redirect_uri="https://osu.example/v2/oauth/discord/callback",
        state="csrf-token",
    )

    split = urlsplit(url)
    assert f"{split.scheme}://{split.netloc}{split.path}" == DISCORD_AUTHORIZE_URL

    query = parse_qs(split.query)
    assert query["client_id"] == ["my-client"]
    assert query["redirect_uri"] == ["https://osu.example/v2/oauth/discord/callback"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["csrf-token"]
    # least-privilege default scope.
    assert query["scope"] == ["identify"]


def test_authorize_url_percent_encodes_the_redirect() -> None:
    url = build_authorize_url(
        client_id="c",
        redirect_uri="https://osu.example/cb?x=1",
        state="s",
    )

    # the raw ':' '/' '?' '=' of the redirect must not leak into the outer query
    # unescaped, or a client would parse a truncated redirect_uri.
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == ["https://osu.example/cb?x=1"]


def test_token_exchange_body_is_the_authorization_code_grant() -> None:
    data = build_token_exchange_data(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://osu.example/cb",
        code="the-code",
    )

    assert data == {
        "client_id": "cid",
        "client_secret": "secret",
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "https://osu.example/cb",
    }


def test_identity_headers_are_bearer_auth() -> None:
    assert build_identity_headers("abc123") == {"Authorization": "Bearer abc123"}


def test_parse_access_token_returns_the_token() -> None:
    assert parse_access_token({"access_token": "tok", "token_type": "Bearer"}) == "tok"


def test_parse_access_token_rejects_missing_or_empty_or_wrong_type() -> None:
    # a Discord error body has no access_token at all.
    assert parse_access_token({"error": "invalid_grant"}) is None
    assert parse_access_token({"access_token": ""}) is None
    assert parse_access_token({"access_token": None}) is None
    assert parse_access_token({"access_token": 12345}) is None


def test_parse_identity_reads_id_and_username() -> None:
    identity = parse_identity({"id": "123", "username": "coolguy"})
    assert identity == DiscordIdentity(id="123", username="coolguy")


def test_parse_identity_coerces_a_numeric_snowflake_to_string() -> None:
    # Discord may render the snowflake as a JSON number; we store it as a string.
    identity = parse_identity({"id": 123456789012345678, "username": "n"})
    assert identity is not None
    assert identity.id == "123456789012345678"
    assert isinstance(identity.id, str)


def test_parse_identity_rejects_missing_id_or_username() -> None:
    assert parse_identity({"username": "no-id"}) is None
    assert parse_identity({"id": "123"}) is None
    assert parse_identity({"id": "123", "username": ""}) is None
    assert parse_identity({"id": "123", "username": 42}) is None
    assert parse_identity({"error": "unauthorized"}) is None
