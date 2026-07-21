import pytest

from app.application.context_service import ContextService
from app.domain.agent import AgentState
from app.domain.session import ConversationMessage, ConversationSession
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


@pytest.mark.asyncio
async def test_build_context_state_excludes_historical_system_messages(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    await store.append_message("s1", ConversationMessage(role="user", content="q1"))
    await store.append_message("s1", ConversationMessage(role="system", name="ui.task_command", content="[任务指令] 执行命令: /task list"))
    await store.append_message("s1", ConversationMessage(role="assistant", content="a1"))
    await store.append_message("s1", ConversationMessage(role="system", name="ui.task_lifecycle", content="[任务状态] 开始运行: t1"))

    svc = ContextService(store)
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "next"}])
    state = await svc.build_context_state(state)

    roles = [m["role"] for m in state.working_messages]
    # 首条为运行时 build_system_prompt（system），历史 system 通知被排除
    assert roles[0] == "system"
    assert roles.count("system") == 1
    # user/assistant 历史保留
    contents = [str(m.get("content", "")) for m in state.working_messages]
    assert any("q1" in c for c in contents)
    assert any("a1" in c for c in contents)
    # system 通知不进上下文
    assert all("[任务指令]" not in c for c in contents)
    assert all("[任务状态]" not in c for c in contents)


@pytest.mark.asyncio
async def test_build_context_state_preserves_order_with_system_between_non_system(tmp_path):
    """system 通知夹在非 system 之间：过滤后非 system 仍按原顺序出现。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    await store.append_message("s1", ConversationMessage(role="user", content="u1"))
    await store.append_message("s1", ConversationMessage(role="system", name="ui.task_command", content="cmd"))
    await store.append_message("s1", ConversationMessage(role="assistant", content="a1"))
    await store.append_message("s1", ConversationMessage(role="system", name="ui.task_lifecycle", content="life"))
    await store.append_message("s1", ConversationMessage(role="user", content="u2"))

    svc = ContextService(store)
    state = AgentState(session_id="s1", input_messages=[])
    state = await svc.build_context_state(state)

    # 跳过首条 runtime system，剩余历史应为 u1, a1, u2（system 被剔除，顺序不变）
    history = state.working_messages[1:]
    # 末尾 input 为空，故 history 即过滤后历史
    contents = [str(m.get("content", "")) for m in history]
    assert "u1" in contents
    assert "a1" in contents
    assert "u2" in contents
    # u1 在 a1 前，a1 在 u2 前
    assert contents.index("u1") < contents.index("a1") < contents.index("u2")


@pytest.mark.asyncio
async def test_build_context_state_excludes_card_payload(tmp_path):
    """card 字段不进入 LLM 上下文：working_messages 不含 card 哨兵。

    card 挂在 role=system 消息上，ContextService 已过滤历史 system 出候选；
    card.summary 含唯一哨兵字符串，但 working_messages 的任何 content 均不含它，
    证明 card 不进 LLM 上下文（含压缩/摘要/外部记忆 pre-compress 候选，均基于此候选）。
    不为 card 新建第二套过滤逻辑，复用既有 system 过滤。
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    sentinel = "CARD_SENTINEL_不进上下文"
    card = {"schema_version": 1, "kind": "task_lifecycle", "task_id": "t1",
            "status": "waiting_approval", "title": "T", "summary": sentinel,
            "available_actions": ["approve"]}
    await store.append_message("s1", ConversationMessage(
        role="system", name="ui.task_lifecycle", content="等待批准: t1 - T", card=card))
    await store.append_message("s1", ConversationMessage(role="user", content="q1"))

    svc = ContextService(store)
    state = AgentState(session_id="s1", input_messages=[{"role": "user", "content": "next"}])
    state = await svc.build_context_state(state)

    for m in state.working_messages:
        assert sentinel not in str(m.get("content", ""))
