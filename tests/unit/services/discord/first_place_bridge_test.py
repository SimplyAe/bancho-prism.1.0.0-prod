"""Guards for the #1-score -> Discord bridge.

The bridge is the fire-and-forget mirror of the in-game #1 announce: when a play
takes the top of a ranked leaderboard it posts an embed to a Discord webhook. It
runs as a background task off the submission hot path, so these tests pin the two
things that matter there:

- the embed it *builds* carries what a reader needs -- map link, accuracy, mods,
  performance, the player's profile/avatar, and the previous #1 holder when it
  was somebody else (and never when it was the same player beating themselves);
- the producer is inert where it must be -- a non-#1, unranked, non-best, or
  restricted play posts nothing (the exact gate the in-game announce uses) -- and
  a webhook that raises never propagates, because a Discord post must not be able
  to disturb a score that is already durably committed.

The embed builder is pure, so its tests call it directly. The webhook POST is an
injected callable, faked to record embeds (and optionally blow up); the score is
a hand-built ``SimpleNamespace``, matching the submission service's own tests. No
database, no live HTTP, no event-loop scheduling.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import app.settings
from app.constants.beatmap_statuses import RankedStatus
from app.constants.gamemodes import GameMode
from app.constants.mods import Mods
from app.constants.score_statuses import SubmissionStatus
from app.discord import Embed
from app.repositories.scores import FirstPlaceScore
from app.services.discord import first_place_bridge


class _FakePoster:
    """Records the embeds it's handed; can be told to raise to prove isolation."""

    def __init__(self, *, raise_on_post: bool = False) -> None:
        self.embeds: list[Embed] = []
        self._raise = raise_on_post

    async def __call__(self, embed: Embed) -> None:
        self.embeds.append(embed)
        if self._raise:
            raise RuntimeError("discord returned 500")


def _score(
    *,
    rank: int = 1,
    status: SubmissionStatus = SubmissionStatus.BEST,
    has_leaderboard: bool = True,
    restricted: bool = False,
    mods: Mods = Mods.NOMOD,
) -> Any:
    return SimpleNamespace(
        mode=GameMode.VANILLA_OSU,
        status=status,
        rank=rank,
        pp=727.0,
        acc=98.76,
        score=1_000_000,
        mods=mods,
        player=SimpleNamespace(
            id=6,
            name="mrekk",
            restricted=restricted,
            url="https://osu.example.com/u/6",
            avatar_url="https://a.example.com/6",
        ),
        bmap=SimpleNamespace(
            id=315,
            set_id=99,
            status=RankedStatus.Ranked,
            has_leaderboard=has_leaderboard,
            full_name="Artist - Title [Diff]",
            url="https://osu.example.com/b/315",
        ),
    )


# --- the embed (pure builder) ------------------------------------------------


def test_embed_renders_the_score_and_map() -> None:
    embed = first_place_bridge.build_first_place_embed(
        _score(),
        previous_first_place_score=None,
    )

    assert embed.title == "Artist - Title [Diff]"
    assert embed.url == "https://osu.example.com/b/315"
    assert embed.color == 0xFFD700

    assert embed.description is not None
    assert "**mrekk**" in embed.description
    assert "**#1**" in embed.description
    assert "[Artist - Title [Diff]](https://osu.example.com/b/315)" in embed.description
    assert "98.76%" in embed.description
    assert "727.00pp" in embed.description


def test_embed_attributes_the_player_and_cover() -> None:
    embed = first_place_bridge.build_first_place_embed(
        _score(),
        previous_first_place_score=None,
    )

    assert embed.author is not None
    assert embed.author.name == "mrekk"
    assert embed.author.url == "https://osu.example.com/u/6"
    assert embed.author.icon_url == "https://a.example.com/6"

    assert embed.thumbnail is not None
    assert embed.thumbnail.url == "https://assets.ppy.sh/beatmaps/99/covers/cover.jpg"


def test_embed_shows_mods_when_present() -> None:
    mods = Mods.HIDDEN | Mods.HARDROCK
    embed = first_place_bridge.build_first_place_embed(
        _score(mods=mods),
        previous_first_place_score=None,
    )

    assert embed.description is not None
    assert f"+{mods!r}" in embed.description


def test_embed_omits_mods_when_nomod() -> None:
    embed = first_place_bridge.build_first_place_embed(
        _score(mods=Mods.NOMOD),
        previous_first_place_score=None,
    )

    assert embed.description is not None
    assert "+" not in embed.description


def test_embed_names_a_different_previous_holder() -> None:
    embed = first_place_bridge.build_first_place_embed(
        _score(),
        previous_first_place_score=FirstPlaceScore(id=9, name="vaxei"),
    )

    assert embed.description is not None
    assert "Previous #1" in embed.description
    assert "vaxei" in embed.description
    assert f"https://{app.settings.DOMAIN}/u/9" in embed.description


def test_embed_skips_a_self_replacing_previous_holder() -> None:
    # the same player beating their own top score is not a "previous #1" worth
    # naming -- it's just them again.
    embed = first_place_bridge.build_first_place_embed(
        _score(),
        previous_first_place_score=FirstPlaceScore(id=6, name="mrekk"),
    )

    assert embed.description is not None
    assert "Previous #1" not in embed.description


# --- the producer (gating + isolation) ---------------------------------------


async def test_a_real_first_place_posts_one_embed() -> None:
    poster = _FakePoster()

    await first_place_bridge.announce_first_place_to_discord(
        poster,
        _score(rank=1),
        previous_first_place_score=None,
    )

    assert len(poster.embeds) == 1
    assert poster.embeds[0].title == "Artist - Title [Diff]"


async def test_a_non_first_place_score_posts_nothing() -> None:
    poster = _FakePoster()

    await first_place_bridge.announce_first_place_to_discord(
        poster,
        _score(rank=2),
        previous_first_place_score=None,
    )

    assert poster.embeds == []


async def test_a_first_place_on_an_unranked_map_posts_nothing() -> None:
    poster = _FakePoster()

    await first_place_bridge.announce_first_place_to_discord(
        poster,
        _score(rank=1, has_leaderboard=False),
        previous_first_place_score=None,
    )

    assert poster.embeds == []


async def test_a_non_best_status_posts_nothing() -> None:
    poster = _FakePoster()

    await first_place_bridge.announce_first_place_to_discord(
        poster,
        _score(rank=1, status=SubmissionStatus.SUBMITTED),
        previous_first_place_score=None,
    )

    assert poster.embeds == []


async def test_a_restricted_player_posts_nothing() -> None:
    poster = _FakePoster()

    await first_place_bridge.announce_first_place_to_discord(
        poster,
        _score(rank=1, restricted=True),
        previous_first_place_score=None,
    )

    assert poster.embeds == []


async def test_a_failing_post_never_propagates() -> None:
    # the webhook blows up; the committed score must not care.
    poster = _FakePoster(raise_on_post=True)

    # no exception escapes -- the producer swallows and logs it.
    await first_place_bridge.announce_first_place_to_discord(
        poster,
        _score(rank=1),
        previous_first_place_score=None,
    )

    # it did try (so the swallow is real, not an early return).
    assert len(poster.embeds) == 1


def test_first_place_webhook_defaults_to_disabled() -> None:
    # the feed is opt-in: absent config leaves the setting empty, and the DI
    # wiring treats empty as "don't post".
    assert app.settings.DISCORD_FIRST_PLACE_WEBHOOK == ""
