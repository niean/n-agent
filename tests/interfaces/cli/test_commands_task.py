from __future__ import annotations

from types import SimpleNamespace

from app.domain.task import TaskListPage
from app.interfaces.cli.commands import task as task_cmd
from app.interfaces.cli.main import build_parser


def _fake_task(task_id: str = "t1", title: str = "T1"):
    """Minimal task-like object covering _task_to_dict getattr reads."""
    return SimpleNamespace(
        id=task_id,
        title=title,
        body="body-text",
        assignee=None,
        priority=0,
        status=SimpleNamespace(value="TRIAGE"),
        block_kind=None,
        block_reason=None,
        block_recurrences=0,
        consecutive_failures=0,
        max_retries=0,
        goal_mode=False,
        goal_max_turns=None,
        skills=(),
        model_override=None,
        workspace_kind=SimpleNamespace(value="LOCAL"),
        workspace_path=None,
        origin_session_id=None,
        execution_session_id=None,
        current_run_id=None,
        worker_token=None,
        version=1,
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
        started_at=None,
        completed_at=None,
        result=None,
    )


class _FakeTaskService:
    def __init__(self, page: TaskListPage | None = None):
        self._page = page or TaskListPage(items=(_fake_task(),), next_cursor=None)

    async def list_tasks(self, board="default", cursor=None, limit=100):
        return self._page


def _args(**overrides):
    base = dict(task_command="ls", status=None, all=False, json=False, form=False, yaml=False)
    base.update(overrides)
    return SimpleNamespace(**base)


class _PagedTaskService:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def list_tasks(self, board="default", cursor=None, limit=100):
        self.calls.append((cursor, limit))
        return self.pages.pop(0)


def test_task_list_and_ls_parser_accept_all():
    parser = build_parser(plugin_commands=[])
    assert parser.parse_args(["task", "list", "--all"]).all is True
    assert parser.parse_args(["task", "ls", "--all"]).all is True


def test_task_list_iterates_page_items(monkeypatch, capsys):
    """Regression: list_tasks returns TaskListPage, CLI must iterate .items."""
    fake = _FakeTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: fake)
    rc = task_cmd.run(_args(task_command="ls"))
    assert rc == 0
    assert "t1" in capsys.readouterr().out


def test_task_list_alias_matches_list(monkeypatch, capsys):
    fake = _FakeTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: fake)
    rc = task_cmd.run(_args(task_command="list"))
    assert rc == 0
    assert "t1" in capsys.readouterr().out


def test_task_list_empty_page(monkeypatch, capsys):
    fake = _FakeTaskService(page=TaskListPage(items=(), next_cursor=None))
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: fake)
    rc = task_cmd.run(_args(task_command="ls"))
    assert rc == 0


def test_task_list_all_aggregates_cursor_pages(monkeypatch, capsys):
    cursor = SimpleNamespace(
        created_at="2026-07-19T00:00:00+00:00",
        task_id="t1",
    )
    service = _PagedTaskService([
        TaskListPage(items=(_fake_task("t1", "first"),), next_cursor=cursor),
        TaskListPage(items=(_fake_task("t2", "second"),), next_cursor=None),
    ])
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)

    rc = task_cmd.run(_args(task_command="list", all=True))

    assert rc == 0
    assert service.calls == [(None, 100), (cursor, 100)]
    output = capsys.readouterr().out
    assert '"id": "t1"' in output
    assert '"id": "t2"' in output


def test_task_ls_all_aggregates_cursor_pages(monkeypatch, capsys):
    cursor = SimpleNamespace(
        created_at="2026-07-19T00:00:00+00:00",
        task_id="t1",
    )
    service = _PagedTaskService([
        TaskListPage(items=(_fake_task("t1", "first"),), next_cursor=cursor),
        TaskListPage(items=(_fake_task("t2", "second"),), next_cursor=None),
    ])
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)

    rc = task_cmd.run(_args(task_command="ls", all=True))

    assert rc == 0
    assert service.calls == [(None, 100), (cursor, 100)]
    output = capsys.readouterr().out
    assert '"id": "t1"' in output
    assert '"id": "t2"' in output


def test_task_list_default_keeps_single_page(monkeypatch):
    cursor = SimpleNamespace(
        created_at="2026-07-19T00:00:00+00:00",
        task_id="t1",
    )
    service = _PagedTaskService([
        TaskListPage(items=(_fake_task("t1"),), next_cursor=cursor),
    ])
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)

    assert task_cmd.run(_args(task_command="list", all=False)) == 0
    assert service.calls == [(None, 100)]
