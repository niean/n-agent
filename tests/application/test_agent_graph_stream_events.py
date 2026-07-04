import asyncio
import threading
import time

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import ToolCallRequest, ToolDefinition, ToolExecutionContext, ToolResult, ToolResultStatus
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import BuiltinToolExecutor, build_builtin_tool_executor


class _ToolProvider:
    """Provider：第一次返回 calculator tool_call，第二次返回 final message。"""

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
    # 工具调用必须产生 pending + success 两个 TOOL_CALL_DELTA
    tool_deltas = [e for e in events if e.type == ChatEventType.TOOL_CALL_DELTA]
    assert len(tool_deltas) >= 2
    statuses = [e.tool_call.get("status") for e in tool_deltas]
    assert "pending" in statuses
    assert "success" in statuses
    # 工具事件应在 content 之前
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


@pytest.mark.asyncio
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
    """无工具调用时不应发 TOOL_CALL_DELTA。"""

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
