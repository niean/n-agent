"""T11: Infrastructure task tool executor (TaskManagementToolExecutor).

Covers plan T11 (trusted_metadata 门控 + ownership + 7 工具 dispatch +
build_worker_context).

Spec reference:
  - 只从 ToolExecutionContext.trusted_metadata.task 读取 task_id / run_id /
    claim_lock / write_origin，禁止从 untrusted metadata 读
  - complete/block/heartbeat/comment 同时校验当前 task、run、claim
  - worker 只能改自己 claim 的 Task；task_create 只能创建当前 Task 的子任务；
    task_link 只能以当前 Task 为 parent 或 child
  - task_show 返回 task、parents/children、comments、最近 50 events、runs 与
    脱敏 worker_context
  - complete/block 只返回终态意图，不调 finish_run（TaskRunService 拥有终结权）
  - 模式十二 trusted_metadata 门控：OpenAI HTTP 客户端无法伪造 trusted_metadata
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.domain.task import (
    BlockKind,
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


# ---------------------------------------------------------------------------
# FakeTaskService: 实现执行器依赖的 TaskService 子集（Batch D 实装前的 mock）
# ---------------------------------------------------------------------------


class FakeTaskService:
    """模拟 Batch D 的 TaskService，记录调用并返回受控响应。

    执行器只依赖以下方法（duck-typed Protocol）：
      - get_task_detail(task_id) -> dict
      - complete(task_id, summary, metadata, artifacts) -> dict
      - block(task_id, reason, kind) -> dict
      - heartbeat(task_id, note) -> dict
      - add_comment(task_id, body, author) -> dict
      - create_subtask(parent_task_id, title, body, assignee, parents, skills) -> dict
      - link(parent_id, child_id) -> dict
      - build_worker_context(task) -> str
    """

    def __init__(
        self,
        *,
        tasks: dict[str, Task] | None = None,
        runs: dict[str, list[TaskRun]] | None = None,
    ):
        self.tasks: dict[str, Task] = dict(tasks or {})
        self.runs: dict[str, list[TaskRun]] = dict(runs or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, dict(kwargs)))

    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        self._record("get_task_detail", task_id=task_id)
        task = self.tasks.get(task_id)
        if task is None:
            return None
        return {
            "task": _serialize_task(task),
            "parents": [],
            "children": [],
            "comments": [],
            "events": [],
            "runs": [_serialize_run(r) for r in self.runs.get(task_id, [])],
            "attachments": [],
            "worker_context": self.build_worker_context(task),
        }

    async def complete(
        self,
        task_id: str,
        summary: str,
        metadata: dict[str, Any],
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._record(
            "complete",
            task_id=task_id,
            summary=summary,
            metadata=metadata,
            artifacts=artifacts,
        )
        return {
            "task_id": task_id,
            "intent": "complete",
            "summary": summary,
        }

    async def block(
        self,
        task_id: str,
        reason: str,
        kind: str,
    ) -> dict[str, Any]:
        self._record("block", task_id=task_id, reason=reason, kind=kind)
        return {
            "task_id": task_id,
            "intent": "block",
            "reason": reason,
            "kind": kind,
        }

    async def heartbeat(self, task_id: str, note: str) -> dict[str, Any]:
        self._record("heartbeat", task_id=task_id, note=note)
        return {"task_id": task_id, "heartbeat_recorded": True}

    async def add_comment(
        self,
        task_id: str,
        body: str,
        author: str = "worker",
    ) -> dict[str, Any]:
        self._record("add_comment", task_id=task_id, body=body, author=author)
        return {"task_id": task_id, "comment_added": True}

    async def create_subtask(
        self,
        parent_task_id: str,
        title: str,
        body: str = "",
        assignee: str | None = None,
        parents: list[str] | None = None,
        skills: list[str] | None = None,
    ) -> dict[str, Any]:
        self._record(
            "create_subtask",
            parent_task_id=parent_task_id,
            title=title,
            body=body,
            assignee=assignee,
            parents=parents,
            skills=skills,
        )
        new_id = "t_new_child"
        return {"task_id": new_id, "parent_task_id": parent_task_id}

    async def link(self, parent_id: str, child_id: str) -> dict[str, Any]:
        self._record("link", parent_id=parent_id, child_id=child_id)
        return {"parent_id": parent_id, "child_id": child_id}

    def build_worker_context(self, task: Task) -> str:
        """构造 worker 上下文字符串（spec: title/body/先前尝试/父交接/评论/附件受控引用）。"""
        parts = [f"# {task.title}", ""]
        if task.body:
            parts.append(task.body)
            parts.append("")
        parts.append(f"task_id: {task.id}")
        parts.append(f"status: {task.status.value}")
        return "\n".join(parts)


def _serialize_task(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "status": task.status.value,
        "assignee": task.assignee,
        "priority": task.priority,
    }


def _serialize_run(run: TaskRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "task_id": run.task_id,
        "status": run.status.value,
        "outcome": run.outcome.value if run.outcome else None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str = "t_1",
    *,
    title: str = "demo task",
    status: TaskStatus = TaskStatus.RUNNING,
    claim_lock: str | None = "lock-own",
    current_run_id: int | None = 1,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        body="task body content",
        status=status,
        claim_lock=claim_lock,
        current_run_id=current_run_id,
    )


def _trusted_ctx(
    *,
    task_id: str = "t_1",
    write_origin: str = "worker",
    claim_lock: str = "lock-own",
    run_id: int = 1,
    session_id: str = "task-t_1",
    mode: str = "realtime",
    permitted: set[str] | None = None,
    untrusted_metadata: dict[str, Any] | None = None,
    trusted_metadata_extra: dict[str, Any] | None = None,
) -> ToolExecutionContext:
    """构造带 trusted_metadata.task 上下文的 ToolExecutionContext。

    untrusted_metadata 用于负向测试：模拟客户端伪造 task_id/claim_lock 等，
    验证执行器不读取 untrusted 通道。
    """
    trusted_task: dict[str, Any] = {
        "task_id": task_id,
        "write_origin": write_origin,
        "claim_lock": claim_lock,
        "run_id": run_id,
    }
    if trusted_metadata_extra:
        trusted_task.update(trusted_metadata_extra)
    if permitted is None:
        effective_permitted = {
            "task_show",
            "task_complete",
            "task_block",
            "task_heartbeat",
            "task_comment",
            "task_create",
            "task_link",
        }
    else:
        effective_permitted = permitted
    return ToolExecutionContext(
        session_id=session_id,
        metadata=untrusted_metadata or {},
        trusted_metadata={"task": trusted_task},
        execution_context_mode=mode,
        permitted_managed_tools=effective_permitted,
    )


def _no_task_trusted_ctx(
    *,
    session_id: str = "s_normal",
    mode: str = "realtime",
    permitted: set[str] | None = None,
    untrusted_metadata: dict[str, Any] | None = None,
) -> ToolExecutionContext:
    """构造无 trusted_metadata.task 上下文的 ToolExecutionContext。

    模拟普通 chat 或客户端伪造 task 字段的场景。
    """
    return ToolExecutionContext(
        session_id=session_id,
        metadata=untrusted_metadata or {},
        trusted_metadata={},
        execution_context_mode=mode,
        permitted_managed_tools=permitted or set(),
    )


def _payload(result) -> dict[str, Any]:
    if isinstance(result.content, str):
        return json.loads(result.content)
    return result.content if result.content is not None else {}


def _req(
    name: str,
    arguments: dict[str, Any] | None = None,
    call_id: str = "c1",
) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments or {})


# ---------------------------------------------------------------------------
# T11 S1-S2: trusted_metadata 门控（fail-closed）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_show_requires_trusted_task_context():
    """无 trusted_metadata.task 上下文 -> permission_denied。"""
    fake = FakeTaskService()
    executor = TaskManagementToolExecutor(fake)
    ctx = _no_task_trusted_ctx()
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    # 不应调用 TaskService
    assert fake.calls == []


@pytest.mark.asyncio
async def test_any_task_tool_requires_trusted_task_context():
    """所有 7 个工具都要求 trusted_metadata.task 上下文。"""
    fake = FakeTaskService()
    executor = TaskManagementToolExecutor(fake)
    requests = [
        _req("task_show", {"task_id": "t_1"}),
        _req("task_complete", {"summary": "done"}),
        _req("task_block", {"reason": "x", "kind": "needs_input"}),
        _req("task_heartbeat", {"note": "still working"}),
        _req("task_comment", {"task_id": "t_1", "body": "hi"}),
        _req("task_create", {"title": "sub"}),
        _req("task_link", {"parent_id": "t_1", "child_id": "t_2"}),
    ]
    for req in requests:
        ctx = _no_task_trusted_ctx()
        result = await executor.execute(req, ctx)
        assert result.status is ToolResultStatus.PERMISSION_DENIED, req.name
    assert fake.calls == []


@pytest.mark.asyncio
async def test_task_tool_requires_tool_in_permitted_managed_tools():
    """工具名必须出现在 permitted_managed_tools；不在则 permission_denied。

    这是模式十二的第二层门控：trusted_metadata 存在但 permitted_managed_tools
    不含工具名时，仍然拒绝（模拟 worker 未被授予该工具）。
    """
    fake = FakeTaskService(tasks={"t_1": _task()})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(
        task_id="t_1",
        permitted={"task_show"},  # 只授予 task_show
    )
    # task_complete 不在 permitted -> 拒绝
    result = await executor.execute(_req("task_complete", {"summary": "done"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_untrusted_metadata_cannot_forge_task_context():
    """客户端把 task 字段写入 untrusted metadata 不应授予访问。

    模式十二关键约束：trusted_metadata 只能由服务端写入。
    """
    fake = FakeTaskService(tasks={"t_1": _task()})
    executor = TaskManagementToolExecutor(fake)
    # 客户端在 untrusted metadata 中伪造 task 字段
    ctx = _no_task_trusted_ctx(
        untrusted_metadata={
            "task": {
                "task_id": "t_1",
                "write_origin": "worker",
                "claim_lock": "lock-own",
                "run_id": 1,
            }
        },
    )
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_untrusted_metadata_cannot_override_trusted_task_id():
    """trusted_metadata.task.task_id 优先；untrusted 伪造 task_id 无效。

    worker 持有 t_own 的 claim，但客户端在 untrusted metadata 中传 task_id=t_other
    试图越权 -- 必须按 trusted_metadata.task.task_id 校验。
    """
    fake = FakeTaskService(tasks={"t_own": _task("t_own"), "t_other": _task("t_other")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(
        task_id="t_own",
        untrusted_metadata={"task_id": "t_other"},  # 伪造
    )
    # task_complete 的 target_task_id 来自 arguments.task_id -- 但执行器应使用
    # trusted_metadata.task.task_id 做所有权校验。arguments.task_id 若与 trusted
    # 不一致 -> 拒绝（ownership 防越权）。
    result = await executor.execute(
        _req("task_complete", {"summary": "done"}),  # 无 task_id 参数，按 trusted 走
        ctx,
    )
    # 这里 arguments 没有显式 task_id，执行器应使用 trusted task_id=t_own
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    assert payload["task_id"] == "t_own"


# ---------------------------------------------------------------------------
# T11 S3: ownership 强制（worker 只能改自己 claim 的 task）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_can_complete_own_task():
    """worker 持有 t_own 的 claim_lock -> 可以 complete。"""
    fake = FakeTaskService(tasks={"t_own": _task("t_own")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(
        _req("task_complete", {"summary": "done", "metadata": {"k": "v"}}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    assert payload["intent"] == "complete"
    # 调用记录确认 task_id
    assert any(c[0] == "complete" and c[1]["task_id"] == "t_own" for c in fake.calls)


@pytest.mark.asyncio
async def test_worker_cannot_complete_other_task_via_arguments_task_id():
    """arguments.task_id 与 trusted task_id 不一致 -> 拒绝。

    worker 持有 t_own，但试图 complete t_other -- 必须 permission_denied。
    注意 task_complete 的 schema 没有 task_id 参数（默认操作 own task），
    但执行器对带 task_id 的 comment/link 必须显式校验。
    """
    fake = FakeTaskService(
        tasks={"t_own": _task("t_own"), "t_other": _task("t_other")}
    )
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    # task_comment 带 task_id=t_other，worker 持有 t_own -> 跨 task 评论 -> 拒绝
    result = await executor.execute(
        _req("task_comment", {"task_id": "t_other", "body": "hi"}),
        ctx,
    )
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_worker_can_comment_own_task():
    """worker 持有 t_own -> 可以 comment t_own。"""
    fake = FakeTaskService(tasks={"t_own": _task("t_own")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(
        _req("task_comment", {"task_id": "t_own", "body": "progress"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert any(c[0] == "add_comment" and c[1]["task_id"] == "t_own" for c in fake.calls)


@pytest.mark.asyncio
async def test_worker_can_create_subtask_of_own_task():
    """worker 持有 t_own -> 可以 create_subtask，parent 默认为 t_own。"""
    fake = FakeTaskService(tasks={"t_own": _task("t_own")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(
        _req("task_create", {"title": "sub work", "body": "sub body"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    assert "task_id" in payload
    # parent 必须是 t_own
    assert any(
        c[0] == "create_subtask" and c[1]["parent_task_id"] == "t_own"
        for c in fake.calls
    )


@pytest.mark.asyncio
async def test_worker_can_link_own_task_as_parent():
    """task_link: 当前 task 作为 parent -> 允许。"""
    fake = FakeTaskService(tasks={"t_own": _task("t_own"), "t_other": _task("t_other")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(
        _req("task_link", {"parent_id": "t_own", "child_id": "t_other"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert any(c[0] == "link" for c in fake.calls)


@pytest.mark.asyncio
async def test_worker_can_link_own_task_as_child():
    """task_link: 当前 task 作为 child -> 允许。"""
    fake = FakeTaskService(tasks={"t_own": _task("t_own"), "t_parent": _task("t_parent")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(
        _req("task_link", {"parent_id": "t_parent", "child_id": "t_own"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_worker_cannot_link_two_other_tasks():
    """task_link: parent 和 child 都不是当前 task -> 拒绝。"""
    fake = FakeTaskService(
        tasks={
            "t_own": _task("t_own"),
            "t_a": _task("t_a"),
            "t_b": _task("t_b"),
        }
    )
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_own")
    result = await executor.execute(
        _req("task_link", {"parent_id": "t_a", "child_id": "t_b"}),
        ctx,
    )
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


# ---------------------------------------------------------------------------
# T11 S3: 7 工具 dispatch 验证
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
async def test_task_block_dispatches_with_kind():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(
        _req("task_block", {"reason": "waiting", "kind": "needs_input"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    assert payload["intent"] == "block"
    assert payload["kind"] == "needs_input"
    assert any(
        c[0] == "block" and c[1]["kind"] == "needs_input" for c in fake.calls
    )


@pytest.mark.asyncio
async def test_task_heartbeat_dispatches():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(
        _req("task_heartbeat", {"note": "still working"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert any(c[0] == "heartbeat" and c[1]["note"] == "still working" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_complete_with_artifacts_dispatches():
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    artifacts = [
        {
            "type": "file",
            "name": "report.md",
            "mime": "text/markdown",
            "size": 100,
            "storage_ref": "tasks/t_1/report.md",
            "summary": "report",
            "checksum": "sha256:abc",
        }
    ]
    result = await executor.execute(
        _req(
            "task_complete",
            {"summary": "done", "metadata": {"k": "v"}, "artifacts": artifacts},
        ),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    # 验证 artifacts 透传给 TaskService.complete
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


# ---------------------------------------------------------------------------
# T11 S5-S6: build_worker_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_worker_context_includes_title_and_body():
    """worker_context 必须包含 task 的 title 和 body。"""
    fake = FakeTaskService(tasks={"t_1": _task("t_1", title="调研任务", )})
    fake.tasks["t_1"] = Task(
        id="t_1",
        title="调研任务",
        body="调研 N-Agent 架构并产出报告",
        status=TaskStatus.RUNNING,
        claim_lock="lock-own",
        current_run_id=1,
    )
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    wc = payload["worker_context"]
    assert "调研任务" in wc
    assert "调研 N-Agent 架构并产出报告" in wc
    assert "t_1" in wc


@pytest.mark.asyncio
async def test_build_worker_context_includes_prior_attempts():
    """worker_context 应包含先前 failed run 的摘要（如果存在）。"""
    failed_run = TaskRun(
        id=1,
        task_id="t_1",
        status=TaskRunStatus.FAILED,
        outcome=TaskRunOutcome.FAILED,
        summary="attempt 1 failed: timeout",
        started_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 1, 10, 5, tzinfo=timezone.utc),
    )
    # Override build_worker_context to include runs
    class FakeWithAttempts(FakeTaskService):
        def build_worker_context(self, task: Task) -> str:
            parts = [f"# {task.title}", "", task.body, "", f"task_id: {task.id}"]
            for r in self.runs.get(task.id, []):
                if r.outcome and r.outcome.value in ("failed", "crashed", "timed_out"):
                    parts.append(f"prior attempt #{r.id}: {r.summary or 'no summary'}")
            return "\n".join(parts)

    fake = FakeWithAttempts(
        tasks={"t_1": _task("t_1")},
        runs={"t_1": [failed_run]},
    )
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    wc = payload["worker_context"]
    assert "attempt 1 failed: timeout" in wc


@pytest.mark.asyncio
async def test_build_worker_context_excludes_host_absolute_paths():
    """worker_context 不得包含宿主绝对路径（spec 安全约束）。"""
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    wc = payload["worker_context"]
    # 简单启发式：不应出现 /Users/ 或 /home/ 或 C:\\ 等
    assert "/Users/" not in wc
    assert "/home/" not in wc
    assert "C:\\" not in wc


# ---------------------------------------------------------------------------
# T11 S8: managed 门控负向测试（ forging ）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forging_trusted_metadata_via_untrusted_channel_fails():
    """客户端把 task 写入 metadata（untrusted）并伪造 permitted_managed_tools
    在 granted_tools 中 -> 仍然 permission_denied。

    关键：执行器只读 trusted_metadata.task，不读 metadata.task；
    permitted_managed_tools 由服务端注入，不由 granted_tools 决定。
    """
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = ToolExecutionContext(
        session_id="s_forge",
        metadata={
            "task": {  # 客户端伪造的 untrusted metadata
                "task_id": "t_1",
                "write_origin": "worker",
                "claim_lock": "lock-own",
                "run_id": 1,
            }
        },
        trusted_metadata={},  # 无 trusted task
        execution_context_mode="realtime",
        permitted_managed_tools=set(),  # 不含 task 工具
        granted_tools=frozenset({"task_show", "task_complete"}),  # 伪造 granted
    )
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_forging_permitted_managed_tools_via_untrusted_does_not_grant():
    """permitted_managed_tools 是 ToolExecutionContext 字段（服务端构造），
    客户端无法通过 arguments 或 metadata 写入。

    本测试验证：permitted_managed_tools 为空时，即使 trusted_metadata 有 task
    上下文，工具仍不暴露。这模拟了"worker 未被授予该工具"的场景。
    """
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(
        task_id="t_1",
        permitted=set(),  # 空集 -- 不授予任何 task 工具
    )
    result = await executor.execute(_req("task_show", {"task_id": "t_1"}), ctx)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_unknown_block_kind_rejected():
    """task_block 的 kind 必须是合法 BlockKind 枚举值。"""
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(
        _req("task_block", {"reason": "x", "kind": "invalid_kind"}),
        ctx,
    )
    # kind 在 schema 层被 enum 拦截，但执行器也应防御性校验
    assert result.status in (ToolResultStatus.ERROR, ToolResultStatus.PERMISSION_DENIED)
    # 不应调用 block
    assert not any(c[0] == "block" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_show_returns_terminal_intent_only_for_complete():
    """task_complete 只返回终态意图，不调 finish_run（TaskRunService 拥有终结权）。

    FakeTaskService.complete 只返回 dict，不模拟 finish_run。执行器调用 complete
    后直接把返回值序列化为 ToolResult.content。
    """
    fake = FakeTaskService(tasks={"t_1": _task("t_1")})
    executor = TaskManagementToolExecutor(fake)
    ctx = _trusted_ctx(task_id="t_1")
    result = await executor.execute(
        _req("task_complete", {"summary": "done"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    # 意图存在，但没有 finish_run 调用（FakeTaskService 不提供 finish_run 方法）
    assert payload["intent"] == "complete"
    # 没有 finish_run 调用记录
    assert not any(c[0] == "finish_run" for c in fake.calls)


@pytest.mark.asyncio
async def test_task_show_returns_50_events_limit():
    """task_show 返回最近 50 events（spec 约束）。

    FakeTaskService.get_task_detail 返回的 events 列表已经是受控的；
    本测试验证执行器把 events 透传到 ToolResult.content。
    """
    # 构造 60 个 events，FakeTaskService 应只返回 50
    class FakeWithEvents(FakeTaskService):
        async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
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
    payload = _payload(result)
    # FakeTaskService 返回 60，但执行器应限制到 50（spec: 最近 50 events）
    assert len(payload["events"]) <= 50
