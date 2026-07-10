import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionInput, ChatCompletionService
from app.application.events import ChatEventType
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationMessage, ConversationSession
from app.domain.tool import ToolExecutionContext
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


class RecordingRunner:
    def __init__(self):
        self.options = None
        self.calls = []
        self.compress_calls = []

    async def run(self, state, model, options=None):
        self.options = dict(options or {})
        self.calls.append({"state": state, "model": model, "options": self.options})
        state.final_message = {"role": "assistant", "content": "ok"}
        state.finish_reason = "stop"
        return state

    async def compress_session(self, session_id):
        self.compress_calls.append(session_id)
        return {"compressed": True, "reason": None}

    def stream_events(self, state, model, options=None):
        raise AssertionError("stream not used")


def _build_service_with_runner(store, runner):
    session_service = SessionService(store)
    return ChatCompletionService(store, runner, session_service)


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
    assert result.session_id.startswith("api-")


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
async def test_chat_service_realtime_infers_confirm_context(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = ChatCompletionService(store, runner, SessionService(store))

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "新增 MCP 站点 https://example.com"}],
            stream=False,
        )
    )

    assert "tool_execution_context" in runner.options


@pytest.mark.asyncio
async def test_chat_service_unattended_disables_confirm_context_and_uses_safe_only(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = ChatCompletionService(store, runner, SessionService(store))

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "新增 MCP 站点 https://example.com"}],
            stream=False,
            options={"execution_context_mode": "unattended"},
        )
    )

    ctx = runner.options["tool_execution_context"]
    assert isinstance(ctx, ToolExecutionContext)
    assert ctx.execution_context_mode == "unattended"
    assert ctx.allowed_confirm_tools == {}
    assert ctx.permitted_managed_tools == set()
    assert runner.options["tool_exposure_policy"] == "safe_only"


@pytest.mark.asyncio
async def test_complete_injects_tool_execution_context_with_trusted_metadata(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = ChatCompletionService(store, runner, SessionService(store))

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            trusted_metadata={
                "gateway.platform": "feishu",
                "receive_id": "oc_a",
                "receive_id_type": "chat_id",
            },
            session_id="s1",
        )
    )

    ctx = runner.options["tool_execution_context"]
    assert isinstance(ctx, ToolExecutionContext)
    assert ctx.session_id == "s1"
    assert ctx.trusted_metadata["gateway.platform"] == "feishu"
    assert ctx.execution_context_mode == "realtime"
    assert ctx.permitted_managed_tools == {"manage_schedule"}


@pytest.mark.asyncio
async def test_complete_no_trusted_metadata_means_no_managed_tools(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = ChatCompletionService(store, runner, SessionService(store))

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            metadata={"gateway.platform": "feishu"},
            session_id="s2",
        )
    )

    ctx = runner.options["tool_execution_context"]
    assert ctx.permitted_managed_tools == set()


@pytest.mark.asyncio
async def test_complete_unattended_mode_has_no_managed_tools(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = ChatCompletionService(store, runner, SessionService(store))

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            options={"execution_context_mode": "unattended"},
            trusted_metadata={"gateway.platform": "feishu"},
            session_id="s3",
        )
    )

    ctx = runner.options["tool_execution_context"]
    assert ctx.execution_context_mode == "unattended"
    assert ctx.permitted_managed_tools == set()
    assert runner.options["tool_exposure_policy"] == "safe_only"


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


@pytest.mark.asyncio
async def test_chat_service_locks_external_memory_on_first_turn(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = ChatCompletionService(store, runner, SessionService(store))

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            session_id="s-memory",
            options={"external_memory_enabled": ["builtin", "project_memory_1"]},
        )
    )
    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "switch"}],
            stream=False,
            session_id="s-memory",
            options={"external_memory_enabled": ["builtin", "project_memory_2"]},
        )
    )

    session = await store.get_session("s-memory")
    assert session is not None
    assert session.external_memory_enabled == ["builtin", "project_memory_1"]
    assert runner.calls[0]["options"]["external_memory_enabled"] == ["builtin", "project_memory_1"]
    assert runner.calls[1]["options"]["external_memory_enabled"] == ["builtin", "project_memory_1"]
    assert runner.calls[1]["options"]["tool_execution_context"].enabled_override == ["builtin", "project_memory_1"]


@pytest.mark.asyncio
async def test_chat_service_locks_legacy_session_to_empty_profile(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-legacy"))
    await store.append_message("s-legacy", ConversationMessage(role="user", content="old"))
    runner = RecordingRunner()
    service = ChatCompletionService(store, runner, SessionService(store))

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "new"}],
            stream=False,
            session_id="s-legacy",
            options={"external_memory_enabled": ["builtin", "project_memory_1"]},
        )
    )

    session = await store.get_session("s-legacy")
    assert session is not None
    assert session.external_memory_enabled == []
    assert runner.options["external_memory_enabled"] == []


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


@pytest.mark.asyncio
async def test_complete_preserves_existing_acp_session_source(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = _build_service(store, tmp_path)
    await store.create_session(ConversationSession(id="s1", source="acp"))

    stream = await service.complete(
        ChatCompletionInput(
            model="test",
            session_id="s1",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    )
    events = [event async for event in stream]

    assert events[0].type == ChatEventType.MESSAGE_START
    assert events[-1].type == ChatEventType.DONE
    session = await store.get_session("s1")
    assert session is not None
    assert session.source == "acp"


@pytest.mark.asyncio
async def test_chat_service_normalizes_input_image_to_image_url_when_persisted(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = _build_service(store, tmp_path)

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看这张图"},
                        {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="},
                    ],
                }
            ],
            stream=False,
            session_id="s-vision",
        )
    )

    messages = await store.list_messages("s-vision")
    user_msgs = [m for m in messages if m.role == "user"]
    assert user_msgs
    content = user_msgs[0].content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_chat_service_title_receives_text_only_without_image_data(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    title_generator = StubTitleGenerator("图片问答")
    session_service = SessionService(store, title_generator=title_generator)
    service = ChatCompletionService(store, runner, session_service)

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "帮我分析图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
            stream=False,
            session_id="s-title-img",
        )
    )

    import asyncio

    for _ in range(20):
        session = await store.get_session("s-title-img")
        if session and not session.has_default_title():
            break
        await asyncio.sleep(0.05)

    assert title_generator.calls
    title_input = title_generator.calls[0]
    assert "data:image" not in title_input
    assert "image_url" not in title_input
    assert "[" not in title_input
    assert title_input == "帮我分析图"


@pytest.mark.asyncio
async def test_chat_service_image_only_message_persisted_and_not_dropped(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = _build_service(store, tmp_path)

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
            stream=False,
            session_id="s-img-only",
        )
    )

    messages = await store.list_messages("s-img-only")
    user_msgs = [m for m in messages if m.role == "user"]
    assert user_msgs
    content = user_msgs[0].content
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["type"] == "image_url"


@pytest.mark.asyncio
async def test_chat_service_compress_slash_command_skips_llm(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = _build_service_with_runner(store, runner)

    result = await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "/compress"}],
            stream=False,
            session_id="s-compress",
        )
    )

    assert result.finish_reason == "stop"
    assert "已压缩上下文" in result.message["content"]
    assert runner.compress_calls == ["s-compress"]
    assert runner.calls == []
    messages = await store.list_messages("s-compress")
    assert all(m.role != "user" for m in messages)


@pytest.mark.asyncio
async def test_chat_service_compress_slash_command_stream(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = _build_service_with_runner(store, runner)

    stream = await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "/compress"}],
            stream=True,
            session_id="s-compress-stream",
        )
    )

    events = [e async for e in stream]
    content_events = [e for e in events if e.type is ChatEventType.CONTENT_DELTA]
    assert content_events
    assert "已压缩上下文" in content_events[0].content
    assert runner.compress_calls == ["s-compress-stream"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_chat_service_conversational_compress_sets_force_option(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = _build_service_with_runner(store, runner)

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "请帮我压缩上下文"}],
            stream=False,
            session_id="s-conv-compress",
        )
    )

    assert runner.calls
    assert runner.options.get("force_compress") is True


@pytest.mark.asyncio
async def test_chat_service_conversational_compress_english_keyword(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = _build_service_with_runner(store, runner)

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "please compress context for me"}],
            stream=False,
            session_id="s-conv-en",
        )
    )

    assert runner.calls
    assert runner.options.get("force_compress") is True


@pytest.mark.asyncio
async def test_chat_service_no_force_for_unrelated_message(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = RecordingRunner()
    service = _build_service_with_runner(store, runner)

    await service.complete(
        ChatCompletionInput(
            model="test",
            messages=[{"role": "user", "content": "上下文太长了"}],
            stream=False,
            session_id="s-no-force",
        )
    )

    assert runner.calls
    assert "force_compress" not in runner.options
