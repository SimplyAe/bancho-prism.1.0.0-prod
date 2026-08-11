"""Rebuilding the redis leaderboards from mysql.

Player ranks are served from redis sorted sets, but the pp they are
derived from lives in mysql. Redis is therefore a *cache* -- yet nothing
in the server could rebuild it. If redis lost its data (a flush, an
eviction, a restart without a persisted AOF), every player's global and
country rank silently read as unranked, and stayed that way until each
player happened to log in or submit a score again. There was no way to
recover the ranks of a player who did neither.

This service reconstructs the sorted sets from mysql, which is the source
of truth. It is safe to run at any time: it stages into temporary keys
and swaps them in atomically, so live traffic never observes a partially
rebuilt leaderboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants.gamemodes import GameMode
from app.logging import Ansi
from app.logging import log
from app.repositories.leaderboard_ranks import LeaderboardRanksRepository
from app.repositories.stats import StatsRepository

# how many rows to pull from mysql (and pipeline into redis) at a time.
# large enough to keep round-trips down, small enough that a rebuild
# doesn't hold a huge result set in memory.
DEFAULT_BATCH_SIZE = 5_000


@dataclass(frozen=True, slots=True)
class RebuildResult:
    modes_rebuilt: int
    players_ranked: int
    leaderboards_written: int


@dataclass(frozen=True)
class LeaderboardRecoveryService:
    stats: StatsRepository
    leaderboard_ranks: LeaderboardRanksRepository
    batch_size: int = DEFAULT_BATCH_SIZE

    async def is_empty(self) -> bool:
        """Whether the leaderboards look like they have been lost.

        Used at startup to decide whether an automatic rebuild is
        warranted. Two things must both hold for that to be data loss:

        - the vn!std global leaderboard is empty. Any server with ranked
          players has that set populated, so its being empty is the
          symptom we are looking for.
        - mysql *does* have at least one ranked vn!std player. Without
          this a brand-new install -- legitimately empty on both sides --
          would report loss and rebuild on every single boot.
        """
        mode = int(GameMode.VANILLA_OSU)

        key = self.leaderboard_ranks.global_key(mode)
        if await self.leaderboard_ranks.count_ranked(key) != 0:
            return False

        # cheapest possible existence probe: one row, same ranked-player
        # predicate the rebuild itself uses.
        ranked_players = await self.stats.fetch_ranked_pp_for_mode(
            mode=mode,
            batch_size=1,
        )
        return bool(ranked_players)

    async def rebuild_mode(self, mode: int) -> tuple[int, int]:
        """Rebuild every leaderboard for a single mode.

        Returns (players_ranked, leaderboards_written).
        """
        global_key = self.leaderboard_ranks.global_key(mode)

        # which keys to commit at the end: everything we staged *plus*
        # every key that already exists, so a country leaderboard whose
        # last ranked player left gets cleared rather than left stale.
        touched_keys = await self.leaderboard_ranks.fetch_existing_keys(mode)
        players_ranked = 0
        after_player_id = 0

        try:
            while True:
                batch = await self.stats.fetch_ranked_pp_for_mode(
                    mode=mode,
                    batch_size=self.batch_size,
                    after_player_id=after_player_id,
                )
                if not batch:
                    break

                global_scores: dict[str, float] = {}
                country_scores: dict[str, dict[str, float]] = {}

                for row in batch:
                    member = str(row.player_id)
                    global_scores[member] = float(row.pp)

                    country_key = self.leaderboard_ranks.country_key(
                        mode,
                        row.country,
                    )
                    country_scores.setdefault(country_key, {})[member] = float(row.pp)

                await self.leaderboard_ranks.stage_scores(global_key, global_scores)
                for country_key, scores in country_scores.items():
                    if country_key not in touched_keys:
                        touched_keys.append(country_key)
                    await self.leaderboard_ranks.stage_scores(country_key, scores)

                players_ranked += len(batch)
                after_player_id = batch[-1].player_id

                if len(batch) < self.batch_size:
                    break

            await self.leaderboard_ranks.commit_staged(touched_keys)
        except Exception:
            # never leave staging keys behind to be picked up (and
            # committed) by a later run.
            await self.leaderboard_ranks.discard_staging(touched_keys)
            raise

        return players_ranked, len(touched_keys)

    async def rebuild_all(self) -> RebuildResult:
        """Rebuild the leaderboards for every gamemode."""
        log("Rebuilding leaderboards from the database...", Ansi.LCYAN)

        modes_rebuilt = 0
        players_ranked = 0
        leaderboards_written = 0

        # skips the modes bancho.py never assigns scores to (rx!mania,
        # ap!taiko/catch/mania), so we don't touch keys that can't exist.
        for mode in GameMode.valid_gamemodes():
            mode_players, mode_leaderboards = await self.rebuild_mode(int(mode))

            modes_rebuilt += 1
            players_ranked += mode_players
            leaderboards_written += mode_leaderboards

        log(
            f"Rebuilt {leaderboards_written} leaderboard(s) across "
            f"{modes_rebuilt} mode(s) ({players_ranked} ranked entries).",
            Ansi.LGREEN,
        )

        return RebuildResult(
            modes_rebuilt=modes_rebuilt,
            players_ranked=players_ranked,
            leaderboards_written=leaderboards_written,
        )
