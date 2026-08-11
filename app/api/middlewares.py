from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app import metrics
from app.logging import Ansi
from app.logging import log
from app.logging import magnitude_fmt_time
from app.state import inflight


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        start_time = time.perf_counter_ns()
        # tracked so shutdown can wait for in-flight work instead of
        # severing connections mid-response.
        inflight.increment()
        metrics.http_requests_in_flight.inc()
        try:
            response = await call_next(request)
        finally:
            inflight.decrement()
            metrics.http_requests_in_flight.dec()
        end_time = time.perf_counter_ns()

        time_elapsed = end_time - start_time

        col = Ansi.LGREEN if response.status_code < 400 else Ansi.LRED

        # NOTE: `.get`, not `[...]` -- a request without a Host header
        # (HTTP/1.0, or a bare healthcheck) would otherwise raise a
        # KeyError from inside the middleware and fail the request.
        host = request.headers.get("host", "-")
        url = f"{host}{request['path']}"

        # label by host rather than path: bancho.py is host-routed
        # (osu./c./b./api.), and paths carry ids that would explode
        # prometheus cardinality.
        metrics.http_requests_total.labels(
            method=request.method,
            host=host,
            status=response.status_code,
        ).inc()
        metrics.http_request_duration_seconds.labels(
            method=request.method,
            host=host,
        ).observe(time_elapsed / 1e9)

        log(
            f"[{request.method}] {response.status_code} {url}{Ansi.RESET!r} | {Ansi.LBLUE!r}Request took: {magnitude_fmt_time(time_elapsed)}",
            col,
        )

        response.headers["process-time"] = str(round(time_elapsed) / 1e6)
        return response
