"""统一任务执行会话选择器。

Application 低层模块，不依赖 Interfaces/Infrastructure。TaskAgentExecutor 的 run /
goal loop 每轮 / judge fork 与 TaskRunService / TaskService 的生命周期写入必须共用此
函数，禁止复制派生表达式（spec: 统一执行会话选择）。
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from app.domain.task import Task


def task_session_id_fallback(task_id: str) -> str:
    """Deterministic fallback execution session id for a task.

    Returns ``task-{uuid5(NAMESPACE_URL, task_id)}`` -- stable across runs
    and computable from task id alone (no Task record needed). Used by
    ``task_execution_session_id`` priority 3 and by the session resolver
    when a task record is gone (deleted) but its artifacts remain.
    """
    return f"task-{uuid5(NAMESPACE_URL, task_id)}"


def task_execution_session_id(task: Task) -> str:
    """选择任务 worker 的执行会话 ID。

    优先级：
      1. ``task.execution_session_id`` -- 显式存量/外部值，最高优先级（兼容）
      2. ``task.origin_session_id`` -- Dashboard ``/task create`` 捕获的 Chat 会话；
         仅 dashboard /task 任务设置，kanban/CLI/feishu 不设置（origin=None）
      3. ``task_session_id_fallback(task.id)`` -- 确定性回退，跨 run 稳定、无需持久化

    本函数不写回 tasks 表；派生值仅在运行时使用。删除任务时 task_service 按持久化的
    显式 ``execution_session_id`` 清理（当前 DB NULL -> 不删 origin Chat 会话），
    不受派生影响。
    """
    if task.execution_session_id:
        return task.execution_session_id
    if task.origin_session_id:
        return task.origin_session_id
    return task_session_id_fallback(task.id)
