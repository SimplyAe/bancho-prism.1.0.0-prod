"""Guards for the Discord account-linking flow.

``DiscordLinkingService`` is the orchestration behind "connect your Discord": it
mints a CSRF state, trades the callback's code for a Discord identity, and stores
the pairing -- with two invariants it must never break. These tests pin them
against fakes for the repositories and the two injected network steps, so the
whole flow runs with no live HTTP:

- **gating**: with any OAuth credential missing the service is disabled;
  ``begin_link`` hands back ``None`` and ``complete_link`` returns ``DISABLED``
  without touching the network, so a half-configured server never emits a dead
  redirect;
- **begin_link** mints a state, binds it to the requesting account in the state
  store, and puts *that* state (not a fresh one) in the returned authorize URL;
- **the callback links the account that started the flow**: ``complete_link``
  trusts the ``user_id`` behind the consumed state, and every failure step
  (unknown state, failed exchange, failed identity) maps to its own outcome and
  never writes a link;
- **one Discord per osu!**: a Discord id already backing a *different* account is
  refused (``ALREADY_LINKED_ELSEWHERE``), while the same account re-linking the
  same Discord is allowed through to a clean upsert.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.adapters import discord_oauth
from app.repositories.user_discord_links import DiscordLink
from app.services.social.discord_linking import DISCORD_OAUTH_STATE_EXPIRY_SECONDS
from app.services.social.discord_linking import DiscordLinkingService
from app.services.social.discord_linking import LinkOutcome

_LINKED_AT = datetime(2026, 8, 17, 12, 0, 0)


class _FakeLinksRepository:
    """In-memory stand-in mirroring the repo's user_id-keyed store."""

    def __init__(self, *, existing: DiscordLink | None = None) -> None:
        self.rows: list[DiscordLink] = [existing] if existing is not None else []
        self.upserts: list[dict[str, Any]] = []
        self.deletes: list[int] = []

    async def fetch_by_user_id(self, user_id: int) -> DiscordLink | None:
        return next((r for r in self.rows if r.user_id == user_id), None)

    async def fetch_by_discord_id(self, discord_id: str) -> DiscordLink | None:
        return next((r for r in self.rows if r.discord_id == discord_id), None)

    async def upsert_link(
        self,
        *,
        user_id: int,
        discord_id: str,
        discord_username: str,
    ) -> DiscordLink:
        self.upserts.append(
            {
                "user_id": user_id,
                "discord_id": discord_id,
                "discord_username": discord_username,
            },
        )
        link = DiscordLink(
            user_id=user_id,
            discord_id=discord_id,
            discord_username=discord_username,
            linked_at=_LINKED_AT,
        )
        self.rows = [r for r in self.rows if r.user_id != user_id]
        self.rows.append(link)
        return link

    async def delete(self, user_id: int) -> None:
        self.deletes.append(user_id)
        self.rows = [r for r in self.rows if r.user_id != user_id]


class _FakeStateRepository:
    """State store recording writes and honouring single-use consume."""

    def __init__(self) -> None:
        self.stored: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.store_calls: list[tuple[str, int, int]] = []

    async def store(self, state: str, user_id: int, expiry_seconds: int) -> None:
        self.stored[state] = user_id
        self.expiries[state] = expiry_seconds
        self.store_calls.append((state, user_id, expiry_seconds))

    async def consume(self, state: str) -> int | None:
        return self.stored.pop(state, None)


class _RecordingSeam:
    """A stand-in for an injected network step: returns a canned body."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, second: dict[str, str]) -> dict[str, Any]:
        self.calls.append((url, second))
        return self._payload


class _RaisingSeam:
    """An injected network step that fails, to exercise the try/except wrap."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, second: dict[str, str]) -> dict[str, Any]:
        self.calls.append((url, second))
        raise RuntimeError("discord is down")


def _service(
    *,
    client_id: str = "cid",
    client_secret: str = "secret",
    redirect_uri: str = "https://osu.example/cb",
    links: _FakeLinksRepository | None = None,
    state_repo: _FakeStateRepository | None = None,
    exchange: Any = None,
    identity: Any = None,
    state_token: str = "state-xyz",
) -> tuple[DiscordLinkingService, _FakeLinksRepository, _FakeStateRepository]:
    links = links or _FakeLinksRepository()
    state_repo = state_repo or _FakeStateRepository()
    service = DiscordLinkingService(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        links=links,  # type: ignore[arg-type]
        oauth_state=state_repo,  # type: ignore[arg-type]
        exchange_code=exchange or _RecordingSeam({"access_token": "tok"}),
        fetch_identity=identity or _RecordingSeam({"id": "999", "username": "n"}),
        generate_state=lambda: state_token,
    )
    return service, links, state_repo


# --- gating ----------------------------------------------------------------


async def test_disabled_when_a_credential_is_missing() -> None:
    service, _, _ = _service(client_secret="")
    assert service.enabled is False


async def test_begin_link_returns_none_when_disabled() -> None:
    service, _, state_repo = _service(redirect_uri="")

    assert await service.begin_link(6) is None
    # nothing was minted -- a disabled flow leaves no state behind.
    assert state_repo.store_calls == []


async def test_complete_link_is_disabled_without_touching_the_network() -> None:
    exchange = _RecordingSeam({"access_token": "tok"})
    service, links, _ = _service(client_id="", exchange=exchange)

    result = await service.complete_link(state="s", code="c")

    assert result.outcome is LinkOutcome.DISABLED
    assert result.link is None
    assert exchange.calls == []
    assert links.upserts == []


# --- begin_link ------------------------------------------------------------


async def test_begin_link_mints_binds_and_embeds_the_state() -> None:
    service, _, state_repo = _service(state_token="the-state")

    url = await service.begin_link(6)

    # the minted state is bound to the requesting account, with the flow's TTL...
    assert state_repo.stored["the-state"] == 6
    assert state_repo.expiries["the-state"] == DISCORD_OAUTH_STATE_EXPIRY_SECONDS
    # ...and it is *that* state the player is sent to Discord with.
    assert url is not None
    assert url.startswith(discord_oauth.DISCORD_AUTHORIZE_URL)
    assert "state=the-state" in url
    assert "client_id=cid" in url


# --- complete_link happy paths --------------------------------------------


async def test_complete_link_stores_the_pairing_for_the_state_owner() -> None:
    exchange = _RecordingSeam({"access_token": "tok"})
    identity = _RecordingSeam({"id": "42", "username": "gamer"})
    state_repo = _FakeStateRepository()
    await state_repo.store("state-xyz", 6, 600)
    service, links, _ = _service(
        state_repo=state_repo,
        exchange=exchange,
        identity=identity,
    )

    result = await service.complete_link(state="state-xyz", code="the-code")

    assert result.outcome is LinkOutcome.LINKED
    assert result.link is not None
    # linked to the account behind the state (6), with the fetched identity.
    assert result.link.user_id == 6
    assert result.link.discord_id == "42"
    assert result.link.discord_username == "gamer"
    assert links.upserts == [
        {"user_id": 6, "discord_id": "42", "discord_username": "gamer"},
    ]
    # the exchange used the callback's code and the token drove the identity call.
    assert exchange.calls[0][0] == discord_oauth.DISCORD_TOKEN_URL
    assert exchange.calls[0][1]["code"] == "the-code"
    assert identity.calls[0] == (
        discord_oauth.DISCORD_IDENTITY_URL,
        {"Authorization": "Bearer tok"},
    )


async def test_complete_link_allows_the_same_account_to_relink() -> None:
    existing = DiscordLink(
        user_id=6,
        discord_id="42",
        discord_username="old",
        linked_at=_LINKED_AT,
    )
    links = _FakeLinksRepository(existing=existing)
    state_repo = _FakeStateRepository()
    await state_repo.store("state-xyz", 6, 600)
    service, links, _ = _service(
        links=links,
        state_repo=state_repo,
        identity=_RecordingSeam({"id": "42", "username": "new"}),
    )

    result = await service.complete_link(state="state-xyz", code="c")

    # same osu! account, same Discord id -> not a conflict, a clean replace.
    assert result.outcome is LinkOutcome.LINKED
    assert links.upserts == [
        {"user_id": 6, "discord_id": "42", "discord_username": "new"},
    ]


# --- complete_link failures ------------------------------------------------


async def test_complete_link_rejects_an_unknown_state() -> None:
    exchange = _RecordingSeam({"access_token": "tok"})
    service, links, _ = _service(exchange=exchange)  # nothing stored

    result = await service.complete_link(state="never-issued", code="c")

    assert result.outcome is LinkOutcome.INVALID_STATE
    # a forged/stale callback never reaches Discord or the DB.
    assert exchange.calls == []
    assert links.upserts == []


async def test_complete_link_maps_a_failed_exchange() -> None:
    state_repo = _FakeStateRepository()
    await state_repo.store("state-xyz", 6, 600)
    # a Discord error body has no access_token -> parse returns None.
    service, links, _ = _service(
        state_repo=state_repo,
        exchange=_RecordingSeam({"error": "invalid_grant"}),
    )

    result = await service.complete_link(state="state-xyz", code="c")

    assert result.outcome is LinkOutcome.EXCHANGE_FAILED
    assert links.upserts == []


async def test_complete_link_maps_a_raised_exchange_to_a_clean_failure() -> None:
    state_repo = _FakeStateRepository()
    await state_repo.store("state-xyz", 6, 600)
    service, _, _ = _service(state_repo=state_repo, exchange=_RaisingSeam())

    result = await service.complete_link(state="state-xyz", code="c")

    # an outage is wrapped, not propagated: still a typed EXCHANGE_FAILED.
    assert result.outcome is LinkOutcome.EXCHANGE_FAILED


async def test_complete_link_maps_a_failed_identity() -> None:
    state_repo = _FakeStateRepository()
    await state_repo.store("state-xyz", 6, 600)
    service, links, _ = _service(
        state_repo=state_repo,
        identity=_RecordingSeam({"error": "unauthorized"}),
    )

    result = await service.complete_link(state="state-xyz", code="c")

    assert result.outcome is LinkOutcome.IDENTITY_FAILED
    assert links.upserts == []


async def test_complete_link_refuses_a_discord_linked_elsewhere() -> None:
    # the Discord account (id 42) already backs a *different* osu! account (7).
    existing = DiscordLink(
        user_id=7,
        discord_id="42",
        discord_username="owner",
        linked_at=_LINKED_AT,
    )
    links = _FakeLinksRepository(existing=existing)
    state_repo = _FakeStateRepository()
    await state_repo.store("state-xyz", 6, 600)
    service, links, _ = _service(
        links=links,
        state_repo=state_repo,
        identity=_RecordingSeam({"id": "42", "username": "thief"}),
    )

    result = await service.complete_link(state="state-xyz", code="c")

    assert result.outcome is LinkOutcome.ALREADY_LINKED_ELSEWHERE
    # the existing link is left untouched -- not stolen.
    assert links.upserts == []
    leftover = await links.fetch_by_discord_id("42")
    assert leftover is not None
    assert leftover.user_id == 7


# --- fetch / unlink --------------------------------------------------------


async def test_fetch_link_reads_the_current_link() -> None:
    existing = DiscordLink(
        user_id=6,
        discord_id="42",
        discord_username="me",
        linked_at=_LINKED_AT,
    )
    service, _, _ = _service(links=_FakeLinksRepository(existing=existing))

    assert await service.fetch_link(6) == existing
    assert await service.fetch_link(999) is None


async def test_unlink_delegates_to_the_repository() -> None:
    links = _FakeLinksRepository()
    service, links, _ = _service(links=links)

    await service.unlink(6)

    assert links.deletes == [6]
