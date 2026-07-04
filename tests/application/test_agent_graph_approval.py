import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import (
    ApprovalDecision,
    ApprovalRequest,
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


class _ManageScheduleProvider:
    """Provider that emits a manage_schedule tool_call once, then a final message."""

    def __init__(self):
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "manage_schedule",
                                "arguments": '{"action":"list"}',
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(
            message={"role": "assistant", "content": "done"},
            finish_reason="stop",
        )


class _RecordingExecutor:
    """Records whether execute was called."""

    def __init__(self):
        self.calls: list[ToolCallRequest] = []

    async def execute(self, request: ToolCallRequest, context=None) -> ToolResult:
        self.calls.append(request)
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            {"ok": True},
        )


def _manage_schedule_def() -> ToolDefinition:
    return ToolDefinition(
        name="manage_schedule",
        description="Manage scheduled tasks.",
        input_schema={
            "type": "object",
            "properties": {"action": {"type": "string"}},
        },
        risk_level=RiskLevel.CONFIRM,
        source_type=ToolSourceType.AGENT,
        toolset="schedule",
        managed=True,
    )


@pytest.mark.asyncio
async def test_approval_allow_once_executes_tool_and_persists_success(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-allow-once"))
    executor = _RecordingExecutor()
    decider_calls: list[ApprovalRequest] = []

    def decider(req: ApprovalRequest) -> ApprovalDecision:
        decider_calls.append(req)
        return ApprovalDecision(allowed=True, scope="once")

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id="s-allow-once",
        input_messages=[{"role": "user", "content": "list schedules"}],
    )
    ctx = ToolExecutionContext(
        session_id="s-allow-once",
        approval_decider=decider,
    )
    options = {"tool_execution_context": ctx}
    events = [e async for e in runner.stream_events(state, "test", options)]

    # Decider called exactly once
    assert len(decider_calls) == 1
    assert decider_calls[0].tool_name == "manage_schedule"
    assert decider_calls[0].risk_level is RiskLevel.CONFIRM
    # Executor was called (tool executed)
    assert len(executor.calls) == 1
    assert executor.calls[0].name == "manage_schedule"
    # SQLite tool_calls status is success
    tool_calls = await store.list_tool_calls("s-allow-once")
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "success"
    # Stream completed without error
    assert any(e.type is ChatEventType.MESSAGE_DONE for e in events)


@pytest.mark.asyncio
async def test_approval_deny_skips_executor_and_persists_permission_denied(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-deny"))
    executor = _RecordingExecutor()

    def decider(req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(allowed=False, scope="deny", reason="user said no")

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id="s-deny",
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id="s-deny", approval_decider=decider)
    options = {"tool_execution_context": ctx}
    events = [e async for e in runner.stream_events(state, "test", options)]

    # Executor NOT called
    assert len(executor.calls) == 0
    # SQLite tool_calls status is permission_denied
    tool_calls = await store.list_tool_calls("s-deny")
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "permission_denied"


@pytest.mark.asyncio
async def test_no_decider_falls_back_to_permission_denied(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-no-decider"))
    executor = _RecordingExecutor()
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id="s-no-decider",
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id="s-no-decider")  # no approval_decider
    options = {"tool_execution_context": ctx}
    events = [e async for e in runner.stream_events(state, "test", options)]

    # Executor NOT called (existing ToolService logic denies CONFIRM without permitted_managed_tools)
    assert len(executor.calls) == 0
    tool_calls = await store.list_tool_calls("s-no-decider")
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "permission_denied"


@pytest.mark.asyncio
async def test_approval_allow_once_does_not_leak_into_subsequent_calls(tmp_path):
    """allow_once must not persist permission into the next tool call.

    Provider emits two tool_calls in sequence (separate iterations).
    First call: decider allows once -> executor runs.
    Second call: decider denies -> executor must NOT run, even though the
    previous allow_once added manage_schedule to permitted_managed_tools
    for that single call.
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-no-leak"))

    class _TwoCallProvider:
        def __init__(self):
            self.calls = 0

        async def list_models(self):
            return [ModelInfo("test", "test", "fake")]

        async def supports_tools(self, model):
            return True

        async def chat(self, messages, tools, stream, model, options):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "manage_schedule",
                                    "arguments": '{"action":"list"}',
                                },
                            }
                        ],
                    },
                    finish_reason="tool_calls",
                )
            if self.calls == 2:
                return LLMResult(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {
                                    "name": "manage_schedule",
                                    "arguments": '{"action":"list"}',
                                },
                            }
                        ],
                    },
                    finish_reason="tool_calls",
                )
            return LLMResult(
                message={"role": "assistant", "content": "done"},
                finish_reason="stop",
            )

    executor = _RecordingExecutor()
    decider_calls: list[ApprovalRequest] = []

    def decider(req: ApprovalRequest) -> ApprovalDecision:
        decider_calls.append(req)
        # Allow first, deny second
        if len(decider_calls) == 1:
            return ApprovalDecision(allowed=True, scope="once")
        return ApprovalDecision(allowed=False, scope="deny", reason="second denied")

    runner = AgentGraphRunner(
        _TwoCallProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=5,
    )
    state = AgentState(
        session_id="s-no-leak",
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id="s-no-leak", approval_decider=decider)
    options = {"tool_execution_context": ctx}
    [e async for e in runner.stream_events(state, "test", options)]

    # Decider called twice (once per tool_call)
    assert len(decider_calls) == 2
    # Executor called once (only the first, allowed call)
    assert len(executor.calls) == 1
    # SQLite records: one success, one permission_denied
    tool_calls = await store.list_tool_calls("s-no-leak")
    statuses = sorted(tc.status for tc in tool_calls)
    assert statuses == ["permission_denied", "success"]


@pytest.mark.asyncio
async def test_approval_async_decider_is_awaited(tmp_path):
    """Decider returning an awaitable must be awaited correctly."""
    import asyncio

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-async"))
    executor = _RecordingExecutor()

    async def decider(req: ApprovalRequest) -> ApprovalDecision:
        await asyncio.sleep(0)
        return ApprovalDecision(allowed=True, scope="once")

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id="s-async",
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id="s-async", approval_decider=decider)
    options = {"tool_execution_context": ctx}
    [e async for e in runner.stream_events(state, "test", options)]

    assert len(executor.calls) == 1
    tool_calls = await store.list_tool_calls("s-async")
    assert tool_calls[0].status == "success"


@pytest.mark.asyncio
async def test_approval_session_scope_executes_via_existing_context(tmp_path):
    """allow_session: runner does not persist metadata (S 7); it expects the
    ACP agent to have pre-populated context.permitted_managed_tools. The runner
    simply consumes the context and lets ToolService.execute proceed.

    We simulate the ACP agent's pre-population by setting permitted_managed_tools
    directly. The decider returns scope="session" but the runner does NOT need
    to mutate context for session scope (it's already set up externally).
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-session"))
    executor = _RecordingExecutor()
    decider_calls: list[ApprovalRequest] = []

    def decider(req: ApprovalRequest) -> ApprovalDecision:
        decider_calls.append(req)
        return ApprovalDecision(allowed=True, scope="session")

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id="s-session",
        input_messages=[{"role": "user", "content": "list"}],
    )
    # ACP agent has already persisted the session-scope permission into context
    ctx = ToolExecutionContext(
        session_id="s-session",
        approval_decider=decider,
        permitted_managed_tools={"manage_schedule"},
    )
    options = {"tool_execution_context": ctx}
    [e async for e in runner.stream_events(state, "test", options)]

    assert len(decider_calls) == 1
    assert len(executor.calls) == 1
    tool_calls = await store.list_tool_calls("s-session")
    assert tool_calls[0].status == "success"
