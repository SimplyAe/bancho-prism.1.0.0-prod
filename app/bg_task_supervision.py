"""Supervision primitives for background work.

Two failure modes are handled here, because both silently degrade a
running server rather than crashing it:

1. A periodic loop written as `while True: await work()` dies permanently
   the first time `work()` raises. One transient database error and the
   loop is gone for the lifetime of the process, with nothing logged.
   `run_supervised_loop` moves the try/except *inside* the iteration so a
   failure costs one cycle instead of the whole loop.

2. `asyncio.create_task(coro)` without keeping the returned task alive
   relies on the event loop, which holds only a weak reference. The task
   can be garbage collected before it finishes, and any exception it
   raises is discarded. `spawn_background_task` keeps a strong reference
   until completion and logs whatever comes out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from app.logging import Ansi
from app.logging import log

# On failure we back off exponentially rather than reusing the loop's
# normal interval: a 30-minute loop should retry sooner than 30 minutes,
# and a 100-second loop should stop hammering a database that is down.
BASE_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0


@dataclass
class BackgroundTaskMetrics:
    """Per-task counters, exported by the observability layer."""

    iterations: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None


_metrics: dict[str, BackgroundTaskMetrics] = {}


def get_background_task_metrics() -> dict[str, BackgroundTaskMetrics]:
    """Snapshot of every supervised task's counters."""
    return dict(_metrics)


def _metrics_for(task_name: str) -> BackgroundTaskMetrics:
    return _metrics.setdefault(task_name, BackgroundTaskMetrics())


def _backoff_seconds(consecutive_failures: int) -> float:
    return float(
        min(
            MAX_BACKOFF_SECONDS,
            BASE_BACKOFF_SECONDS * 2 ** (consecutive_failures - 1),
        ),
    )


async def run_supervised_loop(
    task_name: str,
    *,
    interval: float,
    work: Callable[[], Awaitable[None]],
    run_immediately: bool = False,
) -> None:
    """Run `work` every `interval` seconds, forever, surviving failures.

    `CancelledError` is never swallowed -- graceful shutdown cancels these
    tasks and must continue to work.
    """
    metrics = _metrics_for(task_name)

    if not run_immediately:
        await asyncio.sleep(interval)

    while True:
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.failures += 1
            metrics.consecutive_failures += 1
            metrics.last_error = repr(exc)

            delay = _backoff_seconds(metrics.consecutive_failures)
            log(
                f"Background task {task_name!r} failed "
                f"({metrics.consecutive_failures} in a row), "
                f"retrying in {delay:.0f}s: {exc!r}",
                Ansi.LRED,
            )
            await asyncio.sleep(delay)
            continue

        metrics.iterations += 1

        if metrics.consecutive_failures:
            log(
                f"Background task {task_name!r} recovered after "
                f"{metrics.consecutive_failures} consecutive failures.",
                Ansi.LGREEN,
            )
            metrics.consecutive_failures = 0

        await asyncio.sleep(interval)


# Strong references to in-flight one-shot tasks, so the garbage collector
# cannot reclaim them mid-await.
_background_tasks: set[asyncio.Task[Any]] = set()


def _on_task_done(task_name: str, task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)

    if task.cancelled():
        return

    exception = task.exception()
    if exception is not None:
        metrics = _metrics_for(task_name)
        metrics.failures += 1
        metrics.last_error = repr(exception)
        log(
            f"Background task {task_name!r} raised: {exception!r}",
            Ansi.LRED,
        )


def spawn_background_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
) -> asyncio.Task[Any]:
    """Fire-and-forget `coro` without losing it, or its exceptions."""
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(lambda t: _on_task_done(name, t))
    return task


async def cancel_background_tasks() -> None:
    """Cancel in-flight one-shot tasks during shutdown."""
    if not _background_tasks:
        return

    log(
        f"-> Cancelling {len(_background_tasks)} background tasks.",
        Ansi.LMAGENTA,
    )

    for task in list(_background_tasks):
        task.cancel()

    await asyncio.gather(*list(_background_tasks), return_exceptions=True)
