from __future__ import annotations

import json
from types import SimpleNamespace

from app.interfaces.cli.commands import config as config_cmd


def _make_settings():
    return SimpleNamespace(
        provider_base_url="http://x",
        provider_api_key="sk-secret",
        provider_model="m",
        sqlite_path="/tmp/x.db",
        workspace_root="/tmp",
        agent_iteration_limit=10,
        kb_enabled=False,
        kb_base_url="",
        mcp_connect_timeout_seconds=10,
        sandbox_enabled=False,
        sandbox_type="docker",
        feishu_app_id="",
        feishu_app_secret="feishu-secret",
        web_fetch_enabled=True,
    )


def _args(**kw):
    base = {"json": False, "form": False, "yaml": False, "section": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_config_shows_provider_api_key_present_only(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["provider_api_key_present"] is True
    assert "sk-secret" not in json.dumps(data)
    assert "provider_api_key" not in data or data.get("provider_api_key") in (None, "")


def test_config_section_filter(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(section="provider", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "provider_base_url" in data
    assert "provider_model" in data
    assert "sqlite_path" not in data


def test_config_json_output(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "provider_api_key_present" in data
    assert "sk-secret" not in json.dumps(data)


def test_config_feishu_secret_redacted(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["feishu_app_secret_present"] is True
    assert "feishu-secret" not in json.dumps(data)


def test_config_table_output(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "provider_base_url" in out
    assert "sk-secret" not in out
