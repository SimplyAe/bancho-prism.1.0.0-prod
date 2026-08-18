"""Guards the private beatmap id allocator.

The allocator's only job is to hand out ids that cannot collide with a real osu!
beatmap id. That matters more than it sounds: `maps.id` and `maps.md5` are
independently unique and `BeatmapSet._save_to_sql` writes with `REPLACE INTO`, so
a colliding id does not raise -- it deletes the mirrored row and takes its place.

Two properties are pinned:

- an allocation returns the row id the insert produced, and records what it was
  for and who asked (auditing);
- **an id below the private floor raises**. The floor is enforced at runtime
  rather than assumed because the `auto_increment` seed exists only in the schema:
  a database restored from a data-only dump comes back with the counter at 1, and
  the first allocation would then point straight at a real beatmap. A loud failure
  there is a rejected upload; a silent one is a hole in the beatmap table.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.repositories.map_id_sequence import PRIVATE_BEATMAP_ID_FLOOR
from app.repositories.map_id_sequence import BeatmapIdAllocationError
from app.repositories.map_id_sequence import MapIdKind
from app.repositories.map_id_sequence import MapIdSequenceRepository


class _FakeDatabase:
    """Hands back scripted insert ids, recording the statements it was given."""

    def __init__(self, *, insert_ids: list[int]) -> None:
        self._insert_ids = list(insert_ids)
        self.statements: list[dict[str, Any]] = []

    async def execute(self, statement: Any) -> int:
        self.statements.append(statement.compile().params)
        return self._insert_ids.pop(0)


def _repo(*, insert_ids: list[int]) -> tuple[MapIdSequenceRepository, _FakeDatabase]:
    database = _FakeDatabase(insert_ids=insert_ids)
    return MapIdSequenceRepository(database), database  # type: ignore[arg-type]


async def test_allocate_returns_the_new_row_id() -> None:
    repository, database = _repo(insert_ids=[PRIVATE_BEATMAP_ID_FLOOR])

    allocated = await repository.allocate(kind=MapIdKind.SET, allocated_by=6)

    assert allocated == PRIVATE_BEATMAP_ID_FLOOR
    # the allocation records what it was for and who asked.
    assert database.statements[0]["kind"] is MapIdKind.SET
    assert database.statements[0]["allocated_by"] == 6


async def test_allocate_rejects_an_id_below_the_private_floor() -> None:
    # what a database restored without the auto_increment seed would return.
    repository, _ = _repo(insert_ids=[1])

    with pytest.raises(BeatmapIdAllocationError) as excinfo:
        await repository.allocate(kind=MapIdKind.MAP, allocated_by=6)

    # the message has to name the cause: this is only ever seen during an
    # incident, and "id 1" alone does not explain itself.
    assert "seed" in str(excinfo.value)


async def test_allocate_rejects_an_id_just_under_the_floor() -> None:
    repository, _ = _repo(insert_ids=[PRIVATE_BEATMAP_ID_FLOOR - 1])

    with pytest.raises(BeatmapIdAllocationError):
        await repository.allocate(kind=MapIdKind.MAP, allocated_by=6)


async def test_allocate_many_returns_distinct_ids_in_order() -> None:
    ids = [PRIVATE_BEATMAP_ID_FLOOR + offset for offset in range(3)]
    repository, database = _repo(insert_ids=ids)

    allocated = await repository.allocate_many(
        kind=MapIdKind.MAP,
        count=3,
        allocated_by=6,
    )

    assert allocated == ids
    assert len(set(allocated)) == 3
    assert len(database.statements) == 3


async def test_allocate_many_of_zero_touches_nothing() -> None:
    repository, database = _repo(insert_ids=[])

    allocated = await repository.allocate_many(
        kind=MapIdKind.MAP,
        count=0,
        allocated_by=6,
    )

    assert allocated == []
    assert database.statements == []


async def test_allocate_many_propagates_a_bad_floor() -> None:
    # the guard must not be bypassable by going through the bulk path.
    repository, _ = _repo(insert_ids=[PRIVATE_BEATMAP_ID_FLOOR, 2])

    with pytest.raises(BeatmapIdAllocationError):
        await repository.allocate_many(kind=MapIdKind.MAP, count=2, allocated_by=6)
