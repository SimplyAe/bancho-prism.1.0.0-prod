"""Tracking of in-flight http requests, for graceful shutdown.

Uvicorn stops accepting new connections when it receives a shutdown
signal, but the ASGI app is torn down as soon as the lifespan context
exits. Any request still executing at that moment loses the database and
redis connections underneath it -- which, for a score submission, means
the client is told the submission failed after the play already happened.

The middleware increments a counter around each request; shutdown waits
for it to reach zero (up to a bounded deadline) before closing services.
"""

from __future__ import annotations

import asyncio

from app.logging import Ansi
from app.logging import log

_count = 0
# signalled whenever the count reaches zero, so the drain can await
# rather than poll.
_idle = asyncio.Event()
_idle.set()


def increment() -> None:
    global _count
    _count += 1
    _idle.clear()


def decrement() -> None:
    global _count
    _count -= 1
    if _count <= 0:
        # clamp: a decrement without a matching increment would otherwise
        # drive this negative and wedge the drain permanently.
        _count = 0
        _idle.set()


def count() -> int:
    return _count


async def drain(timeout: float) -> bool:
    """Wait for in-flight requests to finish.

    Returns True if the server drained cleanly, False if `timeout`
    elapsed first (in which case remaining requests are abandoned, and
    the caller should expect errors in their logs).
    """
    if _count == 0:
        return True

    log(f"Waiting for {_count} in-flight request(s) to finish...", Ansi.LYELLOW)

    try:
        await asyncio.wait_for(_idle.wait(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        log(
            f"Drain timed out after {timeout}s with {_count} request(s) "
            "still in flight; shutting down anyway.",
            Ansi.LRED,
        )
        return False

    log("All in-flight requests finished.", Ansi.LGREEN)
    return True
