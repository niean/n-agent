import pytest

from typing import Any

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.domain.tool import RiskLevel, ToolCallRequest, ToolDefinition, ToolResult, ToolResultStatus
from app.domain.session import ConversationSession
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor
from app.infrastructure.tools.composite import CompositeToolExecutor


class FakeProvider:
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


class LoopProvider(FakeProvider):
    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        return LLMResult(
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{self.calls}",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression":"1+2"}'},
                    }
                ],
            },
            finish_reason="tool_calls",
        )


class DirectProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.last_messages = []

    async def chat(self, messages, tools, stream, model, options):
        self.last_messages = list(messages)
        self.calls += 1
        return LLMResult(message={"role": "assistant", "content": "hello"}, finish_reason="stop")


class KnowledgeProvider(FakeProvider):
    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-kb-1",
                            "type": "function",
                            "function": {"name": "search_knowledge", "arguments": '{"query":"python"}'},
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(message={"role": "assistant", "content": "Python answer from snippets"}, finish_reason="stop")


class CapturingToolsProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.tools = []

    async def chat(self, messages, tools, stream, model, options):
        self.tools = list(tools)
        self.calls += 1
        return LLMResult(message={"role": "assistant", "content": "hello"}, finish_reason="stop")


class CapturingReplayProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.messages_by_call = []

    async def chat(self, messages, tools, stream, model, options):
        self.messages_by_call.append(list(messages))
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
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": '{"expression":"2+3"}'},
                        },
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(message={"role": "assistant", "content": "done"}, finish_reason="stop")


class InspectingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.messages = []

    async def chat(self, messages, tools, stream, model, options):
        self.messages = list(messages)
        self.calls += 1
        return LLMResult(message={"role": "assistant", "content": "next"}, finish_reason="stop")


class FakeKnowledgeExecutor:
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            {"site": "N-KB", "query": request.arguments["query"], "results": [{"snippet": "Python snippet"}]},
        )


class CapturingSummarizer(HeuristicSummarizer):
    def __init__(self):
        self.messages = []

    async def summarize(self, messages, existing_summary=""):
        self.messages = list(messages)
        return " | ".join(str(message.get("content", "")) for message in messages)


@pytest.mark.asyncio
async def test_agent_graph_executes_tool_loop_and_finalizes(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    state = await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}]), "test")

    assert state.final_message["content"] == "result is 3"
    assert (await store.list_tool_calls("s1"))[0].tool_name == "calculator"


@pytest.mark.asyncio
async def test_agent_graph_calls_knowledge_tool_and_persists_tool_call(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    builtin = build_builtin_tool_executor(tmp_path)
    kb = FakeKnowledgeExecutor()
    runner = AgentGraphRunner(
        KnowledgeProvider(),
        ToolService(
            CompositeToolExecutor(
                {
                    "get_current_time": builtin,
                    "calculator": builtin,
                    "list_directory": builtin,
                    "read_text_file": builtin,
                    "search_knowledge": kb,
                }
            ),
            builtin_tool_definitions() + knowledge_tool_definitions(),
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    state = await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "search kb"}]), "test")
    tool_calls = await store.list_tool_calls("s1")

    assert state.final_message["content"] == "Python answer from snippets"
    assert tool_calls[0].tool_name == "search_knowledge"
    assert tool_calls[0].status == "success"


@pytest.mark.asyncio
async def test_agent_graph_injects_system_prompt_without_persisting_message(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = DirectProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]), "test")

    assert provider.last_messages[0]["role"] == "system"
    assert "N-Agent(Niean's Agent)" in provider.last_messages[0]["content"]
    assert "search_knowledge" in provider.last_messages[0]["content"]
    persisted_messages = await store.list_messages("s1")
    assert [message.role for message in persisted_messages] == ["assistant"]


@pytest.mark.asyncio
async def test_agent_graph_excludes_system_prompt_from_summary(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = DirectProvider()
    summarizer = CapturingSummarizer()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        summarizer,
        iteration_limit=3,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]), "test")

    assert all(message.get("role") != "system" for message in summarizer.messages)
    summary = await store.get_summary("s1")
    assert summary is not None
    assert "N-Agent(Niean's Agent)" not in summary.summary


@pytest.mark.asyncio
async def test_agent_graph_uses_safe_only_tool_surface_when_requested(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = CapturingToolsProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(
            build_builtin_tool_executor(tmp_path),
            builtin_tool_definitions()
            + [ToolDefinition("confirm_tool", "confirm", {"type": "object"}, RiskLevel.CONFIRM)],
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    await runner.run(
        AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
        "test",
        {"tool_exposure_policy": "safe_only"},
    )

    names = {schema["function"]["name"] for schema in provider.tools}
    assert "confirm_tool" not in names
    assert "calculator" in names


@pytest.mark.asyncio
async def test_agent_graph_reports_iteration_limit(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        LoopProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=1,
    )

    state = await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "loop"}]), "test")

    assert state.error == "iteration limit reached"
    messages = await store.list_messages("s1")
    assert messages[-1].role == "assistant"
    assert "工具调用上限" in messages[-1].content


@pytest.mark.asyncio
async def test_agent_graph_streams_chat_events(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    events = [
        event
        async for event in runner.stream_events(
            AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}]),
            "test",
        )
    ]

    assert events[0].type == ChatEventType.MESSAGE_START
    assert any(event.type == ChatEventType.CONTENT_DELTA for event in events)
    assert events[-1].type == ChatEventType.DONE


@pytest.mark.asyncio
async def test_agent_graph_persists_tool_message_content_as_string(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}]), "test")

    persisted = await store.list_messages("s1")
    tool_messages = [message for message in persisted if message.role == "tool"]
    assert len(tool_messages) == 1
    assert all(isinstance(message.content, str) for message in tool_messages)


@pytest.mark.asyncio
async def test_agent_graph_preserves_assistant_tool_calls_for_replay(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = CapturingReplayProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}]), "test")

    second_call_messages = provider.messages_by_call[1]
    assistant_messages = [message for message in second_call_messages if message["role"] == "assistant"]
    assert assistant_messages[-1]["tool_calls"][0]["id"] == "call-1"
    assert len(assistant_messages[-1]["tool_calls"]) == 2
    persisted = await store.list_messages("s1")
    persisted_assistant = next(message for message in persisted if message.role == "assistant")
    assert persisted_assistant.content["tool_calls"][1]["id"] == "call-2"

    inspecting = InspectingProvider()
    replay_runner = AgentGraphRunner(
        inspecting,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    await replay_runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "again"}]), "test")

    replayed_assistant = [message for message in inspecting.messages if message["role"] == "assistant"][0]
    assert replayed_assistant["tool_calls"][0]["id"] == "call-1"
    assert len(replayed_assistant["tool_calls"]) == 2


@pytest.mark.asyncio
async def test_agent_graph_serializes_legacy_dict_tool_content_to_provider(tmp_path):
    from app.application.agent_graph import _message_to_provider
    from app.domain.session import ConversationMessage

    legacy = ConversationMessage(role="tool", content={"status": "success", "content": {"a": 1}}, tool_call_id="x")
    payload = _message_to_provider(legacy)
    assert isinstance(payload["content"], str)
    assert "success" in payload["content"]


class MemoryContextProvider:
    """Returns assistant content containing <memory-context> blocks."""

    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        return LLMResult(message={"role": "assistant", "content": self._content}, finish_reason="stop")


@pytest.mark.asyncio
async def test_stream_events_scrubs_complete_memory_context_block(tmp_path):
    """Complete <memory-context>...</memory-context> in model output never reaches SSE client."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        MemoryContextProvider("visible<memory-context>secret</memory-context>tail"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    events = [
        event
        async for event in runner.stream_events(
            AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
            "test",
        )
    ]

    deltas = [event.content for event in events if event.type == ChatEventType.CONTENT_DELTA]
    combined = "".join(deltas)
    assert "<memory-context>" not in combined
    assert "</memory-context>" not in combined
    assert "secret" not in combined
    assert "visible" in combined
    assert "tail" in combined


@pytest.mark.asyncio
async def test_stream_events_scrubs_unclosed_memory_context_span(tmp_path):
    """Unclosed <memory-context> (partial tag) survives one-shot scrub but streaming scrubber drops it."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        MemoryContextProvider("clean<memory-context>leaked payload never closed"),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    events = [
        event
        async for event in runner.stream_events(
            AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
            "test",
        )
    ]

    deltas = [event.content for event in events if event.type == ChatEventType.CONTENT_DELTA]
    combined = "".join(deltas)
    assert "<memory-context>" not in combined
    assert "leaked payload" not in combined
    assert "clean" in combined


class PreCompressCapturingProvider:
    """Minimal ExternalMemoryProvider that captures on_pre_compress invocations."""

    def __init__(self, *, rescue: str | None = None):
        self._rescue = rescue
        self.pre_compress_calls: list[list[dict]] = []

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        pass

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str) -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str) -> None:
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        return "{}"

    def shutdown(self) -> None:
        pass

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str | None:
        self.pre_compress_calls.append([dict(m) for m in messages])
        return self._rescue


@pytest.mark.asyncio
async def test_agent_graph_calls_on_pre_compress_before_summary(tmp_path):
    """on_pre_compress is invoked with non-system messages before summarizer runs."""
    from app.application.external_memory_manager import ExternalMemoryManager

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    capturing = PreCompressCapturingProvider()
    manager = ExternalMemoryManager()
    manager.add_provider(capturing)
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        external_memory_manager=manager,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]), "test")

    assert len(capturing.pre_compress_calls) >= 1
    for compressed_messages in capturing.pre_compress_calls:
        assert all(message.get("role") != "system" for message in compressed_messages)
    first_call_messages = capturing.pre_compress_calls[0]
    roles = [message.get("role") for message in first_call_messages]
    assert "user" in roles


@pytest.mark.asyncio
async def test_agent_graph_prepends_rescued_context_to_summary(tmp_path):
    """Non-empty on_pre_compress return value is prepended to the persisted summary."""
    from app.application.external_memory_manager import ExternalMemoryManager

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    capturing = PreCompressCapturingProvider(rescue="RESCUED_FACT")
    manager = ExternalMemoryManager()
    manager.add_provider(capturing)
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        external_memory_manager=manager,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]), "test")

    summary = await store.get_summary("s1")
    assert summary is not None
    assert "RESCUED_FACT" in summary.summary
