from __future__ import annotations

import random
import secrets
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import cast

import httpx
from fastapi import Depends

import app.packets
import app.state.services
import app.state.sessions
import app.utils
from app import settings
from app import state
from app.bg_task_supervision import spawn_background_task
from app.discord import Embed
from app.discord import Webhook
from app.objects.beatmap import Beatmap
from app.objects.beatmap import ensure_osu_file_is_available
from app.objects.player import Player
from app.objects.score import Score
from app.repositories.achievements import AchievementsRepository
from app.repositories.activity_events import ActivityEventsRepository
from app.repositories.anticheat_flags import AnticheatFlagsRepository
from app.repositories.clans import ClansRepository
from app.repositories.client_hashes import ClientHashesRepository
from app.repositories.comments import CommentsRepository
from app.repositories.discord_oauth_state import DiscordOAuthStateRepository
from app.repositories.favourites import FavouritesRepository
from app.repositories.ingame_logins import IngameLoginsRepository
from app.repositories.leaderboard_ranks import LeaderboardRanksRepository
from app.repositories.logs import LogsRepository
from app.repositories.mail import MailRepository
from app.repositories.maps import MapsRepository
from app.repositories.mp_matches import MpMatchesRepository
from app.repositories.ratings import RatingsRepository
from app.repositories.relationships import RelationshipsRepository
from app.repositories.replay_analysis_queue import ReplayAnalysisQueue
from app.repositories.score_replay_stats import ScoreReplayStatsRepository
from app.repositories.scores import FirstPlaceScore
from app.repositories.scores import ScoresRepository
from app.repositories.spectator_sessions import SpectatorSessionsRepository
from app.repositories.stat_snapshots import StatSnapshotsRepository
from app.repositories.stats import StatsRepository
from app.repositories.tourney_pool_maps import TourneyPoolMapsRepository
from app.repositories.tourney_pools import TourneyPoolsRepository
from app.repositories.user_achievements import UserAchievementsRepository
from app.repositories.user_discord_links import DiscordLinksRepository
from app.repositories.users import UsersRepository
from app.repositories.web_sessions import WebSessionsRepository
from app.services.account_settings import AccountSettingsService
from app.services.accounts import AccountRegistrationService
from app.services.anticheat.flag_review_queue import AnticheatReviewService
from app.services.avatars import AvatarsService
from app.services.bancho import BanchoAuthenticationService
from app.services.bancho import BanchoLoginService
from app.services.beatmap_leaderboards import BeatmapLeaderboardService
from app.services.captcha import CaptchaService
from app.services.clans import ClansService
from app.services.client_integrity import ClientIntegrityService
from app.services.comments import CommentsService
from app.services.direct_search import DirectSearchParams
from app.services.direct_search import DirectSearchService
from app.services.discord.first_place_bridge import announce_first_place_to_discord
from app.services.favourites import FavouritesService
from app.services.leaderboard_recovery import LeaderboardRecoveryService
from app.services.mail import MailReadService
from app.services.maps import BeatmapInfoService
from app.services.maps import BeatmapRatingService
from app.services.maps import BeatmapSetService
from app.services.maps import MapsService
from app.services.multiplayer.match_history_reader import MatchHistoryService
from app.services.performance import PerformanceService
from app.services.player_leaderboards import PlayerLeaderboardsService
from app.services.players import PlayersService
from app.services.relationships import RelationshipsService
from app.services.replays import ReplayService
from app.services.score_leaderboards import ScoreLeaderboardsService
from app.services.score_submission import ScoreSubmissionService
from app.services.score_submission import SubmittedScore
from app.services.scores import ScoresService
from app.services.screenshots import ScreenshotService
from app.services.social.activity_feed import ActivityFeedService
from app.services.social.activity_producers import publish_score_submission_activity
from app.services.social.discord_linking import DiscordLinkingService
from app.services.spectator.spectator_history_reader import SpectatorHistoryService
from app.services.stat_snapshots import StatSnapshotService
from app.services.tourney_pools import TourneyPoolsService
from app.services.web_sessions import WebSessionsService

AVATARS_PATH = Path.cwd() / ".data/avatars"
SCREENSHOTS_PATH = Path.cwd() / ".data/ss"
REPLAYS_PATH = Path.cwd() / ".data/osr"


async def _fetch_mirror_search(
    url: str,
    *,
    params: DirectSearchParams,
) -> httpx.Response:
    http_params: dict[str, str | int | float | bool | None] = {
        "amount": params["amount"],
        "offset": params["offset"],
    }
    if "query" in params:
        http_params["query"] = params["query"]
    if "mode" in params:
        http_params["mode"] = params["mode"]
    if "status" in params:
        http_params["status"] = params["status"]

    return await app.state.services.http_client.get(url, params=http_params)


def _increment_metric(metric: str) -> None:
    if app.state.services.datadog:
        app.state.services.datadog.increment(metric)  # type: ignore[no-untyped-call]


def _send_notification(player: Player, message: str) -> None:
    player.enqueue(app.packets.notification(message))


def _publish_user_stats(player: Player) -> None:
    app.state.sessions.players.enqueue(app.packets.user_stats(player))


async def _record_strange_occurrence_stacktrace() -> None:
    stacktrace = app.utils.get_appropriate_stacktrace()
    await app.state.services.log_strange_occurrence(stacktrace)


def _schedule_replay_view_increment(score: Score) -> None:
    _ = spawn_background_task(
        score.increment_replay_views(),
        name="increment-replay-views",
    )


def _schedule_replay_analysis(score_id: int, mode: int) -> None:
    """Fire-and-forget enqueue of a committed score for anticheat analysis.

    Score submission is latency-sensitive, so the LPUSH is spawned as a
    supervised background task rather than awaited on the hot path. A dropped
    enqueue (redis briefly down, process killed before the push) is not a
    correctness problem: the worker's backfill scan re-derives the score from
    the durable ``score_replay_stats`` table on its next pass.
    """
    queue = get_replay_analysis_queue()
    _ = spawn_background_task(
        queue.enqueue(score_id, mode),
        name="enqueue-replay-analysis",
    )


def _publish_activity_events(submitted: SubmittedScore) -> None:
    """Fire-and-forget publish of a finished submission to the activity feed.

    Like ``_schedule_replay_analysis``, this runs off the latency-sensitive
    submission path as a supervised background task. The feed is a convenience
    log, not a source of truth, so a dropped publish (redis/mysql blip, process
    killed) is harmless -- the durable ``scores``/``stats`` rows are unaffected,
    and the producer itself swallows and logs its own errors.
    """
    feed_service = ActivityFeedService(
        events=get_activity_events_repository(),
        relationships=get_relationships_repository(),
    )
    _ = spawn_background_task(
        publish_score_submission_activity(feed_service, submitted),
        name="publish-activity-events",
    )


async def _post_first_place_embed(embed: Embed) -> None:
    """Post a single embed to the configured #1-score webhook."""
    webhook = Webhook(settings.DISCORD_FIRST_PLACE_WEBHOOK, embeds=[embed])
    await webhook.post()


def _announce_first_place_to_discord(
    score: Score,
    previous_first_place_score: FirstPlaceScore | None,
) -> None:
    """Fire-and-forget mirror of an in-game #1 to a Discord webhook.

    Like ``_publish_activity_events``, this runs off the submission hot path as
    a supervised background task. Skipped entirely when no webhook is configured,
    so the default deployment pays nothing; the producer itself swallows and logs
    any post failure so a bad webhook never touches the committed score.
    """
    if not settings.DISCORD_FIRST_PLACE_WEBHOOK:
        return

    _ = spawn_background_task(
        announce_first_place_to_discord(
            _post_first_place_embed,
            score,
            previous_first_place_score=previous_first_place_score,
        ),
        name="announce-first-place-discord",
    )


async def _post_captcha_siteverify(url: str, data: dict[str, str]) -> dict[str, Any]:
    response = await app.state.services.http_client.post(url, data=data)
    response.raise_for_status()
    return cast("dict[str, Any]", response.json())


def _generate_web_session_token() -> str:
    return secrets.token_urlsafe(32)


async def _post_discord_token_exchange(
    url: str,
    data: dict[str, str],
) -> dict[str, Any]:
    response = await app.state.services.http_client.post(url, data=data)
    response.raise_for_status()
    return cast("dict[str, Any]", response.json())


async def _fetch_discord_identity(
    url: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = await app.state.services.http_client.get(url, headers=headers)
    response.raise_for_status()
    return cast("dict[str, Any]", response.json())


def _generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def _utc_today() -> date:
    """The current UTC calendar day for filing a stat snapshot.

    UTC so the day boundary is identical across processes regardless of host
    timezone -- the same reason the daily loop uses it.
    """
    return datetime.now(timezone.utc).date()


def get_achievements_repository() -> AchievementsRepository:
    return AchievementsRepository(app.state.services.database)


def get_activity_events_repository() -> ActivityEventsRepository:
    return ActivityEventsRepository(app.state.services.database)


def get_clans_repository() -> ClansRepository:
    return ClansRepository(app.state.services.database)


def get_client_hashes_repository() -> ClientHashesRepository:
    return ClientHashesRepository(app.state.services.database)


def get_comments_repository() -> CommentsRepository:
    return CommentsRepository(app.state.services.database)


def get_discord_links_repository() -> DiscordLinksRepository:
    return DiscordLinksRepository(app.state.services.database)


def get_discord_oauth_state_repository() -> DiscordOAuthStateRepository:
    return DiscordOAuthStateRepository(app.state.services.redis)


def get_favourites_repository() -> FavouritesRepository:
    return FavouritesRepository(app.state.services.database)


def get_ingame_logins_repository() -> IngameLoginsRepository:
    return IngameLoginsRepository(app.state.services.database)


def get_mail_repository() -> MailRepository:
    return MailRepository(app.state.services.database)


def get_leaderboard_ranks_repository() -> LeaderboardRanksRepository:
    return LeaderboardRanksRepository(app.state.services.redis)


def get_logs_repository() -> LogsRepository:
    return LogsRepository(app.state.services.database)


def get_replay_analysis_queue() -> ReplayAnalysisQueue:
    return ReplayAnalysisQueue(app.state.services.redis)


def get_score_replay_stats_repository() -> ScoreReplayStatsRepository:
    return ScoreReplayStatsRepository(app.state.services.database)


def get_anticheat_flags_repository() -> AnticheatFlagsRepository:
    return AnticheatFlagsRepository(app.state.services.database)


def get_stat_snapshots_repository() -> StatSnapshotsRepository:
    return StatSnapshotsRepository(app.state.services.database)


def get_maps_repository() -> MapsRepository:
    return MapsRepository(app.state.services.database)


def get_mp_matches_repository() -> MpMatchesRepository:
    return MpMatchesRepository(app.state.services.database)


def get_ratings_repository() -> RatingsRepository:
    return RatingsRepository(app.state.services.database)


def get_relationships_repository() -> RelationshipsRepository:
    return RelationshipsRepository(app.state.services.database)


def get_scores_repository() -> ScoresRepository:
    return ScoresRepository(app.state.services.database)


def get_spectator_sessions_repository() -> SpectatorSessionsRepository:
    return SpectatorSessionsRepository(app.state.services.database)


def get_stats_repository() -> StatsRepository:
    return StatsRepository(app.state.services.database)


def get_tourney_pool_maps_repository() -> TourneyPoolMapsRepository:
    return TourneyPoolMapsRepository(app.state.services.database)


def get_tourney_pools_repository() -> TourneyPoolsRepository:
    return TourneyPoolsRepository(app.state.services.database)


def get_user_achievements_repository() -> UserAchievementsRepository:
    return UserAchievementsRepository(app.state.services.database)


def get_users_repository() -> UsersRepository:
    return UsersRepository(app.state.services.database)


def get_web_sessions_repository() -> WebSessionsRepository:
    return WebSessionsRepository(app.state.services.redis)


def get_clans_service(
    clans: Annotated[ClansRepository, Depends(get_clans_repository)],
    users: Annotated[UsersRepository, Depends(get_users_repository)],
) -> ClansService:
    return ClansService(
        clans=clans,
        users=users,
        online_players=app.state.sessions.players,
        database=app.state.services.database,
    )


def get_bancho_authentication_service(
    users: Annotated[UsersRepository, Depends(get_users_repository)],
) -> BanchoAuthenticationService:
    return BanchoAuthenticationService(
        users=users,
        online_players=app.state.sessions.players,
        password_cache=state.cache.bcrypt,
    )


def get_bancho_login_service(
    authentication: Annotated[
        BanchoAuthenticationService,
        Depends(get_bancho_authentication_service),
    ],
    users: Annotated[UsersRepository, Depends(get_users_repository)],
    ingame_logins: Annotated[
        IngameLoginsRepository,
        Depends(get_ingame_logins_repository),
    ],
    client_hashes: Annotated[
        ClientHashesRepository,
        Depends(get_client_hashes_repository),
    ],
    mail: Annotated[MailRepository, Depends(get_mail_repository)],
) -> BanchoLoginService:
    return BanchoLoginService(
        authentication=authentication,
        users=users,
        ingame_logins=ingame_logins,
        client_hashes=client_hashes,
        mail=mail,
    )


def get_maps_service(
    maps: Annotated[MapsRepository, Depends(get_maps_repository)],
) -> MapsService:
    return MapsService(maps=maps)


def get_account_registration_service(
    users: Annotated[UsersRepository, Depends(get_users_repository)],
    stats: Annotated[StatsRepository, Depends(get_stats_repository)],
) -> AccountRegistrationService:
    return AccountRegistrationService(
        users=users,
        stats=stats,
        database=app.state.services.database,
        password_cache=state.cache.bcrypt,
        ip_resolver=app.state.services.ip_resolver,
        fetch_geoloc=app.state.services.fetch_geoloc,
        increment_metric=_increment_metric,
        ingame_registration_disallowed=settings.DISALLOW_INGAME_REGISTRATION,
        disallowed_names=settings.DISALLOWED_NAMES,
        disallowed_passwords=settings.DISALLOWED_PASSWORDS,
    )


def get_account_settings_service(
    users: Annotated[UsersRepository, Depends(get_users_repository)],
    stats: Annotated[StatsRepository, Depends(get_stats_repository)],
    leaderboard_ranks: Annotated[
        LeaderboardRanksRepository,
        Depends(get_leaderboard_ranks_repository),
    ],
    bancho_authentication: Annotated[
        BanchoAuthenticationService,
        Depends(get_bancho_authentication_service),
    ],
) -> AccountSettingsService:
    return AccountSettingsService(
        users=users,
        stats=stats,
        leaderboard_ranks=leaderboard_ranks,
        authentication=bancho_authentication,
        online_players=app.state.sessions.players,
        password_cache=state.cache.bcrypt,
        disallowed_names=settings.DISALLOWED_NAMES,
        disallowed_passwords=settings.DISALLOWED_PASSWORDS,
    )


def get_avatars_service() -> AvatarsService:
    return AvatarsService(avatars_path=AVATARS_PATH)


def get_screenshot_service() -> ScreenshotService:
    return ScreenshotService(
        screenshots_path=SCREENSHOTS_PATH,
        token_urlsafe=secrets.token_urlsafe,
        log_strange_occurrence=app.state.services.log_strange_occurrence,
    )


def get_client_integrity_service() -> ClientIntegrityService:
    return ClientIntegrityService(
        restriction_admin=app.state.sessions.bot,
        restriction_roll=random.randrange,
        send_notification=_send_notification,
    )


def get_direct_search_service() -> DirectSearchService:
    return DirectSearchService(
        mirror_search_endpoint=settings.MIRROR_SEARCH_ENDPOINT,
        fetch_mirror_search=_fetch_mirror_search,
    )


def get_beatmap_info_service(
    maps: Annotated[MapsRepository, Depends(get_maps_repository)],
    scores: Annotated[ScoresRepository, Depends(get_scores_repository)],
) -> BeatmapInfoService:
    return BeatmapInfoService(maps=maps, scores=scores)


def get_beatmap_rating_service(
    ratings: Annotated[RatingsRepository, Depends(get_ratings_repository)],
) -> BeatmapRatingService:
    return BeatmapRatingService(
        ratings=ratings,
        beatmap_cache=state.cache.beatmap,
    )


def get_beatmap_set_service(
    maps: Annotated[MapsRepository, Depends(get_maps_repository)],
) -> BeatmapSetService:
    return BeatmapSetService(maps=maps)


def get_comments_service(
    comments: Annotated[CommentsRepository, Depends(get_comments_repository)],
) -> CommentsService:
    return CommentsService(comments=comments)


def get_favourites_service(
    favourites: Annotated[FavouritesRepository, Depends(get_favourites_repository)],
) -> FavouritesService:
    return FavouritesService(favourites=favourites)


def get_mail_read_service(
    mail: Annotated[MailRepository, Depends(get_mail_repository)],
) -> MailReadService:
    return MailReadService(
        mail=mail,
        players=app.state.sessions.players,
    )


def get_replay_service(
    scores: Annotated[ScoresRepository, Depends(get_scores_repository)],
) -> ReplayService:
    return ReplayService(
        replays_path=REPLAYS_PATH,
        fetch_score=Score.from_sql,
        fetch_replay_header=scores.fetch_replay_header,
        schedule_replay_view_increment=_schedule_replay_view_increment,
    )


def get_player_leaderboards_service(
    stats: Annotated[StatsRepository, Depends(get_stats_repository)],
    leaderboard_ranks: Annotated[
        LeaderboardRanksRepository,
        Depends(get_leaderboard_ranks_repository),
    ],
) -> PlayerLeaderboardsService:
    return PlayerLeaderboardsService(
        stats=stats,
        leaderboard_ranks=leaderboard_ranks,
    )


def get_leaderboard_recovery_service(
    stats: Annotated[StatsRepository, Depends(get_stats_repository)],
    leaderboard_ranks: Annotated[
        LeaderboardRanksRepository,
        Depends(get_leaderboard_ranks_repository),
    ],
) -> LeaderboardRecoveryService:
    return LeaderboardRecoveryService(
        stats=stats,
        leaderboard_ranks=leaderboard_ranks,
    )


def get_stat_snapshot_service(
    stat_snapshots: Annotated[
        StatSnapshotsRepository,
        Depends(get_stat_snapshots_repository),
    ],
) -> StatSnapshotService:
    return StatSnapshotService(
        capture_mode=stat_snapshots.capture_mode,
        today=_utc_today,
    )


def get_players_service(
    users: Annotated[UsersRepository, Depends(get_users_repository)],
    stats: Annotated[StatsRepository, Depends(get_stats_repository)],
    player_leaderboards: Annotated[
        PlayerLeaderboardsService,
        Depends(get_player_leaderboards_service),
    ],
) -> PlayersService:
    return PlayersService(
        users=users,
        stats=stats,
        online_players=app.state.sessions.players,
        player_leaderboards=player_leaderboards,
    )


def get_performance_service() -> PerformanceService:
    return PerformanceService()


def get_tourney_pools_service(
    tourney_pools: Annotated[
        TourneyPoolsRepository,
        Depends(get_tourney_pools_repository),
    ],
    tourney_pool_maps: Annotated[
        TourneyPoolMapsRepository,
        Depends(get_tourney_pool_maps_repository),
    ],
) -> TourneyPoolsService:
    return TourneyPoolsService(
        tourney_pools=tourney_pools,
        tourney_pool_maps=tourney_pool_maps,
        database=app.state.services.database,
    )


def get_score_leaderboards_service(
    scores: Annotated[ScoresRepository, Depends(get_scores_repository)],
) -> ScoreLeaderboardsService:
    return ScoreLeaderboardsService(scores=scores)


def get_beatmap_leaderboard_service(
    score_leaderboards: Annotated[
        ScoreLeaderboardsService,
        Depends(get_score_leaderboards_service),
    ],
    clans: Annotated[ClansRepository, Depends(get_clans_repository)],
    maps: Annotated[MapsRepository, Depends(get_maps_repository)],
    ratings: Annotated[RatingsRepository, Depends(get_ratings_repository)],
) -> BeatmapLeaderboardService:
    return BeatmapLeaderboardService(
        score_leaderboards=score_leaderboards,
        clans=clans,
        maps=maps,
        ratings=ratings,
        beatmap_fetcher=Beatmap.from_md5,
        unsubmitted_cache=app.state.cache.unsubmitted,
        needs_update_cache=app.state.cache.needs_update,
        beatmapset_cache=app.state.cache.beatmapset,
        publish_user_stats=_publish_user_stats,
        increment_metric=_increment_metric,
        log_strange_occurrence=app.state.services.log_strange_occurrence,
        get_appropriate_stacktrace=app.utils.get_appropriate_stacktrace,
    )


def get_score_submission_service(
    bancho_authentication: Annotated[
        BanchoAuthenticationService,
        Depends(get_bancho_authentication_service),
    ],
    scores: Annotated[ScoresRepository, Depends(get_scores_repository)],
    stats: Annotated[StatsRepository, Depends(get_stats_repository)],
    maps: Annotated[MapsRepository, Depends(get_maps_repository)],
    achievements: Annotated[
        AchievementsRepository,
        Depends(get_achievements_repository),
    ],
    user_achievements: Annotated[
        UserAchievementsRepository,
        Depends(get_user_achievements_repository),
    ],
) -> ScoreSubmissionService:
    return ScoreSubmissionService(
        replays_path=REPLAYS_PATH,
        restriction_admin=app.state.sessions.bot,
        fetch_beatmap=Beatmap.from_md5,
        bancho_authentication=bancho_authentication,
        score_submission_locks=app.state.score_submission_locks,
        database=app.state.services.database,
        scores=scores,
        stats=stats,
        maps=maps,
        achievements=achievements,
        user_achievements=user_achievements,
        ensure_osu_file_is_available=ensure_osu_file_is_available,
        publish_user_stats=_publish_user_stats,
        send_personal_best_notification=_send_notification,
        announce_channel=app.state.sessions.channels.get_by_name("#announce"),
        domain=settings.DOMAIN,
        increment_metric=_increment_metric,
        record_submission_integrity_failure=_record_strange_occurrence_stacktrace,
        schedule_replay_analysis=_schedule_replay_analysis,
        publish_activity_events=_publish_activity_events,
        announce_first_place_to_discord=_announce_first_place_to_discord,
    )


def get_relationships_service(
    relationships: Annotated[
        RelationshipsRepository,
        Depends(get_relationships_repository),
    ],
    users: Annotated[UsersRepository, Depends(get_users_repository)],
) -> RelationshipsService:
    return RelationshipsService(
        relationships=relationships,
        users=users,
        online_players=app.state.sessions.players,
    )


def get_scores_service(
    scores: Annotated[ScoresRepository, Depends(get_scores_repository)],
    users: Annotated[UsersRepository, Depends(get_users_repository)],
    clans: Annotated[ClansRepository, Depends(get_clans_repository)],
) -> ScoresService:
    return ScoresService(
        scores=scores,
        users=users,
        clans=clans,
        fetch_beatmap=Beatmap.from_md5,
    )


def get_captcha_service() -> CaptchaService:
    # NOTE: the provider name is validated at startup by
    # `app.settings.Settings` (a `Literal`), so an unsupported value can no
    # longer reach this point -- it fails on boot rather than on the first
    # registration attempt.
    return CaptchaService(
        provider=settings.CAPTCHA_PROVIDER,
        secret=settings.CAPTCHA_SECRET,
        post_siteverify=_post_captcha_siteverify,
    )


def get_web_sessions_service(
    bancho_authentication: Annotated[
        BanchoAuthenticationService,
        Depends(get_bancho_authentication_service),
    ],
    users: Annotated[UsersRepository, Depends(get_users_repository)],
    web_sessions: Annotated[
        WebSessionsRepository,
        Depends(get_web_sessions_repository),
    ],
) -> WebSessionsService:
    return WebSessionsService(
        authentication=bancho_authentication,
        users=users,
        web_sessions=web_sessions,
        generate_token=_generate_web_session_token,
    )


def get_anticheat_review_service(
    anticheat_flags: Annotated[
        AnticheatFlagsRepository,
        Depends(get_anticheat_flags_repository),
    ],
    logs: Annotated[LogsRepository, Depends(get_logs_repository)],
) -> AnticheatReviewService:
    return AnticheatReviewService(flags=anticheat_flags, logs=logs)


def get_discord_linking_service(
    links: Annotated[
        DiscordLinksRepository,
        Depends(get_discord_links_repository),
    ],
    oauth_state: Annotated[
        DiscordOAuthStateRepository,
        Depends(get_discord_oauth_state_repository),
    ],
) -> DiscordLinkingService:
    return DiscordLinkingService(
        client_id=settings.DISCORD_OAUTH_CLIENT_ID,
        client_secret=settings.DISCORD_OAUTH_CLIENT_SECRET,
        redirect_uri=settings.DISCORD_OAUTH_REDIRECT_URI,
        links=links,
        oauth_state=oauth_state,
        exchange_code=_post_discord_token_exchange,
        fetch_identity=_fetch_discord_identity,
        generate_state=_generate_oauth_state,
    )


def get_activity_feed_service(
    activity_events: Annotated[
        ActivityEventsRepository,
        Depends(get_activity_events_repository),
    ],
    relationships: Annotated[
        RelationshipsRepository,
        Depends(get_relationships_repository),
    ],
) -> ActivityFeedService:
    return ActivityFeedService(
        events=activity_events,
        relationships=relationships,
    )


def get_match_history_service(
    mp_matches: Annotated[
        MpMatchesRepository,
        Depends(get_mp_matches_repository),
    ],
) -> MatchHistoryService:
    return MatchHistoryService(matches=mp_matches)


def get_spectator_history_service(
    spectator_sessions: Annotated[
        SpectatorSessionsRepository,
        Depends(get_spectator_sessions_repository),
    ],
) -> SpectatorHistoryService:
    return SpectatorHistoryService(sessions=spectator_sessions)
