import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionInput, ChatCompletionService
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor


class FakeProvider:
    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        return LLMResult(message={"role": "assistant", "content": "hello"}, finish_reason="stop")


class ErrorProvider(FakeProvider):
    async def chat(self, messages, tools, stream, model, options):
        raise RuntimeError("provider failure")


@pytest.mark.asyncio
async def test_chat_service_non_stream_returns_message(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "agent.db")
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    service = ChatCompletionService(store, runner)

    result = await service.complete(
        ChatCompletionInput(model="test", messages=[{"role": "user", "content": "hi"}], stream=False)
    )

    assert result.message["content"] == "hello"
    assert result.session_id.startswith("tmp-")


@pytest.mark.asyncio
async def test_chat_service_stream_produces_events(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "agent.db")
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    service = ChatCompletionService(store, runner)

    stream = await service.complete(
        ChatCompletionInput(model="test", messages=[{"role": "user", "content": "hi"}], stream=True)
    )
    events = [event async for event in stream]

    assert events[0].type == ChatEventType.MESSAGE_START
    assert events[-1].type == ChatEventType.DONE
    assert any(event.type == ChatEventType.CONTENT_DELTA for event in events)


@pytest.mark.asyncio
async def test_chat_service_binds_session_id(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "agent.db")
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    service = ChatCompletionService(store, runner)

    result = await service.complete(
        ChatCompletionInput(model="test", messages=[{"role": "user", "content": "hi"}], stream=False, session_id="my-session")
    )

    assert result.session_id == "my-session"
    messages = await store.list_messages("my-session")
    assert len(messages) >= 1