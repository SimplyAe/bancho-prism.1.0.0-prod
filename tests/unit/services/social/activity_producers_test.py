"""Guards for the score-submission -> activity-feed producer.

``publish_score_submission_activity`` is the fire-and-forget bridge that turns a
finished ``SubmittedScore`` into feed events. It runs as a background task off
the submission hot path, so these tests pin the two things that matter there:

- it publishes exactly the events the submission implies -- a #1 on a ranked
  leaderboard becomes one ``first_place`` event, and each unlocked achievement
  becomes one ``achievement`` event -- attributed to the player, in the score's
  mode, with the ids/titles a renderer needs;
- it is inert where it must be: a non-#1 or unranked score publishes no
  first-place event, a restricted player publishes *nothing* (their play is
  hidden everywhere else), and -- crucially -- a feed service that raises never
  propagates, because a feed write must not be able to disturb a score that is
  already durably committed.

The feed service is faked to record publish calls (and optionally to blow up);
the score/achievements are hand-built ``SimpleNamespace`` stand-ins, matching the
submission service's own tests. No database, no event loop scheduling.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.constants.gamemodes import GameMode
from app.constants.score_statuses import SubmissionStatus
from app.services.social import activity_producers


class _FakeFeedService:
    """Records publish_* calls; can be told to raise to prove isolation."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.calls: list[SimpleNamespace] = []
        self._raise = raise_on_publish

    async def publish_first_place(self, **kwargs: Any) -> None:
        self.calls.append(SimpleNamespace(method="first_place", **kwargs))
        if self._raise:
            raise RuntimeError("redis is down")

    async def publish_achievement(self, **kwargs: Any) -> None:
        self.calls.append(SimpleNamespace(method="achievement", **kwargs))
        if self._raise:
            raise RuntimeError("redis is down")


def _score(
    *,
    rank: int = 1,
    status: SubmissionStatus = SubmissionStatus.BEST,
    has_leaderboard: bool = True,
    restricted: bool = False,
) -> Any:
    return SimpleNamespace(
        mode=GameMode.VANILLA_OSU,
        status=status,
        rank=rank,
        pp=727.0,
        player=SimpleNamespace(id=6, restricted=restricted),
        bmap=SimpleNamespace(id=315, title="test map", has_leaderboard=has_leaderboard),
    )


def _submitted(score: Any, *, score_id: int = 42, achievements: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        score=score,
        score_id=score_id,
        previous_stats=SimpleNamespace(),  # unread by the producer
        current_stats=SimpleNamespace(),  # unread by the producer
        unlocked_achievements=achievements or [],
    )


async def test_first_place_on_a_ranked_map_publishes_one_first_place_event() -> None:
    feed = _FakeFeedService()
    submitted = _submitted(_score(rank=1), score_id=42)

    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    assert len(feed.calls) == 1
    call = feed.calls[0]
    assert call.method == "first_place"
    assert call.user_id == 6
    assert call.mode == GameMode.VANILLA_OSU.value
    assert call.score_id == 42
    assert call.beatmap_id == 315
    assert call.beatmap_title == "test map"
    assert call.pp == 727.0


async def test_a_non_first_place_score_publishes_no_first_place_event() -> None:
    feed = _FakeFeedService()
    submitted = _submitted(_score(rank=2))

    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    assert feed.calls == []


async def test_a_first_place_on_an_unranked_map_publishes_nothing() -> None:
    feed = _FakeFeedService()
    submitted = _submitted(_score(rank=1, has_leaderboard=False))

    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    assert feed.calls == []


async def test_a_non_best_status_publishes_no_first_place_event() -> None:
    feed = _FakeFeedService()
    submitted = _submitted(_score(rank=1, status=SubmissionStatus.SUBMITTED))

    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    assert feed.calls == []


async def test_each_unlocked_achievement_publishes_one_event() -> None:
    feed = _FakeFeedService()
    achievements = [
        SimpleNamespace(id=1, name="500 Combo"),
        SimpleNamespace(id=2, name="Perfectionist"),
    ]
    # a non-#1 score so only the achievement events are published.
    submitted = _submitted(_score(rank=5), achievements=achievements)

    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    assert [c.method for c in feed.calls] == ["achievement", "achievement"]
    assert [(c.achievement_id, c.achievement_name) for c in feed.calls] == [
        (1, "500 Combo"),
        (2, "Perfectionist"),
    ]
    assert all(c.user_id == 6 for c in feed.calls)


async def test_first_place_and_achievements_are_both_published() -> None:
    feed = _FakeFeedService()
    submitted = _submitted(
        _score(rank=1),
        achievements=[SimpleNamespace(id=1, name="500 Combo")],
    )

    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    assert {c.method for c in feed.calls} == {"first_place", "achievement"}


async def test_a_restricted_player_publishes_nothing() -> None:
    feed = _FakeFeedService()
    # a #1 with an achievement, but the player is restricted -> hidden entirely.
    submitted = _submitted(
        _score(rank=1, restricted=True),
        achievements=[SimpleNamespace(id=1, name="500 Combo")],
    )

    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    assert feed.calls == []


async def test_a_failing_feed_write_never_propagates() -> None:
    # the feed raises on the first publish; the committed score must not care.
    feed = _FakeFeedService(raise_on_publish=True)
    submitted = _submitted(_score(rank=1))

    # no exception escapes -- the producer swallows and logs it.
    await activity_producers.publish_score_submission_activity(feed, submitted)  # type: ignore[arg-type]

    # it did try (so the swallow is real, not an early return).
    assert len(feed.calls) == 1
