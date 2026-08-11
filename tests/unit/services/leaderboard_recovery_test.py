from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping

from app.constants.gamemodes import GameMode
from app.repositories.stats import RankedPlayerPP
from app.services.leaderboard_recovery import LeaderboardRecoveryService


class _FakeStats:
    """Serves ranked players per mode, honoring keyset pagination."""

    def __init__(self, rows_by_mode: Mapping[int, list[RankedPlayerPP]]) -> None:
        self.rows_by_mode = rows_by_mode
        self.calls: list[tuple[int, int, int]] = []

    async def fetch_ranked_pp_for_mode(
        self,
        *,
        mode: int,
        batch_size: int,
        after_player_id: int = 0,
    ) -> list[RankedPlayerPP]:
        self.calls.append((mode, batch_size, after_player_id))
        rows = sorted(
            self.rows_by_mode.get(mode, []),
            key=lambda row: row.player_id,
        )
        remaining = [row for row in rows if row.player_id > after_player_id]
        return remaining[:batch_size]


class _FakeLeaderboardRanks:
    """In-memory stand-in for the redis sorted sets."""

    def __init__(self, existing: Mapping[str, dict[str, float]] | None = None) -> None:
        self.live: dict[str, dict[str, float]] = {
            key: dict(value) for key, value in (existing or {}).items()
        }
        self.staging: dict[str, dict[str, float]] = {}
        self.commits: list[list[str]] = []
        self.discards: list[list[str]] = []

    @staticmethod
    def global_key(mode: int) -> str:
        return f"bancho:leaderboard:{mode}"

    @staticmethod
    def country_key(mode: int, country: str) -> str:
        return f"bancho:leaderboard:{mode}:{country}"

    async def count_ranked(self, key: str) -> int:
        return len(self.live.get(key, {}))

    async def fetch_existing_keys(self, mode: int) -> list[str]:
        global_key = self.global_key(mode)
        keys = [
            key
            for key in self.live
            if key.startswith(f"{global_key}:") and not key.endswith(":rebuilding")
        ]
        keys.append(global_key)
        return keys

    async def stage_scores(self, key: str, scores: Mapping[str, float]) -> None:
        if not scores:
            return
        self.staging.setdefault(key, {}).update(scores)

    async def discard_staging(self, keys: Iterable[str]) -> None:
        key_list = list(keys)
        self.discards.append(key_list)
        for key in key_list:
            self.staging.pop(key, None)

    async def commit_staged(self, keys: Iterable[str]) -> None:
        key_list = list(keys)
        self.commits.append(key_list)
        for key in key_list:
            staged = self.staging.pop(key, None)
            if staged is None:
                # nothing staged -> the leaderboard should now be empty.
                self.live.pop(key, None)
            else:
                self.live[key] = staged


def _service(
    stats: _FakeStats,
    ranks: _FakeLeaderboardRanks,
    *,
    batch_size: int = 5_000,
) -> LeaderboardRecoveryService:
    return LeaderboardRecoveryService(
        stats=stats,  # type: ignore[arg-type]
        leaderboard_ranks=ranks,  # type: ignore[arg-type]
        batch_size=batch_size,
    )


async def test_is_empty_false_on_a_fresh_install() -> None:
    """Both sides empty is a new server, not data loss -- otherwise we
    would 'rebuild' on every boot before anyone has registered."""
    service = _service(_FakeStats({}), _FakeLeaderboardRanks())
    assert await service.is_empty() is False


async def test_is_empty_true_when_redis_lost_ranked_players() -> None:
    """The real signal: mysql has ranked players, redis does not."""
    stats = _FakeStats({0: [RankedPlayerPP(player_id=1, country="us", pp=5000)]})
    service = _service(stats, _FakeLeaderboardRanks())
    assert await service.is_empty() is True


async def test_is_empty_checks_the_vanilla_osu_leaderboard() -> None:
    """Only vn!std is probed -- any server with ranked players has it
    populated, so another mode being empty is not evidence of data loss."""
    stats = _FakeStats({0: [RankedPlayerPP(player_id=1, country="us", pp=5000)]})
    ranks = _FakeLeaderboardRanks({"bancho:leaderboard:1": {"1": 1234.0}})
    service = _service(stats, ranks)
    assert await service.is_empty() is True


async def test_is_empty_false_when_leaderboard_populated() -> None:
    ranks = _FakeLeaderboardRanks({"bancho:leaderboard:0": {"1": 1234.0}})
    service = _service(_FakeStats({}), ranks)
    assert await service.is_empty() is False


async def test_rebuild_mode_populates_global_and_country_leaderboards() -> None:
    stats = _FakeStats(
        {
            0: [
                RankedPlayerPP(player_id=1, country="us", pp=5000),
                RankedPlayerPP(player_id=2, country="us", pp=4000),
                RankedPlayerPP(player_id=3, country="ca", pp=3000),
            ],
        },
    )
    ranks = _FakeLeaderboardRanks()
    service = _service(stats, ranks)

    players, leaderboards = await service.rebuild_mode(0)

    assert players == 3
    # global + us + ca
    assert leaderboards == 3

    assert ranks.live["bancho:leaderboard:0"] == {
        "1": 5000.0,
        "2": 4000.0,
        "3": 3000.0,
    }
    assert ranks.live["bancho:leaderboard:0:us"] == {"1": 5000.0, "2": 4000.0}
    assert ranks.live["bancho:leaderboard:0:ca"] == {"3": 3000.0}


async def test_rebuild_mode_paginates_across_batches() -> None:
    """A rebuild larger than one batch must not stop early or loop."""
    rows = [
        RankedPlayerPP(player_id=player_id, country="us", pp=1000 - player_id)
        for player_id in range(1, 11)
    ]
    stats = _FakeStats({0: rows})
    ranks = _FakeLeaderboardRanks()
    service = _service(stats, ranks, batch_size=3)

    players, _ = await service.rebuild_mode(0)

    assert players == 10
    assert len(ranks.live["bancho:leaderboard:0"]) == 10
    # keyset pagination should walk forward, never repeating an offset.
    offsets = [call[2] for call in stats.calls]
    assert offsets == sorted(offsets)
    assert len(offsets) == len(set(offsets))


async def test_rebuild_clears_country_with_no_remaining_players() -> None:
    """The regression this guards: a country whose last ranked player
    left must be *cleared*, not left serving stale ranks."""
    ranks = _FakeLeaderboardRanks(
        {
            "bancho:leaderboard:0": {"1": 5000.0, "9": 100.0},
            "bancho:leaderboard:0:us": {"1": 5000.0},
            # this country no longer has any ranked players.
            "bancho:leaderboard:0:jp": {"9": 100.0},
        },
    )
    stats = _FakeStats({0: [RankedPlayerPP(player_id=1, country="us", pp=5000)]})
    service = _service(stats, ranks)

    await service.rebuild_mode(0)

    assert ranks.live["bancho:leaderboard:0"] == {"1": 5000.0}
    assert ranks.live["bancho:leaderboard:0:us"] == {"1": 5000.0}
    assert "bancho:leaderboard:0:jp" not in ranks.live


async def test_rebuild_mode_with_no_players_clears_leaderboards() -> None:
    ranks = _FakeLeaderboardRanks({"bancho:leaderboard:0": {"1": 5000.0}})
    service = _service(_FakeStats({}), ranks)

    players, _ = await service.rebuild_mode(0)

    assert players == 0
    assert "bancho:leaderboard:0" not in ranks.live


async def test_rebuild_discards_staging_on_failure() -> None:
    """A failed rebuild must not leave staging keys behind, or the next
    run would commit a half-built leaderboard."""

    class _ExplodingStats(_FakeStats):
        async def fetch_ranked_pp_for_mode(
            self,
            *,
            mode: int,
            batch_size: int,
            after_player_id: int = 0,
        ) -> list[RankedPlayerPP]:
            raise RuntimeError("database went away")

    ranks = _FakeLeaderboardRanks()
    service = _service(_ExplodingStats({}), ranks)

    try:
        await service.rebuild_mode(0)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the rebuild to propagate the failure")

    assert ranks.discards, "staging keys should have been discarded"
    assert ranks.staging == {}
    assert ranks.commits == []


async def test_rebuild_all_covers_every_valid_gamemode() -> None:
    stats = _FakeStats({})
    ranks = _FakeLeaderboardRanks()
    service = _service(stats, ranks)

    result = await service.rebuild_all()

    expected_modes = {int(mode) for mode in GameMode.valid_gamemodes()}
    assert result.modes_rebuilt == len(expected_modes)

    # exactly the valid modes -- the unused ones (rx!mania, ap!taiko,
    # ap!catch, ap!mania) never hold scores, so rebuilding them would
    # touch keys that cannot exist.
    rebuilt_modes = {call[0] for call in stats.calls}
    assert rebuilt_modes == expected_modes
    assert int(GameMode.RELAX_MANIA) not in rebuilt_modes
    assert int(GameMode.AUTOPILOT_MANIA) not in rebuilt_modes
