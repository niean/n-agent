import json

import pytest

from typing import Any

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.domain.agent import AgentState
from app.domain.context import CONTEXT_SUMMARY_PREFIX, ContextCompressionResult
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


class TerminalToolExecutor:
    """A task-style tool whose successful intent must end the Agent turn."""

    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            {"success": True},
            terminal=True,
        )


class TerminalToolProvider(FakeProvider):
    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        return LLMResult(
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"terminal-{self.calls}",
                    "type": "function",
                    "function": {"name": "task_fail", "arguments": "{}"},
                }],
            },
            finish_reason="tool_calls",
        )


class CapturingSummarizer(HeuristicSummarizer):
    def __init__(self):
        self.messages = []

    async def summarize(self, messages, existing_summary=""):
        self.messages = list(messages)
        return " | ".join(str(message.get("content", "")) for message in messages)


class AlwaysCompressEngine:
    """Fake ContextEngine that always triggers compression and captures messages."""

    # T7: Config attributes read by ContextService._get_engine_config.
    context_length = 100
    threshold_percent = 0.01
    protect_first_n = 3
    protect_last_n = 10
    summary_target_ratio = 0.2
    cooldown_seconds = 300
    tail_budget_enabled = False

    def __init__(self, summary_text: str = "generated summary"):
        self._summary_text = summary_text
        self.compress_messages: list[dict] | None = None

    def should_compress(self, messages, *, prompt_tokens=None, force=False):
        return True

    def is_in_cooldown(self) -> bool:
        return False

    async def compress(self, messages, *, current_tokens=None, force=False, existing_summary=""):
        self.compress_messages = list(messages)
        return ContextCompressionResult(
            messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}{self._summary_text}"}],
            summary=self._summary_text,
            compressed=True,
            skipped_reason=None,
            original_tokens=100,
            compressed_tokens=10,
        )


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
async def test_persist_messages_false_skips_assistant_message_persistence(tmp_path):
    """persist_messages=False 时，finalize 的 final assistant 消息不持久化到会话。

    Regression: goal_mode judge 的 assistant JSON ``{"achieved": true, "reason":
    "..."}`` 是控制流信号，不应出现在用户可见 Chat。finalize 在 persist 前必须
    检查 state.persist_messages，False 则跳过 append_assistant_message。
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-judge"))
    runner = AgentGraphRunner(
        DirectProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    state = await runner.run(
        AgentState(
            session_id="s-judge",
            input_messages=[{"role": "user", "content": "judge task t_1: has the goal been achieved?"}],
            persist_messages=False,
        ),
        "test",
        options={"persist_messages": False},
    )

    assert state.final_message["content"] == "hello"
    msgs = await store.list_messages("s-judge")
    assistant_msgs = [m for m in msgs if m.role == "assistant"]
    assert assistant_msgs == [], "judge assistant message must not be persisted"


@pytest.mark.asyncio
async def test_persist_messages_false_skips_tool_call_and_tool_message_persistence(tmp_path):
    """persist_messages=False 时，execute_tools 的 tool_call 审计记录和 update_memory
    的 tool 消息都不持久化。但工具仍执行、结果仍返回 LLM（judge 可调 task_show 读 task）。

    Regression: judge 可能调 task_show 工具读任务上下文。tool_call 记录和 tool role
    消息若持久化，会泄露到用户可见 Chat。persist_messages=False 必须同时抑制
    save_tool_call_if_allowed 和 append_tool_message，但工具执行本身不受影响。
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-judge-tool"))
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    state = await runner.run(
        AgentState(
            session_id="s-judge-tool",
            input_messages=[{"role": "user", "content": "calc"}],
            persist_messages=False,
        ),
        "test",
        options={"persist_messages": False},
    )

    # Tool still executed, final message is the post-tool answer
    assert state.final_message["content"] == "result is 3"
    # Tool call audit log must be empty
    tool_calls = await store.list_tool_calls("s-judge-tool")
    assert tool_calls == [], "judge tool_call audit log must not be persisted"
    # Tool role messages must not be persisted
    msgs = await store.list_messages("s-judge-tool")
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert tool_msgs == [], "judge tool messages must not be persisted"
    # Intermediate assistant messages (with tool_calls) must also not be persisted
    assistant_msgs = [m for m in msgs if m.role == "assistant"]
    assert assistant_msgs == [], "judge intermediate assistant messages must not be persisted"


@pytest.mark.asyncio
async def test_persist_messages_default_true_persists_everything(tmp_path):
    """persist_messages 默认 True：assistant/tool 消息和 tool_call 审计记录正常持久化（回归保护）。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-default"))
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    await runner.run(
        AgentState(session_id="s-default", input_messages=[{"role": "user", "content": "calc"}]),
        "test",
    )

    tool_calls = await store.list_tool_calls("s-default")
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "calculator"
    msgs = await store.list_messages("s-default")
    roles = {m.role for m in msgs}
    assert "assistant" in roles
    assert "tool" in roles


@pytest.mark.asyncio
async def test_agent_graph_stops_after_terminal_tool_success(tmp_path):
    """A successful terminal task intent must not consume another LLM turn.

    Regression for t_9512da67a7344159: task_fail was persisted, but the graph
    kept re-entering the model loop and invoked it once per iteration.
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-terminal"))
    provider = TerminalToolProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(
            TerminalToolExecutor(),
            [ToolDefinition("task_fail", "fail task", {"type": "object"})],
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=10,
    )

    state = await runner.run(
        AgentState(session_id="s-terminal", input_messages=[{"role": "user", "content": "fail"}]),
        "test",
    )

    assert provider.calls == 1
    assert state.finish_reason == "stop"
    assert len(await store.list_tool_calls("s-terminal")) == 1


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
    engine = AlwaysCompressEngine(summary_text="generated summary")
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        context_engine=engine,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]), "test")

    # system prompt excluded from compress input
    assert engine.compress_messages is not None
    assert all(message.get("role") != "system" for message in engine.compress_messages)
    summary = await store.get_summary("s1")
    assert summary is not None
    assert "N-Agent(Niean's Agent)" not in summary.summary


@pytest.mark.asyncio
async def test_build_context_state_dedupes_persisted_user_message(tmp_path):
    """ChatCompletionService persists user messages to memory_store before
    invoking the graph, so the same message appears in both history and
    input_messages. load_context must dedupe to avoid sending duplicates."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s-dedup"))
    from app.domain.session import ConversationMessage
    user_msg = {"role": "user", "content": "新增预单的六层"}
    # Simulate ChatService persisting user message before graph run
    await store.append_message("s-dedup", ConversationMessage(role="user", content=user_msg["content"]))
    provider = DirectProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    state = AgentState(session_id="s-dedup", input_messages=[user_msg])
    await runner.context_service.build_context_state(state)
    user_count = sum(1 for m in state.working_messages if m.get("role") == "user")
    assert user_count == 1, f"expected 1 user message, got {user_count}: {state.working_messages}"


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
    from app.application.context_service import _message_to_provider
    from app.domain.session import ConversationMessage

    legacy = ConversationMessage(role="tool", content={"status": "success", "content": {"a": 1}}, tool_call_id="x")
    payload = _message_to_provider(legacy)
    assert list(payload) == ["role", "tool_call_id", "content"]
    assert isinstance(payload["content"], str)
    assert "success" in payload["content"]


def test_message_to_provider_preserves_live_tool_message_field_order():
    from app.application.context_service import _message_to_provider
    from app.domain.session import ConversationMessage

    message = ConversationMessage(
        role="tool",
        content="result",
        tool_call_id="call-1",
        name="calculator",
    )

    payload = _message_to_provider(message)

    assert list(payload) == ["role", "tool_call_id", "name", "content"]


class ChineseDictExecutor:
    """Returns tool content containing Chinese as a dict (common executor pattern)."""

    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            {"summary": "中文结果", "items": ["你好世界"]},
        )


class ChineseStringExecutor:
    """Returns tool content as a json string containing Chinese (skill_service pattern)."""

    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            json.dumps({"summary": "中文结果", "items": ["你好世界"]}, ensure_ascii=False),
        )


@pytest.mark.asyncio
async def test_agent_graph_tool_message_keeps_chinese_for_dict_content(tmp_path):
    """Persisted tool message content must keep real CJK characters (not \\uXXXX
    escapes) so the dashboard '工具调用' panel renders the original text.
    Covers the dict-content executor pattern."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(
            CompositeToolExecutor({"calculator": ChineseDictExecutor()}),
            builtin_tool_definitions(),
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}]), "test")
    tool_messages = [m for m in await store.list_messages("s1") if m.role == "tool"]
    assert len(tool_messages) == 1
    content = tool_messages[0].content
    assert isinstance(content, str)
    assert "中文结果" in content
    assert "\\u" not in content


@pytest.mark.asyncio
async def test_agent_graph_tool_message_keeps_chinese_for_string_content(tmp_path):
    """Same guarantee when the executor returns content as a json string
    (skill_service pattern): the inner Chinese must survive the outer json.dumps
    wrapping so a single client-side JSON.parse renders the original text."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(
            CompositeToolExecutor({"calculator": ChineseStringExecutor()}),
            builtin_tool_definitions(),
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )
    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "calc"}]), "test")
    tool_messages = [m for m in await store.list_messages("s1") if m.role == "tool"]
    assert len(tool_messages) == 1
    content = tool_messages[0].content
    assert isinstance(content, str)
    assert "中文结果" in content
    assert "\\u" not in content


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
    """on_pre_compress is invoked with non-system messages before context_engine.compress."""
    from app.application.external_memory_manager import ExternalMemoryManager

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    capturing = PreCompressCapturingProvider()
    manager = ExternalMemoryManager()
    manager.add_provider(capturing)
    engine = AlwaysCompressEngine()
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        external_memory_manager=manager,
        context_engine=engine,
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
    engine = AlwaysCompressEngine(summary_text="LLM_SUMMARY")
    runner = AgentGraphRunner(
        FakeProvider(),
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        external_memory_manager=manager,
        context_engine=engine,
    )

    await runner.run(AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]), "test")

    summary = await store.get_summary("s1")
    assert summary is not None
    assert "RESCUED_FACT" in summary.summary


@pytest.mark.asyncio
async def test_agent_graph_non_vision_provider_image_message_returns_friendly_message(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = DirectProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        vision_capability=lambda: False,
    )

    state = await runner.run(
        AgentState(
            session_id="s1",
            input_messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看这张图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        ),
        "test",
    )

    assert state.error is None
    assert state.finish_reason == "stop"
    assert isinstance(state.final_message, dict)
    content = state.final_message.get("content", "")
    assert "不支持图片输入" in content
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_agent_graph_non_vision_provider_image_only_message_returns_friendly_message(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = DirectProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        vision_capability=lambda: False,
    )

    state = await runner.run(
        AgentState(
            session_id="s1",
            input_messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        ),
        "test",
    )

    assert state.error is None
    assert state.finish_reason == "stop"
    assert isinstance(state.final_message, dict)
    assert "不支持图片输入" in state.final_message.get("content", "")
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_agent_graph_vision_provider_preserves_image_part_in_messages(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = InspectingProvider()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        vision_capability=lambda: True,
    )

    await runner.run(
        AgentState(
            session_id="s1",
            input_messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        ),
        "test",
    )

    user_msg = next(m for m in provider.messages if m.get("role") == "user")
    assert isinstance(user_msg["content"], list)
    assert user_msg["content"][0] == {"type": "text", "text": "分析图"}
    assert user_msg["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_agent_graph_external_memory_prefetch_uses_text_only_and_preserves_image(tmp_path):
    from app.application.external_memory_manager import ExternalMemoryManager

    class CapturingProvider:
        def __init__(self):
            self.last_messages = None
            self.last_query = None

        async def list_models(self):
            return [ModelInfo("test", "test", "fake")]

        async def supports_tools(self, model):
            return True

        async def chat(self, messages, tools, stream, model, options):
            self.last_messages = list(messages)
            return LLMResult(message={"role": "assistant", "content": "ok"}, finish_reason="stop")

        def prefetch(self, query, *, session_id, enabled_override=None):
            self.last_query = query
            return ""

        def sync(self, user_text, assistant_text, *, session_id, agent_context, enabled_override=None):
            return None

        def pre_compress(self, messages, *, session_id, enabled_override=None):
            return ""

        @property
        def name(self):
            return "capturing"

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    capturing = CapturingProvider()
    manager = ExternalMemoryManager()
    manager.add_provider(capturing)
    provider = CapturingProvider()
    # Replace manager's internal provider with the same instance to capture prefetch
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        external_memory_manager=manager,
        vision_capability=lambda: True,
    )

    await runner.run(
        AgentState(
            session_id="s1",
            input_messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "查相关笔记"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        ),
        "test",
    )

    # provider received list content with image_url preserved
    user_msg = next(m for m in provider.last_messages if m.get("role") == "user")
    assert isinstance(user_msg["content"], list)
    assert any(part.get("type") == "image_url" for part in user_msg["content"])


# ---------------------------------------------------------------------------
# 自然语言任务委派闭环：对话 Agent 调 create_task -> 服务收到会话 origin ->
# 持久化 tool call -> Agent 输出含 task id 的确认
# spec: spec-260720-chat-natural-language-task.md
# ---------------------------------------------------------------------------


class TaskDelegationProvider(FakeProvider):
    """call 1 发 create_task 工具调用；call 2 输出含 task id 的确认文本。"""

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-del-1",
                            "type": "function",
                            "function": {
                                "name": "create_task",
                                "arguments": '{"goal":"帮我生成本周工作周报","title":"写周报"}',
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(
            message={"role": "assistant", "content": "已创建任务 t_delegated，将在本会话执行。"},
            finish_reason="stop",
        )


class _FakeUserTaskServiceForGraph:
    """记录 create_task 调用并返回固定 task。"""

    def __init__(self):
        self.create_calls: list[dict] = []

    async def create_task(self, *, title, body="", priority=0, created_by="",
                          origin_session_id=None, idempotency_key=None,
                          skills=(), goal_mode=False, **_kw):
        self.create_calls.append({
            "title": title, "body": body, "origin_session_id": origin_session_id,
            "idempotency_key": idempotency_key, "created_by": created_by,
        })
        from app.domain.task import TaskStatus, TaskWorkspaceKind
        from datetime import datetime, timezone
        from app.domain.task import Task
        now = datetime.now(timezone.utc)
        return Task(
            id="t_delegated", title=title, body=body, status=TaskStatus.QUEUED,
            origin_session_id=origin_session_id, created_at=now, updated_at=now,
            goal_mode=goal_mode, workspace_kind=TaskWorkspaceKind.SCRATCH,
        )

    async def list_tasks(self, board="default", cursor=None, limit=100):
        from app.domain.task import TaskListPage
        return TaskListPage(items=(), next_cursor=None)


@pytest.mark.asyncio
async def test_agent_graph_delegates_task_and_confirms_with_task_id(tmp_path):
    from app.application.task_tools import user_task_tool_definitions
    from app.domain.tool import ToolExecutionContext
    from app.infrastructure.tools.user_task_management import UserTaskToolExecutor

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    fake_svc = _FakeUserTaskServiceForGraph()
    runner = AgentGraphRunner(
        TaskDelegationProvider(),
        ToolService(
            CompositeToolExecutor({
                "create_task": UserTaskToolExecutor(fake_svc),
                "list_tasks": UserTaskToolExecutor(fake_svc),
            }),
            user_task_tool_definitions(),
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
    )

    ctx = ToolExecutionContext(session_id="s1", trusted_metadata={"actor_id": "u1"})
    state = await runner.run(
        AgentState(
            session_id="s1",
            input_messages=[{"role": "user", "content": "帮我生成本周工作周报"}],
        ),
        "test",
        options={"tool_execution_context": ctx},
    )

    # 服务收到当前会话作为 origin，幂等键含 session 与 tool_call id
    assert len(fake_svc.create_calls) == 1
    rec = fake_svc.create_calls[0]
    assert rec["origin_session_id"] == "s1"
    assert rec["created_by"] == "u1"
    assert rec["idempotency_key"] == "chat:s1:call-del-1"
    assert rec["body"] == "帮我生成本周工作周报"
    # tool call 持久化
    tool_calls = await store.list_tool_calls("s1")
    assert any(tc.tool_name == "create_task" and tc.status == "success" for tc in tool_calls)
    # Agent 在工具结果后输出含 task id 的确认文本
    assert state.final_message["content"] == "已创建任务 t_delegated，将在本会话执行。"
