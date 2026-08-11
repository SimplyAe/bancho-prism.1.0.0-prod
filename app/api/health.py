"""Health & readiness endpoints.

The container healthcheck probes `/health`; orchestrators and load
balancers probe `/ready`. The distinction matters:

- **liveness** (`/health`) -- "is the process alive?" A hung event loop
  returns this fine, which is exactly why the docker healthcheck uses it:
  we do not want a transiently-busy-but-alive server to be restarted.

- **readiness** (`/ready`) -- "can it actually serve traffic?" Each
  dependency is checked, so a server whose mysql or redis just went down
  fails readiness and is pulled out of rotation, without a restart.

Both endpoints are registered on the app root (not on a Host), so they
answer regardless of the Host header.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response

import app.metrics
import app.settings
import app.state
from app.adapters.database import Database
from app.logging import Ansi
from app.logging import log


async def _check_mysql() -> tuple[bool, str]:
    database: Database = app.state.services.database
    try:
        # the cheapest round-trip that proves the pool can serve a query.
        await database.fetch_val("SELECT 1")
        return True, "ok"
    except Exception as exc:
        return False, repr(exc)


async def _check_redis() -> tuple[bool, str]:
    redis = app.state.services.redis
    try:
        await redis.ping()
        return True, "ok"
    except Exception as exc:
        return False, repr(exc)


async def _check_replays_volume() -> tuple[bool, str]:
    # the replay directory must exist and be writable, or score
    # submission will start failing in a way the databases won't show.
    from app.api.dependencies import REPLAYS_PATH

    if not REPLAYS_PATH.is_dir():
        return False, f"missing: {REPLAYS_PATH}"
    try:
        probe = REPLAYS_PATH / ".write-probe"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        return True, "ok"
    except OSError as exc:
        return False, repr(exc)


async def check_dependencies() -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}

    for name, check in (
        ("mysql", _check_mysql),
        ("redis", _check_redis),
        ("replays", _check_replays_volume),
    ):
        try:
            healthy, detail = await check()
        except Exception as exc:  # a check itself must never crash the probe
            healthy, detail = False, repr(exc)

        checks[name] = {"healthy": healthy, "detail": detail}
        if not healthy:
            log(
                f"Readiness check {name!r} failed: {detail}",
                Ansi.LRED,
            )

    return checks


router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness probe: the process is up."""
    return JSONResponse({"status": "ok"})


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe: dependencies are reachable and volumes writable."""
    # refuse readiness while draining so orchestrators stop routing new
    # traffic to a server that is about to disappear.
    if app.state.shutting_down:
        return JSONResponse(
            content={"status": "draining", "checks": {}},
            status_code=503,
        )

    checks = await check_dependencies()
    healthy = all(check["healthy"] for check in checks.values())

    return JSONResponse(
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
        status_code=200 if healthy else 503,
    )


@router.get("/metrics")
async def prometheus_metrics(request: Request) -> Response:
    """Prometheus scrape endpoint.

    SECURITY: this router is mounted on the app root so probes work
    without a Host header, which also means it answers on the public
    hostnames. Metrics expose operational data (player counts, request
    volumes, error rates), so the endpoint is gated: it serves only when
    METRICS_TOKEN is configured and presented as a bearer token. Leaving
    METRICS_TOKEN empty disables the endpoint entirely rather than
    silently publishing it.
    """
    expected_token = app.settings.METRICS_TOKEN
    if not expected_token:
        return Response(status_code=404)

    supplied = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not supplied.startswith(prefix) or not secrets.compare_digest(
        supplied[len(prefix) :],
        expected_token,
    ):
        return Response(status_code=401)

    return Response(
        content=app.metrics.render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
