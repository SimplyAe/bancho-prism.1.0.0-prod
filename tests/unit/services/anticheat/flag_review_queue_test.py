"""Guards for the staff-facing review-queue service.

``AnticheatReviewService`` is the thin seam the HTTP layer reaches through to
list, read, and resolve flags -- the read/action counterpart to the worker's
``FlagReviewService``. These tests pin its wiring:

- ``list_flags`` pairs a page of flags with the total for the *same* filter (so
  a client can render "page N of M" without a second call), passing every filter
  through to the repository unchanged and *not* leaking paging into the count;
- ``fetch_flag`` is a straight pass-through, None-for-missing intact;
- ``resolve_flag`` passes the staff decision through and surfaces the
  repository's None (no such flag) rather than inventing a row;
- a *terminal* verdict (actioned/dismissed) also writes exactly one moderation
  ``logs`` entry -- attributed to the acting staff member, aimed at the flagged
  player, and never restricting anyone -- while an interim ``reviewing`` claim
  and a resolve that found no flag write none. The log is written only *after* a
  successful resolve, so a resolution that did not happen leaves no audit trail.

The repository is faked: it records the arguments it was handed and returns
canned rows, so these assert the service's behaviour, not MySQL.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from app.repositories.anticheat_flags import AnticheatFlag
from app.repositories.anticheat_flags import AnticheatFlagStatus
from app.services.anticheat.flag_review_queue import AnticheatReviewService

_FIXED_TIME = datetime(2026, 8, 12, 12, 0, 0)


def _flag(score_id: int, *, status: AnticheatFlagStatus = AnticheatFlagStatus.OPEN) -> AnticheatFlag:
    return AnticheatFlag(
        score_id=score_id,
        user_id=7,
        mode=0,
        status=status,
        severity="high",
        top_signal_code="B3_AIM_CONTROLLER",
        top_signal_title="Aim-controller cleanliness",
        confidence=0.9,
        triggered_count=1,
        detail="jump aim is too clean for a human",
        evidence={"aim_machineness": 0.95},
        first_flagged_at=_FIXED_TIME,
        last_flagged_at=_FIXED_TIME,
        resolved_by=None,
        resolved_at=None,
        resolution_note=None,
    )


class _FakeFlagsRepository:
    """Records calls and returns canned rows; no SQL."""

    def __init__(self) -> None:
        self.many: list[AnticheatFlag] = []
        self.count = 0
        self.one: AnticheatFlag | None = None
        self.resolved: AnticheatFlag | None = None
        self.calls: list[SimpleNamespace] = []

    async def fetch_many(
        self,
        *,
        status: AnticheatFlagStatus | None = None,
        mode: int | None = None,
        user_id: int | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> list[AnticheatFlag]:
        self.calls.append(
            SimpleNamespace(
                method="fetch_many",
                status=status,
                mode=mode,
                user_id=user_id,
                page=page,
                page_size=page_size,
            ),
        )
        return self.many

    async def fetch_count(
        self,
        *,
        status: AnticheatFlagStatus | None = None,
        mode: int | None = None,
        user_id: int | None = None,
    ) -> int:
        self.calls.append(
            SimpleNamespace(
                method="fetch_count",
                status=status,
                mode=mode,
                user_id=user_id,
            ),
        )
        return self.count

    async def fetch_one(self, score_id: int) -> AnticheatFlag | None:
        self.calls.append(SimpleNamespace(method="fetch_one", score_id=score_id))
        return self.one

    async def resolve(
        self,
        score_id: int,
        *,
        status: AnticheatFlagStatus,
        resolved_by: int,
        note: str | None = None,
    ) -> AnticheatFlag | None:
        self.calls.append(
            SimpleNamespace(
                method="resolve",
                score_id=score_id,
                status=status,
                resolved_by=resolved_by,
                note=note,
            ),
        )
        return self.resolved


class _FakeLogsRepository:
    """Records the moderation-log writes; no SQL.

    ``create`` mirrors ``LogsRepository.create`` (positional ``_from``/``to``)
    and returns a ``SimpleNamespace`` standing in for the ``Log`` row; the tests
    care only about *whether* and *with what* it was called.
    """

    def __init__(self) -> None:
        self.writes: list[SimpleNamespace] = []

    async def create(self, _from: int, to: int, action: str, msg: str) -> Any:
        self.writes.append(
            SimpleNamespace(_from=_from, to=to, action=action, msg=msg),
        )
        return SimpleNamespace(id=len(self.writes))


def _service(
    repo: _FakeFlagsRepository,
    logs: _FakeLogsRepository | None = None,
) -> AnticheatReviewService:
    return AnticheatReviewService(
        flags=repo,  # type: ignore[arg-type]
        logs=logs if logs is not None else _FakeLogsRepository(),  # type: ignore[arg-type]
    )


def _call(repo: _FakeFlagsRepository, method: str) -> Any:
    calls = [c for c in repo.calls if c.method == method]
    assert len(calls) == 1
    return calls[0]


async def test_list_flags_pairs_the_page_with_the_total_and_passes_filters() -> None:
    repo = _FakeFlagsRepository()
    repo.many = [_flag(1), _flag(2)]
    repo.count = 7

    listing = await _service(repo).list_flags(
        status=AnticheatFlagStatus.OPEN,
        mode=0,
        user_id=10,
        page=2,
        page_size=5,
    )

    assert [f.score_id for f in listing.flags] == [1, 2]
    assert listing.total == 7  # the whole filtered set, not just this page

    # the same filter reaches both calls; only the page reaches fetch_many.
    many = _call(repo, "fetch_many")
    assert (many.status, many.mode, many.user_id) == (AnticheatFlagStatus.OPEN, 0, 10)
    assert (many.page, many.page_size) == (2, 5)

    count = _call(repo, "fetch_count")
    assert (count.status, count.mode, count.user_id) == (AnticheatFlagStatus.OPEN, 0, 10)


async def test_fetch_flag_passes_through() -> None:
    repo = _FakeFlagsRepository()
    repo.one = _flag(5)

    found = await _service(repo).fetch_flag(5)

    assert found is not None
    assert found.score_id == 5
    assert _call(repo, "fetch_one").score_id == 5


async def test_fetch_flag_is_none_when_the_score_was_never_flagged() -> None:
    repo = _FakeFlagsRepository()
    repo.one = None

    assert await _service(repo).fetch_flag(999) is None


async def test_resolve_flag_passes_through_the_staff_decision() -> None:
    repo = _FakeFlagsRepository()
    repo.resolved = _flag(5, status=AnticheatFlagStatus.ACTIONED)

    resolved = await _service(repo).resolve_flag(
        5,
        status=AnticheatFlagStatus.ACTIONED,
        resolved_by=3,
        note="restricted after review",
    )

    assert resolved is not None
    assert resolved.status is AnticheatFlagStatus.ACTIONED
    call = _call(repo, "resolve")
    assert call.score_id == 5
    assert call.status is AnticheatFlagStatus.ACTIONED
    assert call.resolved_by == 3
    assert call.note == "restricted after review"


async def test_resolve_flag_returns_none_when_there_is_no_such_flag() -> None:
    repo = _FakeFlagsRepository()
    repo.resolved = None  # repository found nothing to update

    resolved = await _service(repo).resolve_flag(
        404,
        status=AnticheatFlagStatus.DISMISSED,
        resolved_by=1,
    )

    assert resolved is None  # surfaced, so the HTTP layer can answer 404


# --- audit trail: terminal verdicts reach the moderation log ----------------


async def test_actioned_verdict_writes_one_moderation_log() -> None:
    repo = _FakeFlagsRepository()
    repo.resolved = _flag(5, status=AnticheatFlagStatus.ACTIONED)
    logs = _FakeLogsRepository()

    await _service(repo, logs).resolve_flag(
        5,
        status=AnticheatFlagStatus.ACTIONED,
        resolved_by=99,
        note="cheating confirmed",
    )

    assert len(logs.writes) == 1
    write = logs.writes[0]
    # attributed to the acting staff member, aimed at the flagged player.
    assert write._from == 99
    assert write.to == 7
    assert write.action == "ac_flag_actioned"
    # the note and a self-contained summary of the decided flag are recorded.
    assert "cheating confirmed" in write.msg
    assert "score 5" in write.msg


async def test_dismissed_verdict_writes_one_moderation_log() -> None:
    repo = _FakeFlagsRepository()
    repo.resolved = _flag(5, status=AnticheatFlagStatus.DISMISSED)
    logs = _FakeLogsRepository()

    await _service(repo, logs).resolve_flag(
        5,
        status=AnticheatFlagStatus.DISMISSED,
        resolved_by=99,
    )

    assert len(logs.writes) == 1
    assert logs.writes[0].action == "ac_flag_dismissed"


async def test_reviewing_claim_writes_no_moderation_log() -> None:
    repo = _FakeFlagsRepository()
    repo.resolved = _flag(5, status=AnticheatFlagStatus.REVIEWING)
    logs = _FakeLogsRepository()

    await _service(repo, logs).resolve_flag(
        5,
        status=AnticheatFlagStatus.REVIEWING,
        resolved_by=99,
    )

    # claiming a flag is not a verdict; it must not spam the moderation trail.
    assert logs.writes == []


async def test_no_moderation_log_when_there_is_no_such_flag() -> None:
    repo = _FakeFlagsRepository()
    repo.resolved = None  # nothing was resolved...
    logs = _FakeLogsRepository()

    await _service(repo, logs).resolve_flag(
        404,
        status=AnticheatFlagStatus.ACTIONED,
        resolved_by=99,
    )

    # ...so there is no resolution to audit -- resolve-then-log, never a phantom.
    assert logs.writes == []


async def test_long_note_is_truncated_to_the_log_column_width() -> None:
    repo = _FakeFlagsRepository()
    repo.resolved = _flag(5, status=AnticheatFlagStatus.ACTIONED)
    logs = _FakeLogsRepository()

    await _service(repo, logs).resolve_flag(
        5,
        status=AnticheatFlagStatus.ACTIONED,
        resolved_by=99,
        note="x" * 5000,  # far wider than the varchar(2048) `logs.msg`
    )

    assert len(logs.writes[0].msg) <= 2048
