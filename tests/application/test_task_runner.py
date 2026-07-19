"""T15: TaskRunner tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.application.task_runner import TaskRunner
from app.domain.task import (
    ClaimResult,
    FinishRunCommand,
    FinishRunResult,
    Task,
    TaskEvent,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStatus,
)


def _ready_task(id: str = "t_1") -> Task:
    return Task(
        id=id, title="x", status=TaskStatus.READY, assignee="d",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class FakeRunService:
    """Minimal run_service for TaskRunner: dispatch_once + run_claim."""

    def __init__(self):
        self.dispatch_calls = 0
        self.run_claim_calls: list[tuple[str, int, str]] = []
        self._run_claim_delay = 0

    async def dispatch_once(self):
        self.dispatch_calls += 1

    async def run_claim(self, task, run_id, claim_lock):
        self.run_claim_calls.append((task.id, run_id, claim_lock))
        if self._run_claim_delay:
            await asyncio.sleep(self._run_claim_delay)


@pytest.mark.asyncio
async def test_start_stop_ticks_dispatch():
    rs = FakeRunService()
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.set_run_service(rs)
    await runner.start()
    await asyncio.sleep(0.1)
    assert rs.dispatch_calls >= 1
    await runner.stop()


@pytest.mark.asyncio
async def test_start_idempotent():
    rs = FakeRunService()
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.set_run_service(rs)
    await runner.start()
    await runner.start()  # no-op
    assert runner._loop_task is not None
    await runner.stop()
    # stop is also idempotent
    await runner.stop()


@pytest.mark.asyncio
async def test_spawn_tracks_by_run_id_and_inspect():
    rs = FakeRunService()
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.set_run_service(rs)
    task = _ready_task()
    token = await runner.spawn(task, run_id=1, claim_lock="L1")
    assert token.startswith("wt-1-")
    snap = await runner.inspect()
    assert len(snap["active"]) == 1
    assert snap["active"][0]["run_id"] == 1
    assert snap["active"][0]["task_id"] == "t_1"
    # let the worker finish
    await asyncio.sleep(0.05)
    snap2 = await runner.inspect()
    assert len(snap2["active"]) == 0
    assert rs.run_claim_calls == [("t_1", 1, "L1")]


@pytest.mark.asyncio
async def test_spawn_keyed_by_run_id_not_task_id():
    """Late old worker for prior run does not overwrite newer run's slot."""
    rs = FakeRunService()
    rs._run_claim_delay = 0.2
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.set_run_service(rs)
    task = _ready_task()
    # Spawn run 1, then run 2 for the same task (simulates re-claim).
    await runner.spawn(task, run_id=1, claim_lock="L1")
    await runner.spawn(task, run_id=2, claim_lock="L2")
    snap = await runner.inspect()
    run_ids = {a["run_id"] for a in snap["active"]}
    assert run_ids == {1, 2}
    # Both slots are distinct (keyed by run_id).
    await asyncio.sleep(0.3)
    snap2 = await runner.inspect()
    assert len(snap2["active"]) == 0


@pytest.mark.asyncio
async def test_cancel_worker():
    rs = FakeRunService()
    rs._run_claim_delay = 1.0  # long-running
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.set_run_service(rs)
    task = _ready_task()
    token = await runner.spawn(task, run_id=1, claim_lock="L1")
    cancelled = await runner.cancel(token)
    assert cancelled is True
    snap = await runner.inspect()
    assert len(snap["active"]) == 0


@pytest.mark.asyncio
async def test_cancel_unknown_token_returns_false():
    rs = FakeRunService()
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.set_run_service(rs)
    cancelled = await runner.cancel("nonexistent")
    assert cancelled is False


@pytest.mark.asyncio
async def test_get_crashed_workers():
    class CrashRunService:
        async def dispatch_once(self):
            pass

        async def run_claim(self, task, run_id, claim_lock):
            raise RuntimeError("worker crashed")

    rs = CrashRunService()
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.set_run_service(rs)
    task = _ready_task()
    await runner.spawn(task, run_id=1, claim_lock="L1")
    await asyncio.sleep(0.1)
    crashed = await runner.get_crashed_workers()
    assert len(crashed) == 1
    assert crashed[0]["run_id"] == 1
    assert crashed[0]["task_id"] == "t_1"
    # Queue is cleared after read.
    crashed2 = await runner.get_crashed_workers()
    assert len(crashed2) == 0


@pytest.mark.asyncio
async def test_spawn_requires_run_service_bound():
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    with pytest.raises(RuntimeError):
        await runner.spawn(_ready_task(), run_id=1, claim_lock="L1")


@pytest.mark.asyncio
async def test_dispatch_tick_exception_does_not_stop_loop():
    class FlakyRunService:
        def __init__(self):
            self.calls = 0

        async def dispatch_once(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")

        async def run_claim(self, task, run_id, claim_lock):
            pass

    rs = FlakyRunService()
    runner = TaskRunner(interval_seconds=1, shutdown_grace_seconds=1)
    runner.interval_seconds = 0.05  # short interval for fast multi-tick
    runner.set_run_service(rs)
    await runner.start()
    await asyncio.sleep(0.2)
    await runner.stop()
    # Loop survived the first exception and ticked again.
    assert rs.calls >= 2
