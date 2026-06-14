import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.domain.tool import ToolCallRequest, ToolResult, ToolResultStatus
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
    assert "N-Agent(Niean's Agent MVP)" in provider.last_messages[0]["content"]
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
    assert "N-Agent(Niean's Agent MVP)" not in summary.summary


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
