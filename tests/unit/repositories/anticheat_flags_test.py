"""Guards for the staff review-queue repository.

``anticheat_flags`` is the durable side of **flag, never auto-ban**: one row per
flagged score, holding the strongest detector signal and its evidence, plus the
resolution a staff member records. These tests pin the behaviour the review
queue depends on, and one guarantee above all:

- ``record`` is an upsert that refreshes only the *detection* columns on a
  re-flag and never the *resolution* columns (``status``, ``resolved_by``,
  ``resolved_at``, ``resolution_note``) or ``first_flagged_at`` -- so a flag a
  reviewer already dismissed does not silently re-open every time the backfill
  loop re-analyses the score. This is the whole point of the table and the test
  that would matter most if it regressed;
- ``resolve`` is the only writer of the resolution columns, and is a no-op
  (returns ``None``) when there is no such flag;
- ``fetch_many`` filters by status/mode/user, orders newest-re-flag-first, and
  pages; ``fetch_count`` counts the same filtered set;
- prose columns (title/detail/note) are truncated so a long string can never
  fail the write, and ``evidence`` round-trips through JSON.

No MySQL is involved: a fake ``Database`` stores rows and interprets the Core
statements it is handed. Crucially the fake reads the *actual*
``ON DUPLICATE KEY UPDATE`` column set off the compiled ``Insert`` (rather than
hard-coding which columns survive), so the preserve-resolution guarantee is
tested against the repository's own SQL.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any

from app.repositories.anticheat_flags import AnticheatFlag
from app.repositories.anticheat_flags import AnticheatFlagStatus
from app.repositories.anticheat_flags import AnticheatFlagsRepository
from app.repositories.anticheat_flags import AnticheatFlagsTable

# columns the single-row insert/update bind by name; the on-duplicate `param_N`
# binds and the where-clause bind (`score_id_1`) are not column names, so they
# are filtered out and never mistaken for row values.
_COLUMN_NAMES = frozenset(
    column.name for column in AnticheatFlagsTable.__table__.columns
)

# a deterministic monotonic clock: each write advances it a second, so
# `last_flagged_at` on a later write really is later (the ordering tests lean on
# this) without any dependence on wall-clock time.
_BASE = datetime(2026, 8, 11, 12, 0, 0)


class _FakeDatabase:
    """Stores flag rows and answers reads by interpreting the statement.

    ``execute`` handles both the ``record`` upsert (a MySQL ``Insert`` with
    ``ON DUPLICATE KEY UPDATE``) and the ``resolve`` ``Update``. On a duplicate
    insert it refreshes exactly the columns named in the statement's own
    on-duplicate clause -- so the columns it *leaves alone* (status, resolution,
    ``first_flagged_at``) are decided by the repository, not by this fake.
    ``fetch_one`` / ``fetch_all`` walk the ``Select``'s WHERE / ORDER BY /
    LIMIT / OFFSET so the repository's filtering, ordering and paging decide the
    result.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._tick = 0

    def _advance(self) -> datetime:
        self._tick += 1
        return _BASE + timedelta(seconds=self._tick)

    def _find(self, score_id: int) -> dict[str, Any] | None:
        for row in self.rows:
            if row["score_id"] == score_id:
                return row
        return None

    async def execute(self, statement: Any) -> int:
        now = self._advance()
        if statement.is_insert:
            return self._execute_insert(statement, now)
        if statement.is_update:
            return self._execute_update(statement, now)
        raise AssertionError("fake only handles the repository's insert/update")

    def _execute_insert(self, statement: Any, now: datetime) -> int:
        params = statement.compile().params
        values = {k: v for k, v in params.items() if k in _COLUMN_NAMES}
        score_id = values["score_id"]

        existing = self._find(score_id)
        if existing is not None:
            # refresh exactly the columns the repository's own ON DUPLICATE KEY
            # UPDATE names -- and nothing else. The scalar new values equal the
            # insert binds (the repository passes the same values to both), so
            # we read them from `values`; the server-side `now()` we supply.
            for column_name in _on_duplicate_columns(statement):
                if column_name == "last_flagged_at":
                    existing[column_name] = now
                else:
                    existing[column_name] = values[column_name]
            return 1

        row: dict[str, Any] = {name: None for name in _COLUMN_NAMES}
        row.update(values)
        row["first_flagged_at"] = now
        row["last_flagged_at"] = now
        # resolution columns start empty -- staff write them via `resolve`.
        row["resolved_by"] = None
        row["resolved_at"] = None
        row["resolution_note"] = None
        self.rows.append(row)
        return 1

    def _execute_update(self, statement: Any, now: datetime) -> int:
        params = statement.compile().params
        values = {k: v for k, v in params.items() if k in _COLUMN_NAMES}
        score_id = _eq_value(statement.whereclause, "score_id")

        row = self._find(score_id)
        if row is None:
            return 0
        row["status"] = values["status"]
        row["resolved_by"] = values["resolved_by"]
        row["resolution_note"] = values.get("resolution_note")
        row["resolved_at"] = now  # server-side now(); not a column-name bind.
        return 1

    async def fetch_one(self, statement: Any) -> dict[str, Any] | None:
        if _is_count(statement):
            return {"count": len(self._query(statement))}
        matched = self._query(statement)
        return matched[0] if matched else None

    async def fetch_all(self, statement: Any) -> list[dict[str, Any]]:
        return self._query(statement)

    def _query(self, statement: Any) -> list[dict[str, Any]]:
        predicates = _where_predicates(statement.whereclause)
        matched = [row for row in self.rows if all(p(row) for p in predicates)]

        for column_name, descending in reversed(_order_specs(statement)):
            matched.sort(key=lambda r: r[column_name], reverse=descending)

        offset = _offset_value(statement)
        if offset is not None:
            matched = matched[offset:]
        limit = _limit_value(statement)
        if limit is not None:
            matched = matched[:limit]
        return matched


def _on_duplicate_columns(statement: Any) -> set[str]:
    """The column names in the statement's ON DUPLICATE KEY UPDATE clause."""
    update = statement._post_values_clause.update
    return {getattr(key, "name", key) for key in update}


def _is_count(statement: Any) -> bool:
    return list(statement.selected_columns.keys()) == ["count"]


def _where_predicates(whereclause: Any) -> list[Any]:
    """Turn a Core WHERE tree into a list of row -> bool predicates.

    The repository only ever filters with equality (status/mode/user_id/
    score_id), so equality is all this needs to model.
    """
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None) or [whereclause]

    predicates = []
    for clause in clauses:
        operator = clause.operator.__name__
        if operator != "eq":  # pragma: no cover - repository only uses eq
            raise AssertionError(f"unhandled operator in fake: {operator}")
        predicates.append(_eq(clause.left.name, clause.right.value))
    return predicates


def _eq(column_name: str, value: Any) -> Any:
    return lambda row: row[column_name] == value


def _eq_value(whereclause: Any, column_name: str) -> Any:
    """Read the value a single-column equality WHERE compares against."""
    clauses = getattr(whereclause, "clauses", None) or [whereclause]
    for clause in clauses:
        if clause.left.name == column_name and clause.operator.__name__ == "eq":
            return clause.right.value
    raise AssertionError(f"no equality on {column_name} in where clause")


def _order_specs(statement: Any) -> list[tuple[str, bool]]:
    specs: list[tuple[str, bool]] = []
    for element in statement._order_by_clauses:
        descending = element.modifier.__name__ == "desc_op"
        specs.append((element.element.name, descending))
    return specs


def _limit_value(statement: Any) -> int | None:
    limit_clause = getattr(statement, "_limit_clause", None)
    return getattr(limit_clause, "value", None) if limit_clause is not None else None


def _offset_value(statement: Any) -> int | None:
    offset_clause = getattr(statement, "_offset_clause", None)
    return getattr(offset_clause, "value", None) if offset_clause is not None else None


def _repo() -> tuple[AnticheatFlagsRepository, _FakeDatabase]:
    database = _FakeDatabase()
    return AnticheatFlagsRepository(database), database  # type: ignore[arg-type]


async def _record(
    repository: AnticheatFlagsRepository,
    *,
    score_id: int,
    user_id: int = 7,
    mode: int = 0,
    severity: str = "high",
    top_signal_code: str = "B3_AIM_CONTROLLER",
    top_signal_title: str = "Aim-controller cleanliness",
    confidence: float = 0.9,
    triggered_count: int = 1,
    detail: str = "jump aim is too clean for a human",
    evidence: dict[str, float] | None = None,
) -> AnticheatFlag:
    return await repository.record(
        score_id,
        user_id=user_id,
        mode=mode,
        severity=severity,
        top_signal_code=top_signal_code,
        top_signal_title=top_signal_title,
        confidence=confidence,
        triggered_count=triggered_count,
        detail=detail,
        evidence=evidence if evidence is not None else {"aim_machineness": 0.94},
    )


async def test_record_round_trips_a_flag() -> None:
    repository, _ = _repo()

    flag = await _record(
        repository,
        score_id=100,
        user_id=42,
        mode=3,
        severity="medium",
        top_signal_code="B1_HOLD_DURATION",
        top_signal_title="Hold-duration uniformity",
        confidence=0.7,
        triggered_count=2,
        detail="hold widths barely vary",
        evidence={"hold_cov": 0.02, "n": 120.0},
    )

    assert flag.score_id == 100
    assert flag.user_id == 42
    assert flag.mode == 3
    assert flag.status is AnticheatFlagStatus.OPEN  # a fresh flag is unreviewed
    assert flag.severity == "medium"
    assert flag.top_signal_code == "B1_HOLD_DURATION"
    assert flag.top_signal_title == "Hold-duration uniformity"
    assert flag.confidence == 0.7
    assert flag.triggered_count == 2
    assert flag.detail == "hold widths barely vary"
    assert flag.evidence == {"hold_cov": 0.02, "n": 120.0}  # JSON round-trip
    assert flag.first_flagged_at == flag.last_flagged_at  # first flag: equal
    assert flag.resolved_by is None
    assert flag.resolved_at is None
    assert flag.resolution_note is None


async def test_record_is_a_single_row_per_score_not_a_duplicate() -> None:
    repository, database = _repo()

    await _record(repository, score_id=1, severity="medium")
    await _record(repository, score_id=1, severity="high")

    assert len(database.rows) == 1  # re-flagged in place, not appended


async def test_reflagging_refreshes_detection_but_preserves_staff_resolution() -> None:
    # THE guarantee: a reviewer's decision survives re-analysis. A dismissed flag
    # must not silently re-open, and its detection payload should still refresh so
    # a reviewer who looks again sees current evidence.
    repository, _ = _repo()

    await _record(repository, score_id=5, severity="medium", detail="first look")
    resolved = await repository.resolve(
        5,
        status=AnticheatFlagStatus.DISMISSED,
        resolved_by=999,
        note="reviewed: legit player",
    )
    assert resolved is not None
    original_first_flagged = resolved.first_flagged_at

    # backfill re-analyses the same score with a stronger verdict.
    reflagged = await _record(
        repository,
        score_id=5,
        severity="high",
        detail="second look, stronger",
        evidence={"aim_machineness": 0.99},
    )

    # detection columns refreshed...
    assert reflagged.severity == "high"
    assert reflagged.detail == "second look, stronger"
    assert reflagged.evidence == {"aim_machineness": 0.99}
    assert reflagged.last_flagged_at > original_first_flagged
    # ...but the staff decision and original flag time are untouched.
    assert reflagged.status is AnticheatFlagStatus.DISMISSED
    assert reflagged.resolved_by == 999
    assert reflagged.resolution_note == "reviewed: legit player"
    assert reflagged.resolved_at is not None
    assert reflagged.first_flagged_at == original_first_flagged


async def test_reflagging_bumps_last_flagged_at_but_keeps_first_flagged_at() -> None:
    repository, _ = _repo()

    first = await _record(repository, score_id=8)
    second = await _record(repository, score_id=8)

    assert second.first_flagged_at == first.first_flagged_at
    assert second.last_flagged_at > first.last_flagged_at


async def test_resolve_records_a_staff_decision() -> None:
    repository, _ = _repo()
    await _record(repository, score_id=12)

    resolved = await repository.resolve(
        12,
        status=AnticheatFlagStatus.ACTIONED,
        resolved_by=3,
        note="restricted after review",
    )

    assert resolved is not None
    assert resolved.status is AnticheatFlagStatus.ACTIONED
    assert resolved.resolved_by == 3
    assert resolved.resolution_note == "restricted after review"
    assert resolved.resolved_at is not None


async def test_resolve_is_a_no_op_when_the_flag_does_not_exist() -> None:
    repository, database = _repo()

    resolved = await repository.resolve(
        404,
        status=AnticheatFlagStatus.DISMISSED,
        resolved_by=1,
    )

    assert resolved is None
    assert database.rows == []  # never conjures a row for a non-existent flag


async def test_fetch_one_is_none_when_the_score_was_never_flagged() -> None:
    repository, _ = _repo()
    assert await repository.fetch_one(777) is None


async def test_fetch_many_filters_by_status() -> None:
    repository, _ = _repo()
    await _record(repository, score_id=1)
    await _record(repository, score_id=2)
    await repository.resolve(2, status=AnticheatFlagStatus.DISMISSED, resolved_by=1)

    open_flags = await repository.fetch_many(status=AnticheatFlagStatus.OPEN)
    dismissed = await repository.fetch_many(status=AnticheatFlagStatus.DISMISSED)

    assert [f.score_id for f in open_flags] == [1]
    assert [f.score_id for f in dismissed] == [2]


async def test_fetch_many_filters_by_mode_and_user() -> None:
    repository, _ = _repo()
    await _record(repository, score_id=1, user_id=10, mode=0)
    await _record(repository, score_id=2, user_id=10, mode=1)
    await _record(repository, score_id=3, user_id=20, mode=0)

    by_user = await repository.fetch_many(user_id=10)
    by_mode = await repository.fetch_many(mode=0)

    assert {f.score_id for f in by_user} == {1, 2}
    assert {f.score_id for f in by_mode} == {1, 3}


async def test_fetch_many_orders_newest_reflag_first() -> None:
    repository, _ = _repo()
    await _record(repository, score_id=1)
    await _record(repository, score_id=2)
    # re-flagging score 1 makes it the most-recently-flagged again.
    await _record(repository, score_id=1)

    flags = await repository.fetch_many()

    assert [f.score_id for f in flags] == [1, 2]


async def test_fetch_many_pages() -> None:
    repository, _ = _repo()
    await _record(repository, score_id=1)
    await _record(repository, score_id=2)
    await _record(repository, score_id=3)  # newest

    page1 = await repository.fetch_many(page=1, page_size=1)
    page2 = await repository.fetch_many(page=2, page_size=1)

    assert [f.score_id for f in page1] == [3]
    assert [f.score_id for f in page2] == [2]


async def test_fetch_count_counts_the_filtered_set() -> None:
    repository, _ = _repo()
    await _record(repository, score_id=1, user_id=10)
    await _record(repository, score_id=2, user_id=10)
    await _record(repository, score_id=3, user_id=20)
    await repository.resolve(1, status=AnticheatFlagStatus.DISMISSED, resolved_by=1)

    assert await repository.fetch_count() == 3
    assert await repository.fetch_count(user_id=10) == 2
    assert await repository.fetch_count(status=AnticheatFlagStatus.OPEN) == 2
    assert await repository.fetch_count(status=AnticheatFlagStatus.DISMISSED) == 1


async def test_record_truncates_overlong_title_and_detail() -> None:
    repository, _ = _repo()

    flag = await _record(
        repository,
        score_id=1,
        top_signal_title="T" * 500,
        detail="D" * 1000,
    )

    assert len(flag.top_signal_title) == 128
    assert len(flag.detail) == 512


async def test_resolve_truncates_an_overlong_note() -> None:
    repository, _ = _repo()
    await _record(repository, score_id=1)

    resolved = await repository.resolve(
        1,
        status=AnticheatFlagStatus.ACTIONED,
        resolved_by=1,
        note="N" * 1000,
    )

    assert resolved is not None
    assert resolved.resolution_note is not None
    assert len(resolved.resolution_note) == 512
