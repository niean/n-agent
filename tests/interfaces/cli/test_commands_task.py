from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.task import TaskStateError, TaskValidationError
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


# ---------------------------------------------------------------------------
# T10: task revise / approve / reject -- parser, dispatch, output, errors
# ---------------------------------------------------------------------------


class _ApprovalTaskService:
    """Fake service recording approve/reject/revise calls.

    Returns a dict shaped like TaskService._resolve_proposal's whitelist
    response (task_id/decision/proposal_event_id/note/status). revise_change
    additionally includes ``title`` (mirrors the real service).
    """

    def __init__(self, *, response: dict | None = None, exc: Exception | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._response = response or {
            "task_id": "t_1",
            "decision": "revised",
            "proposal_event_id": "evt_42",
            "note": "改一下",
            "status": "QUEUED",
            "title": "T1",
        }
        self._exc = exc

    async def approve_change(self, task_id, note=None):
        self.calls.append(("approve_change", (task_id,), {"note": note}))
        if self._exc is not None:
            raise self._exc
        resp = dict(self._response)
        resp["decision"] = "approved"
        resp["note"] = note
        resp.pop("title", None)
        return resp

    async def reject_change(self, task_id, note=None):
        self.calls.append(("reject_change", (task_id,), {"note": note}))
        if self._exc is not None:
            raise self._exc
        resp = dict(self._response)
        resp["decision"] = "rejected"
        resp["note"] = note
        resp.pop("title", None)
        return resp

    async def revise_change(self, task_id, note=None):
        self.calls.append(("revise_change", (task_id,), {"note": note}))
        if self._exc is not None:
            raise self._exc
        resp = dict(self._response)
        resp["decision"] = "revised"
        resp["note"] = note
        return resp


# --- parser ---------------------------------------------------------------


def test_task_revise_parser_accepts_id_and_required_note():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["task", "revise", "t_1", "--note", "改一下"])
    assert args.task_command == "revise"
    assert args.id == "t_1"
    assert args.note == "改一下"


def test_task_revise_parser_requires_note():
    parser = build_parser(plugin_commands=[])
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["task", "revise", "t_1"])
    assert exc.value.code == 2


def test_task_approve_note_is_optional_in_parser():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["task", "approve", "t_1"])
    assert args.task_command == "approve"
    assert args.id == "t_1"
    assert args.note is None


def test_task_reject_note_is_optional_in_parser():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["task", "reject", "t_1"])
    assert args.task_command == "reject"
    assert args.id == "t_1"
    assert args.note is None


# --- dispatch -------------------------------------------------------------


def test_task_revise_dispatch_calls_revise_change_with_id_and_note(monkeypatch):
    service = _ApprovalTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    rc = task_cmd.run(_args(task_command="revise", id="t_1", note="改一下"))
    assert rc == 0
    assert service.calls == [("revise_change", ("t_1",), {"note": "改一下"})]


def test_task_approve_dispatch_passes_optional_note(monkeypatch):
    service = _ApprovalTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    rc = task_cmd.run(_args(task_command="approve", id="t_1", note="ok"))
    assert rc == 0
    assert service.calls == [("approve_change", ("t_1",), {"note": "ok"})]


def test_task_reject_dispatch_passes_none_note_when_omitted(monkeypatch):
    service = _ApprovalTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    rc = task_cmd.run(_args(task_command="reject", id="t_1", note=None))
    assert rc == 0
    assert service.calls == [("reject_change", ("t_1",), {"note": None})]


# --- output (json/form/yaml) ---------------------------------------------


_REQUIRED_KEYS = {"task_id", "decision", "proposal_event_id", "note", "status"}


def test_task_revise_json_output_contains_required_keys(monkeypatch, capsys):
    service = _ApprovalTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    rc = task_cmd.run(_args(task_command="revise", id="t_1", note="改一下", json=True))
    assert rc == 0
    import json as _json
    payload = _json.loads(capsys.readouterr().out)
    assert _REQUIRED_KEYS.issubset(payload.keys())
    assert payload["task_id"] == "t_1"
    assert payload["decision"] == "revised"
    assert payload["proposal_event_id"] == "evt_42"
    assert payload["note"] == "改一下"
    assert payload["status"] == "QUEUED"


def test_task_revise_yaml_output_contains_required_keys(monkeypatch, capsys):
    service = _ApprovalTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    rc = task_cmd.run(_args(task_command="revise", id="t_1", note="改一下", yaml=True))
    assert rc == 0
    import yaml as _yaml
    payload = _yaml.safe_load(capsys.readouterr().out)
    assert _REQUIRED_KEYS.issubset(payload.keys())
    assert payload["decision"] == "revised"
    assert payload["note"] == "改一下"


def test_task_revise_form_output_contains_required_keys(monkeypatch, capsys):
    service = _ApprovalTaskService()
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    rc = task_cmd.run(_args(task_command="revise", id="t_1", note="改一下", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    # form renderer (render_object) prints "key: value" lines for each field
    for key in _REQUIRED_KEYS:
        assert key in out
    assert "改一下" in out


# --- top-level error integration -----------------------------------------


def test_task_revise_business_error_flows_through_cli_top_level(monkeypatch, capsys):
    """Service-level validation errors must surface via the CLI top-level
    exception handler as a single stderr line and exit code 1.

    The command function itself must NOT invent HTTP/ToolResult codes; it
    lets the exception bubble so ``_invoke_handler`` can format it.
    """
    from app.interfaces.cli.main import main

    service = _ApprovalTaskService(exc=TaskValidationError("note must not be empty"))
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    monkeypatch.setattr(
        "app.main.collect_plugin_cli_commands", lambda: [], raising=False
    )

    rc = main(["task", "revise", "t_1", "--note", "改一下"])
    assert rc == 1
    err = capsys.readouterr().err
    # single line, prefixed by the top-level handler
    assert err.strip() == "error: note must not be empty"
    assert err.count("\n") <= 1


def test_task_revise_state_error_flows_through_cli_top_level(monkeypatch, capsys):
    """TaskStateError (e.g. wrong status) also routes through the top-level
    handler without command-level code invention."""
    from app.interfaces.cli.main import main

    service = _ApprovalTaskService(
        exc=TaskStateError("resolve_proposal requires WAITING_APPROVAL, got RUNNING")
    )
    monkeypatch.setattr(task_cmd, "_load_task_service", lambda: service)
    monkeypatch.setattr(
        "app.main.collect_plugin_cli_commands", lambda: [], raising=False
    )

    rc = main(["task", "revise", "t_1", "--note", "改一下"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.strip().startswith("error: ")
    assert "WAITING_APPROVAL" in err
