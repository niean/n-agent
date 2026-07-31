import pytest

from app.application.session_service import SessionService
from app.domain.session import (
    ConversationSession,
    SessionNotFoundError,
    SessionValidationError,
)
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


def _service(tmp_path) -> SessionService:
    return SessionService(SQLiteMemoryStore(tmp_path / "sessions.db"))


@pytest.mark.asyncio
async def test_append_task_command_message_persists_fixed_role_name(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    msg = await svc.append_task_command_message("s1", "  [任务指令] 执行命令: /task list  ")
    assert msg.role == "system"
    assert msg.name == "ui.task_command"
    assert msg.content == "[任务指令] 执行命令: /task list"  # trimmed
    msgs = await store.list_messages("s1")
    assert len(msgs) == 1 and msgs[0].name == "ui.task_command"


@pytest.mark.asyncio
async def test_append_task_lifecycle_message_uses_lifecycle_name(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    msg = await svc.append_task_lifecycle_message("s1", "[任务状态] 已完成: t1 - 完成报告")
    assert msg.role == "system"
    assert msg.name == "ui.task_lifecycle"
    assert msg.content.startswith("[任务状态]")


@pytest.mark.asyncio
async def test_append_task_result_message_uses_result_name(tmp_path):
    """最终结果走 ui.task_result（普通消息渲染），区别于 ui.task_lifecycle 状态卡片。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    msg = await svc.append_task_result_message("s1", "任务已完成：完成报告\n\n已生成 Q3 总结")
    assert msg.role == "system"
    assert msg.name == "ui.task_result"
    assert "已完成" in msg.content and "Q3 总结" in msg.content
    persisted = (await store.list_messages("s1"))[0]
    assert persisted.name == "ui.task_result"


@pytest.mark.asyncio
async def test_append_message_rejects_blank_and_non_string(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    with pytest.raises(SessionValidationError):
        await svc.append_task_command_message("s1", "   ")
    with pytest.raises(SessionValidationError):
        await svc.append_task_command_message("s1", 123)  # type: ignore[arg-type]
    # 零写入
    assert await store.list_messages("s1") == []


@pytest.mark.asyncio
async def test_append_message_raises_not_found_when_session_absent(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(SessionNotFoundError):
        await svc.append_task_lifecycle_message("no-such", "[任务状态] 开始运行: t1")
    with pytest.raises(SessionNotFoundError):
        await svc.append_task_command_message("no-such", "[任务指令] x")
    with pytest.raises(SessionNotFoundError):
        await svc.append_task_result_message("no-such", "任务已完成：t1")


@pytest.mark.asyncio
async def test_append_message_truncates_oversize_utf8(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    # 65537 字节中文（每字符 3 字节）
    big = "中" * 22000  # ~66000 bytes
    msg = await svc.append_task_lifecycle_message("s1", big)
    assert len(msg.content.encode("utf-8")) <= 65536
    assert msg.content.endswith("…[内容已截断]")
    # 持久化正文与返回一致
    persisted = (await store.list_messages("s1"))[0]
    assert persisted.content == msg.content


@pytest.mark.asyncio
async def test_append_message_boundary_65536_bytes_ok(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    # 构造恰好不超限的正文（ASCII，1 字节/字符）
    content = "a" * 65536
    msg = await svc.append_task_command_message("s1", content)
    assert len(msg.content.encode("utf-8")) <= 65536
    assert "…[内容已截断]" not in msg.content


@pytest.mark.asyncio
async def test_append_task_command_message_rejects_oversize(tmp_path):
    """HTTP 命令路径不截断超长，直接抛 SessionValidationError（422）；前端负责截断。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    with pytest.raises(SessionValidationError):
        await svc.append_task_command_message("s1", "a" * 65537)
    # 零写入
    assert await store.list_messages("s1") == []


@pytest.mark.asyncio
async def test_append_task_lifecycle_message_persists_card(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    card = {"schema_version": 1, "kind": "task_lifecycle", "task_id": "t1", "status": "waiting_approval", "title": "T", "summary": "p", "available_actions": ["approve"]}
    await svc.append_task_lifecycle_message("s1", "等待批准", card=card)
    msgs = await store.list_messages("s1")
    assert msgs[-1].name == "ui.task_lifecycle"
    assert msgs[-1].card == card


@pytest.mark.asyncio
async def test_append_tool_approval_message_persists_whitelisted_card(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    approval = {
        "confirmation_id": "confirm-1",
        "tool_name": "browser_click",
        "description": "Open article",
        "arguments_summary": '{"element_ref":"el-1"}',
        "expires_at": "2026-07-28T12:00:00Z",
        "unexpected": "must not persist",
    }

    await svc.append_tool_approval_message("s1", approval)

    message = (await store.list_messages("s1"))[-1]
    assert message.role == "system"
    assert message.name == "ui.tool_approval"
    assert message.card == {
        "kind": "tool_approval",
        "approval": {key: approval[key] for key in (
            "confirmation_id", "tool_name", "description", "arguments_summary", "expires_at",
        )},
    }


@pytest.mark.asyncio
async def test_append_tool_approval_resolution_persists_confirmation_status(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))

    await svc.append_tool_approval_resolution_message("s1", "confirm-1", "approved")

    message = (await store.list_messages("s1"))[-1]
    assert message.name == "ui.tool_approval_resolution"
    assert message.card == {
        "kind": "tool_approval_resolution",
        "confirmation_id": "confirm-1",
        "status": "approved",
    }


@pytest.mark.asyncio
async def test_append_tool_approval_resolution_persists_scope(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))

    # approved + session scope -> card carries scope (信任本会话)
    await svc.append_tool_approval_resolution_message(
        "s1", "confirm-session", "approved", scope="session"
    )
    # approved + once scope -> card carries scope (仅信任本次)
    await svc.append_tool_approval_resolution_message(
        "s1", "confirm-once", "approved", scope="once"
    )
    # rejected -> no scope even if one is passed
    await svc.append_tool_approval_resolution_message(
        "s1", "confirm-cancel", "rejected", scope="deny"
    )

    messages = await store.list_messages("s1")
    by_id = {m.card["confirmation_id"]: m.card for m in messages}
    assert by_id["confirm-session"] == {
        "kind": "tool_approval_resolution",
        "confirmation_id": "confirm-session",
        "status": "approved",
        "scope": "session",
    }
    assert by_id["confirm-once"]["scope"] == "once"
    assert "scope" not in by_id["confirm-cancel"]


@pytest.mark.asyncio
async def test_append_task_lifecycle_message_truncates_card_summary(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    long_summary = "x" * 100000
    card = {"schema_version": 1, "kind": "task_lifecycle", "task_id": "t1", "status": "failed", "title": "T", "summary": long_summary, "available_actions": ["retry"]}
    await svc.append_task_lifecycle_message("s1", "已失败", card=card)
    msgs = await store.list_messages("s1")
    assert len(msgs[-1].card["summary"].encode("utf-8")) <= 65536
    assert msgs[-1].card["summary"].endswith("…[内容已截断]")


@pytest.mark.asyncio
async def test_append_task_lifecycle_message_truncates_multibyte_card_summary(tmp_path):
    """中文多字节截断不产生替换字符或超限（spec 验收要求）。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    big = "中" * 22000  # ~66000 bytes
    card = {"schema_version": 1, "kind": "task_lifecycle", "task_id": "t1", "status": "failed", "title": "T", "summary": big, "available_actions": ["retry"]}
    await svc.append_task_lifecycle_message("s1", "已失败", card=card)
    msgs = await store.list_messages("s1")
    summary_bytes = msgs[-1].card["summary"].encode("utf-8")
    assert len(summary_bytes) <= 65536
    assert msgs[-1].card["summary"].endswith("…[内容已截断]")
    assert b"\xef\xbf\xbd" not in summary_bytes  # 无 U+FFFD 替换字符


@pytest.mark.asyncio
async def test_append_task_lifecycle_message_does_not_mutate_caller_card(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    card = {"schema_version": 1, "kind": "task_lifecycle", "task_id": "t1", "status": "failed", "title": "T", "summary": "x" * 100000, "available_actions": ["retry"]}
    original_summary = card["summary"]
    await svc.append_task_lifecycle_message("s1", "已失败", card=card)
    assert card["summary"] == original_summary  # caller dict not mutated


@pytest.mark.asyncio
async def test_append_task_lifecycle_message_null_card_default(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    svc = SessionService(store)
    await store.create_session(ConversationSession(id="s1"))
    await svc.append_task_lifecycle_message("s1", "已批准")
    assert (await store.list_messages("s1"))[-1].card is None
