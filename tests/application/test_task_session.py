from uuid import uuid5, NAMESPACE_URL

from app.application.task_session import task_execution_session_id
from app.domain.task import Task


def _task(**kwargs) -> Task:
    defaults = {"id": "t1", "title": "完成报告", "board": "default"}
    defaults.update(kwargs)
    return Task(**defaults)


def test_selector_prefers_explicit_execution_session():
    t = _task(origin_session_id="dashboard-abc", execution_session_id="task-explicit")
    assert task_execution_session_id(t) == "task-explicit"


def test_selector_uses_origin_when_no_explicit():
    t = _task(origin_session_id="dashboard-abc", execution_session_id=None)
    assert task_execution_session_id(t) == "dashboard-abc"


def test_selector_falls_back_to_uuid5_when_no_origin():
    t = _task(origin_session_id=None, execution_session_id=None)
    assert task_execution_session_id(t) == f"task-{uuid5(NAMESPACE_URL, 't1')}"


def test_selector_stable_across_calls():
    t = _task(origin_session_id=None, execution_session_id=None)
    assert task_execution_session_id(t) == task_execution_session_id(t)


def test_selector_origin_reuse_for_dashboard_task():
    """dashboard /task 任务：origin_session_id = Chat 会话 -> worker 在 Chat 会话执行。"""
    t = _task(origin_session_id="dashboard-7f320d59-9a0e-57e2-acf3-4a36af1cfa4c")
    assert task_execution_session_id(t) == "dashboard-7f320d59-9a0e-57e2-acf3-4a36af1cfa4c"


def test_selector_no_origin_kanban_cli_falls_back():
    """kanban/CLI 任务（origin=None）-> 回退 task-{uuid5}，Chat 不可见。"""
    t = _task(id="t_kanban", origin_session_id=None)
    assert task_execution_session_id(t) == f"task-{uuid5(NAMESPACE_URL, 't_kanban')}"
