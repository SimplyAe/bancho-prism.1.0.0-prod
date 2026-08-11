"""Guards for the activity-feed service seam.

``ActivityFeedService`` is where the opaque ``activity_events`` log acquires
meaning. The repository stores ``(user_id, event_type, mode, data)`` and treats
``event_type``/``data`` as opaque; this service owns the closed
:class:`ActivityEventType` vocabulary and builds each event's ``data`` payload,
so these tests pin:

- each ``publish_*`` method records its event under the right type slug, with
  the right ``mode``, and with a payload that contains exactly the fields a
  renderer needs (a caller cannot, say, publish a pp record without both the old
  and the new pp);
- ``fetch_user_feed`` is a straight pass-through to the per-player read, keyset
  args intact;
- ``fetch_friends_feed`` resolves the viewer's ``FRIEND`` edges to their target
  ids and fans out over them -- excluding the viewer's own events and anyone they
  have not friended -- returns an empty feed (without a read) when the viewer has
  no friends, and caps the number of feed sources so a huge friend list cannot
  blow up the fan-out.

Both repositories are faked: the events fake stores rows and answers the two
reads by the same id/keyset rules the real repository uses, so the service's
own wiring -- not MySQL -- decides every result.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from app.repositories.activity_events import ActivityEvent
from app.repositories.relationships import Relationship
from app.repositories.relationships import RelationshipType
from app.services.social.activity_feed import _MAX_FEED_SOURCES
from app.services.social.activity_feed import ActivityEventType
from app.services.social.activity_feed import ActivityFeedService

_FIXED_TIME = datetime(2026, 8, 12, 12, 0, 0)


class _FakeEventsRepository:
    """Stores appended events and answers the two feed reads.

    ``record`` assigns increasing ids and keeps the row; ``fetch_for_user`` and
    ``fetch_feed`` apply the same newest-first, ``id < before_id`` keyset rules
    the real repository does, so a fan-out test sees a realistic merge.
    """

    def __init__(self) -> None:
        self.events: list[ActivityEvent] = []
        self._next_id = 1
        self.feed_calls: list[SimpleNamespace] = []

    async def record(
        self,
        *,
        user_id: int,
        event_type: str,
        mode: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> ActivityEvent:
        event = ActivityEvent(
            id=self._next_id,
            user_id=user_id,
            event_type=event_type,
            mode=mode,
            data=data,
            created_at=_FIXED_TIME,
        )
        self.events.append(event)
        self._next_id += 1
        return event

    async def fetch_for_user(
        self,
        user_id: int,
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        matched = [e for e in self.events if e.user_id == user_id]
        return self._page(matched, before_id=before_id, limit=limit)

    async def fetch_feed(
        self,
        user_ids: list[int],
        *,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[ActivityEvent]:
        self.feed_calls.append(
            SimpleNamespace(user_ids=user_ids, before_id=before_id, limit=limit),
        )
        if not user_ids:
            return []
        matched = [e for e in self.events if e.user_id in user_ids]
        return self._page(matched, before_id=before_id, limit=limit)

    @staticmethod
    def _page(
        rows: list[ActivityEvent],
        *,
        before_id: int | None,
        limit: int,
    ) -> list[ActivityEvent]:
        if before_id is not None:
            rows = [e for e in rows if e.id < before_id]
        rows = sorted(rows, key=lambda e: e.id, reverse=True)  # newest first
        return rows[:limit]


class _FakeRelationshipsRepository:
    """Returns canned FRIEND edges and records how it was queried."""

    def __init__(self, friend_ids: list[int]) -> None:
        self._friend_ids = friend_ids
        self.calls: list[SimpleNamespace] = []

    async def fetch_all(
        self,
        user1: int,
        type: RelationshipType | None = None,
    ) -> list[Relationship]:
        self.calls.append(SimpleNamespace(user1=user1, type=type))
        return [
            Relationship(user1=user1, user2=friend_id, type=RelationshipType.FRIEND)
            for friend_id in self._friend_ids
        ]


def _service(
    events: _FakeEventsRepository | None = None,
    relationships: _FakeRelationshipsRepository | None = None,
) -> ActivityFeedService:
    return ActivityFeedService(
        events=events if events is not None else _FakeEventsRepository(),  # type: ignore[arg-type]
        relationships=(  # type: ignore[arg-type]
            relationships
            if relationships is not None
            else _FakeRelationshipsRepository([])
        ),
    )


# --- publishing: each method builds the right typed payload -----------------


async def test_publish_rank_up_records_the_rank_transition() -> None:
    events = _FakeEventsRepository()

    event = await _service(events).publish_rank_up(
        user_id=7,
        mode=0,
        old_rank=None,
        new_rank=12,
    )

    assert event.event_type == ActivityEventType.RANK_UP.value == "rank_up"
    assert event.user_id == 7
    assert event.mode == 0
    # `None` old_rank is preserved (first time ranked), not dropped.
    assert event.data == {"old_rank": None, "new_rank": 12}


async def test_publish_pp_record_records_both_pp_values() -> None:
    events = _FakeEventsRepository()

    event = await _service(events).publish_pp_record(
        user_id=7,
        mode=3,
        old_pp=4000,
        new_pp=4200,
    )

    assert event.event_type == "pp_record"
    assert event.mode == 3
    assert event.data == {"old_pp": 4000, "new_pp": 4200}


async def test_publish_first_place_records_the_beatmap_and_score() -> None:
    events = _FakeEventsRepository()

    event = await _service(events).publish_first_place(
        user_id=7,
        mode=0,
        score_id=555,
        beatmap_id=99,
        beatmap_title="Freedom Dive",
        pp=727.0,
    )

    assert event.event_type == "first_place"
    assert event.data == {
        "score_id": 555,
        "beatmap_id": 99,
        "beatmap_title": "Freedom Dive",
        "pp": 727.0,
    }


async def test_publish_achievement_records_the_unlock() -> None:
    events = _FakeEventsRepository()

    event = await _service(events).publish_achievement(
        user_id=7,
        mode=0,
        achievement_id=3,
        achievement_name="500,000 Combo",
    )

    assert event.event_type == "achievement"
    assert event.data == {"id": 3, "name": "500,000 Combo"}


# --- reading: the user feed is a pass-through -------------------------------


async def test_fetch_user_feed_returns_only_that_players_events_newest_first() -> None:
    events = _FakeEventsRepository()
    await events.record(user_id=7, event_type="a")  # id 1
    await events.record(user_id=8, event_type="b")  # id 2 -- not the viewer
    await events.record(user_id=7, event_type="c")  # id 3

    feed = await _service(events).fetch_user_feed(7)

    assert [e.id for e in feed] == [3, 1]


async def test_fetch_user_feed_forwards_keyset_paging() -> None:
    events = _FakeEventsRepository()
    for _ in range(5):  # ids 1..5, all the viewer's
        await events.record(user_id=7, event_type="e")

    page = await _service(events).fetch_user_feed(7, before_id=4, limit=2)

    # strictly older than id 4, newest-first, limited to 2.
    assert [e.id for e in page] == [3, 2]


# --- reading: the friends feed fans out over FRIEND edges -------------------


async def test_fetch_friends_feed_merges_friends_events_excluding_the_viewer() -> None:
    events = _FakeEventsRepository()
    await events.record(user_id=7, event_type="mine")  # id 1 -- the viewer
    await events.record(user_id=8, event_type="a")  # id 2 -- friend
    await events.record(user_id=9, event_type="b")  # id 3 -- friend
    await events.record(user_id=10, event_type="c")  # id 4 -- NOT a friend
    await events.record(user_id=8, event_type="d")  # id 5 -- friend

    relationships = _FakeRelationshipsRepository([8, 9])
    feed = await _service(events, relationships).fetch_friends_feed(7)

    # only friends 8 and 9, merged newest-first; the viewer's own and the
    # stranger's events are absent.
    assert [e.id for e in feed] == [5, 3, 2]

    # the FRIEND edges were resolved for the viewer with the right filter.
    call = relationships.calls[0]
    assert call.user1 == 7
    assert call.type is RelationshipType.FRIEND


async def test_fetch_friends_feed_is_empty_without_querying_when_no_friends() -> None:
    events = _FakeEventsRepository()
    await events.record(user_id=8, event_type="a")  # a stranger's event exists

    relationships = _FakeRelationshipsRepository([])  # viewer has no friends
    feed = await _service(events, relationships).fetch_friends_feed(7)

    assert feed == []
    # the fan-out reached the repository with an empty id list (which the real
    # repository short-circuits) and never returned a stranger's events.
    assert events.feed_calls[-1].user_ids == []


async def test_fetch_friends_feed_forwards_keyset_paging() -> None:
    events = _FakeEventsRepository()
    for _ in range(5):  # ids 1..5, all by one friend
        await events.record(user_id=8, event_type="e")

    relationships = _FakeRelationshipsRepository([8])
    page = await _service(events, relationships).fetch_friends_feed(
        7,
        before_id=4,
        limit=2,
    )

    assert [e.id for e in page] == [3, 2]
    assert events.feed_calls[-1].before_id == 4
    assert events.feed_calls[-1].limit == 2


async def test_fetch_friends_feed_caps_the_number_of_feed_sources() -> None:
    events = _FakeEventsRepository()
    # more friends than the fan-out cap.
    friend_ids = list(range(1000, 1000 + _MAX_FEED_SOURCES + 50))
    relationships = _FakeRelationshipsRepository(friend_ids)

    await _service(events, relationships).fetch_friends_feed(7)

    # the id list handed to the repository is bounded, so one feed read can never
    # explode into an unbounded IN-list.
    assert len(events.feed_calls[-1].user_ids) == _MAX_FEED_SOURCES
