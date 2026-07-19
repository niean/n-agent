"""T13: TaskAgentExecutor tests.

Tests:
  - Calls ChatCompletionService with source=task, UNATTENDED
  - Same run_id in IngressFacts and trusted_metadata
  - task 7 tools in permitted_managed_tools (NOT granted_tools)
  - trusted_metadata.task carries claim context
  - user prompt is "work task {task.id}"
  - Returns terminal intent (does NOT finalize run)
  - goal_mode multi-turn until judge passes
  - goal_mode max_turns blocks
  - goal_mode invalid judge JSON is retryable FAILED
  - judge has no write tools
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.application.chat_service import ChatCompletionResult
from app.application.task_agent_executor import (
    TASK_GUIDANCE,
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

    async def list_events(self, task_id, since=0, limit=100):
        return tuple(
            e for e in self._events
            if e.task_id == task_id and e.id > since
        )[-limit:]


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
        assignee="default",
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
    # All 7 task tools should be in permitted_managed_tools
    from app.application.task_tools import TASK_TOOL_NAMES
    assert TASK_TOOL_NAMES.issubset(permitted)
    # Task tools should NOT be in granted_tools (which is for additional tools)
    granted = set(call.trusted_metadata.get("granted_tools", []))
    assert not TASK_TOOL_NAMES.intersection(granted)


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
async def test_executor_user_prompt_is_work_task(executor, fake_chat):
    task = _task(id="t_xyz")
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    user_msg = call.messages[-1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "work task t_xyz"


@pytest.mark.asyncio
async def test_executor_injects_task_guidance(executor, fake_chat):
    task = _task()
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    system_msg = call.messages[0]["content"]
    assert TASK_GUIDANCE in system_msg or "Task Worker Guidance" in system_msg


@pytest.mark.asyncio
async def test_executor_session_id_is_task_prefix(executor, fake_chat):
    task = _task(id="t_1", execution_session_id=None)
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    assert call.session_id == "task-t_1"


@pytest.mark.asyncio
async def test_executor_reuses_existing_execution_session(executor, fake_chat):
    task = _task(id="t_1", execution_session_id="task-t_1-existing")
    await executor.run(task, task_run_id=1, claim_lock="L1")
    call = fake_chat.complete_calls[0]
    assert call.session_id == "task-t_1-existing"


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
    # The executor does not have a finish_run method and does not call registry.finish_run
    assert not hasattr(executor, "finish_run")


@pytest.mark.asyncio
async def test_executor_returns_blocked_intent(executor, fake_registry):
    fake_registry._events = [
        TaskEvent(
            id=1, task_id="t_1", kind="block_requested",
            payload={"outcome": "blocked", "reason": "need user input", "kind": "needs_input"},
            run_id=1, created_at=datetime.now(timezone.utc),
        ),
    ]
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.BLOCKED
    assert result.error == "need user input"


@pytest.mark.asyncio
async def test_executor_no_intent_defaults_completed(executor, fake_registry):
    """If no intent event is found, default to COMPLETED."""
    fake_registry._events = []
    task = _task()
    result = await executor.run(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.COMPLETED


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
async def test_goal_mode_max_turns_blocks():
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
    task = _task(goal_mode=True, goal_max_turns=3)
    result = await executor.run_goal_loop(task, task_run_id=1, claim_lock="L1")
    assert result.status == TaskRunOutcome.BLOCKED
    assert "needs_input" in (result.metadata.get("block_kind") or "") or "not achieved" in (result.error or "")


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
    assert judge_call.trusted_metadata.get("permitted_managed_tools") == []


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
    """If a turn returns BLOCKED/FAILED, goal_loop returns immediately."""
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
