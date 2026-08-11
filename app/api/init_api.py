# #!/usr/bin/env python3.11
from __future__ import annotations

import asyncio
import io
import pprint
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import starlette.routing
from fastapi import FastAPI
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.requests import Request
from fastapi.responses import Response
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import ClientDisconnect

import app.bg_loops
import app.settings
import app.state
import app.utils
from app.api import api_router  # type: ignore[attr-defined]
from app.api import domains
from app.api import health
from app.api import middlewares
from app.api.v2.common.json import ORJSONResponse
from app.bg_task_supervision import cancel_background_tasks
from app.logging import Ansi
from app.logging import log
from app.objects import collections
from app.repositories.leaderboard_ranks import LeaderboardRanksRepository
from app.repositories.stats import StatsRepository
from app.services.leaderboard_recovery import LeaderboardRecoveryService


class BanchoAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        if not self.openapi_schema:
            routes = self.routes
            starlette_hosts = [
                host
                for host in super().routes
                if isinstance(host, starlette.routing.Host)
            ]

            # XXX:HACK fastapi will not show documentation for routes
            # added through use sub applications using the Host class
            # (e.g. app.host('other.domain', app2))
            for host in starlette_hosts:
                for route in host.routes:
                    if route not in routes:
                        routes.append(route)

            self.openapi_schema = get_openapi(
                title=self.title,
                version=self.version,
                openapi_version=self.openapi_version,
                description=self.description,
                terms_of_service=self.terms_of_service,
                contact=self.contact,
                license_info=self.license_info,
                routes=routes,
                tags=self.openapi_tags,
                servers=self.servers,
            )

        return self.openapi_schema


async def _connect_dependencies_with_retry() -> None:
    """Connect to mysql & redis, retrying with exponential backoff.

    Without this, a dependency that is merely *slow* to accept
    connections -- the common case after a host reboot, where docker may
    start us before mysql finishes crash recovery -- crashes the process
    on the first refused connection. A restart policy would still
    converge, but only after repeatedly tearing down and rebuilding the
    whole process.

    Each dependency is tracked independently so a retry does not attempt
    to reconnect one that already succeeded.
    """
    attempts = app.settings.STARTUP_CONNECT_ATTEMPTS
    max_wait = app.settings.STARTUP_CONNECT_MAX_WAIT

    database_connected = False
    redis_connected = False

    for attempt_number in range(1, attempts + 1):
        try:
            if not database_connected:
                await app.state.services.database.connect()
                database_connected = True

            if not redis_connected:
                await app.state.services.redis.initialize()  # type: ignore[unused-awaitable]
                redis_connected = True

            return
        except Exception as exc:
            if attempt_number == attempts:
                log(
                    f"Could not reach dependencies after {attempts} attempts; "
                    "giving up.",
                    Ansi.LRED,
                )
                raise

            # 0.5s, 1s, 2s, 4s, ... capped at STARTUP_CONNECT_MAX_WAIT.
            delay = min(0.5 * (2 ** (attempt_number - 1)), max_wait)
            log(
                f"Dependency connection failed ({exc!r}); retrying in "
                f"{delay:.1f}s (attempt {attempt_number}/{attempts}).",
                Ansi.LYELLOW,
            )
            await asyncio.sleep(delay)


async def _rebuild_leaderboards_if_lost() -> None:
    """Restore the redis leaderboards if they appear to have been lost.

    Ranks are a redis-derived cache of `stats.pp`. If redis comes back
    empty (flush, eviction, restart without a persisted AOF) every player
    reads as unranked and only recovers by logging in again -- so a
    player who never returns stays unranked forever. Detect that here and
    rebuild from mysql, which is the source of truth.

    Failure is non-fatal: a server with stale ranks is far better than a
    server that will not boot.
    """
    service = LeaderboardRecoveryService(
        stats=StatsRepository(app.state.services.database),
        leaderboard_ranks=LeaderboardRanksRepository(app.state.services.redis),
    )

    try:
        if not await service.is_empty():
            return

        log(
            "Leaderboards are empty -- rebuilding from the database.",
            Ansi.LYELLOW,
        )
        await service.rebuild_all()
    except Exception as exc:
        log(f"Failed to rebuild leaderboards: {exc!r}", Ansi.LRED)


@asynccontextmanager
async def lifespan(asgi_app: BanchoAPI) -> AsyncIterator[None]:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    app.utils.ensure_persistent_volumes_are_available()

    app.state.loop = asyncio.get_running_loop()

    if app.utils.is_running_as_admin():
        log(
            "Running the server with root privileges is not recommended.",
            Ansi.LYELLOW,
        )

    await _connect_dependencies_with_retry()

    if app.state.services.datadog is not None:
        app.state.services.datadog.start(  # type: ignore[no-untyped-call]
            flush_in_thread=True,
            flush_interval=15,
        )
        app.state.services.datadog.gauge("bancho.online_players", 0)  # type: ignore[no-untyped-call]

    if app.settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.init(
            dsn=app.settings.SENTRY_DSN,
            release=f"bancho.py@{app.settings.VERSION}",
            # the osu! client closes connections mid-request all the time
            # (it cancels a leaderboard fetch the moment you switch maps);
            # that is expected traffic, not an error to page anyone about.
            ignore_errors=[ClientDisconnect],
        )
        log("Sentry error tracking enabled.", Ansi.LGREEN)

    app.state.services.ip_resolver = app.state.services.IPResolver(
        trusted_proxies=app.settings.TRUSTED_PROXIES,
    )

    await app.state.services.run_sql_migrations()

    # after migrations (the schema must be current to read stats) and
    # before serving traffic, so players are never briefly unranked.
    await _rebuild_leaderboards_if_lost()

    await collections.initialize_ram_caches()

    await app.bg_loops.initialize_housekeeping_tasks()

    log("Startup process complete.", Ansi.LGREEN)
    log(
        f"Listening @ {app.settings.APP_HOST}:{app.settings.APP_PORT}",
        Ansi.LMAGENTA,
    )

    yield

    # ---- shutdown ----
    # mark first so anything checking the flag (e.g. health probes) can
    # start failing readiness before we tear connections down.
    app.state.shutting_down = True

    # let requests already executing finish, rather than pulling the
    # database out from under an in-progress score submission.
    await app.state.inflight.drain(app.settings.SHUTDOWN_DRAIN_TIMEOUT)

    # we want to attempt to gracefully finish any ongoing connections
    # and shut down any of the housekeeping tasks running in the background.
    await app.state.sessions.cancel_housekeeping_tasks()
    await cancel_background_tasks()

    # shutdown services

    await app.state.services.http_client.aclose()
    await app.state.services.database.disconnect()
    await app.state.services.redis.aclose()

    if app.state.services.datadog is not None:
        app.state.services.datadog.stop()  # type: ignore[no-untyped-call]
        app.state.services.datadog.flush()  # type: ignore[no-untyped-call]


def init_exception_handlers(asgi_app: BanchoAPI) -> None:
    @asgi_app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        """Wrapper around 422 validation errors to print out info for devs."""
        log(f"Validation error on {request.url}", Ansi.LRED)
        pprint.pprint(exc.errors())

        return ORJSONResponse(
            content={"detail": jsonable_encoder(exc.errors())},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )


def init_middlewares(asgi_app: BanchoAPI) -> None:
    """Initialize our app's middleware stack."""
    asgi_app.add_middleware(middlewares.MetricsMiddleware)

    @asgi_app.middleware("http")
    async def http_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # if an osu! client is waiting on leaderboard data
        # and switches to another leaderboard, it will cancel
        # the previous request midway, resulting in a large
        # error in the console. this is to catch that :)

        try:
            return await call_next(request)
        except ClientDisconnect:
            # client disconnected from the server
            # while we were reading the body.
            return Response("Client is stupppod")
        except RuntimeError as exc:
            if exc.args[0] == "No response returned.":
                # client disconnected from the server
                # while we were sending the response.
                return Response("Client is stupppod")

            # unrelated issue, raise normally
            raise exc


def init_routes(asgi_app: BanchoAPI) -> None:
    """Initialize our app's route endpoints."""
    # health/metrics are registered at the app root (not on a Host), so a
    # container healthcheck hitting 127.0.0.1/:port/health answers even
    # though every other route is bound to a hostname.
    asgi_app.include_router(health.router)

    for domain in ("ppy.sh", app.settings.DOMAIN):
        for subdomain in ("c", "ce", "c4", "c5", "c6"):
            asgi_app.host(f"{subdomain}.{domain}", domains.cho.router)

        asgi_app.host(f"osu.{domain}", domains.osu.router)
        asgi_app.host(f"b.{domain}", domains.map.router)

        # bancho.py's developer-facing api
        asgi_app.host(f"api.{domain}", api_router)


def init_api() -> BanchoAPI:
    """Create & initialize our app."""
    asgi_app = BanchoAPI(lifespan=lifespan)

    init_middlewares(asgi_app)
    init_exception_handlers(asgi_app)
    init_routes(asgi_app)

    return asgi_app


asgi_app = init_api()
