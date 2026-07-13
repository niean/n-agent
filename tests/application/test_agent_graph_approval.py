import asyncio

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService
from app.domain.agent import AgentState
from app.domain.gateway import GatewaySessionKey
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
from app.interfaces.cli.cli_tool_approval import CliToolApprovalBridge


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
async def test_allow_once_is_isolated_between_same_response_tool_calls(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-same-response"))
    executor = _RecordingExecutor()
    approvals: list[ApprovalRequest] = []

    def decider(req: ApprovalRequest) -> ApprovalDecision:
        approvals.append(req)
        if len(approvals) == 1:
            return ApprovalDecision(True, "once")
        return ApprovalDecision(False, "deny", "second denied")

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-same-response",
        pending_tool_calls=[
            {
                "id": "call-1",
                "function": {
                    "name": "manage_schedule",
                    "arguments": '{"action":"list"}',
                },
            },
            {
                "id": "call-2",
                "function": {
                    "name": "manage_schedule",
                    "arguments": '{"action":"list"}',
                },
            },
        ],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                session_id="s-same-response",
                approval_decider=decider,
            )
        },
    )

    await runner.execute_tools(state)

    assert [request.tool_call_id for request in approvals] == ["call-1", "call-2"]
    assert [call.id for call in executor.calls] == ["call-1"]
    assert [result["status"] for result in state.tool_results] == [
        "success",
        "permission_denied",
    ]


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
    """A session approval authorizes the current call even before reload."""
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
    ctx = ToolExecutionContext(
        session_id="s-session",
        approval_decider=decider,
    )
    options = {"tool_execution_context": ctx}
    [e async for e in runner.stream_events(state, "test", options)]

    assert len(decider_calls) == 1
    assert len(executor.calls) == 1
    tool_calls = await store.list_tool_calls("s-session")
    assert tool_calls[0].status == "success"


@pytest.mark.asyncio
async def test_ordinary_confirm_allow_once_authorizes_original_arguments(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-ordinary"))
    executor = _RecordingExecutor()
    definition = ToolDefinition(
        name="publish",
        description="Publish a target.",
        input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        risk_level=RiskLevel.CONFIRM,
    )
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [definition]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-ordinary",
        pending_tool_calls=[{
            "id": "call-ordinary",
            "function": {"name": "publish", "arguments": '{"target":"prod"}'},
        }],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                session_id="s-ordinary",
                approval_decider=lambda _: ApprovalDecision(True, "once"),
            )
        },
    )

    await runner.execute_tools(state)

    assert [call.arguments for call in executor.calls] == [{"target": "prod"}]
    assert state.tool_results[0]["status"] == "success"


@pytest.mark.asyncio
async def test_approval_display_arguments_cannot_mutate_executed_request(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-argument-isolation"))
    executor = _RecordingExecutor()
    definition = ToolDefinition(
        name="publish",
        description="Publish a target.",
        input_schema={"type": "object"},
        risk_level=RiskLevel.CONFIRM,
    )

    def decider(req: ApprovalRequest) -> ApprovalDecision:
        req.arguments.clear()
        return ApprovalDecision(True, "once")

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [definition]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-argument-isolation",
        pending_tool_calls=[{
            "id": "call-ordinary",
            "function": {"name": "publish", "arguments": '{"target":"prod"}'},
        }],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                approval_decider=decider,
            )
        },
    )

    await runner.execute_tools(state)

    assert executor.calls[0].arguments == {"target": "prod"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decider",
    [
        lambda _: (_ for _ in ()).throw(RuntimeError("sync failure")),
        lambda _: object(),
        lambda _: ApprovalDecision(True, "invalid"),
    ],
)
async def test_invalid_or_failing_decider_fails_closed(tmp_path, decider):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-fail-closed"))
    executor = _RecordingExecutor()
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-fail-closed",
        pending_tool_calls=[{
            "id": "call-1",
            "function": {"name": "manage_schedule", "arguments": '{"action":"list"}'},
        }],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                session_id="s-fail-closed",
                approval_decider=decider,
            )
        },
    )

    await runner.execute_tools(state)

    assert executor.calls == []
    assert state.tool_results[0]["status"] == "permission_denied"
    assert "sync failure" not in str(state.tool_results[0]["content"])


@pytest.mark.asyncio
async def test_decider_await_exception_fails_closed(tmp_path):
    async def decider(_: ApprovalRequest) -> ApprovalDecision:
        await asyncio.sleep(0)
        raise RuntimeError("await failure")

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-await-failure"))
    executor = _RecordingExecutor()
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-await-failure",
        pending_tool_calls=[{
            "id": "call-1",
            "function": {"name": "manage_schedule", "arguments": "{}"},
        }],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                approval_decider=decider,
            )
        },
    )

    await runner.execute_tools(state)

    assert executor.calls == []
    assert state.tool_results[0]["status"] == "permission_denied"
    assert "await failure" not in str(state.tool_results[0]["content"])


class _ApprovalAbort(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), _ApprovalAbort()])
async def test_decider_base_exceptions_propagate(tmp_path, error):
    async def decider(_: ApprovalRequest) -> ApprovalDecision:
        await asyncio.sleep(0)
        raise error

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-base-exception"))
    executor = _RecordingExecutor()
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-base-exception",
        pending_tool_calls=[{
            "id": "call-1",
            "function": {"name": "manage_schedule", "arguments": "{}"},
        }],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                approval_decider=decider,
            )
        },
    )

    with pytest.raises(type(error)):
        await runner.execute_tools(state)
    assert executor.calls == []


@pytest.mark.asyncio
async def test_unknown_tool_does_not_request_approval(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-missing"))
    executor = _RecordingExecutor()
    approvals: list[ApprovalRequest] = []
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, []),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-missing",
        pending_tool_calls=[{
            "id": "call-missing",
            "function": {"name": "missing", "arguments": "{}"},
        }],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                approval_decider=lambda request: approvals.append(request),
            )
        },
    )

    await runner.execute_tools(state)

    assert approvals == []
    assert state.tool_results[0]["status"] == "error"
    assert state.tool_results[0]["content"] == {"error": "tool not found"}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["null", "1", "[1, 2]"])
async def test_non_object_json_arguments_fail_without_approval_or_execution(
    tmp_path,
    arguments,
):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-invalid-arguments"))
    executor = _RecordingExecutor()
    approvals: list[ApprovalRequest] = []
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-invalid-arguments",
        pending_tool_calls=[{
            "id": "call-invalid",
            "function": {
                "name": "manage_schedule",
                "arguments": arguments,
            },
        }],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                approval_decider=lambda request: approvals.append(request),
            )
        },
    )

    await runner.execute_tools(state)

    assert approvals == []
    assert executor.calls == []
    assert state.tool_results == [{
        "tool_call_id": "call-invalid",
        "name": "manage_schedule",
        "status": "error",
        "content": {"error": "invalid arguments"},
        "duration_ms": 0,
    }]


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_interrupt_other_calls_in_same_response(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-invalid-batch"))
    executor = _RecordingExecutor()
    approvals: list[ApprovalRequest] = []

    def decider(request: ApprovalRequest) -> ApprovalDecision:
        approvals.append(request)
        return ApprovalDecision(True, "once")

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(
        session_id="s-invalid-batch",
        pending_tool_calls=[
            {
                "id": "call-invalid",
                "function": {
                    "name": "manage_schedule",
                    "arguments": "null",
                },
            },
            {
                "id": "call-valid",
                "function": {
                    "name": "manage_schedule",
                    "arguments": '{"action":"list"}',
                },
            },
        ],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                approval_decider=decider,
            )
        },
    )

    await runner.execute_tools(state)

    assert [request.tool_call_id for request in approvals] == ["call-valid"]
    assert [call.id for call in executor.calls] == ["call-valid"]
    assert [result["status"] for result in state.tool_results] == [
        "error",
        "success",
    ]


# --- T8: CliToolApprovalBridge integration with real AgentGraph ---


def _bridge_session_key(conv_id: str = "conv-bridge") -> GatewaySessionKey:
    return GatewaySessionKey("cli", conv_id, display_name=conv_id)


def _bridge_actor_id(conv_id: str = "conv-bridge") -> str:
    return f"cli:{conv_id}"


async def _drain_graph(runner: AgentGraphRunner, state: AgentState, options: dict) -> list:
    events = []
    async for evt in runner.stream_events(state, "test", options):
        events.append(evt)
    return events


@pytest.mark.asyncio
async def test_bridge_once_executes_tool_after_claim(tmp_path):
    """Real AgentGraph + bridge: pending appears, executor NOT called before claim,
    after claim+complete executor called exactly once, tool result flows back."""
    session_id = "s-bridge-once"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id=session_id))
    executor = _RecordingExecutor()

    bridge = CliToolApprovalBridge()
    notifier_records: list[dict] = []

    def notifier(meta: dict) -> None:
        notifier_records.append(meta)

    decider = bridge.create_decider(
        _bridge_session_key(), _bridge_actor_id(), notifier
    )
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list schedules"}],
    )
    ctx = ToolExecutionContext(session_id=session_id, approval_decider=decider)
    options = {"tool_execution_context": ctx}

    task = asyncio.create_task(_drain_graph(runner, state, options))
    # Wait for pending to appear
    for _ in range(100):
        if bridge.pending_count > 0:
            break
        await asyncio.sleep(0.01)
    assert bridge.pending_count == 1
    # Before claim, executor NOT called
    assert len(executor.calls) == 0
    # Notifier received metadata with id, kind, tool_name
    assert len(notifier_records) == 1
    assert notifier_records[0]["kind"] == "tool_policy"
    assert notifier_records[0]["tool_name"] == "manage_schedule"
    cid = notifier_records[0]["id"]
    # Claim and complete
    claim = bridge.claim(
        cid, "once", actor_id=_bridge_actor_id(), session_key=_bridge_session_key()
    )
    bridge.complete(claim)
    # Await graph completion
    events = await task
    # Executor called exactly once
    assert len(executor.calls) == 1
    assert executor.calls[0].name == "manage_schedule"
    # Tool call persisted as success
    tool_calls = await store.list_tool_calls(session_id)
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "success"
    # Stream completed
    assert any(e.type is ChatEventType.MESSAGE_DONE for e in events)
    # Pending cleaned up
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_bridge_trust_session_grants_and_second_call_skips_pending(tmp_path):
    """trust_session: grant updater receives real (session_id, actor_id, tool_name);
    second call with same tuple skips pending (grant checker returns True);
    different session still creates pending."""
    session_id = "s-bridge-trust"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id=session_id))
    executor = _RecordingExecutor()

    grant_store: set[tuple[str, str, str]] = set()

    def grant_updater(sid: str, aid: str, tn: str) -> None:
        grant_store.add((sid, aid, tn))

    def grant_checker(sid: str, aid: str, tn: str) -> bool:
        return (sid, aid, tn) in grant_store

    bridge = CliToolApprovalBridge()
    notifier_records: list[dict] = []

    def notifier(meta: dict) -> None:
        notifier_records.append(meta)

    decider = bridge.create_decider(
        _bridge_session_key(),
        _bridge_actor_id(),
        notifier,
        session_grant_updater=grant_updater,
        session_grant_checker=grant_checker,
    )
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list schedules"}],
    )
    ctx = ToolExecutionContext(session_id=session_id, approval_decider=decider)
    options = {"tool_execution_context": ctx}

    # First run: pending created, claim with trust_session
    task = asyncio.create_task(_drain_graph(runner, state, options))
    for _ in range(100):
        if bridge.pending_count > 0:
            break
        await asyncio.sleep(0.01)
    assert bridge.pending_count == 1
    assert len(notifier_records) == 1
    cid = notifier_records[0]["id"]
    claim = bridge.claim(
        cid, "trust_session",
        actor_id=_bridge_actor_id(), session_key=_bridge_session_key(),
    )
    bridge.complete(claim)
    await task
    # Grant updater received real internal session id, actor, tool name
    assert (session_id, _bridge_actor_id(), "manage_schedule") in grant_store
    assert len(executor.calls) == 1
    assert bridge.pending_count == 0

    # Second run with same session_id/actor/tool: grant checker returns True,
    # no pending created, executor called directly. Use a fresh provider so it
    # emits a tool call again (the original provider's call counter is exhausted).
    notifier_records.clear()
    runner2 = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state2 = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list again"}],
    )
    ctx2 = ToolExecutionContext(session_id=session_id, approval_decider=decider)
    options2 = {"tool_execution_context": ctx2}
    await _drain_graph(runner2, state2, options2)
    # No pending was created (grant hit)
    assert notifier_records == []
    assert bridge.pending_count == 0
    # Executor called once more (total 2)
    assert len(executor.calls) == 2

    # Different session: grant miss, pending created
    other_session = "s-bridge-trust-other"
    await store.create_session(ConversationSession(id=other_session))
    notifier_records.clear()
    runner3 = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state3 = AgentState(
        session_id=other_session,
        input_messages=[{"role": "user", "content": "list other"}],
    )
    ctx3 = ToolExecutionContext(session_id=other_session, approval_decider=decider)
    options3 = {"tool_execution_context": ctx3}
    task3 = asyncio.create_task(_drain_graph(runner3, state3, options3))
    for _ in range(100):
        if bridge.pending_count > 0:
            break
        await asyncio.sleep(0.01)
    assert bridge.pending_count == 1
    assert len(notifier_records) == 1
    cid3 = notifier_records[0]["id"]
    claim3 = bridge.claim(
        cid3, "once",
        actor_id=_bridge_actor_id(), session_key=_bridge_session_key(),
    )
    bridge.complete(claim3)
    await task3
    assert len(executor.calls) == 3
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_bridge_cancel_denies_and_executor_not_called(tmp_path):
    """cancel: deny/cancelled, executor=0, pending=0."""
    session_id = "s-bridge-cancel"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id=session_id))
    executor = _RecordingExecutor()

    bridge = CliToolApprovalBridge()
    notifier_records: list[dict] = []

    decider = bridge.create_decider(
        _bridge_session_key(), _bridge_actor_id(), notifier_records.append
    )
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id=session_id, approval_decider=decider)
    options = {"tool_execution_context": ctx}

    task = asyncio.create_task(_drain_graph(runner, state, options))
    for _ in range(100):
        if bridge.pending_count > 0:
            break
        await asyncio.sleep(0.01)
    cid = notifier_records[0]["id"]
    claim = bridge.claim(
        cid, "cancel",
        actor_id=_bridge_actor_id(), session_key=_bridge_session_key(),
    )
    bridge.complete(claim)
    await task
    assert len(executor.calls) == 0
    assert bridge.pending_count == 0
    tool_calls = await store.list_tool_calls(session_id)
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "permission_denied"


@pytest.mark.asyncio
async def test_bridge_timeout_denies_and_executor_not_called(tmp_path):
    """Short TTL timeout: deny/timeout, executor=0, pending=0."""
    session_id = "s-bridge-timeout"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id=session_id))
    executor = _RecordingExecutor()

    bridge = CliToolApprovalBridge(timeout_seconds=0.05)
    notifier_records: list[dict] = []

    decider = bridge.create_decider(
        _bridge_session_key(), _bridge_actor_id(), notifier_records.append
    )
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id=session_id, approval_decider=decider)
    options = {"tool_execution_context": ctx}

    # Run graph; do NOT claim - let pending expire
    await _drain_graph(runner, state, options)
    assert len(executor.calls) == 0
    assert bridge.pending_count == 0
    tool_calls = await store.list_tool_calls(session_id)
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "permission_denied"


@pytest.mark.asyncio
async def test_bridge_notifier_failure_denies_and_executor_not_called(tmp_path):
    """Notifier raises: deny/notification_failed, executor=0, pending=0."""
    session_id = "s-bridge-notifier-fail"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id=session_id))
    executor = _RecordingExecutor()

    bridge = CliToolApprovalBridge()

    def bad_notifier(_meta: dict) -> None:
        raise RuntimeError("notifier broken")

    decider = bridge.create_decider(
        _bridge_session_key(), _bridge_actor_id(), bad_notifier
    )
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id=session_id, approval_decider=decider)
    options = {"tool_execution_context": ctx}

    await _drain_graph(runner, state, options)
    assert len(executor.calls) == 0
    assert bridge.pending_count == 0
    tool_calls = await store.list_tool_calls(session_id)
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "permission_denied"


@pytest.mark.asyncio
async def test_bridge_decider_task_cancel_cleans_up(tmp_path):
    """Graph task cancelled while waiting for approval: executor=0, pending=0."""
    session_id = "s-bridge-task-cancel"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id=session_id))
    executor = _RecordingExecutor()

    bridge = CliToolApprovalBridge()
    notifier_records: list[dict] = []

    decider = bridge.create_decider(
        _bridge_session_key(), _bridge_actor_id(), notifier_records.append
    )
    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id=session_id, approval_decider=decider)
    options = {"tool_execution_context": ctx}

    task = asyncio.create_task(_drain_graph(runner, state, options))
    for _ in range(100):
        if bridge.pending_count > 0:
            break
        await asyncio.sleep(0.01)
    assert bridge.pending_count == 1
    # Cancel the graph task while waiting for approval
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass
    # Executor NOT called
    assert len(executor.calls) == 0
    # Bridge pending cleaned up (decider finally block)
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_bridge_concurrent_second_approval_denied(tmp_path):
    """Two concurrent decider calls: second gets deny/concurrent_approval.
    Uses direct decider calls (not AgentGraph, which is sequential) to simulate
    concurrent approval requests through the same bridge."""
    session_id = "s-bridge-concurrent"
    bridge = CliToolApprovalBridge()
    notifier_records: list[dict] = []

    def notifier(meta: dict) -> None:
        notifier_records.append(meta)

    decider = bridge.create_decider(
        _bridge_session_key(), _bridge_actor_id(), notifier
    )
    req1 = ApprovalRequest(
        session_id=session_id,
        tool_call_id="call-1",
        tool_name="manage_schedule",
        arguments={"action": "list"},
        description="d",
        risk_level=RiskLevel.CONFIRM,
    )
    req2 = ApprovalRequest(
        session_id=session_id,
        tool_call_id="call-2",
        tool_name="manage_schedule",
        arguments={"action": "list"},
        description="d",
        risk_level=RiskLevel.CONFIRM,
    )
    # Start first decider as a task (it will await the future)
    task1 = asyncio.create_task(decider(req1))
    # Wait for first pending to be registered
    for _ in range(100):
        if bridge.pending_count > 0:
            break
        await asyncio.sleep(0.01)
    assert bridge.pending_count == 1
    # Second decider called while first is still pending -> immediate deny
    result2 = await decider(req2)
    assert result2.allowed is False
    assert result2.scope == "deny"
    assert result2.reason == "concurrent_approval"
    # First pending still intact, second did not create a new pending
    assert bridge.pending_count == 1
    # Clean up the first pending
    bridge.discard_pending_for_actor(_bridge_actor_id(), _bridge_session_key())
    # First decider resolves with deny/cancelled
    result1 = await task1
    assert result1.allowed is False
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_bridge_arguments_summary_does_not_mutate_original_request(tmp_path):
    """Bridge's arguments_summary and notifier metadata must NOT mutate the
    original ToolCallRequest.arguments or ApprovalRequest.arguments."""
    session_id = "s-bridge-no-mutate"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id=session_id))
    executor = _RecordingExecutor()

    bridge = CliToolApprovalBridge()
    captured_requests: list[ApprovalRequest] = []

    def notifier(meta: dict) -> None:
        # Don't mutate anything here
        pass

    decider = bridge.create_decider(
        _bridge_session_key(), _bridge_actor_id(), notifier
    )

    # Wrap decider to capture the original request
    original_decider = decider

    async def capturing_decider(req: ApprovalRequest) -> ApprovalDecision:
        captured_requests.append(req)
        return await original_decider(req)

    runner = AgentGraphRunner(
        _ManageScheduleProvider(),
        ToolService(executor, [_manage_schedule_def()]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(
        session_id=session_id,
        input_messages=[{"role": "user", "content": "list"}],
    )
    ctx = ToolExecutionContext(session_id=session_id, approval_decider=capturing_decider)
    options = {"tool_execution_context": ctx}

    task = asyncio.create_task(_drain_graph(runner, state, options))
    for _ in range(100):
        if bridge.pending_count > 0:
            break
        await asyncio.sleep(0.01)
    cid = bridge._pending.keys() and next(iter(bridge._pending))
    claim = bridge.claim(
        cid, "once",
        actor_id=_bridge_actor_id(), session_key=_bridge_session_key(),
    )
    bridge.complete(claim)
    await task
    # Original request arguments unchanged
    assert len(captured_requests) == 1
    assert captured_requests[0].arguments == {"action": "list"}
    # Executor received original arguments
    assert len(executor.calls) == 1
    assert executor.calls[0].arguments == {"action": "list"}
