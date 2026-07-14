import asyncio

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


class _SlowProvider:
    """Provider that sleeps so we can interrupt mid-run."""

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model):
        return True

    async def chat(self, messages, tools, stream, model, options):
        await asyncio.sleep(0.5)
        return LLMResult(message={"role": "assistant", "content": "done"}, finish_reason="stop")


class _NoToolExecutor:
    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


def _build_runner(tmp_path) -> AgentGraphRunner:
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = AgentGraphRunner(
        _SlowProvider(),
        ToolService(_NoToolExecutor(), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    return runner


@pytest.mark.asyncio
async def test_interrupt_returns_false_for_missing_session(tmp_path):
    runner = _build_runner(tmp_path)
    assert runner.interrupt("missing-session") is False


@pytest.mark.asyncio
async def test_interrupt_cancels_running_stream(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-cancel"))
    runner = AgentGraphRunner(
        _SlowProvider(),
        ToolService(_NoToolExecutor(), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(session_id="s-cancel", input_messages=[{"role": "user", "content": "hi"}])

    events: list = []

    async def consume():
        async for event in runner.stream_events(state, "test"):
            events.append(event)

    consumer = asyncio.create_task(consume())
    # Wait for run to register
    await asyncio.sleep(0.05)
    assert "s-cancel" in runner._running_tasks
    assert runner.interrupt("s-cancel") is True
    await asyncio.wait_for(consumer, timeout=2.0)

    types = [e.type for e in events]
    assert ChatEventType.ERROR in types
    error_event = next(e for e in events if e.type is ChatEventType.ERROR)
    assert error_event.finish_reason == "cancelled"
    assert error_event.error == "cancelled"
    assert ChatEventType.DONE in types
    # Registry cleaned up after stream ends
    assert "s-cancel" not in runner._running_tasks
    assert "s-cancel" not in runner._cancel_events


@pytest.mark.asyncio
async def test_clear_run_removes_registry_entries(tmp_path):
    runner = _build_runner(tmp_path)
    fake_task = asyncio.get_event_loop().create_future()
    runner.register_run("s-fake", fake_task)  # type: ignore[arg-type]
    assert "s-fake" in runner._running_tasks
    assert "s-fake" in runner._cancel_events
    runner.clear_run("s-fake")
    assert "s-fake" not in runner._running_tasks
    assert "s-fake" not in runner._cancel_events


@pytest.mark.asyncio
async def test_is_cancelled_reflects_interrupt_state(tmp_path):
    runner = _build_runner(tmp_path)
    assert runner.is_cancelled("s-x") is False
    fake_task = asyncio.get_event_loop().create_future()
    runner.register_run("s-x", fake_task)  # type: ignore[arg-type]
    assert runner.is_cancelled("s-x") is False
    runner.interrupt("s-x")
    assert runner.is_cancelled("s-x") is True
    runner.clear_run("s-x")
    assert runner.is_cancelled("s-x") is False


@pytest.mark.asyncio
async def test_interrupt_missing_does_not_create_entries(tmp_path):
    """Calling interrupt on an unknown session must not pollute the registry."""
    runner = _build_runner(tmp_path)
    assert runner.interrupt("nope") is False
    assert "nope" not in runner._running_tasks
    assert "nope" not in runner._cancel_events
    assert runner.is_cancelled("nope") is False


@pytest.mark.asyncio
async def test_flag_based_cancel_in_finalize_does_not_report_completed(tmp_path):
    """T9: When is_cancelled is True at finalize time (flag-based cancel,
    without CancelledError being raised), the run must NOT report as
    clean COMPLETED. This covers the race where the cancel event is set
    between a node completing and the next routing decision."""
    from app.domain.agent import AgentState, RunStatus
    from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
    from app.domain.session import ConversationSession
    from app.application.tool_service import ToolService, builtin_tool_definitions
    from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
    from app.infrastructure.tools.builtin import build_builtin_tool_executor

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-cancel-flag"))
    runner = AgentGraphRunner(
        _SlowProvider(),
        ToolService(_NoToolExecutor(), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    # Register and immediately cancel -- cancel event is set but no
    # CancelledError is raised (we call finalize directly).
    fake_task = asyncio.get_event_loop().create_future()
    runner.register_run("s-cancel-flag", fake_task)
    runner.interrupt("s-cancel-flag")
    assert runner.is_cancelled("s-cancel-flag") is True

    # Simulate state after call_llm completed (final_message set) but
    # cancel flag was set between call_llm return and the routing decision.
    state = AgentState(
        session_id="s-cancel-flag",
        input_messages=[{"role": "user", "content": "hi"}],
    )
    state.final_message = {"role": "assistant", "content": "partial"}
    state.iteration_count = 1
    state.run_status = RunStatus.RUNNING

    result = await runner.finalize(state)

    # Cancelled run must NOT report as clean COMPLETED
    assert result.run_status is RunStatus.FAILED
    assert result.error is not None
    assert "cancel" in result.error.lower()
    assert result.final_message is not None

    runner.clear_run("s-cancel-flag")
