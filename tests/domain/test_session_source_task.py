"""T9: SessionSource.TASK + migrate/delete cascade on task tables.

Covers plan T9 (SessionSource 第 10 个来源 + migrate_session_id_prefixes 级联
扩展 task 表 + delete_session 级联处理 task origin/execution session).

Spec reference:
  - SessionSource.TASK = "task"（第 10 个来源，非 IM 平台，不进 im_platforms）
  - 前缀 task-，与现有 schedule-/curator- 同级
  - migrate_session_id_prefixes 扩展到 tasks.origin_session_id /
    tasks.execution_session_id
  - delete_session: 删除 origin session 时把 tasks.origin_session_id 置空
    （不删 Task）；删除 execution session 时把 tasks.execution_session_id 置空
    （不删 Task），下次 claim 由 TaskAgentExecutor 以稳定 task-{uuid5(NAMESPACE_URL, task.id)}
    重建/复用 execution session。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.domain.session import ConversationSession, SessionSource
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


# ---------------------------------------------------------------------------
# T9 S1-S4: SessionSource.TASK enum
# ---------------------------------------------------------------------------


def test_session_source_task_enum_value():
    """SessionSource.TASK = 'task'（内部触发来源，非 IM 平台）。"""
    assert SessionSource.TASK.value == "task"
    # 内部触发来源（schedule/curator/task/delegation）与 IM 平台同级
    members = list(SessionSource)
    assert SessionSource.TASK in members
    assert len(members) == 11


def test_session_source_task_not_in_im_platforms():
    """TASK 不是 IM 平台，不应出现在 im_platforms() 集合中。"""
    platforms = SessionSource.im_platforms()
    assert "task" not in platforms
    # IM 平台只有 feishu/dingtalk/wecom
    assert platforms == {"feishu", "dingtalk", "wecom"}


def test_session_source_task_is_str_enum():
    """SessionSource 是 str Enum，TASK 可直接当字符串使用。"""
    assert SessionSource.TASK == "task"
    assert isinstance(SessionSource.TASK, str)


# ---------------------------------------------------------------------------
# T9 S5-S7: migrate_session_id_prefixes + delete_session cascade
# ---------------------------------------------------------------------------


def _create_tasks_table(conn: sqlite3.Connection) -> None:
    """创建 tasks 表，模拟 SQLiteTaskRegistry 的 schema（T6 实现）。

    只创建 migrate 和 delete_session 需要级联处理的列：
      - id (PK)
      - origin_session_id (FK -> sessions, ON DELETE SET NULL)
      - execution_session_id (FK -> sessions, ON DELETE SET NULL)
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            origin_session_id TEXT,
            execution_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (origin_session_id) REFERENCES sessions(id) ON DELETE SET NULL,
            FOREIGN KEY (execution_session_id) REFERENCES sessions(id) ON DELETE SET NULL
        )
        """
    )


def _insert_task_row(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    origin_session_id: str | None = None,
    execution_session_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO tasks (id, title, origin_session_id, execution_session_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, f"title-{task_id}", origin_session_id, execution_session_id, now, now),
    )


def test_migrate_session_id_prefixes_cascades_to_task_tables(tmp_path):
    """migrate_session_id_prefixes 必须级联更新 tasks.origin_session_id 与
    tasks.execution_session_id，保持 session_id 引用一致。"""
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    # 插入一个旧格式 session（dashboard 前缀错用 session-）+ tasks 表引用
    with store._connect() as conn:
        _create_tasks_table(conn)
        conn.execute(
            "INSERT INTO sessions (id, title, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("session-old1", "Old Dashboard", "dashboard", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
        _insert_task_row(
            conn,
            "t_1",
            origin_session_id="session-old1",
            execution_session_id="session-old1",
        )
        conn.commit()

    # 执行迁移
    store.migrate_session_id_prefixes()

    # tasks 表的 session_id 引用必须跟着更新
    with store._connect() as conn:
        row = conn.execute("SELECT origin_session_id, execution_session_id FROM tasks WHERE id = ?", ("t_1",)).fetchone()
        assert row is not None
        # dashboard 前缀: session- -> dashboard-
        assert row["origin_session_id"] == "dashboard-old1"
        assert row["execution_session_id"] == "dashboard-old1"
        # session 行本身也更新了
        session = conn.execute("SELECT id, source FROM sessions WHERE id = ?", ("dashboard-old1",)).fetchone()
        assert session is not None
        assert session["source"] == "dashboard"


def test_migrate_session_id_prefixes_skips_task_tables_when_absent(tmp_path):
    """旧库未启用 Task（tasks 表不存在）时，迁移不应报错，仍能迁移其它表。"""
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    with store._connect() as conn:
        # 故意不创建 tasks 表
        conn.execute(
            "INSERT INTO sessions (id, title, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("session-old2", "Old Dashboard", "dashboard", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg-1", "session-old2", "user", '"hi"', "2026-07-01T00:00:00+00:00"),
        )
        conn.commit()

    # 不应抛异常
    store.migrate_session_id_prefixes()

    with store._connect() as conn:
        session = conn.execute("SELECT id FROM sessions WHERE id = ?", ("dashboard-old2",)).fetchone()
        assert session is not None
        msg = conn.execute("SELECT session_id FROM messages WHERE id = ?", ("msg-1",)).fetchone()
        assert msg["session_id"] == "dashboard-old2"


@pytest.mark.asyncio
async def test_delete_session_clears_task_origin_session_id(tmp_path):
    """删除 origin session 时，tasks.origin_session_id 必须置空，不删除 Task。"""
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    with store._connect() as conn:
        _create_tasks_table(conn)
        conn.execute(
            "INSERT INTO sessions (id, title, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dashboard-origin1", "Origin Session", "dashboard", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
        _insert_task_row(
            conn,
            "t_origin",
            origin_session_id="dashboard-origin1",
            execution_session_id="task-t_origin",
        )
        conn.commit()

    deleted = await store.delete_session("dashboard-origin1")
    assert deleted is True

    with store._connect() as conn:
        # Task 行必须仍然存在
        row = conn.execute("SELECT id, origin_session_id, execution_session_id FROM tasks WHERE id = ?", ("t_origin",)).fetchone()
        assert row is not None
        # origin_session_id 被置空
        assert row["origin_session_id"] is None
        # execution_session_id 不受影响
        assert row["execution_session_id"] == "task-t_origin"


@pytest.mark.asyncio
async def test_delete_session_clears_task_execution_session_id(tmp_path):
    """删除 execution session 时，tasks.execution_session_id 必须置空，不删除 Task。

    下一次 claim 由 TaskAgentExecutor 以稳定 task-{uuid5(NAMESPACE_URL, task.id)} 重建/复用 execution
    session（本测试只验证 delete_session 的置空行为，重建由 T13 验证）。
    """
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    with store._connect() as conn:
        _create_tasks_table(conn)
        conn.execute(
            "INSERT INTO sessions (id, title, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("task-t_exec", "Execution Session", "task", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
        _insert_task_row(
            conn,
            "t_exec",
            origin_session_id="dashboard-origin2",
            execution_session_id="task-t_exec",
        )
        conn.commit()

    deleted = await store.delete_session("task-t_exec")
    assert deleted is True

    with store._connect() as conn:
        row = conn.execute("SELECT id, origin_session_id, execution_session_id FROM tasks WHERE id = ?", ("t_exec",)).fetchone()
        assert row is not None
        # Task 仍存在
        assert row["id"] == "t_exec"
        # execution_session_id 被置空
        assert row["execution_session_id"] is None
        # origin_session_id 不受影响
        assert row["origin_session_id"] == "dashboard-origin2"


@pytest.mark.asyncio
async def test_delete_session_without_task_table_does_not_error(tmp_path):
    """旧库未启用 Task（tasks 表不存在）时，delete_session 不应报错。"""
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    await store.create_session(ConversationSession(id="s-no-task-table"))
    # 不创建 tasks 表
    deleted = await store.delete_session("s-no-task-table")
    assert deleted is True
