"""Tests for AgentGraphRunner.stream_events.

Original tests: tool call delta emission, pending before slow tool, pure chat.
S7 tests: StreamGuard integration (secret redaction, cross-chunk, tool args, guard exception).
"""
import asyncio
import json
import threading
import time

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.information_flow_service import InformationFlowService
from app.application.policy_snapshot import InformationFlowPolicyConfig
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.information_flow import SecretCatalog
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import ToolCallRequest, ToolDefinition, ToolExecutionContext, ToolResult, ToolResultStatus
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import BuiltinToolExecutor, build_builtin_tool_executor


# ---------------------------------------------------------------------------
# Original test helpers
# ---------------------------------------------------------------------------


class _ToolProvider:
    """Provider: first call returns calculator tool_call, second returns final message."""

    def __init__(self):
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
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
                            "function": {"name": "calculator", "arguments": '{"expression":"1+2"}'},
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(message={"role": "assistant", "content": "result is 3"}, finish_reason="stop")


class _SlowToolExecutor:
    def __init__(self, started: asyncio.Event):
        self.started = started

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        self.started.set()
        await asyncio.sleep(0.2)
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


@pytest.mark.asyncio
async def test_stream_events_emits_tool_call_delta_for_tool_use(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        _ToolProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}])
    events = [e async for e in runner.stream_events(state, "test")]

    types = [e.type for e in events]
    assert ChatEventType.MESSAGE_START in types
    tool_deltas = [e for e in events if e.type == ChatEventType.TOOL_CALL_DELTA]
    assert len(tool_deltas) >= 2
    statuses = [e.tool_call.get("status") for e in tool_deltas]
    assert "pending" in statuses
    assert "success" in statuses
    first_tool_idx = next(i for i, e in enumerate(events) if e.type is ChatEventType.TOOL_CALL_DELTA)
    first_content_idx = next(
        (i for i, e in enumerate(events) if e.type is ChatEventType.CONTENT_DELTA),
        len(events),
    )
    assert first_tool_idx < first_content_idx
    assert ChatEventType.MESSAGE_DONE in types
    assert ChatEventType.DONE in types


@pytest.mark.asyncio
async def test_stream_events_emits_pending_before_slow_tool_finishes(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-slow"))
    started = asyncio.Event()
    runner = AgentGraphRunner(
        _ToolProvider(),
        ToolService(
            _SlowToolExecutor(started),
            [
                ToolDefinition(
                    "calculator",
                    "Slow test tool",
                    {"type": "object", "properties": {"expression": {"type": "string"}}},
                )
            ],
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(session_id="s-slow", input_messages=[{"role": "user", "content": "calc"}])
    stream = runner.stream_events(state, "test")

    first = await stream.__anext__()
    started_at = time.monotonic()
    second = await stream.__anext__()
    elapsed = time.monotonic() - started_at

    assert first.type is ChatEventType.MESSAGE_START
    assert second.type is ChatEventType.TOOL_CALL_DELTA
    assert second.tool_call["status"] == "pending"
    assert not started.is_set()
    assert elapsed < 0.15

    remaining = [event async for event in stream]
    assert started.is_set()
    assert any(event.type is ChatEventType.DONE for event in remaining)


class _WebFetchProvider:
    def __init__(self):
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
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
                            "id": "call-web-1",
                            "type": "function",
                            "function": {
                                "name": "web_fetch",
                                "arguments": '{"url":"https://wttr.in/Datong?format=j1&lang=zh","format":"json"}',
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(message={"role": "assistant", "content": "weather done"}, finish_reason="stop")


class _BlockingWebFetchExecutor(BuiltinToolExecutor):
    def __init__(self, workspace_root, started: threading.Event):
        super().__init__(workspace_root)
        self.started = started

    def _web_fetch(self, url: str, output_format: str):
        self.started.set()
        time.sleep(0.2)
        return {"url": url, "format": output_format, "ok": True}


@pytest.mark.asyncio
async def test_stream_events_emits_pending_before_blocking_web_fetch_finishes(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-web-fetch"))
    started = threading.Event()
    runner = AgentGraphRunner(
        _WebFetchProvider(),
        ToolService(_BlockingWebFetchExecutor(tmp_path, started), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(session_id="s-web-fetch", input_messages=[{"role": "user", "content": "weather"}])
    stream = runner.stream_events(state, "test")

    first = await stream.__anext__()
    started_at = time.monotonic()
    second = await stream.__anext__()
    elapsed = time.monotonic() - started_at

    assert first.type is ChatEventType.MESSAGE_START
    assert second.type is ChatEventType.TOOL_CALL_DELTA
    assert second.tool_call["status"] == "pending"
    assert elapsed < 0.15

    remaining = [event async for event in stream]
    assert started.is_set()
    assert any(event.type is ChatEventType.DONE for event in remaining)


@pytest.mark.asyncio
async def test_stream_events_no_tool_call_delta_for_pure_chat(tmp_path):
    """No tool call -> no TOOL_CALL_DELTA events."""

    class _PureProvider:
        async def list_models(self):
            return [ModelInfo("test", "test", "fake")]

        async def supports_tools(self, model: str):
            return True

        async def chat(self, messages, tools, stream, model, options):
            return LLMResult(message={"role": "assistant", "content": "hello"}, finish_reason="stop")

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s2"))
    runner = AgentGraphRunner(
        _PureProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(session_id="s2", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    tool_deltas = [e for e in events if e.type is ChatEventType.TOOL_CALL_DELTA]
    assert tool_deltas == []


# ---------------------------------------------------------------------------
# S7: StreamGuard integration tests
# ---------------------------------------------------------------------------


class _SecretContentProvider:
    """Provider that returns content containing a configured secret value."""

    def __init__(self, response_content: str):
        self._response_content = response_content
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        return LLMResult(
            message={"role": "assistant", "content": self._response_content},
            finish_reason="stop",
        )


def _make_secret_service(secret: str = "sk-secret123") -> InformationFlowService:
    return InformationFlowService(
        InformationFlowPolicyConfig(log_llm_payloads=False, store_usage_payloads=True, redact_secrets=True),
        SecretCatalog(secret_values=frozenset({secret})),
    )


@pytest.mark.asyncio
async def test_stream_events_secret_never_appears_in_content_deltas(tmp_path):
    """Secret value must never appear in any SSE CONTENT_DELTA event."""
    secret = "sk-secret123"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-secret"))
    runner = AgentGraphRunner(
        _SecretContentProvider(f"the key is {secret} and that is all"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        information_flow_service=_make_secret_service(secret),
    )
    state = AgentState(session_id="s-secret", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    content_events = [e for e in events if e.type is ChatEventType.CONTENT_DELTA]
    assert content_events, "expected at least one CONTENT_DELTA"
    for event in content_events:
        assert secret not in event.content, f"secret leaked in content delta: {event.content!r}"
    combined = "".join(e.content for e in content_events)
    assert "[REDACTED]" in combined


@pytest.mark.asyncio
async def test_stream_events_cross_chunk_secret_redacted(tmp_path):
    """Secret split across streaming chunks must be fully redacted."""
    secret = "abcdef"
    content = f"token={secret};done"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-cross"))
    runner = AgentGraphRunner(
        _SecretContentProvider(content),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        information_flow_service=_make_secret_service(secret),
    )
    state = AgentState(session_id="s-cross", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    content_events = [e for e in events if e.type is ChatEventType.CONTENT_DELTA]
    combined = "".join(e.content for e in content_events)
    assert "abcdef" not in combined
    assert "[REDACTED]" in combined


@pytest.mark.asyncio
async def test_stream_events_no_secret_passes_through(tmp_path):
    """When content has no secrets, it passes through unchanged."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-clean"))
    runner = AgentGraphRunner(
        _SecretContentProvider("hello world"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        information_flow_service=_make_secret_service("sk-secret123"),
    )
    state = AgentState(session_id="s-clean", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    content_events = [e for e in events if e.type is ChatEventType.CONTENT_DELTA]
    combined = "".join(e.content for e in content_events)
    assert combined == "hello world"


class _ToolWithSecretArgsProvider:
    """Provider that returns a tool call with credential-bearing arguments."""

    def __init__(self):
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
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
                                "name": "calculator",
                                "arguments": json.dumps({
                                    "expression": "1+2",
                                    "api_key": "sk-secret123",
                                    "token": "tok-abc",
                                }),
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(message={"role": "assistant", "content": "done"}, finish_reason="stop")


class _StubToolExecutor:
    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


@pytest.mark.asyncio
async def test_stream_events_tool_arguments_structurally_redacted(tmp_path):
    """Tool event arguments must have credential fields redacted before publishing."""
    secret = "sk-secret123"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-tool"))
    runner = AgentGraphRunner(
        _ToolWithSecretArgsProvider(),
        ToolService(
            _StubToolExecutor(),
            [ToolDefinition("calculator", "Calculator", {"type": "object"})],
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        information_flow_service=_make_secret_service(secret),
    )
    state = AgentState(session_id="s-tool", input_messages=[{"role": "user", "content": "calc"}])
    events = [e async for e in runner.stream_events(state, "test")]

    tool_events = [e for e in events if e.type is ChatEventType.TOOL_CALL_DELTA]
    assert tool_events, "expected at least one TOOL_CALL_DELTA"
    for event in tool_events:
        args = event.tool_call.get("arguments", {}) if event.tool_call else {}
        if isinstance(args, dict):
            assert args.get("api_key") == "[REDACTED]", f"api_key not redacted: {args}"
            assert args.get("token") == "[REDACTED]", f"token not redacted: {args}"
            assert args.get("expression") == "1+2"
        assert secret not in json.dumps(event.tool_call or {})


@pytest.mark.asyncio
async def test_stream_events_guard_exception_yields_stable_error(tmp_path):
    """When the StreamGuard raises, only a stable error event is emitted."""
    secret = "sk-secret123"
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-err"))
    runner = AgentGraphRunner(
        _SecretContentProvider(f"the key is {secret}"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        information_flow_service=_make_secret_service(secret),
    )

    original_create = runner._information_flow_service.create_stream_guard

    def faulty_guard_factory():
        guard = original_create()
        original_feed = guard.feed

        def faulty_feed(chunk: str) -> str:
            if "key" in chunk:
                raise RuntimeError("simulated guard failure")
            return original_feed(chunk)

        guard.feed = faulty_feed  # type: ignore[assignment]
        return guard

    runner._information_flow_service.create_stream_guard = faulty_guard_factory  # type: ignore[assignment]

    state = AgentState(session_id="s-err", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    error_events = [e for e in events if e.type is ChatEventType.ERROR]
    assert error_events, "expected an ERROR event on guard failure"
    assert error_events[-1].error == "information_release_denied"
    for event in events:
        if event.content:
            assert secret not in event.content
    assert events[-1].type == ChatEventType.DONE


# ---------------------------------------------------------------------------
# T9: Sync/stream end reason consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_and_non_stream_produce_same_finish_reason(tmp_path):
    """T9: stream_events and run() produce the same finish_reason for the same input."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-sync"))
    runner = AgentGraphRunner(
        _ToolProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(session_id="s-sync", input_messages=[{"role": "user", "content": "calc"}])
    result = await runner.run(AgentState(session_id="s-sync", input_messages=[{"role": "user", "content": "calc"}]), "test")
    non_stream_finish = result.finish_reason

    # Reset session for stream run
    store2 = SQLiteMemoryStore(tmp_path / "sessions2.db")
    await store2.create_session(ConversationSession(id="s-sync2"))
    runner2 = AgentGraphRunner(
        _ToolProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store2,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state2 = AgentState(session_id="s-sync2", input_messages=[{"role": "user", "content": "calc"}])
    events = [e async for e in runner2.stream_events(state2, "test")]
    done_event = next(e for e in events if e.type is ChatEventType.MESSAGE_DONE)
    stream_finish = done_event.finish_reason

    assert non_stream_finish == stream_finish


@pytest.mark.asyncio
async def test_stream_and_non_stream_produce_same_iteration_limit_reason(tmp_path):
    """T9: iteration limit produces the same end reason in stream and non-stream."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-il"))
    from app.domain.provider import LLMResult, ModelInfo

    class _LoopProvider:
        async def list_models(self):
            return [ModelInfo("test", "test", "fake")]

        async def supports_tools(self, model: str):
            return True

        async def chat(self, messages, tools, stream, model, options):
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "calculator", "arguments": "{}"}},
                    ],
                },
                finish_reason="tool_calls",
            )

    runner = AgentGraphRunner(
        _LoopProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=1,
    )
    result = await runner.run(
        AgentState(session_id="s-il", input_messages=[{"role": "user", "content": "loop"}]), "test",
    )
    assert result.error == "iteration limit reached"
    assert result.finish_reason == "length"

    # Stream path
    store2 = SQLiteMemoryStore(tmp_path / "sessions2.db")
    await store2.create_session(ConversationSession(id="s-il2"))
    runner2 = AgentGraphRunner(
        _LoopProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store2,
        HeuristicSummarizer(),
        iteration_limit=1,
    )
    state2 = AgentState(session_id="s-il2", input_messages=[{"role": "user", "content": "loop"}])
    events = [e async for e in runner2.stream_events(state2, "test")]
    done_event = next(e for e in events if e.type is ChatEventType.MESSAGE_DONE)
    assert done_event.finish_reason == "length"


# ---------------------------------------------------------------------------
# T10: Hook dispatch integration with streaming
# ---------------------------------------------------------------------------


class _RecordingDispatcher:
    """Minimal hook dispatcher that records calls and can transform output."""

    def __init__(self, transform_llm_output_value: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._transform_value = transform_llm_output_value

    async def invoke_hook(self, hook_name: str, **kwargs):
        self.calls.append((hook_name, dict(kwargs)))
        if hook_name == "transform_llm_output" and self._transform_value is not None:
            return [self._transform_value]
        return []

    def calls_for(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


@pytest.mark.asyncio
async def test_stream_events_with_hooks_no_duplicate_turn_dispatch(tmp_path):
    """T10: stream_events reuses run() path; on_turn_start/end fire exactly once."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s-hook-stream"))
    dispatcher = _RecordingDispatcher()
    runner = AgentGraphRunner(
        _ToolProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s-hook-stream", input_messages=[{"role": "user", "content": "calc"}])
    events = [e async for e in runner.stream_events(state, "test")]

    assert len(dispatcher.calls_for("on_turn_start")) == 1
    assert len(dispatcher.calls_for("on_turn_end")) == 1
    # Streaming should still produce the expected event types
    assert any(e.type is ChatEventType.MESSAGE_DONE for e in events)
    assert any(e.type is ChatEventType.DONE for e in events)


@pytest.mark.asyncio
async def test_stream_events_transform_llm_output_client_matches_db(tmp_path):
    """T10: streaming buffers final content, applies transform_llm_output,
    then yields. Client text must equal DB-persisted text."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s-tx-stream"))
    dispatcher = _RecordingDispatcher(transform_llm_output_value="STREAM_TX_RESULT")
    runner = AgentGraphRunner(
        _SecretContentProvider("original content"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s-tx-stream", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    content_events = [e for e in events if e.type is ChatEventType.CONTENT_DELTA]
    client_text = "".join(e.content for e in content_events)
    assert client_text == "STREAM_TX_RESULT"

    db_messages = await store.list_messages("s-tx-stream")
    assistant_msgs = [m for m in db_messages if m.role == "assistant"]
    assert any(m.content == "STREAM_TX_RESULT" for m in assistant_msgs)


@pytest.mark.asyncio
async def test_stream_events_without_dispatcher_unchanged(tmp_path):
    """T10: streaming without hook_dispatcher produces same behavior as before."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s-no-hook"))
    runner = AgentGraphRunner(
        _ToolProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    state = AgentState(session_id="s-no-hook", input_messages=[{"role": "user", "content": "calc"}])
    events = [e async for e in runner.stream_events(state, "test")]

    types = [e.type for e in events]
    assert ChatEventType.MESSAGE_START in types
    assert ChatEventType.MESSAGE_DONE in types
    assert ChatEventType.DONE in types
    tool_deltas = [e for e in events if e.type is ChatEventType.TOOL_CALL_DELTA]
    assert len(tool_deltas) >= 2
