import asyncio

import pytest

from app.application import telemetry_details, telemetry_semantics
from app.application.task_agent_executor import TaskAgentResult
from app.application.task_session import task_execution_session_id
from app.config import Settings
from app.domain.task import TaskRunOutcome, TaskStatus
from app.main import build_application_services


@pytest.fixture(autouse=True)
def restore_telemetry_configuration(monkeypatch):
    # Real composition publishes global projection settings; do not change
    # subsequent unit tests' operating mode after this integration fixture.
    monkeypatch.setattr(telemetry_details, "_config", telemetry_details._config)
    monkeypatch.setattr(telemetry_semantics, "_span_attr_sanitizer", telemetry_semantics._span_attr_sanitizer)


class WaitingProvider:
    def __init__(self):
        self.started = asyncio.Event()

    async def chat(self, *args, **kwargs):
        self.started.set()
        await asyncio.Future()


@pytest.mark.asyncio
@pytest.mark.parametrize("judge", [False, True])
async def test_explicit_agent_run_cancel_finalizes_real_task_attempt(tmp_path, monkeypatch, judge):
    services = build_application_services(Settings(
        sqlite_path=tmp_path / "sessions.db", workspace_root=tmp_path,
        skills_root=tmp_path / "skills", provider_base_url="", provider_api_key="",
        plugins_root=tmp_path / "plugins",
        provider_model="", scheduler_enabled=False, feishu_enabled=False,
        artifacts_enabled=False, task_enabled=True,
    ))
    provider = WaitingProvider()
    services.chat_service.graph_runner.llm_provider = provider
    task = await services.task_service.create_task(title="cancel this work", goal_mode=judge)
    claim = await services.task_service.registry.claim_task(task.id, "claim", 900)
    if judge:
        async def completed_worker(*args):
            return TaskAgentResult(status=TaskRunOutcome.COMPLETED, output="check result")
        monkeypatch.setattr(services.task_run_service.executor, "run", completed_worker)
    execution = asyncio.create_task(services.task_run_service.run_claim(claim.task, claim.run.id, "claim"))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        row = services.agent_run_service.list_for_session(task_execution_session_id(claim.task))[0]
        assert row["execution_kind"] == ("task_judge" if judge else "task_worker")
        assert services.agent_run_service.cancel(row["id"])
        await asyncio.wait_for(execution, 2)
        final = await services.task_service.registry.get_task(task.id)
        assert final.status is TaskStatus.CANCELLED
        assert final.current_run_id is None and final.claim_lock is None
        attempt = (await services.task_service.registry.list_runs(task.id))[0]
        assert attempt.outcome is TaskRunOutcome.TERMINATED
        assert services.agent_run_service.get(row["id"])["status"] == "cancelled"
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await services.agent_run_service.close()
