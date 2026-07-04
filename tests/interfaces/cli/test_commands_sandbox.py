from __future__ import annotations

import json
from types import SimpleNamespace

from app.interfaces.cli.commands import sandbox


class _FakeSandbox:
    def __init__(self):
        self.history_called: list[tuple[str | None, int | None]] = []
        self.released: list[str] = []
        self.deleted_history: list[str] = []

    async def get_config(self):
        return {"backend": "docker", "enabled": True}

    async def list_active_sandboxes(self):
        return [{"session_id": "s1", "sandbox_type": "docker", "idle_seconds": 10}]

    async def list_released_sandboxes(self):
        return [{"session_id": "s1", "reason": "manual"}]

    async def list_execute_code_history(self, session_id=None, limit=50):
        self.history_called.append((session_id, limit))
        return [{"id": "h1", "session_id": "s1", "status": "success"}]

    async def release_sandbox(self, session_id):
        self.released.append(session_id)
        return {"session_id": session_id, "released": True}

    async def delete_execute_code_history(self, tool_call_id):
        self.deleted_history.append(tool_call_id)
        return {"deleted": tool_call_id}


def _args(**kw):
    base = {"sandbox_command": None, "json": False, "form": False, "yaml": False, "session_id": None,
            "limit": None, "tool_call_id": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_sandbox_list_active_disabled(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: None)
    rc = sandbox.run(_args(sandbox_command="list-active"))
    assert rc == 0
    assert "disabled" in capsys.readouterr().out.lower()


def test_sandbox_list_active(monkeypatch, capsys):
    fake = _FakeSandbox()
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: fake)
    rc = sandbox.run(_args(sandbox_command="list-active"))
    assert rc == 0
    assert "s1" in capsys.readouterr().out


def test_sandbox_list_released_disabled(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: None)
    rc = sandbox.run(_args(sandbox_command="list-released"))
    assert rc == 0


def test_sandbox_list_history_session_filter(monkeypatch, capsys):
    fake = _FakeSandbox()
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: fake)
    rc = sandbox.run(_args(sandbox_command="list-history", session_id="s1", limit=10))
    assert rc == 0
    assert fake.history_called == [("s1", 10)]


def test_sandbox_list_history_disabled(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: None)
    rc = sandbox.run(_args(sandbox_command="list-history"))
    assert rc == 0


def test_sandbox_release_disabled_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: None)
    rc = sandbox.run(_args(sandbox_command="release", session_id="s1"))
    assert rc == 1


def test_sandbox_release(monkeypatch, capsys):
    fake = _FakeSandbox()
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: fake)
    rc = sandbox.run(_args(sandbox_command="release", session_id="s1"))
    assert rc == 0
    assert fake.released == ["s1"]


def test_sandbox_delete_history_disabled_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: None)
    rc = sandbox.run(_args(sandbox_command="delete-history", tool_call_id="tc1"))
    assert rc == 1


def test_sandbox_delete_history(monkeypatch, capsys):
    fake = _FakeSandbox()
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: fake)
    rc = sandbox.run(_args(sandbox_command="delete-history", tool_call_id="tc1"))
    assert rc == 0
    assert fake.deleted_history == ["tc1"]


def test_sandbox_config_disabled(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: None)
    rc = sandbox.run(_args(sandbox_command="config"))
    assert rc == 0


def test_sandbox_config(monkeypatch, capsys):
    fake = _FakeSandbox()
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: fake)
    rc = sandbox.run(_args(sandbox_command="config", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["backend"] == "docker"
