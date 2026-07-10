from __future__ import annotations

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.domain.agent import AgentState
from app.domain.context import CONTEXT_SUMMARY_PREFIX
from app.domain.provider import LLMResult
from app.domain.session import ConversationMessage, ConversationSession
from app.infrastructure.context.context_compressor import ContextCompressor
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


class FakeLLM:
    def __init__(self):
        self.call_count = 0

    async def list_models(self):
        return []

    async def supports_tools(self, model):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.call_count += 1
        return LLMResult(
            message={"role": "assistant", "content": f"summary round {self.call_count}"},
            finish_reason="stop", usage={}, raw=None,
        )


def _make_long_message(i: int, prefix: str = "msg") -> str:
    return f"{prefix} {i} " + "x" * 300


@pytest.mark.asyncio
async def test_incremental_compression_second_round_uses_iterative_path(tmp_path):
    """端到端：第 2 次压缩时 middle 只含新增消息，_generate_summary 走迭代路径"""
    store = SQLiteMemoryStore(tmp_path / "test.db")
    llm = FakeLLM()
    compressor = ContextCompressor(
        llm_provider=llm, model="test-model",
        context_length=1000, threshold_percent=0.5,
        protect_first_n=2, protect_last_n=2,
        summary_target_ratio=0.3, cooldown_seconds=0,
    )
    sid = "integration-1"
    await store.create_session(ConversationSession(id=sid, title="t"))

    for i in range(10):
        await store.append_message(sid, ConversationMessage(
            role="user" if i % 2 == 0 else "assistant", content=_make_long_message(i),
        ))

    runner = AgentGraphRunner(
        llm_provider=llm, tool_service=None, memory_store=store,
        summarizer=None, context_engine=compressor,
    )
    state1 = AgentState(
        session_id=sid,
        working_messages=[{"role": "system", "content": "sys"}],
        input_messages=[], summary="",
    )
    state1 = await runner.load_context(state1)
    state1 = await runner.compress_context(state1)

    assert state1.summary == "summary round 1"
    msgs1 = await store.list_messages(sid)
    summaries1 = [m for m in msgs1 if m.is_summary]
    assert len(summaries1) == 1
    assert summaries1[0].content == f"{CONTEXT_SUMMARY_PREFIX}summary round 1"

    for i in range(10, 20):
        await store.append_message(sid, ConversationMessage(
            role="user" if i % 2 == 0 else "assistant", content=_make_long_message(i),
        ))

    state2 = AgentState(
        session_id=sid,
        working_messages=[{"role": "system", "content": "sys"}],
        input_messages=[], summary=state1.summary,
    )
    state2 = await runner.load_context(state2)
    assert any(m.get("content", "").startswith(CONTEXT_SUMMARY_PREFIX) for m in state2.working_messages)
    state2 = await runner.compress_context(state2)

    assert state2.summary == "summary round 2"
    msgs2 = await store.list_messages(sid)
    summaries2 = [m for m in msgs2 if m.is_summary]
    assert len(summaries2) == 1
    assert summaries2[0].content == f"{CONTEXT_SUMMARY_PREFIX}summary round 2"


@pytest.mark.asyncio
async def test_incremental_compression_summary_replaced_not_accumulated(tmp_path):
    """多次压缩后 messages 表 is_summary=1 的消息只有 1 条（最新摘要）"""
    store = SQLiteMemoryStore(tmp_path / "test.db")
    llm = FakeLLM()
    compressor = ContextCompressor(
        llm_provider=llm, model="test-model",
        context_length=500, threshold_percent=0.5,
        protect_first_n=1, protect_last_n=1,
        summary_target_ratio=0.3, cooldown_seconds=0,
    )
    sid = "integration-2"
    await store.create_session(ConversationSession(id=sid, title="t"))

    for round_num in range(1, 4):
        for i in range(5):
            await store.append_message(sid, ConversationMessage(
                role="user" if i % 2 == 0 else "assistant",
                content=_make_long_message(i, prefix=f"round{round_num}"),
            ))
        runner = AgentGraphRunner(
            llm_provider=llm, tool_service=None, memory_store=store,
            summarizer=None, context_engine=compressor,
        )
        state = AgentState(
            session_id=sid,
            working_messages=[{"role": "system", "content": "sys"}],
            input_messages=[], summary="",
        )
        state = await runner.load_context(state)
        state = await runner.compress_context(state)

    msgs = await store.list_messages(sid)
    summaries = [m for m in msgs if m.is_summary]
    assert len(summaries) == 1
    assert summaries[0].content == f"{CONTEXT_SUMMARY_PREFIX}summary round 3"
