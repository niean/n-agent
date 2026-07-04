from __future__ import annotations

import json
from types import SimpleNamespace

from app.interfaces.cli.commands import logs


class _FakeSandbox:
    def __init__(self):
        self.history_called: list[tuple[str | None, int | None]] = []

    async def list_execute_code_history(self, session_id=None, limit=50):
        self.history_called.append((session_id, limit))
        return [{"id": "h1", "session_id": "s1", "status": "success"}]


class _FakeSession:
    def __init__(self):
        self.tool_calls_session: list[str] = []
        self.detail_called: list[str] = []

    async def list_tool_calls(self, session_id):
        self.tool_calls_session.append(session_id)
        return [{"id": f"tc{i}", "name": "n"} for i in range(100)]

    async def get_session_detail(self, session_id):
        self.detail_called.append(session_id)
        return {"session_id": session_id, "task_state": {"iter": 1}, "messages": []}


class _FakeSchedule:
    def __init__(self):
        self.executions_called: list[tuple[str, int]] = []

    async def list_executions(self, task_id, limit=10):
        self.executions_called.append((task_id, limit))
        return [{"id": "e1", "status": "success"}]


def _args(**kw):
    base = {"logs_command": None, "json": False, "form": False, "yaml": False, "session_id": None,
            "task_id": None, "limit": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_logs_sandbox_disabled_returns_0(monkeypatch, capsys):
    monkeypatch.setattr(logs, "_load_sandbox_service", lambda: None)
    rc = logs.run(_args(logs_command="sandbox"))
    assert rc == 0
    assert "disabled" in capsys.readouterr().out.lower()


def test_logs_sandbox_with_session_filter(monkeypatch, capsys):
    fake = _FakeSandbox()
    monkeypatch.setattr(logs, "_load_sandbox_service", lambda: fake)
    rc = logs.run(_args(logs_command="sandbox", session_id="s1", limit=10))
    assert rc == 0
    assert fake.history_called == [("s1", 10)]


def test_logs_tools_local_limit(monkeypatch, capsys):
    fake = _FakeSession()
    monkeypatch.setattr(logs, "_load_session_service", lambda: fake)
    rc = logs.run(_args(logs_command="tools", session_id="s1", limit=10, json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 10


def test_logs_tools_no_limit_returns_all(monkeypatch, capsys):
    fake = _FakeSession()
    monkeypatch.setattr(logs, "_load_session_service", lambda: fake)
    rc = logs.run(_args(logs_command="tools", session_id="s1", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 100


def test_logs_scheduled_requires_task_id(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(logs, "_load_schedule_service", lambda: fake)
    rc = logs.run(_args(logs_command="scheduled", task_id=None))
    assert rc == 2


def test_logs_scheduled_limit_too_low_returns_2(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(logs, "_load_schedule_service", lambda: fake)
    rc = logs.run(_args(logs_command="scheduled", task_id="t1", limit=0))
    assert rc == 2


def test_logs_scheduled_valid(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(logs, "_load_schedule_service", lambda: fake)
    rc = logs.run(_args(logs_command="scheduled", task_id="t1", limit=20, json=True))
    assert rc == 0
    assert fake.executions_called == [("t1", 20)]


def test_logs_runs_outputs_task_state(monkeypatch, capsys):
    fake = _FakeSession()
    monkeypatch.setattr(logs, "_load_session_service", lambda: fake)
    rc = logs.run(_args(logs_command="runs", session_id="s1", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "task_state" in data
    assert fake.detail_called == ["s1"]
