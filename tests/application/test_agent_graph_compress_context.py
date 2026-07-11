from __future__ import annotations

import asyncio

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.domain.agent import AgentState
from app.domain.context import CONTEXT_SUMMARY_PREFIX, ContextCompressionResult
from app.domain.memory import MemoryStore, Summarizer
from app.domain.provider import LLMProvider, LLMResult
from app.domain.session import ConversationMessage, Summary


class FakeContextEngine:
    def __init__(self, result: ContextCompressionResult):
        self._result = result
        self.compress_calls = 0
        self.should_compress_calls = 0
        self.last_should_compress_force = None
        self.last_compress_force = None

    def should_compress(self, messages, *, prompt_tokens=None, force=False):
        self.should_compress_calls += 1
        self.last_should_compress_force = force
        return True

    async def compress(self, messages, *, current_tokens=None, force=False, existing_summary=""):
        self.compress_calls += 1
        self.last_compress_force = force
        return self._result


class FakeMemoryStore:
    """Minimal MemoryStore for compress_context / update_memory tests."""

    def __init__(self, messages=None):
        self.saved_summaries = []
        self.appended_messages = []
        self.saved_task_states = []
        self.appended_summaries = []
        self.summarized_message_ids: list[str] = []
        self._messages = messages or []

    async def get_summary(self, session_id):
        return None

    async def save_summary(self, summary: Summary):
        self.saved_summaries.append(summary)
        return summary

    async def list_messages(self, session_id):
        return list(self._messages)

    async def append_message(self, session_id, message):
        self.appended_messages.append((session_id, message))
        return message

    async def save_task_state(self, task_state):
        self.saved_task_states.append(task_state)
        return task_state

    async def append_summary_message(self, session_id, message):
        self.appended_summaries.append((session_id, message))
        return message

    async def mark_messages_summarized(self, session_id, message_ids):
        self.summarized_message_ids = list(message_ids)
        return len(message_ids)


class FakeLLMProvider:
    async def list_models(self): return []
    async def supports_tools(self, model): return True

    def __init__(self):
        self.call_count = 0
        self.last_kwargs: dict = {}

    async def chat(self, messages, tools, stream, model, options):
        self.call_count += 1
        self.last_kwargs = {
            "messages": messages, "tools": tools, "stream": stream,
            "model": model, "options": options,
        }
        return LLMResult(message={"role": "assistant", "content": "ok"}, finish_reason="stop", usage={}, raw=None)


@pytest.mark.asyncio
async def test_compress_context_replaces_working_messages():
    compressed_msgs = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary text"}]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="summary text", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "long history"},
        ],
        summary="",
    )
    new_state = await runner.compress_context(state)
    assert fake_engine.compress_calls == 1
    # system message preserved
    assert new_state.working_messages[0]["role"] == "system"
    # non-system replaced by compressed
    assert new_state.working_messages[1] == compressed_msgs[0]
    assert new_state.summary == "summary text"
    assert len(memory_store.saved_summaries) == 1
    assert memory_store.saved_summaries[0].summary == "summary text"
    assert len(memory_store.appended_summaries) == 1


@pytest.mark.asyncio
async def test_compress_context_preserves_all_leading_system_messages():
    result = ContextCompressionResult(
        messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary text"}],
        summary="summary text",
        compressed=True,
        skipped_reason=None,
        original_tokens=500,
        compressed_tokens=50,
    )
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=FakeMemoryStore(),
        summarizer=None,
        context_engine=FakeContextEngine(result),
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "system prompt 1"},
            {"role": "system", "content": "system prompt 2"},
            {"role": "user", "content": "long history"},
        ],
        summary="",
    )
    new_state = await runner.compress_context(state)
    assert [m["content"] for m in new_state.working_messages[:2]] == [
        "system prompt 1",
        "system prompt 2",
    ]
    assert new_state.working_messages[2]["content"] == f"{CONTEXT_SUMMARY_PREFIX}summary text"


@pytest.mark.asyncio
async def test_compress_context_skips_when_engine_none():
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=FakeMemoryStore(),
        summarizer=None,
        context_engine=None,
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[{"role": "system", "content": "sys"}],
        summary="",
    )
    new_state = await runner.compress_context(state)
    assert new_state.working_messages == state.working_messages  # unchanged


@pytest.mark.asyncio
async def test_compress_context_skips_when_should_compress_false():
    class NeverCompressEngine:
        def should_compress(self, messages, *, prompt_tokens=None, force=False):
            return False
        async def compress(self, messages, **kwargs):
            raise AssertionError("should not be called")
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=NeverCompressEngine(),
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg"},
        ],
        summary="old summary",
    )
    new_state = await runner.compress_context(state)
    assert new_state.working_messages == state.working_messages
    assert new_state.summary == "old summary"
    assert len(memory_store.saved_summaries) == 0


@pytest.mark.asyncio
async def test_compress_context_does_not_save_summary_when_compressor_skips():
    result = ContextCompressionResult(
        messages=[{"role": "user", "content": "unchanged"}],
        summary="old summary",
        compressed=False,
        skipped_reason="below_threshold",
        original_tokens=10,
        compressed_tokens=None,
    )
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=FakeContextEngine(result),
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[{"role": "user", "content": "msg"}],
        summary="old summary",
    )
    new_state = await runner.compress_context(state)
    assert new_state.summary == "old summary"
    assert memory_store.saved_summaries == []


@pytest.mark.asyncio
async def test_compress_context_save_summary_failure_does_not_rollback():
    """save_summary 失败时：messages 表已更新（append_summary_message 已成功），
    state 仍更新，不抛异常（降级接受 Dashboard 滞后一轮）。"""
    class FailingMemoryStore(FakeMemoryStore):
        async def save_summary(self, summary: Summary):
            raise RuntimeError("sqlite failed")

    result = ContextCompressionResult(
        messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary"}],
        summary="summary",
        compressed=True,
        skipped_reason=None,
        original_tokens=500,
        compressed_tokens=50,
    )
    memory_store = FailingMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=FakeContextEngine(result),
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "msg"}],
        summary="",
    )
    # 不再抛异常
    new_state = await runner.compress_context(state)
    # append_summary_message 已调用（messages 表已更新）
    assert len(memory_store.appended_summaries) == 1
    # state 仍更新（降级接受 Dashboard 滞后）
    assert new_state.summary == "summary"


@pytest.mark.asyncio
async def test_update_memory_no_longer_calls_summarizer():
    """update_memory should not call summarizer.summarize after refactor."""
    class TrackingSummarizer:
        def __init__(self):
            self.calls = 0
        async def summarize(self, messages, existing_summary=""):
            self.calls += 1
            return "should not be called"
    memory_store = FakeMemoryStore()
    summarizer = TrackingSummarizer()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=summarizer,
        context_engine=None,
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "reply"},
        ],
        summary="",
        final_message={"role": "assistant", "content": "reply"},
    )
    await runner.update_memory(state)
    assert summarizer.calls == 0


class FakeExternalMemoryManager:
    """Tracks pre_compress_all and prefetch_all calls for boundary tests."""

    def __init__(self, rescued_context: str = "RESCUED", memory_context: str = "<memory-context>mem</memory-context>"):
        self.pre_compress_calls = 0
        self.prefetch_calls = 0
        self.last_pre_compress_messages = None
        self._rescued_context = rescued_context
        self._memory_context = memory_context

    def pre_compress_all(self, messages, *, session_id, enabled_override=None):
        self.pre_compress_calls += 1
        self.last_pre_compress_messages = messages
        return self._rescued_context

    def prefetch_all(self, query, *, session_id, enabled_override=None):
        self.prefetch_calls += 1
        return self._memory_context


@pytest.mark.asyncio
async def test_compress_context_calls_pre_compress_all_before_compress():
    """pre_compress_all is called only when should_compress=True, before context_engine.compress."""
    compressed_msgs = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary text"}]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="summary text", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    fake_emm = FakeExternalMemoryManager(rescued_context="RESCUED_KEY_POINTS")
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        external_memory_manager=fake_emm,
        context_engine=fake_engine,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "long"},
        ],
        summary="",
        run_options={"external_memory_enabled": ["kb"]},
    )
    await runner.compress_context(state)
    assert fake_emm.pre_compress_calls == 1
    assert fake_engine.compress_calls == 1
    # pre_compress must be called with non-system messages (rescued before compress)
    assert fake_emm.last_pre_compress_messages == [{"role": "user", "content": "long"}]


@pytest.mark.asyncio
async def test_compress_context_skips_pre_compress_when_should_compress_false():
    """pre_compress_all must NOT be called when should_compress returns False."""
    class NeverCompressEngine:
        def should_compress(self, messages, *, prompt_tokens=None, force=False):
            return False
        async def compress(self, messages, **kwargs):
            raise AssertionError("should not be called")
    fake_emm = FakeExternalMemoryManager()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=FakeMemoryStore(),
        summarizer=None,
        external_memory_manager=fake_emm,
        context_engine=NeverCompressEngine(),
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg"},
        ],
        summary="",
    )
    await runner.compress_context(state)
    assert fake_emm.pre_compress_calls == 0


@pytest.mark.asyncio
async def test_compress_context_concatenates_rescued_context_into_summary():
    """rescued_context from pre_compress_all is prepended to state.summary."""
    compressed_msgs = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}LLM_SUMMARY"}]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="LLM_SUMMARY", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    fake_emm = FakeExternalMemoryManager(rescued_context="RESCUED_KEY_POINTS")
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        external_memory_manager=fake_emm,
        context_engine=fake_engine,
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "long"},
        ],
        summary="",
        run_options={"external_memory_enabled": ["kb"]},
    )
    new_state = await runner.compress_context(state)
    assert "RESCUED_KEY_POINTS" in new_state.summary
    assert "LLM_SUMMARY" in new_state.summary
    assert len(memory_store.saved_summaries) == 1
    assert "RESCUED_KEY_POINTS" in memory_store.saved_summaries[0].summary


@pytest.mark.asyncio
async def test_compress_context_does_not_append_regular_messages():
    """compress_context must not append regular messages; only append_summary_message + save_summary."""
    compressed_msgs = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary"}]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="summary", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "long"},
        ],
        summary="",
    )
    await runner.compress_context(state)
    # No append_message (regular messages not persisted by compress_context)
    assert len(memory_store.appended_messages) == 0
    # Summary appended via append_summary_message
    assert len(memory_store.appended_summaries) == 1
    # Summary saved to summaries table
    assert len(memory_store.saved_summaries) == 1


class FakeToolServiceForLLM:
    """Minimal ToolService for call_llm tests."""
    def get_definition(self, name): return None
    def list_openai_tools(self, risk_level=None, context=None): return []


@pytest.mark.asyncio
async def test_call_llm_still_calls_prefetch_all_for_temp_injection():
    """prefetch_all must remain in call_llm, injecting <memory-context> only into temp api_messages."""
    fake_emm = FakeExternalMemoryManager(memory_context="<memory-context>mem</memory-context>")
    fake_llm = FakeLLMProvider()
    runner = AgentGraphRunner(
        llm_provider=fake_llm,
        tool_service=FakeToolServiceForLLM(),
        memory_store=FakeMemoryStore(),
        summarizer=None,
        external_memory_manager=fake_emm,
        context_engine=None,
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "final question"},
        ],
        summary="",
        run_options={"external_memory_enabled": ["kb"]},
    )
    await runner.call_llm(state)
    assert fake_emm.prefetch_calls == 1
    # working_messages must NOT contain <memory-context> (temp injection only)
    assert not any(
        "<memory-context>" in str(m.get("content", ""))
        for m in state.working_messages
    )
    # But the LLM call must have received the injected context
    assert "<memory-context>" in str(fake_llm.last_kwargs.get("messages", []))


class FakeMemoryStoreWithHistory(FakeMemoryStore):
    """FakeMemoryStore that returns pre-populated history from list_messages."""

    def __init__(self, history=None):
        super().__init__()
        self._history = history or []

    async def list_messages(self, session_id):
        return list(self._history)


@pytest.mark.asyncio
async def test_e2e_compress_context_invoked_in_full_graph_flow():
    """End-to-end: load_context loads history, compress_context compresses, call_llm replies."""
    compressed_result = ContextCompressionResult(
        messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}E2E summary"}],
        summary="E2E summary",
        compressed=True, skipped_reason=None,
        original_tokens=1000, compressed_tokens=100,
    )
    fake_engine = FakeContextEngine(compressed_result)

    # Pre-populate one history message (load_context will return it)
    history = [
        ConversationMessage(role="user", content="past question"),
    ]
    memory_store = FakeMemoryStoreWithHistory(history=history)

    fake_llm = FakeLLMProvider()
    runner = AgentGraphRunner(
        llm_provider=fake_llm,
        tool_service=FakeToolServiceForLLM(),
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
        iteration_limit=1,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": "final question"}],
        working_messages=[],
        summary="",
    )
    # Run the full graph
    final_state = await runner.run(state, model="test-model")
    # Compression should have been invoked
    assert fake_engine.compress_calls >= 1
    # Summary persisted
    assert any(s.summary == "E2E summary" for s in memory_store.saved_summaries)
    # LLM was called (call_llm executed)
    assert fake_llm.call_count >= 1


@pytest.mark.asyncio
async def test_load_context_keeps_all_non_summary_messages_and_latest_summary_only():
    """load_context 保留全部非摘要消息 + 仅最新一条摘要；旧摘要从上下文剔除。

    spec: 上下文只使用最新的摘要。head/middle/tail 非摘要消息全部保留，确保 head 保护生效。
    DB 中摘要是 append 在末尾的，head/middle/tail 都在摘要之前，不能按"摘要+其后"过滤。
    """
    history = [
        ConversationMessage(role="user", content="old question 1"),
        ConversationMessage(role="assistant", content="old reply 1"),
        ConversationMessage(
            role="user", content=f"{CONTEXT_SUMMARY_PREFIX}summary 1", is_summary=True,
        ),
        ConversationMessage(role="user", content="question 2"),
        ConversationMessage(role="assistant", content="reply 2"),
        ConversationMessage(
            role="user", content=f"{CONTEXT_SUMMARY_PREFIX}summary 2", is_summary=True,
        ),
        ConversationMessage(role="user", content="latest question"),
    ]
    memory_store = FakeMemoryStoreWithHistory(history=history)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=FakeToolServiceForLLM(),
        memory_store=memory_store,
        summarizer=None,
        context_engine=None,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": "new input"}],
        working_messages=[],
        summary="",
    )
    new_state = await runner.load_context(state)
    # working_messages = [system, old question 1, old reply 1, question 2, reply 2, summary 2, latest question, new input]
    assert new_state.working_messages[0]["role"] == "system"
    contents = [str(m.get("content", "")) for m in new_state.working_messages]
    # head (old question 1, old reply 1) preserved
    assert "old question 1" in contents
    assert "old reply 1" in contents
    # middle (question 2, reply 2) preserved
    assert "question 2" in contents
    assert "reply 2" in contents
    # tail (latest question) preserved
    assert "latest question" in contents
    # new input preserved
    assert "new input" in contents
    # only latest summary (summary 2) in context; old summary 1 excluded
    assert f"{CONTEXT_SUMMARY_PREFIX}summary 2" in contents
    assert f"{CONTEXT_SUMMARY_PREFIX}summary 1" not in contents


@pytest.mark.asyncio
async def test_load_context_preserves_head_messages_when_summary_appended_last():
    """regression: 摘要 append 在 DB 末尾时，上下文仍按 head + summary + tail 注入。

    用户报告：压缩后只是把摘要加到了消息尾部，没有保留为
    head 3 + [CONTEXT SUMMARY] + tail 3 的顺序。
    """
    history = [
        # head (protect_first_n=3)
        ConversationMessage(role="user", content="head msg 1"),
        ConversationMessage(role="assistant", content="head reply 1"),
        ConversationMessage(role="user", content="head msg 2"),
        # middle (summarized, but DB retains them for audit history)
        ConversationMessage(role="assistant", content="middle reply", is_summarized=True),
        ConversationMessage(role="user", content="middle msg", is_summarized=True),
        # tail (protect_last_n default is 10; this sample only needs one tail message)
        ConversationMessage(role="assistant", content="tail reply"),
        # summary appended last by append_summary_message
        ConversationMessage(
            role="user", content=f"{CONTEXT_SUMMARY_PREFIX}compressed summary", is_summary=True,
        ),
    ]
    memory_store = FakeMemoryStoreWithHistory(history=history)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=FakeToolServiceForLLM(),
        memory_store=memory_store,
        summarizer=None,
        context_engine=None,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[],
        summary="",
    )
    new_state = await runner.load_context(state)
    contents = [str(m.get("content", "")) for m in new_state.working_messages]
    # head messages must survive (this was the bug)
    assert "head msg 1" in contents
    assert "head reply 1" in contents
    assert "head msg 2" in contents
    assert "middle reply" not in contents
    assert "middle msg" not in contents
    # tail must survive
    assert "tail reply" in contents
    # summary must be present
    assert f"{CONTEXT_SUMMARY_PREFIX}compressed summary" in contents
    assert contents[1:] == [
        "head msg 1",
        "head reply 1",
        "head msg 2",
        f"{CONTEXT_SUMMARY_PREFIX}compressed summary",
        "tail reply",
    ]


@pytest.mark.asyncio
async def test_load_context_without_summary_returns_all_messages():
    """无摘要时 load_context 返回全部消息（不过滤）。"""
    history = [
        ConversationMessage(role="user", content="question 1"),
        ConversationMessage(role="assistant", content="reply 1"),
        ConversationMessage(role="user", content="question 2"),
    ]
    memory_store = FakeMemoryStoreWithHistory(history=history)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=FakeToolServiceForLLM(),
        memory_store=memory_store,
        summarizer=None,
        context_engine=None,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[],
        summary="",
    )
    new_state = await runner.load_context(state)
    # working_messages = [system, question 1, reply 1, question 2]
    assert len(new_state.working_messages) == 4
    assert new_state.working_messages[1]["content"] == "question 1"
    assert new_state.working_messages[3]["content"] == "question 2"


@pytest.mark.asyncio
async def test_load_context_filters_is_summarized_messages():
    """load_context 过滤 is_summarized=1 的消息（已被摘要吸收的 middle）。

    压缩成功后 middle 段被标记 is_summarized=1，load 时过滤掉，避免 middle + summary 冗余。
    """
    history = [
        # head (未摘要)
        ConversationMessage(id="m1", role="user", content="head q"),
        # middle (已被摘要，is_summarized=True)
        ConversationMessage(id="m2", role="assistant", content="middle r", is_summarized=True),
        ConversationMessage(id="m3", role="user", content="middle q", is_summarized=True),
        # tail (未摘要)
        ConversationMessage(id="m4", role="assistant", content="tail r"),
        # summary
        ConversationMessage(
            id="s1", role="user", content=f"{CONTEXT_SUMMARY_PREFIX}summary", is_summary=True,
        ),
        # new message after summary
        ConversationMessage(id="m5", role="user", content="new q"),
    ]
    memory_store = FakeMemoryStoreWithHistory(history=history)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=FakeToolServiceForLLM(),
        memory_store=memory_store,
        summarizer=None,
        context_engine=None,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[],
        summary="",
    )
    new_state = await runner.load_context(state)
    contents = [str(m.get("content", "")) for m in new_state.working_messages]
    # head + summary + tail + new all preserved
    assert "head q" in contents
    assert "tail r" in contents
    assert f"{CONTEXT_SUMMARY_PREFIX}summary" in contents
    assert "new q" in contents
    # middle (is_summarized=True) filtered out
    assert "middle r" not in contents
    assert "middle q" not in contents
    assert contents[1:] == [
        "head q",
        f"{CONTEXT_SUMMARY_PREFIX}summary",
        "tail r",
        "new q",
    ]


@pytest.mark.asyncio
async def test_load_context_drops_orphan_tool_messages():
    """History loaded for a new Chat turn must not contain orphan tool messages."""
    history = [
        ConversationMessage(role="user", content="head q"),
        ConversationMessage(
            role="tool",
            content='{"status":"success"}',
            tool_call_id="call-orphan",
            name="schedule_query",
        ),
        ConversationMessage(role="user", content="latest q"),
    ]
    memory_store = FakeMemoryStoreWithHistory(history=history)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=FakeToolServiceForLLM(),
        memory_store=memory_store,
        summarizer=None,
        context_engine=None,
    )
    state = await runner.load_context(AgentState(session_id="s1"))
    assert [m.get("role") for m in state.working_messages] == ["system", "user", "user"]
    assert not any(m.get("role") == "tool" for m in state.working_messages)


@pytest.mark.asyncio
async def test_load_context_sanitizes_tool_pair_split_by_summary_reorder():
    """Summary reordering can separate an old assistant tool_call from its tool result."""
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "schedule_query", "arguments": "{}"},
    }
    history = [
        ConversationMessage(role="user", content="head q"),
        ConversationMessage(
            role="assistant",
            content={"content": "", "tool_calls": [tool_call]},
        ),
        ConversationMessage(role="user", content="middle", is_summarized=True),
        ConversationMessage(
            role="tool",
            content='{"status":"success"}',
            tool_call_id="call-1",
            name="schedule_query",
        ),
        ConversationMessage(
            role="user",
            content=f"{CONTEXT_SUMMARY_PREFIX}summary",
            is_summary=True,
        ),
        ConversationMessage(role="user", content="latest q"),
    ]
    memory_store = FakeMemoryStoreWithHistory(history=history)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=FakeToolServiceForLLM(),
        memory_store=memory_store,
        summarizer=None,
        context_engine=None,
    )
    state = await runner.load_context(AgentState(session_id="s1"))
    contents = [str(m.get("content", "")) for m in state.working_messages]
    assert contents[1:] == [
        "head q",
        f"{CONTEXT_SUMMARY_PREFIX}summary",
        "latest q",
    ]
    assert not any(m.get("role") == "tool" for m in state.working_messages)
    assert not any(m.get("tool_calls") for m in state.working_messages)


@pytest.mark.asyncio
async def test_compress_context_marks_middle_messages_summarized():
    """compress_context 成功后调用 mark_messages_summarized 标记 middle 段。

    result.summarized_message_indices 是相对于 non_system 的索引；
    compress_context 通过 state.context_message_ids 映射到消息 id，调用 mark。
    """
    compressed_msgs = [
        {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S1"},
        {"role": "user", "content": "tail msg"},
    ]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="S1", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
        summarized_message_indices=[1, 2],  # middle 索引（相对于 non_system）
    )
    fake_engine = FakeContextEngine(result)
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    # 模拟 load_context 缓存的 context_message_ids：non_system = [ctx0, ctx1, ctx2, ctx3]
    # middle 索引 [1, 2] 对应 ctx1, ctx2
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ctx0"},
            {"role": "user", "content": "ctx1"},
            {"role": "user", "content": "ctx2"},
            {"role": "user", "content": "ctx3"},
        ],
        summary="",
        context_message_ids=["id0", "id1", "id2", "id3"],
    )
    await runner.compress_context(state)
    # mark_messages_summarized called with middle ids
    assert memory_store.summarized_message_ids == ["id1", "id2"]


class FailingAppendMemoryStore(FakeMemoryStore):
    """FakeMemoryStore whose append_summary_message always raises."""

    async def append_summary_message(self, session_id, message):
        raise RuntimeError("append failed")


@pytest.mark.asyncio
async def test_compress_context_persists_summary_message_via_append():
    """压缩成功时：构造 ConversationMessage(is_summary=True) -> append_summary_message -> save_summary(source_message_id)"""
    compressed_msgs = [
        {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S1"},
        {"role": "user", "content": "tail msg"},
    ]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="S1", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old"},
        ],
        summary="",
    )
    new_state = await runner.compress_context(state)
    # append_summary_message called with ConversationMessage(is_summary=True)
    assert len(memory_store.appended_summaries) == 1
    sid, msg = memory_store.appended_summaries[0]
    assert sid == "s1"
    assert msg.is_summary is True
    assert msg.content == f"{CONTEXT_SUMMARY_PREFIX}S1"
    # save_summary called with source_message_id linking to new summary message id
    assert len(memory_store.saved_summaries) == 1
    assert memory_store.saved_summaries[0].source_message_id == msg.id
    # working_messages contains the new summary message
    assert any(m.get("content", "").startswith(CONTEXT_SUMMARY_PREFIX) for m in new_state.working_messages)


@pytest.mark.asyncio
async def test_compress_context_append_failure_keeps_state_unchanged():
    """append_summary_message 抛异常时：state.working_messages 和 state.summary 不变，不调 save_summary"""
    result = ContextCompressionResult(
        messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S1"}],
        summary="S1", compressed=True, skipped_reason=None,
        original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    memory_store = FailingAppendMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    original_msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
    ]
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=original_msgs.copy(),
        summary="old_summary",
    )
    new_state = await runner.compress_context(state)
    # state unchanged
    assert new_state.working_messages == original_msgs
    assert new_state.summary == "old_summary"
    # save_summary not called
    assert len(memory_store.saved_summaries) == 0


@pytest.mark.asyncio
async def test_compress_context_rejects_result_with_zero_or_multiple_summary_messages():
    """result.messages 中摘要消息数量不是 1 时：state 不变（含 state.summary），不写表"""
    # 0 summary messages
    result0 = ContextCompressionResult(
        messages=[{"role": "user", "content": "no summary here"}],
        summary="S1", compressed=True, skipped_reason=None,
        original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result0)
    memory_store = FakeMemoryStore()
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    original_summary = "old_summary"
    state = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old"},
        ],
        summary=original_summary,
    )
    new_state = await runner.compress_context(state)
    assert len(memory_store.appended_summaries) == 0
    assert new_state.working_messages == state.working_messages
    # spec Error Handling #7: state.summary must also remain unchanged
    assert new_state.summary == original_summary

    # 2 summary messages
    result2 = ContextCompressionResult(
        messages=[
            {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S1"},
            {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S2"},
        ],
        summary="S1", compressed=True, skipped_reason=None,
        original_tokens=500, compressed_tokens=50,
    )
    fake_engine2 = FakeContextEngine(result2)
    memory_store2 = FakeMemoryStore()
    runner2 = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store2,
        summarizer=None,
        context_engine=fake_engine2,
    )
    state2 = AgentState(
        session_id="s1",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old"},
        ],
        summary=original_summary,
    )
    new_state2 = await runner2.compress_context(state2)
    assert len(memory_store2.appended_summaries) == 0
    assert new_state2.working_messages == state2.working_messages
    assert new_state2.summary == original_summary


@pytest.mark.asyncio
async def test_compress_context_passes_force_from_run_options():
    """When run_options has force_compress=True, force=True is passed to should_compress and compress."""
    compressed_msgs = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary"}]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="summary", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=FakeMemoryStore(),
        summarizer=None,
        context_engine=fake_engine,
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg"},
        ],
        summary="",
        run_options={"force_compress": True},
    )
    await runner.compress_context(state)
    assert fake_engine.last_should_compress_force is True
    assert fake_engine.last_compress_force is True


@pytest.mark.asyncio
async def test_compress_context_does_not_force_by_default():
    """When run_options has no force_compress, force=False is passed."""
    compressed_msgs = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary"}]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="summary", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=FakeMemoryStore(),
        summarizer=None,
        context_engine=fake_engine,
    )
    state = AgentState(
        session_id="s1", input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg"},
        ],
        summary="",
        run_options={},
    )
    await runner.compress_context(state)
    assert fake_engine.last_should_compress_force is False
    assert fake_engine.last_compress_force is False


@pytest.mark.asyncio
async def test_compress_session_returns_compressed():
    """compress_session loads messages, forces compression, returns compressed=True."""
    from app.domain.session import ConversationMessage
    compressed_msgs = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary"}]
    result = ContextCompressionResult(
        messages=compressed_msgs, summary="summary", compressed=True,
        skipped_reason=None, original_tokens=500, compressed_tokens=50,
    )
    fake_engine = FakeContextEngine(result)
    messages = [
        ConversationMessage(role="user", content="old message 1"),
        ConversationMessage(role="assistant", content="old reply 1"),
        ConversationMessage(role="user", content="old message 2"),
    ]
    memory_store = FakeMemoryStore(messages=messages)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    status = await runner.compress_session("s1")
    assert status["compressed"] is True
    assert status["reason"] is None
    assert fake_engine.last_compress_force is True


@pytest.mark.asyncio
async def test_compress_session_returns_unavailable_when_no_engine():
    """compress_session returns context_engine_unavailable when context_engine is None."""
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=FakeMemoryStore(),
        summarizer=None,
        context_engine=None,
    )
    status = await runner.compress_session("s1")
    assert status["compressed"] is False
    assert status["reason"] == "context_engine_unavailable"


@pytest.mark.asyncio
async def test_compress_session_returns_no_change_when_too_few():
    """compress_session returns no_change when compression doesn't happen (too few messages)."""
    from app.domain.session import ConversationMessage
    result = ContextCompressionResult(
        messages=[], summary="", compressed=False,
        skipped_reason="too_few_messages", original_tokens=10, compressed_tokens=None,
    )
    fake_engine = FakeContextEngine(result)
    messages = [ConversationMessage(role="user", content="short")]
    memory_store = FakeMemoryStore(messages=messages)
    runner = AgentGraphRunner(
        llm_provider=FakeLLMProvider(),
        tool_service=None,
        memory_store=memory_store,
        summarizer=None,
        context_engine=fake_engine,
    )
    status = await runner.compress_session("s1")
    assert status["compressed"] is False
    assert status["reason"] == "no_change"
