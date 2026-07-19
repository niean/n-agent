"""T11: Infrastructure task tool executor (TaskManagementToolExecutor).

Manus-aligned 6 tools: task_show/task_complete/task_heartbeat/task_comment/
task_propose_change/task_cancel. trusted_metadata gating + ownership.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.domain.task import (
    Task,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStatus,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
)
from app.infrastructure.tools.task_management import TaskManagementToolExecutor


class FakeTaskService:
    def __init__(self, *, tasks=None, runs=None):
        self.tasks: dict[str, Task] = dict(tasks or {})
        self.runs: dict[str, list[TaskRun]] = dict(runs or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method, **kwargs):
        self.calls.append((method, dict(kwargs)))

    async def get_task_detail(self, task_id):
        self._record("get_task_detail", task_id=task_id)
        task = self.tasks.get(task_id)
        if task is None:
            return None
        return {
            "task": _serialize_task(task),
            "comments": [],
            "events": [],
            "runs": [_serialize_run(r) for r in self.runs.get(task_id, [])],
            "attachments": [],
            "worker_context": await self.build_worker_context(task) if _is_async(self.build_worker_context) else self.build_worker_context(task),
        }

    async def complete(self, task_id, summary, metadata, artifacts):
        self._record("complete", task_id=task_id, summary=summary, metadata=metadata, artifacts=artifacts)
        return {"task_id": task_id, "intent": "complete", "summary": summary}

    async def heartbeat(self, task_id, note):
        self._record("heartbeat", task_id=task_id, note=note)
        return {"task_id": task_id, "heartbeat_recorded": True}

    async def add_comment(self, task_id, body, author="worker"):
        self._record("add_comment", task_id=task_id, body=body, author=author)
        return {"task_id": task_id, "comment_added": True}

    async def propose_change(self, task_id, proposal, run_id):
        self._record("propose_change", task_id=task_id, proposal=proposal, run_id=run_id)
        return {"task_id": task_id, "intent": "propose_change", "proposal": proposal}

    async def cancel_task(self, task_id):
        self._record("cancel_task", task_id=task_id)
        return {"task_id": task_id, "intent": "cancel"}

    async def build_worker_context(self, task):
        parts = [f"# {task.title}", "", task.body or "", "", f"task_id: {task.id}", f"status: {task.status.value}"]
        return "\n".join(parts)


def _is_async(fn):
    import asyncio
    return asyncio.iscoroutinefunction(fn)


def _serialize_task(task):
    return {"id": task.id, "title": task.title, "body": task.body, "status": task.status.value, "priority": task.priority}


def _serialize_run(run):
    return {"id": run.id, "task_id": run.task_id, "status": run.status.value, "outcome": run.outcome.value if run.outcome else None}


def _task(task_id="t_1", *, title="demo task", status=TaskStatus.RUNNING, claim_lock="lock-own", current_run_id=1):
    return Task(id=task_id, title=title, body="task body content", status=status, claim_lock=claim_lock, current_run_id=current_run_id)


_DEFAULT_PERMITTED = {
    "task_show", "task_complete", "task_heartbeat", "task_comment",
    "task_propose_change", "task_cancel",
}


def _trusted_ctx(*, task_id="t_1", write_origin="worker", claim_lock="lock-own", run_id=1, session_id="task-t_1", mode="realtime", permitted=None, untrusted_metadata=None):
    trusted_task = {"task_id": task_id, "write_origin": write_origin, "claim_lock": claim_lock, "run_id": run_id}
    return ToolExecutionContext(
        session_id=session_id,
        metadata=untrusted_metadata or {},
        trusted_metadata={"task": trusted_task},
        execution_context_mode=mode,
        permitted_managed_tools=permitted if permitted is not None else _DEFAULT_PERMITTED,
    )


def _no_task_trusted_ctx(*, session_id="s_normal", mode="realtime", permitted=None, untrusted_metadata=None):
    return ToolExecutionContext(
        session_id=session_id,
        metadata=untrusted_metadata or {},
        trusted_metadata={},
        execution_context_mode=mode,
        permitted_managed_tools=permitted or set(),
    )


def _payload(result):
    if isinstance(result.content, str):
        return json.loads(result.content)
    return result.content if result.content is not None else {}


def _req(name, arguments=None, call_id="c1"):
    return ToolCallRequest(id=call_id, name=name, arguments=arguments or {})


# ---------------------------------------------------------------------------
# trusted_metadata gating (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_show_requires_trusted_task_context():
    fake = FakeTaskService()
    executor = TaskManagementToolExecutor(fake)
    ctx = _no_task_trusted_ctx()
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_any_task_tool_requires_trusted_task_context():
    fake = FakeTaskService()
    executor = TaskManagementToolExecutor(fake)
    requests = [
        _req("task_show", {"task_id": "t_1"}),
        _req("task_complete", {"summary": "done"}),
        _req("task_heartbeat", {"note": "still working"}),
        _req("task_comment", {"task_id": "t_1", "body": "hi"}),
        _req("task_propose_change", {"proposal": "change approach"}),
        _req("task_cancel", {}),
    ]
    for req in requests:
        ctx = _no_task_trusted_ctx()
        result = await executor.execute(req, ctx)
        assert result.status is ToolResultStatus.PERMISSION_DENIED, req.name
    assert fake.calls == []


@pytest.mark.asyncio
async def test_task_tool_requires_tool_in_permitted_managed_tools():
    fake = FakeTaskService(tasks={"t_1": _task()})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1", permitted={"task_show"})
    result = await executor.execute(_req("task_complete", {"summary": "done"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_untrusted_metadata_cannot_forge_task_context():
    fake = FakeTaskService(tasks={"t_1": _task()})
    executor = TaskManagementToolExecutor(fake)
    ctx = _no_task_trusted_ctx(untrusted_metadata={"task": {"task_id": "t_1", "write_origin": "worker", "claim_lock": "lock-own", "run_id": 1}})
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_can_complete_own_task():
    fake = FakeTaskService(tasks={"t_own": _task("t_own")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(_req("task_complete", {"summary": "done", "metadata": {"k": "v"}}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    assert any(c[0] == "complete" and c[1]["task_id"] == "t_own" for c in fake.calls)


@pytest.mark.asyncio
async def test_worker_cannot_comment_other_task():
    fake = FakeTaskService(tasks={"t_own": _task("t_own"), "t_other": _task("t_other")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(_req("task_comment", {"task_id": "t_other", "body": "hi"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_worker_can_comment_own_task():
    fake = FakeTaskService(tasks={"t_own": _task("t_own")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(_req("task_comment", {"task_id": "t_own", "body": "progress"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    assert any(c[0] == "add_comment" and c[1]["task_id"] == "t_own" for c in fake.calls)


# ---------------------------------------------------------------------------
# 6-tool dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_show_dispatches_to_get_task_detail():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    assert payload["task"]["id"] == "t_1"
    assert "worker_context" in payload
    assert any(c[0] == "get_task_detail" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_propose_change_dispatches_with_run_id():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1", run_id=7)
    result = await executor.execute(_req("task_propose_change", {"proposal": "switch approach"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    assert payload["intent"] == "propose_change"
    assert any(c[0] == "propose_change" and c[1]["run_id"] == 7 and c[1]["proposal"] == "switch approach" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_propose_change_requires_proposal():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_propose_change", {"proposal": ""}), ctx)
    assert result.status is ToolResultStatus.ERROR
    assert not any(c[0] == "propose_change" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_cancel_dispatches():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_cancel", {}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    assert any(c[0] == "cancel_task" and c[1]["task_id"] == "t_1" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_heartbeat_dispatches():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_heartbeat", {"note": "still working"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    assert any(c[0] == "heartbeat" and c[1]["note"] == "still working" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_complete_with_artifacts_dispatches():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    artifacts = [{"type": "file", "name": "report.md", "mime": "text/markdown", "size": 100, "storage_ref": "tasks/t_1/report.md", "summary": "report", "checksum": "sha256:abc"}]
    result = await executor.execute(_req("task_complete", {"summary": "done", "metadata": {"k": "v"}, "artifacts": artifacts}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    complete_calls = [c for c in fake.calls if c[0] == "complete"]
    assert len(complete_calls) == 1
    assert complete_calls[0][1]["artifacts"] == artifacts


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_unknown"), ctx)
    assert result.status is ToolResultStatus.ERROR


@pytest.mark.asyncio
async def test_removed_tools_not_routed():
    """task_block/task_create/task_link 已移除；调用应 unknown tool -> ERROR."""
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    for name in ("task_block", "task_create", "task_link"):
        result = await executor.execute(_req(name, {}), ctx)
        assert result.status is ToolResultStatus.ERROR, name


# ---------------------------------------------------------------------------
# build_worker_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_worker_context_includes_title_and_body():
    fake = FakeTaskService(tasks={"t_1": _task("t_1", title="调研任务")})
    fake.tasks["t_1"] = Task(id="t_1", title="调研任务", body="调研 N-Agent 架构并产出报告", status=TaskStatus.RUNNING, claim_lock="lock-own", current_run_id=1)
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    wc = _payload(result)["worker_context"]
    assert "调研任务" in wc
    assert "调研 N-Agent 架构并产出报告" in wc
    assert "t_1" in wc


@pytest.mark.asyncio
async def test_build_worker_context_excludes_host_absolute_paths():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    wc = _payload(result)["worker_context"]
    assert "/Users/" not in wc
    assert "/home/" not in wc
    assert "C:\\" not in wc


@pytest.mark.asyncio
async def test_task_show_returns_50_events_limit():
    class FakeWithEvents(FakeTaskService):
        async def get_task_detail(self, task_id):
            base = await super().get_task_detail(task_id)
            if base is None:
                return None
            base["events"] = [{"id": i, "kind": f"event_{i}"} for i in range(60)]
            return base

    fake = FakeWithEvents(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    assert len(_payload(result)["events"]) <= 50


@pytest.mark.asyncio
async def test_forging_trusted_metadata_via_untrusted_channel_fails():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = ToolExecutionContext(
        session_id="s_forge",
        metadata={"task": {"task_id": "t_1", "write_origin": "worker", "claim_lock": "lock-own", "run_id": 1}},
        trusted_metadata={},
        execution_context_mode="realtime",
        permitted_managed_tools=set(),
        granted_tools=frozenset({"task_show", "task_complete"}),
    )
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []
