from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.interfaces.cli.commands import mcp


class _FakeTool:
    def __init__(self, site_id="s1", remote_name="t1", enabled=True, tid="tool-1"):
        self.site_id = site_id
        self.remote_name = remote_name
        self.local_name = remote_name
        self.description = "desc"
        self.input_schema = {}
        self.id = tid
        self.enabled = enabled


class _FakeMcp:
    def __init__(self):
        self.created_payload = None
        self.created_tool_include = None
        self.updated: list[tuple[str, object]] = []
        self.deleted: list[str] = []
        self.probed_payload = None
        self.refreshed: list[str] = []
        self.toggled: list[tuple[str, str, bool]] = []
        self._site = SimpleNamespace(
            id="s1", name="S1", url="http://x",
            transport_type=__import__("app.domain.mcp", fromlist=["McpTransportType"]).McpTransportType.STREAMABLE_HTTP,
            command=None, args=[], env={},
            enabled=True,
            last_probe_status=__import__("app.domain.mcp", fromlist=["McpProbeStatus"]).McpProbeStatus.SUCCESS,
            last_probe_error=None,
        )

    async def list_sites(self):
        return [self._site]

    async def get_site(self, sid):
        return self._site

    async def create_site_with_probe(self, payload, tool_include=None):
        self.created_payload = payload
        self.created_tool_include = tool_include
        return self._site

    async def update_site(self, sid, payload):
        self.updated.append((sid, payload))
        return self._site

    async def delete_site(self, sid):
        self.deleted.append(sid)

    async def probe_site(self, payload):
        self.probed_payload = payload
        return SimpleNamespace(tools=[])

    async def refresh_site_tools(self, sid):
        self.refreshed.append(sid)
        return [_FakeTool(sid, "t1"), _FakeTool(sid, "t2")]

    async def list_site_tools(self, sid):
        return [_FakeTool(sid, "t1")]

    async def set_tool_enabled(self, sid, tid, enabled):
        self.toggled.append((sid, tid, enabled))
        return _FakeTool(sid, "t1", enabled=enabled)


def _args(**kw):
    base = {"mcp_command": None, "json": False, "form": False, "yaml": False, "id": None, "name": None,
            "transport": None, "url": None, "command": None,
            "args": None, "env": None, "include_tools": None,
            "tool_id": None, "enabled": None, "disabled": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_mcp_list(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="list"))
    assert rc == 0
    assert "s1" in capsys.readouterr().out


def test_mcp_create_http(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="create", name="N", transport="http", url="http://x"))
    assert rc == 0
    assert fake.created_payload is not None
    assert fake.created_payload.url == "http://x"


def test_mcp_create_streamable_http_alias(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="create", name="N", transport="streamable-http", url="http://x"))
    assert rc == 0
    assert fake.created_payload.transport_type.value == "streamable_http"


def test_mcp_create_stdio_args_env_json(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="create", name="N", transport="stdio",
                       command="run", args='["a","b"]', env='{"K":"v"}'))
    assert rc == 0
    assert fake.created_payload.command == "run"
    assert fake.created_payload.args == ["a", "b"]
    assert fake.created_payload.env == {"K": "v"}


def test_mcp_create_stdio_invalid_args_returns_2(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="create", name="N", transport="stdio",
                       command="run", args='{"not":"list"}'))
    assert rc == 2


def test_mcp_create_stdio_invalid_env_returns_2(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="create", name="N", transport="stdio",
                       command="run", env='["not","dict"]'))
    assert rc == 2


def test_mcp_update_merge_patch(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="update", id="s1", name="NewName"))
    assert rc == 0
    assert len(fake.updated) == 1
    payload = fake.updated[0][1]
    assert payload.name == "NewName"
    # merge patch：未传字段保留原值
    assert payload.url == "http://x"


def test_mcp_refresh_returns_list(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="refresh", id="s1"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2" in out  # 工具总数


def test_mcp_toggle_uses_tool_id(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="toggle", id="s1", tool_id="tool-1", enabled=True, disabled=False))
    assert rc == 0
    assert fake.toggled == [("s1", "tool-1", True)]


def test_mcp_toggle_disabled_flag(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="toggle", id="s1", tool_id="tool-1", enabled=None, disabled=True))
    assert rc == 0
    assert fake.toggled == [("s1", "tool-1", False)]


def test_mcp_probe_unsaved(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="probe", id="s1"))
    assert rc == 0
    assert fake.probed_payload is not None
    assert fake.probed_payload.url == "http://x"


def test_mcp_tools(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="tools", id="s1"))
    assert rc == 0
    assert "t1" in capsys.readouterr().out


def test_mcp_env_redact_secret(monkeypatch, capsys):
    fake = _FakeMcp()
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: fake)
    rc = mcp.run(_args(mcp_command="list", json=True))
    assert rc == 0
    # _FakeMcp env 为空，验证脱敏函数对含 secret key 的 env 能脱敏
    sensitive_env = {"API_TOKEN": "abc", "NORMAL_VAR": "def"}
    redacted = mcp._redact_env(sensitive_env)
    assert redacted["API_TOKEN"] == "***"
    assert redacted["NORMAL_VAR"] == "def"
