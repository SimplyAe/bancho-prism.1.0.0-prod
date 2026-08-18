"""Guards that the osu!api refresh never destroys a privately-hosted map.

``BeatmapSet._update_if_available`` exists to mirror the osu!api: it updates maps
the api reports, and **deletes maps the api no longer lists -- along with every
score set on them**. That is correct for mirrored maps and catastrophic for maps
this server hosts itself, because a privately-hosted set id is not on the osu!api
at all: asking about it returns "no such set" (a 404, or a 200 carrying an empty
array), which is exactly the shape of "this set was deleted upstream".

So the refresh is scoped to osu!-hosted sets, in depth:

- the whole refresh returns early for a ``private`` set -- nothing is fetched,
  nothing is written (the single guard that matters);
- the md5s collected for deletion exclude privately-hosted maps, which is what
  protects their *scores* (no SQL predicate can, since ``scores`` has no
  ``server`` column);
- the three DELETEs carry ``server = 'osu!'`` as a second line of defence;
- ``server`` is written from the object, never as a literal -- ``maps.id`` and
  ``maps.md5`` are independently unique, so a ``REPLACE`` carrying either would
  otherwise convert a private row.

The osu! path must keep behaving exactly as before, so the deleting cases are
asserted too, not just the skipping ones. Note ``frozen=1`` is *not* a guard
here: it only preserves ``status`` inside ``_parse_from_osuapi_resp``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import app.state.services
from app.constants.beatmap_statuses import RankedStatus
from app.objects import beatmap as beatmap_module
from app.objects.beatmap import Beatmap
from app.objects.beatmap import BeatmapSet
from app.repositories.maps import MapServer

_LAST_CHECK = datetime(2026, 8, 17, 12, 0, 0)


class _RecordingDatabase:
    """Records every statement handed to it, so "wrote nothing" is assertable."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    async def execute(self, query: str, params: Any = None) -> int:
        self.statements.append((query, params))
        return 0

    async def execute_many(self, query: str, params: Any = None) -> None:
        self.statements.append((query, params))

    async def fetch_all(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        self.statements.append((query, params))
        return []

    @property
    def deletes(self) -> list[tuple[str, Any]]:
        return [
            statement
            for statement in self.statements
            if statement[0].lstrip().upper().startswith("DELETE")
        ]

    def deleted_md5s(self) -> set[str]:
        """Every md5 passed to a `DELETE FROM maps` statement."""
        md5s: set[str] = set()
        for query, params in self.deletes:
            # note: "FROM maps" also matches "FROM mapsets", whose params carry a
            # set id rather than md5s -- match the whole table name.
            if "FROM maps " in f"{query} " and params and "map_md5s" in params:
                md5s |= set(params["map_md5s"])
        return md5s


def _bmap(
    bmap_set: BeatmapSet,
    *,
    id: int,
    md5: str,
    server: str = MapServer.OSU,
) -> Beatmap:
    return Beatmap(
        map_set=bmap_set,
        md5=md5,
        id=id,
        set_id=bmap_set.id,
        artist="a",
        title="t",
        version="v",
        creator="c",
        filename=f"a - t (c) [{id}].osu",
        status=RankedStatus.Ranked,
        server=server,
    )


def _set(*, id: int, server: str = MapServer.OSU) -> BeatmapSet:
    return BeatmapSet(id=id, last_osuapi_check=_LAST_CHECK, server=server)


@pytest.fixture(autouse=True)
def recording_database(
    monkeypatch: pytest.MonkeyPatch,
) -> _RecordingDatabase:
    database = _RecordingDatabase()
    monkeypatch.setattr(app.state.services, "database", database)
    return database


def _patch_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: list[dict[str, Any]] | None,
    status_code: int,
) -> list[dict[str, Any]]:
    """Stub the osu!api, recording the params it was called with."""
    calls: list[dict[str, Any]] = []

    async def api_get_beatmaps(**params: Any) -> dict[str, Any]:
        calls.append(params)
        return {"data": data, "status_code": status_code}

    monkeypatch.setattr(beatmap_module, "api_get_beatmaps", api_get_beatmaps)
    return calls


# --- the private-set guard -------------------------------------------------


async def test_private_set_is_never_refreshed_when_api_404s(
    monkeypatch: pytest.MonkeyPatch,
    recording_database: _RecordingDatabase,
) -> None:
    bmap_set = _set(id=2_000_000_000, server=MapServer.PRIVATE)
    bmap_set.maps = [
        _bmap(bmap_set, id=2_000_000_001, md5="a" * 32, server=MapServer.PRIVATE),
    ]
    api_calls = _patch_api(monkeypatch, data=None, status_code=404)

    await bmap_set._update_if_available()

    # the api is not even asked -- there is no upstream for a private set.
    assert api_calls == []
    assert recording_database.statements == []
    assert len(bmap_set.maps) == 1


async def test_private_set_survives_an_empty_200(
    monkeypatch: pytest.MonkeyPatch,
    recording_database: _RecordingDatabase,
) -> None:
    # a 200 with no beatmaps is what the real osu!api returns for an id it has
    # never heard of, so this is the branch that fired for every private set.
    bmap_set = _set(id=2_000_000_000, server=MapServer.PRIVATE)
    bmap_set.maps = [
        _bmap(bmap_set, id=2_000_000_001, md5="a" * 32, server=MapServer.PRIVATE),
    ]
    _patch_api(monkeypatch, data=None, status_code=200)

    await bmap_set._update_if_available()

    assert recording_database.statements == []
    assert len(bmap_set.maps) == 1


# --- the osu! path is unchanged -------------------------------------------


async def test_osu_set_missing_upstream_still_deletes(
    monkeypatch: pytest.MonkeyPatch,
    recording_database: _RecordingDatabase,
) -> None:
    bmap_set = _set(id=42)
    bmap_set.maps = [_bmap(bmap_set, id=101, md5="b" * 32)]
    _patch_api(monkeypatch, data=None, status_code=404)

    await bmap_set._update_if_available()

    # behaviour preserved: maps, their scores, and the set are all removed...
    queries = " | ".join(query for query, _ in recording_database.statements)
    assert "DELETE FROM maps WHERE" in queries
    assert "DELETE FROM scores" in queries
    assert "DELETE FROM mapsets" in queries
    assert recording_database.deleted_md5s() == {"b" * 32}
    # ...and every delete is scoped so it can never reach a private row.
    for query, _ in recording_database.deletes:
        assert "server = 'osu!'" in query or "FROM scores" in query


async def test_osu_set_drops_only_maps_the_api_no_longer_lists(
    monkeypatch: pytest.MonkeyPatch,
    recording_database: _RecordingDatabase,
) -> None:
    bmap_set = _set(id=42)
    kept = _bmap(bmap_set, id=101, md5="c" * 32)
    removed = _bmap(bmap_set, id=102, md5="d" * 32)
    bmap_set.maps = [kept, removed]
    _patch_api(
        monkeypatch,
        data=[
            {
                "beatmap_id": "101",
                "beatmapset_id": "42",
                "file_md5": "c" * 32,
                "approved": "1",
                "artist": "a",
                "title": "t",
                "version": "v",
                "creator": "c",
                "last_update": "2026-08-17 12:00:00",
                "total_length": "100",
                "max_combo": "500",
                "mode": "0",
                "bpm": "180",
                "diff_size": "4",
                "diff_overall": "8",
                "diff_approach": "9",
                "diff_drain": "5",
                "difficultyrating": "5.5",
            },
        ],
        status_code=200,
    )

    await bmap_set._update_if_available()

    assert recording_database.deleted_md5s() == {"d" * 32}


async def test_a_private_map_inside_an_osu_set_is_not_deleted(
    monkeypatch: pytest.MonkeyPatch,
    recording_database: _RecordingDatabase,
) -> None:
    # the api cannot vouch for a privately-hosted map either way, so an osu! set
    # refresh must leave it (and its scores) alone rather than treating its
    # absence from the response as "deleted upstream".
    bmap_set = _set(id=42)
    private = _bmap(bmap_set, id=2_000_000_001, md5="e" * 32, server=MapServer.PRIVATE)
    orphaned = _bmap(bmap_set, id=102, md5="f" * 32)
    bmap_set.maps = [private, orphaned]
    _patch_api(monkeypatch, data=[], status_code=200)

    await bmap_set._update_if_available()

    assert recording_database.deleted_md5s() == {"f" * 32}
    assert private in bmap_set.maps


# --- server is persisted, never assumed -----------------------------------


async def test_save_to_sql_writes_each_map_s_own_server(
    recording_database: _RecordingDatabase,
) -> None:
    bmap_set = _set(id=42)
    bmap_set.maps = [
        _bmap(bmap_set, id=101, md5="1" * 32),
        _bmap(bmap_set, id=2_000_000_001, md5="2" * 32, server=MapServer.PRIVATE),
    ]

    await bmap_set._save_to_sql()

    _, params = recording_database.statements[0]
    assert [row["server"] for row in params] == [MapServer.OSU, MapServer.PRIVATE]


async def test_new_maps_from_the_api_are_marked_osu_hosted(
    monkeypatch: pytest.MonkeyPatch,
    recording_database: _RecordingDatabase,
) -> None:
    # `Beatmap.__new__` bypasses `__init__`, so a missed attribute here is an
    # AttributeError on the *normal* osu! path, not the private one.
    bmap_set = _set(id=42)
    _patch_api(
        monkeypatch,
        data=[
            {
                "beatmap_id": "101",
                "beatmapset_id": "42",
                "file_md5": "9" * 32,
                "approved": "1",
                "artist": "a",
                "title": "t",
                "version": "v",
                "creator": "c",
                "last_update": "2026-08-17 12:00:00",
                "total_length": "100",
                "max_combo": "500",
                "mode": "0",
                "bpm": "180",
                "diff_size": "4",
                "diff_overall": "8",
                "diff_approach": "9",
                "diff_drain": "5",
                "difficultyrating": "5.5",
            },
        ],
        status_code=200,
    )

    await bmap_set._update_if_available()

    assert [bmap.server for bmap in bmap_set.maps] == [MapServer.OSU]


async def test_the_mapset_write_carries_the_set_s_server(
    monkeypatch: pytest.MonkeyPatch,
    recording_database: _RecordingDatabase,
) -> None:
    bmap_set = _set(id=42)
    _patch_api(monkeypatch, data=[], status_code=200)

    await bmap_set._update_if_available()

    mapset_writes = [
        params
        for query, params in recording_database.statements
        if "INTO mapsets" in query
    ]
    assert mapset_writes[0]["server"] == MapServer.OSU
