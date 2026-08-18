"""Guards the schema facts the beatmap-submission id allocator depends on.

Privately-hosted beatmap ids are kept disjoint from osu!'s id space by a single
`auto_increment` seed in the schema. That seed is the whole mechanism, and it is
invisible at runtime: nothing reads it, nothing logs it, and a database restored
from a data-only dump silently loses it. If it were ever dropped or lowered, the
allocator would hand out an id in osu!'s range -- and because `maps.id`/`maps.md5`
are independently unique while the osu!api refresh writes with `REPLACE INTO`,
that collision would not raise, it would *destroy* a real beatmap row.

So the literal is pinned here, against the shipped schema, in both files that
declare it. The repository also asserts the floor on every allocation at runtime;
these tests are the static half of that pair.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.repositories.map_id_sequence import PRIVATE_BEATMAP_ID_FLOOR

_MIGRATIONS_DIR = Path(__file__).parents[3] / "migrations"
_BASE_SQL = (_MIGRATIONS_DIR / "base.sql").read_text(encoding="utf-8")
_MIGRATIONS_SQL = (_MIGRATIONS_DIR / "migrations.sql").read_text(encoding="utf-8")

_SEED_PATTERN = re.compile(
    r"alter\s+table\s+map_id_sequence\s+auto_increment\s*=\s*(\d+)",
    re.IGNORECASE,
)


def test_base_schema_seeds_the_sequence_at_the_private_floor() -> None:
    seeds = _SEED_PATTERN.findall(_BASE_SQL)

    assert seeds, "base.sql must seed map_id_sequence's auto_increment"
    # the seed and the constant the repository enforces must agree exactly; a
    # lower seed would allocate ids inside osu!'s range.
    assert [int(seed) for seed in seeds] == [PRIVATE_BEATMAP_ID_FLOOR]


def test_the_migration_seeds_the_sequence_too() -> None:
    # an existing database upgrades through migrations.sql and never reads
    # base.sql, so the seed has to be in both or one path is unprotected.
    seeds = _SEED_PATTERN.findall(_MIGRATIONS_SQL)

    assert [int(seed) for seed in seeds] == [PRIVATE_BEATMAP_ID_FLOOR]


def test_the_floor_stays_inside_signed_int32() -> None:
    # `maps.id` is a signed int; an id past this ceiling cannot be stored.
    assert PRIVATE_BEATMAP_ID_FLOOR < 2**31 - 1
    # and it must leave room to actually allocate into.
    assert (2**31 - 1) - PRIVATE_BEATMAP_ID_FLOOR > 1_000_000


def test_both_schemas_declare_the_sequence_and_submissions_tables() -> None:
    for sql, source in ((_BASE_SQL, "base.sql"), (_MIGRATIONS_SQL, "migrations.sql")):
        assert "create table map_id_sequence" in sql, source
        assert "create table map_submissions" in sql, source


def test_maps_creator_holds_any_local_username() -> None:
    # `users.name` is varchar(32); a privately-hosted map's creator is a local
    # account, so a narrower column would truncate or reject the write.
    assert re.search(r"name\s+varchar\(32\)", _BASE_SQL) is not None
    assert re.search(r"creator\s+varchar\(32\)", _BASE_SQL) is not None
    assert "alter table maps modify creator varchar(32)" in _MIGRATIONS_SQL
