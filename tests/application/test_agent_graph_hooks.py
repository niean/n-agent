"""T10: AgentGraphRunner lifecycle hook dispatch tests.

Covers 10 of the 12 hook sites (session start/end are in test_session_hooks.py):
- on_turn_start / on_turn_end (S1)
- pre_llm_call / post_llm_call + context injection (S2)
- on_pre_compress (S2)
- pre_tool_call / post_tool_call / transform_tool_result (S3)
- pre_finalize / transform_llm_output + streaming buffer (S4)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.context import CONTEXT_SUMMARY_PREFIX, ContextCompressionResult
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import (
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor


# ---------------------------------------------------------------------------
# Fake hook dispatcher
# ---------------------------------------------------------------------------


class FakeHookDispatcher:
    """Records every invoke_hook call and optionally returns canned results."""

    def __init__(
        self,
        *,
        pre_llm_context: str | None = None,
        transform_tool_result_value: Any = None,
        transform_llm_output_value: str | None = None,
    ):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._pre_llm_context = pre_llm_context
        self._transform_tool_result_value = transform_tool_result_value
        self._transform_llm_output_value = transform_llm_output_value

    async def invoke_hook(self, hook_name: str, **kwargs: Any) -> list[Any]:
        self.calls.append((hook_name, dict(kwargs)))
        if hook_name == "pre_llm_call" and self._pre_llm_context is not None:
            return [self._pre_llm_context]
        if hook_name == "transform_tool_result" and self._transform_tool_result_value is not None:
            return [self._transform_tool_result_value]
        if hook_name == "transform_llm_output" and self._transform_llm_output_value is not None:
            return [self._transform_llm_output_value]
        return []

    def calls_for(self, hook_name: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == hook_name]


# ---------------------------------------------------------------------------
# Shared providers / fakes
# ---------------------------------------------------------------------------


class _SingleTurnProvider:
    """Provider that returns a single plain-text assistant message."""

    def __init__(self, content: str = "hello from llm"):
        self._content = content
        self.chat_calls: list[dict[str, Any]] = []

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.chat_calls.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "options": options,
        })
        return LLMResult(
            message={"role": "assistant", "content": self._content},
            finish_reason="stop",
            usage={"total_tokens": 10},
        )


class _ToolThenReplyProvider:
    """First call returns a tool_call, second returns plain text."""

    def __init__(self, tool_name: str = "calculator", args: str = '{"expression":"1+2"}'):
        self._tool_name = tool_name
        self._args = args
        self.calls = 0
        self.chat_calls: list[dict[str, Any]] = []

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        self.chat_calls.append({"messages": messages, "model": model})
        if self.calls == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": self._tool_name, "arguments": self._args},
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(
            message={"role": "assistant", "content": "tool done"},
            finish_reason="stop",
        )


class _StubToolExecutor:
    def __init__(self, result_content: dict[str, Any] | None = None):
        self._result_content = result_content or {"ok": True}

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        return ToolResult(
            request.id, request.name, ToolResultStatus.SUCCESS, self._result_content,
        )


class _ErrorProvider:
    """Provider that always raises."""

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        raise RuntimeError("provider boom")


class _LoopToolProvider:
    """Provider that always returns tool_calls (triggers iteration limit)."""

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


# ---------------------------------------------------------------------------
# Minimal memory store for graph tests
# ---------------------------------------------------------------------------


class _FakeMemoryStore:
    """Minimal MemoryStore for AgentGraphRunner graph tests."""

    def __init__(self):
        self.appended_assistant: list[tuple[str, Any]] = []
        self.appended_tool: list[tuple[str, Any]] = []
        self.saved_task_states: list[Any] = []
        self.saved_tool_calls: list[Any] = []
        self._sessions: dict[str, ConversationSession] = {}
        self._messages: list[Any] = []

    async def get_session(self, session_id):
        return self._sessions.get(session_id)

    async def create_session(self, session):
        self._sessions[session.id] = session
        return session

    async def list_messages(self, session_id):
        return list(self._messages)

    async def get_summary(self, session_id):
        return None

    async def save_summary(self, summary):
        return summary

    async def append_message(self, session_id, message):
        self._messages.append(message)
        return message

    async def append_summary_message(self, session_id, message):
        return message

    async def mark_messages_summarized(self, session_id, message_ids):
        return 0

    async def save_task_state(self, task_state):
        self.saved_task_states.append(task_state)
        return task_state


# ---------------------------------------------------------------------------
# S1: on_turn_start / on_turn_end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_turn_start_fires_at_run_entry(tmp_path):
    """on_turn_start dispatches once at run() entry with session_id."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _SingleTurnProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    await runner.run(state, "test")

    starts = dispatcher.calls_for("on_turn_start")
    assert len(starts) == 1
    assert starts[0]["session_id"] == "s1"
    assert "metadata" in starts[0]


@pytest.mark.asyncio
async def test_on_turn_end_fires_once_on_normal_completion(tmp_path):
    """on_turn_end dispatches exactly once on normal completion."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _SingleTurnProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    result = await runner.run(state, "test")

    ends = dispatcher.calls_for("on_turn_end")
    assert len(ends) == 1
    assert ends[0]["session_id"] == "s1"
    assert ends[0]["finish_reason"] == result.finish_reason
    assert ends[0]["error"] is None or ends[0]["error"] == result.error


@pytest.mark.asyncio
async def test_on_turn_end_fires_once_on_provider_exception(tmp_path):
    """on_turn_end dispatches exactly once when provider raises (error caught in call_llm)."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _ErrorProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    result = await runner.run(state, "test")

    # call_llm catches the exception and sets state.error
    assert result.error is not None
    ends = dispatcher.calls_for("on_turn_end")
    assert len(ends) == 1
    assert ends[0]["error"] is not None


@pytest.mark.asyncio
async def test_on_turn_end_fires_once_on_iteration_limit(tmp_path):
    """on_turn_end dispatches exactly once when iteration limit is hit."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _LoopToolProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=1,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "loop"}])
    result = await runner.run(state, "test")

    assert result.error == "iteration limit reached"
    ends = dispatcher.calls_for("on_turn_end")
    assert len(ends) == 1


@pytest.mark.asyncio
async def test_on_turn_end_fires_once_on_cancel(tmp_path):
    """on_turn_end dispatches exactly once on cancellation."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()

    class _CancelProvider:
        async def list_models(self):
            return [ModelInfo("test", "test", "fake")]

        async def supports_tools(self, model: str):
            return True

        async def chat(self, messages, tools, stream, model, options):
            raise asyncio.CancelledError()

    runner = AgentGraphRunner(
        _CancelProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(asyncio.CancelledError):
        await runner.run(state, "test")

    ends = dispatcher.calls_for("on_turn_end")
    assert len(ends) == 1


@pytest.mark.asyncio
async def test_stream_events_does_not_duplicate_turn_hooks(tmp_path):
    """stream_events reuses run() path; on_turn_start/end must NOT be dispatched twice."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _SingleTurnProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    assert len(dispatcher.calls_for("on_turn_start")) == 1
    assert len(dispatcher.calls_for("on_turn_end")) == 1


@pytest.mark.asyncio
async def test_no_dispatcher_means_no_dispatch(tmp_path):
    """When hook_dispatcher is None (default), no hooks fire and behavior is unchanged."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        _SingleTurnProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    result = await runner.run(state, "test")
    assert result.finish_reason == "stop"
    assert result.error is None


# ---------------------------------------------------------------------------
# S2: pre_llm_call / post_llm_call + context injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_llm_call_fires_before_each_provider_call(tmp_path):
    """pre_llm_call dispatches once per provider call, after working messages prepared."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _SingleTurnProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    await runner.run(state, "test")

    pre_calls = dispatcher.calls_for("pre_llm_call")
    assert len(pre_calls) == 1
    kw = pre_calls[0]
    assert kw["session_id"] == "s1"
    assert "model" in kw
    assert "user_message" in kw
    assert "conversation_history" in kw
    assert kw["iteration_count"] == 0  # before increment


@pytest.mark.asyncio
async def test_post_llm_call_fires_after_provider_success(tmp_path):
    """post_llm_call dispatches once after provider success + normalization."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    provider = _SingleTurnProvider(content="reply text")
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    await runner.run(state, "test")

    post_calls = dispatcher.calls_for("post_llm_call")
    assert len(post_calls) == 1
    kw = post_calls[0]
    assert kw["session_id"] == "s1"
    assert kw["assistant_content"] == "reply text"
    assert kw["tool_calls"] == []
    assert kw["usage"] == {"total_tokens": 10}
    assert kw["iteration_count"] == 1  # after increment


@pytest.mark.asyncio
async def test_pre_llm_call_context_injected_into_string_content(tmp_path):
    """pre_llm_call merged context is appended to last user message (string)."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher(pre_llm_context="INJECTED_CONTEXT")
    provider = _SingleTurnProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "original"}])
    await runner.run(state, "test")

    # Provider received the injected context
    messages_sent = provider.chat_calls[0]["messages"]
    last_user = next(m for m in reversed(messages_sent) if m.get("role") == "user")
    assert "INJECTED_CONTEXT" in last_user["content"]
    assert "original" in last_user["content"]


@pytest.mark.asyncio
async def test_pre_llm_call_context_injected_into_multimodal_content(tmp_path):
    """pre_llm_call merged context is appended as text part for multimodal content."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher(pre_llm_context="INJECTED")
    provider = _SingleTurnProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        vision_capability=lambda: True,
        hook_dispatcher=dispatcher,
    )
    multimodal_content = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ]
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": multimodal_content}],
    )
    await runner.run(state, "test")

    messages_sent = provider.chat_calls[0]["messages"]
    last_user = next(m for m in reversed(messages_sent) if m.get("role") == "user")
    assert isinstance(last_user["content"], list)
    text_parts = [p for p in last_user["content"] if p.get("type") == "text"]
    assert any("INJECTED" in p.get("text", "") for p in text_parts)


@pytest.mark.asyncio
async def test_pre_llm_call_injection_is_ephemeral(tmp_path):
    """Injected context must NOT be written back to AgentState, session, or summary."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher(pre_llm_context="EPHEMERAL_CTX")
    provider = _ToolThenReplyProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}])
    result = await runner.run(state, "test")

    # State working_messages should NOT contain the injected context
    for msg in result.working_messages:
        content = str(msg.get("content", ""))
        assert "EPHEMERAL_CTX" not in content

    # DB messages should NOT contain the injected context
    db_messages = await store.list_messages("s1")
    for msg in db_messages:
        content = str(getattr(msg, "content", "") or msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", ""))
        assert "EPHEMERAL_CTX" not in content

    # Second provider call's conversation_history (pre_llm_call kwarg) should
    # NOT contain the injected context from the first call (ephemeral injection).
    pre_llm_calls = dispatcher.calls_for("pre_llm_call")
    if len(pre_llm_calls) >= 2:
        second_history = pre_llm_calls[1].get("conversation_history", [])
        for msg in second_history:
            content = str(msg.get("content", ""))
            assert "EPHEMERAL_CTX" not in content


@pytest.mark.asyncio
async def test_pre_llm_call_skips_injection_when_no_user_message(tmp_path, caplog):
    """When there's no user message, injection is skipped with a warning."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher(pre_llm_context="SHOULD_NOT_APPEAR")
    provider = _SingleTurnProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    # State with no user message in working_messages
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[{"role": "system", "content": "sys"}],
    )
    await runner.run(state, "test")

    # Provider should NOT have received the injected context
    messages_sent = provider.chat_calls[0]["messages"]
    for msg in messages_sent:
        content = str(msg.get("content", ""))
        assert "SHOULD_NOT_APPEAR" not in content


@pytest.mark.asyncio
async def test_on_pre_compress_fires_only_when_compressing(tmp_path):
    """on_pre_compress fires only when should_compress is True and compression happens."""
    from app.domain.context import ContextCompressionResult

    class _AlwaysCompressEngine:
        context_length = 100
        threshold_percent = 0.01
        protect_first_n = 3
        protect_last_n = 10
        summary_target_ratio = 0.2
        cooldown_seconds = 300
        tail_budget_enabled = False

        def should_compress(self, messages, *, prompt_tokens=None, force=False):
            return True

        def is_in_cooldown(self):
            return False

        async def compress(self, messages, *, current_tokens=None, force=False, existing_summary=""):
            return ContextCompressionResult(
                messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}sum"}],
                summary="sum",
                compressed=True,
                skipped_reason=None,
                original_tokens=500,
                compressed_tokens=50,
            )

    class _FakeMS:
        def __init__(self):
            self.saved_summaries = []
            self.appended_summaries = []
        async def get_summary(self, sid): return None
        async def save_summary(self, s):
            self.saved_summaries.append(s); return s
        async def list_messages(self, sid): return []
        async def append_message(self, sid, m): return m
        async def save_task_state(self, ts): return ts
        async def append_summary_message(self, sid, m):
            self.appended_summaries.append((sid, m)); return m
        async def mark_messages_summarized(self, sid, ids): return 0

    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _SingleTurnProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        _FakeMS(),
        HeuristicSummarizer(),
        context_engine=_AlwaysCompressEngine(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": "msg"}],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "long history that needs compression"},
        ],
        summary="",
        run_options={"force_compress": True},
    )
    await runner.prepare_context(state)

    compress_calls = dispatcher.calls_for("on_pre_compress")
    assert len(compress_calls) == 1
    assert compress_calls[0]["session_id"] == "s1"
    assert "messages" in compress_calls[0]
    assert "estimated_tokens" in compress_calls[0]


@pytest.mark.asyncio
async def test_on_pre_compress_does_not_fire_when_should_compress_false():
    """on_pre_compress does NOT fire when should_compress returns False."""
    from app.domain.context import ContextCompressionResult

    class _NeverCompressEngine:
        def should_compress(self, messages, *, prompt_tokens=None, force=False):
            return False
        def is_in_cooldown(self):
            return False
        async def compress(self, messages, **kwargs):
            raise AssertionError("should not be called")

    class _FakeMS:
        async def get_summary(self, sid): return None
        async def save_summary(self, s): return s
        async def list_messages(self, sid): return []
        async def append_message(self, sid, m): return m
        async def save_task_state(self, ts): return ts
        async def append_summary_message(self, sid, m): return m
        async def mark_messages_summarized(self, sid, ids): return 0

    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _SingleTurnProvider(),
        None,
        _FakeMS(),
        HeuristicSummarizer(),
        context_engine=_NeverCompressEngine(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg"},
        ],
        summary="",
    )
    await runner.prepare_context(state)

    assert len(dispatcher.calls_for("on_pre_compress")) == 0


# ---------------------------------------------------------------------------
# S3: pre_tool_call / post_tool_call / transform_tool_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_tool_call_fires_before_tool_service_evaluation(tmp_path):
    """pre_tool_call dispatches before ToolService.evaluate_execution."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _ToolThenReplyProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}])
    await runner.run(state, "test")

    pre_calls = dispatcher.calls_for("pre_tool_call")
    assert len(pre_calls) == 1
    kw = pre_calls[0]
    assert kw["session_id"] == "s1"
    assert kw["tool_call_id"] == "call-1"
    assert kw["tool_name"] == "calculator"
    assert "args" in kw
    assert kw["args"].get("expression") == "1+2"


@pytest.mark.asyncio
async def test_post_tool_call_fires_after_tool_result(tmp_path):
    """post_tool_call dispatches after final ToolResult (success)."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _ToolThenReplyProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}])
    await runner.run(state, "test")

    post_calls = dispatcher.calls_for("post_tool_call")
    assert len(post_calls) == 1
    kw = post_calls[0]
    assert kw["tool_call_id"] == "call-1"
    assert kw["tool_name"] == "calculator"
    assert "result" in kw
    assert "duration_ms" in kw
    assert kw["result"]["status"] == "success"


@pytest.mark.asyncio
async def test_post_tool_call_fires_on_denied_tool(tmp_path):
    """post_tool_call dispatches even when the tool is denied (no approval decider)."""
    from app.domain.tool import RiskLevel

    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()

    # Use a CONFIRM-risk tool with no approval decider -> permission_denied
    confirm_tool = ToolDefinition(
        "calculator", "calc",
        {"type": "object", "properties": {"expression": {"type": "string"}}},
        risk_level=RiskLevel.CONFIRM,
    )
    from app.domain.tool import ToolExecutionContext
    tool_ctx = ToolExecutionContext(session_id="s1")

    runner = AgentGraphRunner(
        _ToolThenReplyProvider(),
        ToolService(_StubToolExecutor(), [confirm_tool]),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": "calc"}],
        run_options={"tool_execution_context": tool_ctx},
    )
    await runner.run(state, "test")

    post_calls = dispatcher.calls_for("post_tool_call")
    assert len(post_calls) == 1
    assert post_calls[0]["result"]["status"] == "permission_denied"


@pytest.mark.asyncio
async def test_transform_tool_result_replaces_content_before_persist(tmp_path):
    """transform_tool_result replaces content before tool message encode + persist."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher(
        transform_tool_result_value="TRANSFORMED_RESULT",
    )
    runner = AgentGraphRunner(
        _ToolThenReplyProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}])
    result = await runner.run(state, "test")

    # The tool message in working_messages should contain the transformed content
    tool_msgs = [m for m in result.working_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    tool_content = json.loads(tool_msgs[0]["content"])
    assert tool_content["content"] == "TRANSFORMED_RESULT"

    # DB tool calls should also have the transformed content
    db_tool_calls = await store.list_tool_calls("s1")
    assert len(db_tool_calls) == 1
    assert db_tool_calls[0].result["content"] == "TRANSFORMED_RESULT"


@pytest.mark.asyncio
async def test_transform_tool_result_does_not_expose_trusted_metadata(tmp_path):
    """post_tool_call and transform_tool_result must not expose ToolExecutor or trusted metadata."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()

    class _StubExecutor:
        async def execute(self, request, context=None):
            return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"data": "ok"})

    runner = AgentGraphRunner(
        _ToolThenReplyProvider(),
        ToolService(
            _StubExecutor(),
            [ToolDefinition("calculator", "calc", {"type": "object"})],
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    from app.domain.tool import ToolExecutionContext
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": "calc"}],
        run_options={
            "tool_execution_context": ToolExecutionContext(
                session_id="s1",
                trusted_metadata={"secret_key": "should_not_leak"},
            ),
        },
    )
    await runner.run(state, "test")

    # Check that no hook call payload contains trusted_metadata or secret_key
    for hook_name, kwargs in dispatcher.calls:
        for key, value in kwargs.items():
            serialized = json.dumps(value, default=str, ensure_ascii=False)
            assert "should_not_leak" not in serialized, f"trusted metadata leaked in {hook_name}.{key}"


# ---------------------------------------------------------------------------
# S4: pre_finalize / transform_llm_output + streaming buffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_finalize_fires_at_finalize_entry(tmp_path):
    """pre_finalize dispatches once at finalize node entry."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _SingleTurnProvider(content="final answer"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    result = await runner.run(state, "test")

    pre_calls = dispatcher.calls_for("pre_finalize")
    assert len(pre_calls) == 1
    kw = pre_calls[0]
    assert kw["session_id"] == "s1"
    assert "content" in kw
    assert "finish_reason" in kw
    assert "error" in kw


@pytest.mark.asyncio
async def test_transform_llm_output_replaces_content_in_result(tmp_path):
    """transform_llm_output replaces final assistant content before persist."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher(
        transform_llm_output_value="TRANSFORMED_OUTPUT",
    )
    runner = AgentGraphRunner(
        _SingleTurnProvider(content="original output"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    result = await runner.run(state, "test")

    # Result final_message should have transformed content
    assert result.final_message["content"] == "TRANSFORMED_OUTPUT"

    # DB should also have transformed content
    db_messages = await store.list_messages("s1")
    assistant_msgs = [m for m in db_messages if m.role == "assistant"]
    assert any(m.content == "TRANSFORMED_OUTPUT" for m in assistant_msgs)


@pytest.mark.asyncio
async def test_transform_llm_output_streaming_client_matches_db(tmp_path):
    """Streaming: client-visible text must equal DB-persisted text after transform."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher(
        transform_llm_output_value="STREAM_TRANSFORMED",
    )
    runner = AgentGraphRunner(
        _SingleTurnProvider(content="original"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}])
    events = [e async for e in runner.stream_events(state, "test")]

    # Collect all content deltas
    content_events = [e for e in events if e.type is ChatEventType.CONTENT_DELTA]
    client_text = "".join(e.content for e in content_events)
    assert client_text == "STREAM_TRANSFORMED"

    # DB should also have the transformed text
    db_messages = await store.list_messages("s1")
    assistant_msgs = [m for m in db_messages if m.role == "assistant"]
    assert any(m.content == "STREAM_TRANSFORMED" for m in assistant_msgs)


@pytest.mark.asyncio
async def test_fixed_relative_order_of_hooks(tmp_path):
    """Assert the fixed relative order: turn_start -> pre/post_llm -> pre/post_tool ->
    transform_tool_result -> pre_finalize -> transform_llm_output -> turn_end."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    runner = AgentGraphRunner(
        _ToolThenReplyProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        hook_dispatcher=dispatcher,
    )
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}])
    await runner.run(state, "test")

    hook_names = [name for name, _ in dispatcher.calls]
    # Find key indices
    idx_turn_start = hook_names.index("on_turn_start")
    idx_pre_llm_1 = hook_names.index("pre_llm_call")
    idx_post_llm_1 = hook_names.index("post_llm_call")
    idx_pre_tool = hook_names.index("pre_tool_call")
    idx_post_tool = hook_names.index("post_tool_call")
    idx_transform_tool = hook_names.index("transform_tool_result")
    idx_pre_finalize = hook_names.index("pre_finalize")
    idx_transform_llm = hook_names.index("transform_llm_output")
    idx_turn_end = hook_names.index("on_turn_end")

    assert idx_turn_start < idx_pre_llm_1
    assert idx_pre_llm_1 < idx_post_llm_1
    assert idx_post_llm_1 < idx_pre_tool
    assert idx_pre_tool < idx_post_tool
    assert idx_post_tool < idx_transform_tool
    assert idx_transform_tool < idx_pre_finalize
    assert idx_pre_finalize < idx_transform_llm
    assert idx_transform_llm < idx_turn_end
