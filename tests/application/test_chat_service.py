import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionInput, ChatCompletionService
from app.application.events import ChatEventType
from app.application.session_service import SessionService
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


def _build_service(store, tmp_path):
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    session_service = SessionService(store)
    return ChatCompletionService(store, runner, session_service)


@pytest.mark.asyncio
async def test_chat_service_non_stream_returns_message(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = _build_service(store, tmp_path)

    result = await service.complete(
        ChatCompletionInput(model="test", messages=[{"role": "user", "content": "hi"}], stream=False)
    )

    assert result.message["content"] == "hello"
    assert result.session_id.startswith("tmp-")


@pytest.mark.asyncio
async def test_chat_service_stream_produces_events(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = _build_service(store, tmp_path)

    stream = await service.complete(
        ChatCompletionInput(model="test", messages=[{"role": "user", "content": "hi"}], stream=True)
    )
    events = [event async for event in stream]

    assert events[0].type == ChatEventType.MESSAGE_START
    assert events[-1].type == ChatEventType.DONE
    assert any(event.type == ChatEventType.CONTENT_DELTA for event in events)


@pytest.mark.asyncio
async def test_chat_service_binds_session_id(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = _build_service(store, tmp_path)

    result = await service.complete(
        ChatCompletionInput(model="test", messages=[{"role": "user", "content": "hi"}], stream=False, session_id="my-session")
    )

    assert result.session_id == "my-session"
    messages = await store.list_messages("my-session")
    assert len(messages) >= 1


class StubTitleGenerator:
    def __init__(self, title: str):
        self.title = title
        self.calls: list[str] = []

    async def generate(self, user_message: str) -> str:
        self.calls.append(user_message)
        return self.title


@pytest.mark.asyncio
async def test_chat_service_delegates_title_generation_to_session_service(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    title_generator = StubTitleGenerator("如何新增预单")
    session_service = SessionService(store, title_generator=title_generator)
    service = ChatCompletionService(store, runner, session_service)

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "新增预单的流程"}],
            stream=False,
            session_id="s-title",
        )
    )

    import asyncio

    for _ in range(20):
        session = await store.get_session("s-title")
        if session and not session.has_default_title():
            break
        await asyncio.sleep(0.05)

    session = await store.get_session("s-title")
    assert session is not None
    assert session.title == "如何新增预单"
    assert title_generator.calls == ["新增预单的流程"]