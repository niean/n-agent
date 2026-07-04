from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.interfaces.cli.commands import provider


class _FakeProvider:
    def __init__(self):
        self.listed = False
        self.got: list[str] = []
        self.created_input = None
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.activated: list[str] = []

    async def list_providers(self):
        self.listed = True
        return [
            SimpleNamespace(
                id="p1", name="P1", provider_type="openai-compatible",
                base_url="http://x", model="m", is_active=True,
                api_key_present=True, extra_headers={},
            )
        ]

    async def get_provider(self, pid):
        self.got.append(pid)
        if pid == "missing":
            return None
        return SimpleNamespace(
            id=pid, name="P1", provider_type="openai-compatible",
            base_url="http://x", model="m", is_active=True,
            api_key_present=True, extra_headers={},
        )

    async def create_provider(self, payload):
        self.created_input = payload
        return SimpleNamespace(
            id="p2", name=payload.name, provider_type=payload.provider_type,
            base_url=payload.base_url, model=payload.model, is_active=False,
            api_key_present=True, extra_headers=payload.extra_headers or {},
        )

    async def update_provider(self, pid, payload):
        self.updated.append((pid, payload.__dict__))
        return SimpleNamespace(
            id=pid, name=payload.name or "P1", provider_type="openai-compatible",
            base_url="http://x", model="m", is_active=True,
            api_key_present=True, extra_headers={},
        )

    async def delete_provider(self, pid):
        from app.domain.provider import ProviderInUseError
        if pid == "active":
            raise ProviderInUseError("in use")
        self.deleted.append(pid)

    async def activate_provider(self, pid):
        self.activated.append(pid)
        return SimpleNamespace(
            id=pid, name="P1", provider_type="openai-compatible",
            base_url="http://x", model="m", is_active=True,
            api_key_present=True, extra_headers={},
        )


def _args(**kw):
    base = {"provider_command": None, "json": False, "form": False, "yaml": False, "id": None, "name": None,
            "type": None, "base_url": None, "model": None, "api_key": None,
            "extra_headers": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_provider_list(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="list"))
    assert rc == 0 and fake.listed
    assert "p1" in capsys.readouterr().out


def test_provider_get_not_found(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="get", id="missing"))
    assert rc == 1


def test_provider_create(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="create", name="N", type="openai-compatible",
                             base_url="http://x", model="m", api_key="sk-1"))
    assert rc == 0
    assert fake.created_input is not None
    assert fake.created_input.api_key == "sk-1"
    assert fake.created_input.provider_type == "openai-compatible"


def test_provider_create_missing_api_key(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="create", name="N", type="openai-compatible",
                             base_url="http://x", model="m", api_key=None))
    assert rc == 2


def test_provider_delete_active_returns_error(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="delete", id="active"))
    assert rc == 1


def test_provider_activate(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="activate", id="p1"))
    assert rc == 0
    assert fake.activated == ["p1"]


def test_provider_list_json(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="list", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data[0]["id"] == "p1"
    assert "api_key" not in data[0]


def test_provider_list_isolation(monkeypatch):
    """_load_provider_service indirection allows monkeypatch without build_application_services."""
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    args = SimpleNamespace(provider_command="list", json=True)
    assert provider.run(args) == 0


def test_provider_list_default_is_json(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="list"))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data[0]["id"] == "p1"


def test_provider_list_form_flag_renders_table(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="list", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "p1" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_provider_list_yaml_flag(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="list", yaml=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "id: p1" in out


def test_provider_get_default_is_json(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="get", id="p1"))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == "p1"


def test_provider_delete_default_is_json(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="delete", id="p1"))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"deleted": "p1"}


def test_provider_delete_form_flag(monkeypatch, capsys):
    fake = _FakeProvider()
    monkeypatch.setattr(provider, "_load_provider_service", lambda: fake)
    rc = provider.run(_args(provider_command="delete", id="p1", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "deleted: p1" in out
