"""Prometheus metrics.

bancho.py already had a `_increment_metric` hook wired through the
service layer, but it only fed Datadog -- which most self-hosters do not
run, leaving them with no visibility at all. These metrics are exported
on `/metrics` for any Prometheus-compatible scraper, alongside (not
instead of) the existing Datadog calls.

The counters here are deliberately the ones that answer "is the server
healthy?": request latency, score submissions, login outcomes, and the
liveness of the background loops -- which previously could die silently.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry
from prometheus_client import Counter
from prometheus_client import Gauge
from prometheus_client import Histogram
from prometheus_client import generate_latest

# a dedicated registry keeps our series separate from any library that
# registers into the global default.
REGISTRY = CollectorRegistry()

http_requests_total = Counter(
    "bancho_http_requests_total",
    "HTTP requests handled.",
    labelnames=("method", "host", "status"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "bancho_http_request_duration_seconds",
    "HTTP request latency.",
    labelnames=("method", "host"),
    registry=REGISTRY,
    # osu! clients are latency-sensitive on score submission and
    # leaderboards; the low buckets are where the interesting tail is.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

http_requests_in_flight = Gauge(
    "bancho_http_requests_in_flight",
    "HTTP requests currently being handled.",
    registry=REGISTRY,
)

scores_submitted_total = Counter(
    "bancho_scores_submitted_total",
    "Scores submitted.",
    labelnames=("status",),
    registry=REGISTRY,
)

logins_total = Counter(
    "bancho_logins_total",
    "Login attempts by outcome.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

online_players = Gauge(
    "bancho_online_players",
    "Players currently online.",
    registry=REGISTRY,
)

background_task_iterations_total = Gauge(
    "bancho_background_task_iterations_total",
    "Successful iterations of each supervised background loop.",
    labelnames=("task",),
    registry=REGISTRY,
)

background_task_failures_total = Gauge(
    "bancho_background_task_failures_total",
    "Failed iterations of each supervised background loop.",
    labelnames=("task",),
    registry=REGISTRY,
)

background_task_consecutive_failures = Gauge(
    "bancho_background_task_consecutive_failures",
    (
        "Consecutive failures of each supervised background loop. "
        "Sustained non-zero means a loop is stuck, e.g. donor privileges "
        "silently never expiring."
    ),
    labelnames=("task",),
    registry=REGISTRY,
)

replay_analysis_queue_depth = Gauge(
    "bancho_replay_analysis_queue_depth",
    (
        "Scores waiting in the anticheat replay-analysis queue. Sampled by the "
        "worker each iteration; a persistently climbing depth means analysis is "
        "not keeping up with submissions."
    ),
    registry=REGISTRY,
)


def set_replay_analysis_queue_depth(depth: int) -> None:
    """Publish the current replay-analysis queue depth.

    The worker samples the depth from within its async loop and calls this,
    rather than the scrape reading redis synchronously (``_collect_dynamic_metrics``
    cannot await). Injected into the worker so it stays test-isolated.
    """
    replay_analysis_queue_depth.set(depth)


stat_snapshot_rows_written = Gauge(
    "bancho_stat_snapshot_rows_written",
    (
        "Rows written by the most recent daily stat-snapshot capture. The "
        "supervised-loop iteration counter shows the capture *ran*; this shows "
        "it captured *something*. A persistent 0 while players are ranked means "
        "the capture is silently writing nothing -- history is being lost a day "
        "at a time, which is unrecoverable."
    ),
    registry=REGISTRY,
)


def set_stat_snapshot_rows_written(rows: int) -> None:
    """Publish the row count from the latest daily stat-snapshot capture.

    Pushed from the daily loop after a capture completes (the scrape cannot
    await a fresh count), mirroring the replay-analysis queue-depth gauge.
    """
    stat_snapshot_rows_written.set(rows)


anticheat_flags_recorded_total = Counter(
    "bancho_anticheat_flags_recorded_total",
    (
        "Replay analyses persisted to the staff review queue, by severity. "
        "flag, never ban: this counts surfaces-to-a-human, not punishments. A "
        "rising rate warrants a look at either behaviour or the thresholds, not "
        "an automated response."
    ),
    labelnames=("severity",),
    registry=REGISTRY,
)


def increment_anticheat_flag(severity: str) -> None:
    """Count one flagged score persisted to the review queue.

    Pushed from the worker's result hook after a successful flag write, so the
    metric reflects durably-recorded flags rather than every advisory report.
    """
    anticheat_flags_recorded_total.labels(severity=severity).inc()


def _collect_dynamic_metrics() -> None:
    """Refresh gauges that are sampled rather than incremented."""
    import app.state.sessions
    from app.bg_task_supervision import get_background_task_metrics

    # `players` includes the bot, which is not a real online player.
    online_players.set(max(len(app.state.sessions.players) - 1, 0))

    for task_name, metrics in get_background_task_metrics().items():
        background_task_iterations_total.labels(task=task_name).set(metrics.iterations)
        background_task_failures_total.labels(task=task_name).set(metrics.failures)
        background_task_consecutive_failures.labels(task=task_name).set(
            metrics.consecutive_failures,
        )


def render_metrics() -> bytes:
    """Render the current metrics in the Prometheus text format."""
    _collect_dynamic_metrics()
    return generate_latest(REGISTRY)
