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
