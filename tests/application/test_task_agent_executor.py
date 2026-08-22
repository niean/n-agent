"""T6: TaskAgentExecutor tests (Manus-aligned 7-state machine).

Tests:
  - Calls ChatCompletionService with source=task, UNATTENDED
  - Same run_id in IngressFacts and trusted_metadata
  - task 6 tools in permitted_managed_tools (NOT granted_tools)
    (task_show / task_complete / task_heartbeat / task_comment /
     task_propose_change / task_cancel)
  - trusted_metadata.task carries claim context
  - user prompt is "work task {task.id}"
  - Returns terminal intent (does NOT finalize run)
  - TASK_GUIDANCE contains task_propose_change guidance
  - goal_mode multi-turn until judge passes
  - goal_mode max_turns returns FAILED (BLOCKED removed)
  - goal_mode invalid judge JSON is retryable FAILED
  - judge has no write tools
  - worker calls task_propose_change -> executor returns WAITING_APPROVAL
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4, uuid5, NAMESPACE_URL

import pytest

from app.application.chat_service import ChatCompletionResult
from app.application.prompt_builder import TASK_GUIDANCE
from app.application.task_agent_executor import (
    JudgeResult,
    TaskAgentExecutor,
    TaskAgentResult,
)
from app.domain.policy import ExecutionMode
from app.domain.task import Task, TaskEvent, TaskRunOutcome, TaskStatus


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeChatService:
    """Captures complete() calls and returns configurable results."""

    def __init__(self, result: ChatCompletionResult | None = None):
        self.complete_calls: list[Any] = []
        self._result = result or ChatCompletionResult(
            session_id="task-t_1",
            model="N-Agent",
            message={"role": "assistant", "content": "task done"},
            finish_reason="stop",
        )

    async def complete(self, request):
        self.complete_calls.append(request)
        return self._result

    def set_result(self, result: ChatCompletionResult):
        self._result = result


class FakeTaskRegistry:
    """Minimal registry fake for intent event reading."""

    def __init__(self, events: list[TaskEvent] | None = None):
        self._events = events or []
        self.appended_events: list[dict] = []

    async def list_events(self, task_id, since=0, limit=100):
        return tuple(
            e for e in self._events
            if e.task_id == task_id and e.id > since
        )[-limit:]

    async def append_event(self, task_id, kind, payload, run_id=None):
        self.appended_events.append(
            {"task_id": task_id, "kind": kind, "payload": payload, "run_id": run_id}
        )


class FakePromptBuilder:
    def build_system_prompt(self, **kwargs):
        return "Base system prompt."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _task(**kwargs) -> Task:
    defaults = dict(
        id="t_1",
        title="Test Task",
        body="Do important work",
        status=TaskStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return Task(**defaults)


@pytest.fixture
def fake_chat():
    return FakeChatService()


@pytest.fixture
def fake_registry():
    return FakeTaskRegistry()


@pytest.fixture
def executor(fake_chat, fake_registry):
    return TaskAgentExecutor(
        chat_service=fake_chat,
        task_registry=fake_registry,
        prompt_builder=FakePromptBuilder(),
    )


# ---------------------------------------------------------------------------
# Single-turn run tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_calls_chat_service(executor, fake_chat):
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    assert len(fake_chat.complete_calls) == 1


@pytest.mark.asyncio
async def test_executor_source_is_task(executor, fake_chat):
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    assert call.ingress_facts.source == "task"


@pytest.mark.asyncio
async def test_executor_execution_mode_unattended(executor, fake_chat):
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    assert call.ingress_facts.execution_mode == ExecutionMode.UNATTENDED


@pytest.mark.asyncio
async def test_executor_same_run_id_in_ingress_and_metadata(executor, fake_chat):
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    ingress_run_id = call.ingress_facts.run_id
    metadata_run_id = call.trusted_metadata.get("execution_run_id")
    assert ingress_run_id == metadata_run_id
    assert ingress_run_id.startswith("task-run-")


@pytest.mark.asyncio
async def test_executor_task_tools_in_permitted_managed_not_granted(executor, fake_chat):
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    permitted = set(call.trusted_metadata.get("permitted_managed_tools", []))
    # All 6 task tools should be in permitted_managed_tools
    from app.application.task_tools import TASK_TOOL_NAMES
    assert TASK_TOOL_NAMES.issubset(permitted)
    # Task tools should NOT be in granted_tools (which is for additional tools)
    granted = set(call.trusted_metadata.get("granted_tools", []))
    assert not TASK_TOOL_NAMES.intersection(granted)


@pytest.mark.asyncio
async def test_executor_permitted_managed_tools_contains_propose_and_fail(executor, fake_chat):
    """permitted_managed_tools must contain task_propose_change and task_fail,
    and must NOT contain the removed task_block/task_create/task_link/task_cancel."""
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    permitted = set(call.trusted_metadata.get("permitted_managed_tools", []))
    # New tools present
    assert "task_propose_change" in permitted
    assert "task_fail" in permitted
    # Removed tools absent
    assert "task_block" not in permitted
    assert "task_create" not in permitted
    assert "task_link" not in permitted
    # task_cancel 收回为用户专用，worker 不得持有
    assert "task_cancel" not in permitted


@pytest.mark.asyncio
async def test_executor_trusted_metadata_task_context(executor, fake_chat):
    task = _task(id="t_abc", board="default", created_by="alice")
    await executor.run(task, task_run_id=42, claim_lock="lock-xyz")
    call = fake_chat.complete_calls[0]
    task_ctx = call.trusted_metadata.get("task")
    assert task_ctx is not None
    assert task_ctx["task_id"] == "t_abc"
    assert task_ctx["run_id"] == 42
    assert task_ctx["claim_lock"] == "lock-xyz"
    assert task_ctx["write_origin"] == "worker"
    assert task_ctx["board"] == "default"


@pytest.mark.asyncio
async def test_executor_trusted_claims_carries_task_context(executor, fake_chat):
    """The task sub-dict must travel in ingress_facts.trusted_claims (not just
    trusted_metadata): ChatCompletionService rebuilds trusted_metadata from
    the policy snapshot's trusted_claims (built from IngressFacts.trusted_claims),
    so a task sub-dict only in trusted_metadata is dropped and the task tools
    fail with trusted_task_context_missing."""
    task = _task(id="t_abc", board="default", created_by="alice")
    await executor.run(task, task_run_id=42, claim_lock="lock-xyz")
    call = fake_chat.complete_calls[0]
    task_ctx = call.ingress_facts.trusted_claims.get("task")
    assert task_ctx is not None
    assert task_ctx["task_id"] == "t_abc"
    assert task_ctx["run_id"] == 42
    assert task_ctx["claim_lock"] == "lock-xyz"
    assert task_ctx["write_origin"] == "worker"


@pytest.mark.asyncio
async def test_executor_user_prompt_is_work_task(executor, fake_chat):
    task = _task(id="t_xyz")
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    user_msg = call.messages[-1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "work task t_xyz"


@pytest.mark.asyncio
async def test_executor_request_has_no_system_message(executor, fake_chat):
    """Worker request 只含 user 消息；system prompt 由 ContextService 运行时构建
    （不在 request 传 system），避免 working_messages 出现重复 system + 重复 user。"""
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    roles = [m["role"] for m in call.messages]
    assert roles == ["user"]
    assert call.messages[0]["content"] == "work task t_1"


@pytest.mark.asyncio
async def test_executor_task_guidance_contains_propose_change_guidance():
    """TASK_GUIDANCE must instruct the worker to call task_propose_change when
    encountering changes that require user decision, and that the run ends
    immediately after the call."""
    assert "task_propose_change" in TASK_GUIDANCE
    # Guidance must mention that the run ends after proposing
    assert "ends immediately" in TASK_GUIDANCE or "immediately" in TASK_GUIDANCE
    # Guidance must NOT reference removed tools
    assert "task_block" not in TASK_GUIDANCE
    assert "task_create" not in TASK_GUIDANCE
    assert "task_link" not in TASK_GUIDANCE


@pytest.mark.asyncio
async def test_executor_task_guidance_mentions_proposal_type():
    """TASK_GUIDANCE must instruct the worker to choose proposal_type:
    'approval' for approve/reject proposals, 'intent_request' when the worker
    needs the user to supply information/intent/clarification before continuing.
    """
    assert "proposal_type" in TASK_GUIDANCE
    assert "approval" in TASK_GUIDANCE
    assert "intent_request" in TASK_GUIDANCE


@pytest.mark.asyncio
async def test_executor_session_id_is_task_prefix(executor, fake_chat):
    task = _task(id="t_1", execution_session_id=None)
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    assert call.session_id == f"task-{uuid5(NAMESPACE_URL, 't_1')}"


@pytest.mark.asyncio
async def test_executor_reuses_existing_execution_session(executor, fake_chat):
    task = _task(id="t_1", execution_session_id="task-t_1-existing")
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    assert call.session_id == "task-t_1-existing"


@pytest.mark.asyncio
async def test_executor_reuses_origin_session_for_dashboard_task(executor, fake_chat):
    """dashboard /task 任务：origin_session_id = Chat 会话 -> worker 在 Chat 会话执行。"""
    task = _task(id="t_1", execution_session_id=None, origin_session_id="dashboard-s1")
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    assert call.session_id == "dashboard-s1"


@pytest.mark.asyncio
async def test_executor_returns_terminal_intent_not_finalize(executor, fake_registry):
    """Executor returns COMPLETED intent but does NOT call finish_run."""
    fake_registry._events = [
        TaskEvent(
            id=1, task_id="t_1", kind="complete_requested",
            payload={"outcome": "completed", "summary": "all done", "metadata": {}, "artifacts": []},
            run_id=1, created_at=datetime.now(timezone.utc),
        ),
    ]
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.COMPLETED
    assert result.output == "all done"
    # The executor does not have a finish_run method and does not call registry.finish_run
    assert not hasattr(executor, "finish_run")


@pytest.mark.asyncio
async def test_executor_propose_change_returns_waiting_approval(executor, fake_registry):
    """When the worker calls task_propose_change during the chat, a
    change_proposed event is written. The executor must detect it and return
    WAITING_APPROVAL so that TaskRunService.finalize_propose can终结 the run."""
    fake_registry._events = [
        TaskEvent(
            id=1, task_id="t_1", kind="change_proposed",
            payload={"proposal": "switch to plan B", "run_id": 1},
            run_id=1, created_at=datetime.now(timezone.utc),
        ),
    ]
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.WAITING_APPROVAL
    # The proposal text should be reflected in the output or metadata
    assert "switch to plan B" in (result.output or "") or \
           "switch to plan B" in str(result.metadata or {})


@pytest.mark.asyncio
async def test_executor_no_intent_defaults_completed(executor, fake_registry):
    """If no intent event is found, default to COMPLETED."""
    fake_registry._events = []
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.COMPLETED


@pytest.mark.asyncio
async def test_executor_fail_requested_returns_aborted(executor, fake_registry):
    """Worker 判定快速失败调 task_fail -> 写 fail_requested 事件 -> executor 返回
    ABORTED（-> task FAILED 不重试）。回归 t_a742046a521d46eb：worker 误用 task_cancel
    导致 run 以 COMPLETED 终结 -> SUCCEEDED 的语义 bug。"""
    fake_registry._events = [
        TaskEvent(
            id=1, task_id="t_1", kind="fail_requested",
            payload={"outcome": "aborted", "error": "execute_code unavailable", "run_id": 1},
            run_id=1, created_at=datetime.now(timezone.utc),
        ),
    ]
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.ABORTED
    assert result.error == "execute_code unavailable"
    assert result.output == "execute_code unavailable"


@pytest.mark.asyncio
async def test_executor_budget_exhausted_no_intent_defaults_failed(executor, fake_chat, fake_registry):
    """BUDGET_EXHAUSTED (finish_reason='length') + 无 task_complete intent -> FAILED。

    Regression: 此前默认 COMPLETED，把预算耗尽的 run 误标 SUCCEEDED（看板=succeeded /
    Chat=已达到用量上限，最终结果不一致）。worker 未调用 task_complete 即未完成 -> FAILED。
    """
    fake_registry._events = []
    fake_chat.set_result(ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "已达到用量上限，请稍后重试或联系管理员。"},
        finish_reason="length",
    ))
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.FAILED
    assert "已达到用量上限" in (result.error or "")


@pytest.mark.asyncio
async def test_executor_chat_error_returns_failed(executor, fake_chat):
    fake_chat.set_result(ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "error occurred"},
        finish_reason="error",
    ))
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.FAILED


@pytest.mark.asyncio
async def test_executor_exception_returns_failed(executor, fake_chat):
    async def fail(request):
        raise RuntimeError("connection lost")

    fake_chat.complete = fail
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.FAILED
    assert "connection lost" in (result.error or "")


@pytest.mark.asyncio
async def test_executor_granted_tools_from_execution_policy(executor, fake_chat):
    from app.domain.task import TaskExecutionPolicy
    task = _task(execution_policy=TaskExecutionPolicy(allowed_tools=("host_terminal", "web_search")))
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    granted = call.trusted_metadata.get("granted_tools", [])
    assert "host_terminal" in granted
    assert "web_search" in granted


@pytest.mark.asyncio
async def test_executor_default_grants_execute_code(executor, fake_chat):
    """默认 task（allowed_tools=()）的 worker 仍授予 execute_code，对齐
    TASK_GUIDANCE“用通用工具在 workspace 做事”+ Hermes cron-default-core-tools；
    write_file 仍为沙箱回调，不在此 grant。"""
    task = _task()  # 默认 execution_policy -> allowed_tools=()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    granted = call.trusted_metadata.get("granted_tools", [])
    assert "execute_code" in granted


@pytest.mark.asyncio
async def test_executor_default_task_grants_delegate_agents_when_task_delegation_enabled(
    fake_chat, fake_registry,
):
    """The Task feature switch must grant the tool to normal Task creation.

    Dashboard, HTTP, and CLI Task creation all produce the default empty
    ``allowed_tools`` policy, so the Task-level delegation feature flag is
    the server-side policy snapshot that authorizes this default grant.
    """
    from app.application.delegation_parent_adapter import TaskDelegationAdapter
    from app.application.delegation_policy_config import DelegationPolicyConfig

    task_executor = TaskAgentExecutor(
        chat_service=fake_chat,
        task_registry=fake_registry,
        task_delegation_adapter=TaskDelegationAdapter(),
        delegation_config=DelegationPolicyConfig(enabled=True, task_enabled=True),
    )
    await task_executor.run(_task(), task_run_id=1, claim_lock="L1")

    call = fake_chat.complete_calls[0]
    assert "delegate_agents" in call.trusted_metadata["granted_tools"]
    assert "delegation_capability" in call.trusted_metadata


@pytest.mark.asyncio
async def test_executor_default_task_strips_delegate_agents_when_task_delegation_disabled(
    fake_chat, fake_registry,
):
    from app.application.delegation_parent_adapter import TaskDelegationAdapter
    from app.application.delegation_policy_config import DelegationPolicyConfig

    task_executor = TaskAgentExecutor(
        chat_service=fake_chat,
        task_registry=fake_registry,
        task_delegation_adapter=TaskDelegationAdapter(),
        delegation_config=DelegationPolicyConfig(enabled=True, task_enabled=False),
    )
    await task_executor.run(_task(), task_run_id=1, claim_lock="L1")

    call = fake_chat.complete_calls[0]
    assert "delegate_agents" not in call.trusted_metadata["granted_tools"]
    assert "delegation_capability" not in call.trusted_metadata


@pytest.mark.asyncio
async def test_executor_default_grant_layers_with_explicit_allowed_tools(executor, fake_chat):
    """显式 allowed_tools 叠加在默认 execute_code 之上，不互相覆盖。"""
    from app.domain.task import TaskExecutionPolicy
    task = _task(execution_policy=TaskExecutionPolicy(allowed_tools=("host_terminal",)))
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    granted = call.trusted_metadata.get("granted_tools", [])
    assert "execute_code" in granted
    assert "host_terminal" in granted


# ---------------------------------------------------------------------------
# goal_mode tests
# ---------------------------------------------------------------------------


class FakeJudgeChatService:
    """Chat service that returns different results for worker vs judge calls."""

    def __init__(self, worker_result, judge_result_text):
        self.complete_calls: list[Any] = []
        self._worker_result = worker_result
        self._judge_result_text = judge_result_text

    async def complete(self, request):
        self.complete_calls.append(request)
        # Judge calls have empty granted_tools and "judge" in trusted_claims
        if request.trusted_metadata.get("judge"):
            return ChatCompletionResult(
                session_id=request.session_id,
                model="N-Agent",
                message={"role": "assistant", "content": self._judge_result_text},
                finish_reason="stop",
            )
        return self._worker_result


@pytest.mark.asyncio
async def test_goal_mode_multi_turn_until_judge_passes():
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    # Judge says achieved on 2nd call
    judge_responses = [
        '{"achieved": false, "reason": "not yet"}',
        '{"achieved": true, "reason": "done"}',
    ]
    call_count = [0]

    class SeqChat:
        def __init__(self):
            self.complete_calls = []

        async def complete(self, request):
            self.complete_calls.append(request)
            if request.trusted_metadata.get("judge"):
                resp = judge_responses[call_count[0]]
                call_count[0] += 1
                return ChatCompletionResult(
                    session_id=request.session_id, model="N-Agent",
                    message={"role": "assistant", "content": resp},
                    finish_reason="stop",
                )
            return worker_result

    chat = SeqChat()
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=5)
    result = await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.COMPLETED


@pytest.mark.asyncio
async def test_goal_mode_max_turns_fails():
    """When goal not achieved after max_turns, the run fails (BLOCKED removed;
    worker should use task_propose_change for needs-input scenarios)."""
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    # Judge always says not achieved
    judge_text = '{"achieved": false, "reason": "incomplete"}'

    chat = FakeJudgeChatService(worker_result, judge_text)
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=1)
    result = await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.FAILED
    assert "not achieved" in (result.error or "") or "incomplete" in (result.error or "")


@pytest.mark.asyncio
async def test_goal_mode_worker_abort_returns_without_next_turn():
    """task_fail maps to ABORTED and must end goal_mode immediately.

    Regression: t_2a913349cfe74c5c emitted task_fail repeatedly because
    run_goal_loop did not include ABORTED in its terminal outcomes.
    """
    registry = FakeTaskRegistry([
        TaskEvent(
            id=1, task_id="t_1", kind="fail_requested",
            payload={"error": "execute_code unavailable"}, run_id=1,
            created_at=datetime.now(timezone.utc),
        ),
    ])
    chat = FakeChatService()
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=registry,
        prompt_builder=FakePromptBuilder(),
    )

    result = await executor.run_goal_loop(
        _task(goal_mode=True, goal_max_turns=10), task_run_id=1, claim_lock="L1",
    )

    assert result.status is TaskRunOutcome.ABORTED
    assert result.error == "execute_code unavailable"
    assert len(chat.complete_calls) == 1


@pytest.mark.asyncio
async def test_goal_mode_early_exit_on_consecutive_rejections():
    """连续 GOAL_MAX_CONSECUTIVE_REJECTIONS 次 judge 否决即早退 FAILED，不耗尽 max_turns。

    Regression: t_97d317e953b64edc 明确失败后耗尽 10 轮才判失败。
    """
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = '{"achieved": false, "reason": "incomplete"}'
    chat = FakeJudgeChatService(worker_result, judge_text)
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=10)  # 远大于早退阈值
    result = await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.FAILED
    assert "consecutive" in (result.error or "")
    # 早退：worker 只跑 GOAL_MAX_CONSECUTIVE_REJECTIONS 轮，不是 10 轮
    worker_calls = [c for c in chat.complete_calls if not c.trusted_metadata.get("judge")]
    from app.application.task_agent_executor import GOAL_MAX_CONSECUTIVE_REJECTIONS
    assert len(worker_calls) == GOAL_MAX_CONSECUTIVE_REJECTIONS


@pytest.mark.asyncio
async def test_goal_mode_records_judge_feedback():
    """judge 否决后写 goal_judge_feedback 事件（喂回下一轮 worker，对齐 Hermes continuation prompt）。"""
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = '{"achieved": false, "reason": "缺 Q3 数据"}'
    chat = FakeJudgeChatService(worker_result, judge_text)
    registry = FakeTaskRegistry()
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=registry,
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=10)
    await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    feedback_events = [e for e in registry.appended_events if e["kind"] == "goal_judge_feedback"]
    assert len(feedback_events) >= 1
    assert feedback_events[0]["payload"]["reason"] == "缺 Q3 数据"
    assert feedback_events[0]["run_id"] == 1


@pytest.mark.asyncio
async def test_goal_mode_invalid_judge_json_is_retryable_failure():
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = "This is not valid JSON at all."

    chat = FakeJudgeChatService(worker_result, judge_text)
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=5)
    result = await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.FAILED
    assert "judge" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_judge_has_no_write_tools():
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = '{"achieved": true, "reason": "done"}'

    chat = FakeJudgeChatService(worker_result, judge_text)
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=3)
    await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")

    # Find the judge call
    judge_calls = [
        c for c in chat.complete_calls
        if c.trusted_metadata.get("judge")
    ]
    assert len(judge_calls) >= 1
    judge_call = judge_calls[0]
    assert judge_call.trusted_metadata.get("granted_tools") == []
    # judge 只读 task_show（评估目标达成），无写工具
    permitted = set(judge_call.trusted_metadata.get("permitted_managed_tools", []))
    assert permitted == {"task_show"}
    # judge 注入 task 上下文（使 task_show 通过 _origin_from_trusted，修复 permission_denied）
    task_ctx = judge_call.trusted_metadata.get("task", {})
    assert task_ctx.get("task_id") == task.id
    assert task_ctx.get("write_origin") == "judge"


@pytest.mark.asyncio
async def test_judge_fork_sets_persist_messages_false():
    """judge fork 必须设 persist_messages=False，避免内部判定消息泄露到用户 Chat 会话。

    Regression: goal_mode judge 的 user prompt ``judge task {id}: has the goal
    been achieved?`` 和 assistant JSON ``{"achieved": true, "reason": "..."}``
    被持久化到 execution_session_id，污染用户可见 Chat。judge 的 achieved/reason
    是控制流信号（决定 COMPLETED/FAILED/续轮），不是用户可见结果。
    """
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = '{"achieved": true, "reason": "done"}'

    chat = FakeJudgeChatService(worker_result, judge_text)
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=3)
    await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")

    judge_calls = [c for c in chat.complete_calls if c.trusted_metadata.get("judge")]
    assert len(judge_calls) >= 1
    judge_call = judge_calls[0]
    assert judge_call.persist_messages is False


@pytest.mark.asyncio
async def test_worker_run_keeps_persist_messages_default_true():
    """worker 路径 persist_messages 保持默认 True（worker 消息仍持久化到会话）。"""
    chat = FakeChatService()
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    await executor.run(_task(), task_run_id=1, claim_lock="L1")

    # FakeChatService stores calls; only one worker call
    assert len(chat.complete_calls) == 1
    worker_call = chat.complete_calls[0]
    assert worker_call.persist_messages is True


@pytest.mark.asyncio
async def test_goal_mode_judge_json_in_code_block():
    """Judge wraps JSON in markdown code block -- should still parse."""
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = '```json\n{"achieved": true, "reason": "done"}\n```'

    chat = FakeJudgeChatService(worker_result, judge_text)
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=3)
    result = await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.COMPLETED


@pytest.mark.asyncio
async def test_goal_mode_turn_terminal_returns_immediately():
    """If a turn returns FAILED/TIMED_OUT/CRASHED, goal_loop returns immediately."""
    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "blocked"},
        finish_reason="error",
    )
    chat = FakeChatService(worker_result)
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    task = _task(goal_mode=True, goal_max_turns=5)
    result = await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.FAILED
    # Only one worker call (no judge called)
    assert len(chat.complete_calls) == 1


@pytest.mark.asyncio
async def test_executor_trusted_claims_match_metadata(executor, fake_chat):
    """trusted_claims and trusted_metadata share the same claim identifiers."""
    task = _task(id="t_1")
    await executor.run(task, task_run_id=7, claim_lock="lock-7")
    call = fake_chat.complete_calls[0]
    # Both trusted_claims and trusted_metadata should have the same task_id, run_id, claim_lock
    claims = call.ingress_facts.trusted_claims
    metadata = call.trusted_metadata
    assert claims.get("task_id") == metadata.get("task_id") == "t_1"
    assert claims.get("claim_lock") == metadata.get("claim_lock") == "lock-7"
    assert claims.get("execution_run_id") == metadata.get("execution_run_id")
    assert claims.get("write_origin") == metadata.get("write_origin") == "worker"


# ---------------------------------------------------------------------------
# 防递归：worker / judge 不得 grant 用户侧任务工具（spec 防递归核心）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_does_not_grant_user_task_tools(executor, fake_chat):
    """worker 的 granted_tools 与 permitted_managed_tools 都不得含用户侧工具。"""
    from app.application.task_tools import USER_TASK_TOOL_NAMES

    await executor.run(_task(), task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    granted = set(call.trusted_metadata.get("granted_tools", []))
    permitted = set(call.trusted_metadata.get("permitted_managed_tools", []))
    assert USER_TASK_TOOL_NAMES.isdisjoint(granted)
    assert USER_TASK_TOOL_NAMES.isdisjoint(permitted)


@pytest.mark.asyncio
async def test_judge_does_not_grant_user_task_tools():
    """judge fork 的 granted_tools 与 permitted_managed_tools 都不得含用户侧工具。"""
    from app.application.task_tools import USER_TASK_TOOL_NAMES

    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = '{"achieved": true, "reason": "done"}'
    chat = FakeJudgeChatService(worker_result, judge_text)
    ex = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    await ex.run_goal_loop(
        _task(goal_mode=True, goal_max_turns=3), task_run_id=1, claim_lock="L1",
    )
    judge_calls = [c for c in chat.complete_calls if c.trusted_metadata.get("judge")]
    assert len(judge_calls) >= 1
    judge_call = judge_calls[0]
    granted = set(judge_call.trusted_metadata.get("granted_tools", []))
    permitted = set(judge_call.trusted_metadata.get("permitted_managed_tools", []))
    assert USER_TASK_TOOL_NAMES.isdisjoint(granted)
    assert USER_TASK_TOOL_NAMES.isdisjoint(permitted)


# ---------------------------------------------------------------------------
# 防递归（Task 7）：worker / judge 不得 grant 用户侧审批工具
# approve_task / reject_task / revise_task。即使 task.execution_policy.allowed_tools
# 被误配置为这三个名称，worker boundary 也必须显式剥离（worker 不能审批自己的提案）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_strips_approval_tools_even_when_allowed_tools_misconfigured(
    executor, fake_chat,
):
    """task.execution_policy.allowed_tools 误配置为 approve/reject/revise_task 时，
    worker 的 granted_tools 必须显式移除这三个名称，permitted_managed_tools 也不得含。

    防递归核心：worker 不能审批自己的 task_propose_change 提案。"""
    from app.application.task_tools import USER_TASK_APPROVAL_TOOL_NAMES
    from app.domain.task import TaskExecutionPolicy

    malicious = tuple(USER_TASK_APPROVAL_TOOL_NAMES) + ("host_terminal",)
    task = _task(execution_policy=TaskExecutionPolicy(allowed_tools=malicious))
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    granted = set(call.trusted_metadata.get("granted_tools", []))
    permitted = set(call.trusted_metadata.get("permitted_managed_tools", []))
    # 三个审批工具必须从 granted_tools 中剥离
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(granted)
    # permitted_managed_tools 也不得含（双重保险：TASK_TOOL_NAMES 本就与之不相交）
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(permitted)
    # 其它合法 grant 保留（剥离不能误伤 host_terminal）
    assert "host_terminal" in granted


@pytest.mark.asyncio
async def test_worker_strips_approval_tools_from_trusted_claims(
    executor, fake_chat,
):
    """trusted_claims（IngressFacts）中的 granted_tools / permitted_managed_tools
    也必须剥离三个审批工具。ChatCompletionService 从 trusted_claims 重建
    trusted_metadata，故两侧都必须干净。"""
    from app.application.task_tools import USER_TASK_APPROVAL_TOOL_NAMES
    from app.domain.task import TaskExecutionPolicy

    task = _task(
        execution_policy=TaskExecutionPolicy(
            allowed_tools=tuple(USER_TASK_APPROVAL_TOOL_NAMES),
        ),
    )
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    claims = call.ingress_facts.trusted_claims
    granted = set(claims.get("granted_tools", []))
    permitted = set(claims.get("permitted_managed_tools", []))
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(granted)
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(permitted)


@pytest.mark.asyncio
async def test_judge_does_not_grant_user_approval_task_tools():
    """judge fork 的 granted_tools 与 permitted_managed_tools 都不得含审批工具。

    judge 本就空 grant + 只读 task_show，这里显式断言审批工具三名称不在任何集合。"""
    from app.application.task_tools import USER_TASK_APPROVAL_TOOL_NAMES

    worker_result = ChatCompletionResult(
        session_id="task-t_1", model="N-Agent",
        message={"role": "assistant", "content": "did work"},
        finish_reason="stop",
    )
    judge_text = '{"achieved": true, "reason": "done"}'
    chat = FakeJudgeChatService(worker_result, judge_text)
    ex = TaskAgentExecutor(
        chat_service=chat, task_registry=FakeTaskRegistry(),
        prompt_builder=FakePromptBuilder(),
    )
    await ex.run_goal_loop(
        _task(goal_mode=True, goal_max_turns=3), task_run_id=1, claim_lock="L1",
    )
    judge_calls = [c for c in chat.complete_calls if c.trusted_metadata.get("judge")]
    assert len(judge_calls) >= 1
    judge_call = judge_calls[0]
    granted = set(judge_call.trusted_metadata.get("granted_tools", []))
    permitted = set(judge_call.trusted_metadata.get("permitted_managed_tools", []))
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(granted)
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(permitted)


@pytest.mark.asyncio
async def test_worker_approval_tools_not_visible_under_safe_only_surface(
    executor, fake_chat,
):
    """端到端：worker 即便误配置 allowed_tools 含审批工具三名称，经 boundary 剥离后，
    通过 ToolService SAFE_ONLY surface 断言三个工具最终对 worker 不可见。

    构造 ToolService 注册三个审批工具 + 一个 host_terminal（SAFE AGENT），用 worker
    实际产出的 granted_tools（post-strip）构造 ToolExecutionContext，调用
    list_openai_tools(SAFE_ONLY, context)：
      - approve_task / reject_task / revise_task 不可见（未 grant）
      - host_terminal 可见（证明 surface 本身工作，grant 机制未被破坏）
    """
    from app.application.task_tools import (
        USER_TASK_APPROVAL_TOOL_NAMES,
        user_task_approval_tool_definitions,
    )
    from app.application.tool_service import ToolService
    from app.domain.task import TaskExecutionPolicy
    from app.domain.tool import (
        RiskLevel,
        ToolDefinition,
        ToolExecutionContext,
        ToolSourceType,
    )
    from app.domain.tool_policy import ToolExposurePolicy

    # 恶意配置：allowed_tools 含三个审批工具 + 合法 host_terminal
    malicious = tuple(USER_TASK_APPROVAL_TOOL_NAMES) + ("host_terminal",)
    task = _task(execution_policy=TaskExecutionPolicy(allowed_tools=malicious))
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]

    # 取 worker 实际产出的 granted_tools（post-strip）
    worker_granted = frozenset(call.trusted_metadata.get("granted_tools", []))
    worker_permitted = frozenset(call.trusted_metadata.get("permitted_managed_tools", []))
    # 前置断言：三个审批工具已被 boundary 剥离
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(worker_granted)

    # 构造 ToolService：注册三个审批工具 + host_terminal
    host_terminal_def = ToolDefinition(
        name="host_terminal",
        description="host terminal",
        input_schema={"type": "object", "properties": {}},
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.AGENT,
        managed=False,
    )
    service = ToolService(
        executor=FakeExecutorForExposure(),
        definitions=[*user_task_approval_tool_definitions(), host_terminal_def],
    )

    context = ToolExecutionContext(
        granted_tools=worker_granted,
        permitted_managed_tools=set(worker_permitted),
    )
    visible = {
        schema["function"]["name"]
        for schema in service.list_openai_tools(ToolExposurePolicy.SAFE_ONLY, context)
    }
    # 三个审批工具不可见（worker 不能审批自己的提案）
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(visible)
    # host_terminal 可见：证明 grant 机制本身未被破坏（模式六设计保留）
    assert "host_terminal" in visible


class FakeExecutorForExposure:
    """仅用于 ToolService 构造的占位 executor（exposure 测试不执行工具）。"""

    async def execute(self, request, context=None):
        from app.domain.tool import ToolResult, ToolResultStatus
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})
