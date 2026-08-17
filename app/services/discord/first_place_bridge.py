"""Mirroring in-game #1 scores to a Discord webhook.

When a play takes #1 on a ranked leaderboard the server already shouts it in
``#announce`` (see ``announce_first_place`` in ``app.services.score_submission``).
This module posts the same moment to a Discord channel, so a server's Discord
gets a live "new #1" feed without anyone watching in-game.

The coupling runs one way and off the hot path, exactly like the activity feed:

- ``ScoreSubmissionService`` exposes a bare ``announce_first_place_to_discord``
  hook (a ``Callable`` defaulting to a no-op), called on the finalize path right
  beside the in-game announce. The submission service imports nothing from here.
- the DI layer wires that hook to spawn a *supervised background task* that
  calls :func:`announce_first_place_to_discord`, so a slow or failing webhook
  post is logged and dropped -- never awaited while a player waits on submit.

The webhook POST itself is injected as a ``Callable`` so this module needs no
live HTTP client to build and reason about the embed.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable

import app.settings
from app.discord import Embed
from app.logging import Ansi
from app.logging import log
from app.objects.score import Score
from app.repositories.scores import FirstPlaceScore
from app.services.score_submission import format_score_submission_performance
from app.services.score_submission import score_should_announce_first_place

# posts a finished embed to the configured webhook. Injected so the producer
# carries no dependency on a live HTTP client (or even the webhook URL).
EmbedPoster = Callable[[Embed], Awaitable[None]]

# gold, matching the "#1" celebration tone.
_FIRST_PLACE_COLOR = 0xFFD700


def _beatmapset_cover_url(set_id: int) -> str:
    """The osu!-CDN banner for a set -- the same asset web front-ends use.

    If the set isn't on the CDN Discord simply renders no image; a broken
    thumbnail never costs us the announcement.
    """
    return f"https://assets.ppy.sh/beatmaps/{set_id}/covers/cover.jpg"


def build_first_place_embed(
    score: Score,
    *,
    previous_first_place_score: FirstPlaceScore | None,
) -> Embed:
    """Render a #1 score as a Discord embed.

    Mirrors the in-game announce: map, accuracy, mods, performance, and the
    previous #1 holder when it was somebody else.
    """
    assert score.bmap is not None
    assert score.player is not None

    performance = format_score_submission_performance(score)
    mods = f" +{score.mods!r}" if score.mods else ""

    description = (
        f"**{score.player.name}** just took **#1** on "
        f"[{score.bmap.full_name}]({score.bmap.url}) "
        f"with **{score.acc:.2f}%**{mods} for **{performance}**."
    )

    # only worth naming a predecessor if it was a different player -- otherwise
    # it's just the same person beating their own top score.
    if (
        previous_first_place_score is not None
        and previous_first_place_score.id != score.player.id
    ):
        description += (
            f"\nPrevious #1: [{previous_first_place_score.name}]"
            f"(https://{app.settings.DOMAIN}/u/{previous_first_place_score.id})"
        )

    embed = Embed(
        title=score.bmap.full_name,
        url=score.bmap.url,
        description=description,
        color=_FIRST_PLACE_COLOR,
    )
    embed.set_author(
        name=score.player.name,
        url=score.player.url,
        icon_url=score.player.avatar_url,
    )
    embed.set_thumbnail(url=_beatmapset_cover_url(score.bmap.set_id))
    return embed


async def announce_first_place_to_discord(
    post_embed: EmbedPoster,
    score: Score,
    *,
    previous_first_place_score: FirstPlaceScore | None,
) -> None:
    """Post a #1 score to Discord, if this score is really a #1.

    Intended to run as a fire-and-forget background task: it never raises into
    the caller (any failure is logged and swallowed), because a webhook post
    must not be able to affect a score that has already been durably committed.
    """
    try:
        await _announce_first_place_to_discord(
            post_embed,
            score,
            previous_first_place_score=previous_first_place_score,
        )
    except Exception as exc:  # never let a webhook post escape into the hot path.
        log(f"Failed to post #1 score to Discord: {exc!r}", Ansi.LYELLOW)


async def _announce_first_place_to_discord(
    post_embed: EmbedPoster,
    score: Score,
    *,
    previous_first_place_score: FirstPlaceScore | None,
) -> None:
    # re-check the full #1 condition here (not just BEST): being a personal best
    # doesn't mean the play took the top of the board, and a restricted player's
    # #1 must never leak to Discord. This is the same gate the in-game announce
    # applies, so the two feeds agree on exactly what counts as a #1.
    if not score_should_announce_first_place(score):
        return

    embed = build_first_place_embed(
        score,
        previous_first_place_score=previous_first_place_score,
    )
    await post_embed(embed)
