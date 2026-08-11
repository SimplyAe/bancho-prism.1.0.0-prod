from __future__ import annotations

import asyncio

import pytest

from app import bg_task_supervision


@pytest.fixture(autouse=True)
def _reset_supervision_state() -> None:
    bg_task_supervision._metrics.clear()
    bg_task_supervision._background_tasks.clear()


async def _run_briefly(coro_factory, *, seconds: float = 0.5) -> None:
    """Run a never-ending loop long enough to observe a few iterations."""
    task = asyncio.ensure_future(coro_factory())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_supervised_loop_survives_a_failing_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core fix: one raised exception must cost a single cycle, not
    kill the loop for the lifetime of the process."""
    # keep the backoff short so the test stays fast.
    monkeypatch.setattr(bg_task_supervision, "BASE_BACKOFF_SECONDS", 0.01)

    calls = 0

    async def work() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient database error")

    await _run_briefly(
        lambda: bg_task_supervision.run_supervised_loop(
            "test-loop",
            interval=0.01,
            work=work,
            run_immediately=True,
        ),
        seconds=0.2,
    )

    # it kept going after the failure.
    assert calls > 1

    metrics = bg_task_supervision.get_background_task_metrics()["test-loop"]
    assert metrics.failures == 1
    assert metrics.iterations >= 1
    # recovered, so the consecutive counter was reset.
    assert metrics.consecutive_failures == 0
    assert metrics.last_error is not None
    assert "transient database error" in metrics.last_error


async def test_supervised_loop_backs_off_on_repeated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bg_task_supervision, "BASE_BACKOFF_SECONDS", 0.01)

    async def work() -> None:
        raise RuntimeError("still down")

    await _run_briefly(
        lambda: bg_task_supervision.run_supervised_loop(
            "always-fails",
            interval=0.01,
            work=work,
            run_immediately=True,
        ),
        seconds=0.15,
    )

    metrics = bg_task_supervision.get_background_task_metrics()["always-fails"]
    assert metrics.failures >= 2
    assert metrics.consecutive_failures >= 2
    assert metrics.iterations == 0


async def test_supervised_loop_is_still_cancellable() -> None:
    """Graceful shutdown cancels these tasks; the supervisor must not
    swallow CancelledError while catching everything else."""
    started = asyncio.Event()

    async def work() -> None:
        started.set()

    task = asyncio.ensure_future(
        bg_task_supervision.run_supervised_loop(
            "cancellable",
            interval=60,
            work=work,
            run_immediately=True,
        ),
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_backoff_grows_and_is_capped() -> None:
    assert bg_task_supervision._backoff_seconds(1) == (
        bg_task_supervision.BASE_BACKOFF_SECONDS
    )
    assert bg_task_supervision._backoff_seconds(2) == (
        bg_task_supervision.BASE_BACKOFF_SECONDS * 2
    )
    # a loop that has been failing for hours must not wait forever.
    assert (
        bg_task_supervision._backoff_seconds(50)
        == bg_task_supervision.MAX_BACKOFF_SECONDS
    )


async def test_spawn_background_task_keeps_a_strong_reference() -> None:
    """asyncio.create_task alone lets the GC reclaim a task mid-flight,
    because the event loop only holds a weak reference."""
    finished = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0)
        finished.set()

    task = bg_task_supervision.spawn_background_task(work(), name="one-shot")
    assert task in bg_task_supervision._background_tasks

    await asyncio.wait_for(finished.wait(), timeout=1)
    await task

    # the done-callback removes it once complete. add_done_callback fires
    # via loop.call_soon, so it runs on the next loop iteration rather than
    # synchronously at `await task` -- yield once to let it run.
    await asyncio.sleep(0)
    assert task not in bg_task_supervision._background_tasks


async def test_spawn_background_task_logs_and_records_failures() -> None:
    async def work() -> None:
        raise RuntimeError("webhook exploded")

    task = bg_task_supervision.spawn_background_task(work(), name="failing-one-shot")

    with pytest.raises(RuntimeError):
        await task

    # let the done-callback run.
    await asyncio.sleep(0)

    metrics = bg_task_supervision.get_background_task_metrics()["failing-one-shot"]
    assert metrics.failures == 1
    assert metrics.last_error is not None
    assert "webhook exploded" in metrics.last_error


async def test_cancel_background_tasks_cancels_in_flight_work() -> None:
    async def work() -> None:
        await asyncio.sleep(60)

    task = bg_task_supervision.spawn_background_task(work(), name="long-running")

    await bg_task_supervision.cancel_background_tasks()

    assert task.cancelled()
