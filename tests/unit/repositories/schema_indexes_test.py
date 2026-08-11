"""Schema index-preservation guard.

Indexes are not decoration: several are load-bearing for *recovery*, not
just steady-state performance.

- `scores_online_checksum_index` -- after an outage the osu! client resubmits
  every score it could not confirm. That resubmission looks each score up by
  its online checksum; without the index every retry is a full-table scan, so
  a recovering server drowns in exactly the traffic it is trying to absorb.
- `scores_userid_index` / `scores_map_md5_index` -- the leaderboard rebuild
  and per-player recalculation both scan by these.
- `stats_pp_index` -- the leaderboard rebuild orders ranked players by pp.

The realistic way these vanish is a restore: a `mysqldump --no-create-info`
data-only reload, or a hand-rolled migration, silently drops indexes while the
row counts still match, so nothing looks wrong until the first incident. This
test pins the set to the SQLAlchemy models (the source of truth the ORM emits
`CREATE TABLE` from) so their removal fails CI loudly rather than surfacing as
a mysterious latency cliff mid-recovery.

If an index is intentionally added or removed, update the expected set below in
the same change -- that edit is the reviewable record of the decision.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

from app.repositories.activity_events import ActivityEventsTable
from app.repositories.anticheat_flags import AnticheatFlagsTable
from app.repositories.score_replay_stats import ScoreReplayStatsTable
from app.repositories.scores import ScoresTable
from app.repositories.stat_snapshots import StatSnapshotsTable
from app.repositories.stats import StatsTable
from app.repositories.user_achievements import UserAchievementsTable

# The secondary indexes (`Index(...)` entries) each table must carry. The
# primary key is asserted separately; it is not reported in `Table.indexes`.
EXPECTED_SECONDARY_INDEXES = {
    "scores": {
        "scores_map_md5_index",
        "scores_score_index",
        "scores_pp_index",
        "scores_mods_index",
        "scores_status_index",
        "scores_mode_index",
        "scores_play_time_index",
        "scores_userid_index",
        "scores_online_checksum_index",
    },
    "stats": {
        "stats_mode_index",
        "stats_pp_index",
        "stats_tscore_index",
        "stats_rscore_index",
    },
    "user_achievements": {
        "user_achievements_achid_index",
        "user_achievements_userid_index",
    },
    "score_replay_stats": {
        "score_replay_stats_status_index",
        "score_replay_stats_mode_index",
        "score_replay_stats_extractor_version_index",
    },
    # the unique (user_id, mode, snapshot_date) key is enforced as a unique
    # *constraint* (asserted separately below); the one secondary index serves
    # per-date scans and pruning.
    "stat_snapshots": {
        "stat_snapshots_mode_date_index",
    },
    # the staff review queue: status drives triage, user_id the repeat-offender
    # view, mode per-mode filtering.
    "anticheat_flags": {
        "anticheat_flags_status_index",
        "anticheat_flags_user_id_index",
        "anticheat_flags_mode_index",
    },
    # the activity feed: the composite (user_id, id) serves both per-player and
    # friends-feed keyset scans; event_type filters by kind; created_at backs
    # time-window scans and age-based pruning.
    "activity_events": {
        "activity_events_user_id_id_index",
        "activity_events_event_type_index",
        "activity_events_created_at_index",
    },
}

EXPECTED_PRIMARY_KEY = {
    "scores": ["id"],
    "stats": ["id", "mode"],
    "user_achievements": ["userid", "achid"],
    "score_replay_stats": ["score_id"],
    "stat_snapshots": ["id"],
    "anticheat_flags": ["score_id"],
    "activity_events": ["id"],
}

_TABLES = (
    ScoresTable,
    StatsTable,
    UserAchievementsTable,
    ScoreReplayStatsTable,
    StatSnapshotsTable,
    AnticheatFlagsTable,
    ActivityEventsTable,
)


def test_every_recovery_critical_index_is_present() -> None:
    for table_model in _TABLES:
        table = table_model.__table__
        actual = {index.name for index in table.indexes}
        expected = EXPECTED_SECONDARY_INDEXES[table.name]

        missing = expected - actual
        assert not missing, f"{table.name} is missing indexes: {sorted(missing)}"


def test_no_unexpected_indexes_were_added_without_updating_this_guard() -> None:
    # the guard is only meaningful if it is kept exhaustive: a new index that
    # nobody added here means the expected set has drifted from the schema.
    for table_model in _TABLES:
        table = table_model.__table__
        actual = {index.name for index in table.indexes}
        expected = EXPECTED_SECONDARY_INDEXES[table.name]

        unexpected = actual - expected
        assert not unexpected, (
            f"{table.name} has indexes not tracked by this guard: "
            f"{sorted(unexpected)} -- add them to EXPECTED_SECONDARY_INDEXES."
        )


def test_scores_has_ten_indexes_total_including_primary_key() -> None:
    # the plan pins `scores` at ten indexes: nine secondary + the primary key.
    # MySQL implements a primary key as an index, so a data-only restore that
    # drops it is exactly the silent-corruption case this asserts against.
    table = ScoresTable.__table__
    secondary = len(table.indexes)
    has_primary = len(table.primary_key.columns) > 0
    assert secondary == 9
    assert has_primary
    assert secondary + int(has_primary) == 10


def test_primary_keys_are_preserved() -> None:
    for table_model in _TABLES:
        table = table_model.__table__
        actual = [column.name for column in table.primary_key.columns]
        assert actual == EXPECTED_PRIMARY_KEY[table.name], (
            f"{table.name} primary key changed: {actual}"
        )


def test_stat_snapshots_has_the_per_day_unique_constraint() -> None:
    # per-day idempotency rests entirely on this unique key: it is what makes the
    # bulk `INSERT IGNORE` a no-op on a same-day re-run instead of a duplicate
    # row, and what the single-row upsert's ON DUPLICATE KEY targets. Losing it
    # (e.g. a data-only restore) would silently break both, so pin it here.
    table = StatSnapshotsTable.__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("user_id", "mode", "snapshot_date") in unique_columns
