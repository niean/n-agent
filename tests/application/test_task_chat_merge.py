"""Integration: dashboard /task 任务的生命周期与 worker 对话落到 origin Chat 会话；
非 dashboard 任务隔离；ContextService 过滤 system 通知但保留 worker 对话。

对应 spec: spec-260720-task-chat-merge.md；plan T4 集成验收。
"""
from uuid import uuid5, NAMESPACE_URL

import pytest

from app.application.context_service import ContextService
from app.application.session_service import SessionService
from app.application.task_session import task_execution_session_id
from app.domain.agent import AgentState
from app.domain.session import ConversationMessage, ConversationSession
from app.domain.task import Task
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


@pytest.mark.asyncio
async def test_dashboard_task_lifecycle_lands_in_origin_chat_session(tmp_path):
    """dashboard /task 任务：selector 复用 origin，lifecycle 写入 origin Chat 会话。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    session_svc = SessionService(store)
    await store.create_session(ConversationSession(id="dashboard-s1"))

    async def lifecycle_writer(session_id, content):
        await session_svc.append_task_lifecycle_message(session_id, content)

    task = Task(id="t1", title="完成报告", origin_session_id="dashboard-s1")
    assert task_execution_session_id(task) == "dashboard-s1"

    await lifecycle_writer(task_execution_session_id(task), "[任务状态] 开始运行: t1 - 完成报告")
    msgs = await store.list_messages("dashboard-s1")
    assert len(msgs) == 1
    assert msgs[0].name == "ui.task_lifecycle"
    assert "开始运行" in msgs[0].content
    assert msgs[0].role == "system"


@pytest.mark.asyncio
async def test_non_dashboard_task_does_not_pollute_origin_chat(tmp_path):
    """kanban/CLI 任务（origin=None）：selector 回退 task-{uuid5}，lifecycle 不污染 dashboard Chat。

    task-{uuid5} 会话不存在 -> append_message_if_session_exists 返回 None ->
    SessionNotFoundError（main.py closure 静默跳过）；dashboard-s1 不被写入。
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    session_svc = SessionService(store)
    await store.create_session(ConversationSession(id="dashboard-s1"))

    task = Task(id="t_kanban", title="看板任务")  # origin_session_id=None
    assert task_execution_session_id(task) == f"task-{uuid5(NAMESPACE_URL, 't_kanban')}"

    with pytest.raises(Exception):
        # task-{uuid5} 会话不存在 -> SessionNotFoundError
        await session_svc.append_task_lifecycle_message(
            task_execution_session_id(task), "[任务状态] 开始运行: t_kanban - 看板任务"
        )
    # dashboard Chat 未被污染
    assert await store.list_messages("dashboard-s1") == []


@pytest.mark.asyncio
async def test_context_filter_excludes_lifecycle_keeps_worker_dialogue(tmp_path):
    """worker 在 Chat 会话执行后：lifecycle system 通知被过滤，worker user/assistant 保留。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="dashboard-s1"))
    # worker 对话（worker 在 origin Chat 会话执行，spec 完整合并语义）
    await store.append_message("dashboard-s1", ConversationMessage(role="user", content="work task t1"))
    await store.append_message("dashboard-s1", ConversationMessage(role="assistant", content="正在处理"))
    # 生命周期 system 通知
    await store.append_message(
        "dashboard-s1",
        ConversationMessage(role="system", name="ui.task_lifecycle", content="[任务状态] 已完成: t1 - 完成报告"),
    )

    svc = ContextService(store)
    state = AgentState(session_id="dashboard-s1", input_messages=[{"role": "user", "content": "下一步"}])
    state = await svc.build_context_state(state)

    roles = [m["role"] for m in state.working_messages]
    assert roles[0] == "system"  # 运行时 build_system_prompt
    assert roles.count("system") == 1  # lifecycle system 通知被过滤
    contents = [str(m.get("content", "")) for m in state.working_messages]
    assert any("work task t1" in c for c in contents)  # worker user 保留
    assert any("正在处理" in c for c in contents)  # worker assistant 保留
    assert all("[任务状态]" not in c for c in contents)  # lifecycle 不进上下文
