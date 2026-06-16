from datetime import datetime, timezone

import pytest

from app.application.scheduler_runner import SchedulerRunner


class FakeRunService:
    def __init__(self):
        self.calls = []

    async def run_due_claims(self, now=None):
        self.calls.append(now)
        return [{"status": "ok"}]


@pytest.mark.asyncio
async def test_scheduler_runner_run_due_once_delegates_to_run_service():
    service = FakeRunService()
    runner = SchedulerRunner(service, tick_seconds=0.01)
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)

    result = await runner.run_due_once(now)

    assert result == [{"status": "ok"}]
    assert service.calls == [now]
