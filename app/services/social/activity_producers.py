"""Turning the events of the hot path into activity-feed entries.

The activity feed is written *from* things that already happen during play --
chiefly score submission -- but the code that owns those moments
(``app.services.score_submission``) must not grow a dependency on the feed, and
must never pay for a feed write on its latency-sensitive path. So the coupling
runs one way and off-thread:

- ``ScoreSubmissionService`` exposes a bare ``publish_activity_events`` hook
  (a ``Callable`` defaulting to a no-op), called once with the finished
  ``SubmittedScore``. The submission service imports nothing from here.
- the DI layer wires that hook to spawn a *supervised background task* that
  calls :func:`publish_score_submission_activity`, so a slow or failing feed
  write is logged and dropped, never surfaced to the player mid-submit.

This module is that background function. It reads a ``SubmittedScore`` and
publishes the events that submission unambiguously implies, with no extra
queries on the (already off-thread) path:

- **first place** -- the score took #1 on a ranked leaderboard (the exact
  condition the in-game #1 announce uses);
- **achievement** -- one entry per achievement the score unlocked.

Rank and pp-record events are deliberately *not* derived here: a meaningful
"new peak" needs a day-over-day comparison, which the daily snapshot capture
already holds, so those producers live alongside that capture rather than
firing on every best score. A dropped feed write is not a correctness problem
-- the feed is a convenience log, and the durable ``scores``/``stats`` rows
remain the source of truth.
"""

from __future__ import annotations

from app.logging import Ansi
from app.logging import log
from app.services.score_submission import SubmittedScore
from app.services.score_submission import score_should_announce_first_place
from app.services.social.activity_feed import ActivityFeedService


async def publish_score_submission_activity(
    feed_service: ActivityFeedService,
    submitted: SubmittedScore,
) -> None:
    """Publish the feed events a submitted score implies.

    Intended to run as a fire-and-forget background task: it never raises into
    the caller (any failure is logged and swallowed), because a feed write must
    not be able to affect a score that has already been durably committed.
    """
    try:
        await _publish_score_submission_activity(feed_service, submitted)
    except Exception as exc:  # never let a feed write escape into the hot path.
        log(f"Failed to publish activity for score {submitted.score_id}: {exc!r}", Ansi.LYELLOW)


async def _publish_score_submission_activity(
    feed_service: ActivityFeedService,
    submitted: SubmittedScore,
) -> None:
    score = submitted.score
    player = score.player
    assert player is not None
    assert score.bmap is not None

    # a restricted player's play is hidden everywhere else; it must not leak into
    # anyone's feed either. (Achievements already exclude restricted players
    # upstream, but this keeps the whole producer honest in one place.)
    if player.restricted:
        return

    mode = score.mode.value

    if score_should_announce_first_place(score):
        await feed_service.publish_first_place(
            user_id=player.id,
            mode=mode,
            score_id=submitted.score_id,
            beatmap_id=score.bmap.id,
            beatmap_title=score.bmap.title,
            pp=score.pp,
        )

    for achievement in submitted.unlocked_achievements:
        await feed_service.publish_achievement(
            user_id=player.id,
            mode=mode,
            achievement_id=achievement.id,
            achievement_name=achievement.name,
        )
