from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

from app.interfaces.cli.commands import memory as memory_cmd


class _FakeProviderConfig:
    def __init__(self, pid="p1", name="P1", provider_type="mem0", enabled=False):
        self.id = pid
        self.name = name
        self.provider_type = provider_type
        self.base_url = "http://x"
        self.api_key_present = True
        self.enabled = enabled
        self.extra_config = {}
        self.probe_status = "ok"
        self.last_probe_error = ""
        self.last_probed_at = None
        self.created_at = ""
        self.updated_at = ""


class _FakeActivateResult:
    def __init__(self, config, refresh_failed=False):
        self.config = config
        self.tool_surface_refresh_failed = refresh_failed


class _FakeProviderService:
    def __init__(self):
        self.list_called = False
        self.got: list[str] = []
        self.created_kwargs = None
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.delete_result = True
        self.activated: list[str] = []
        self.deactivated: list[str] = []
        self.probed: list[str] = []
        self.update_result = (_FakeProviderConfig(), False)

    def list(self):
        self.list_called = True
        return [_FakeProviderConfig()]

    def get(self, pid):
        self.got.append(pid)
        if pid == "missing":
            from app.application.external_memory_provider_service import ExternalMemoryProviderNotFoundError
            raise ExternalMemoryProviderNotFoundError(pid)
        return _FakeProviderConfig(pid=pid)

    def create(self, *, name, provider_type, base_url, api_key, extra_config):
        self.created_kwargs = {
            "name": name, "provider_type": provider_type, "base_url": base_url,
            "api_key": api_key, "extra_config": extra_config,
        }
        return _FakeProviderConfig(name=name, provider_type=provider_type)

    def update(self, pid, *, name=None, base_url=None, api_key=None, clear_api_key=False, extra_config=None):
        self.updated.append((pid, {"name": name, "base_url": base_url, "api_key": api_key,
                                    "clear_api_key": clear_api_key, "extra_config": extra_config}))
        return self.update_result

    def delete(self, pid):
        self.deleted.append(pid)
        return self.delete_result

    def activate(self, pid):
        self.activated.append(pid)
        return _FakeActivateResult(_FakeProviderConfig(pid=pid, enabled=True))

    def deactivate(self, pid):
        self.deactivated.append(pid)
        return _FakeProviderConfig(pid=pid, enabled=False)

    def probe(self, pid):
        self.probed.append(pid)
        return "ok"


class _FakeMemoryService:
    def __init__(self):
        self.saved_global: list[list[str]] = []
        self.created_project: list[str] = []
        self.deleted_project: list[str] = []
        self.delete_project_result = True
        self.added_entry: list[tuple[str, str, str]] = []
        self.updated_entry: list[tuple[str, int, str, str]] = []
        self.deleted_entry: list[tuple[str, int, str]] = []

    def list_providers(self):
        return [{"name": "mem0", "enabled_global": True, "slot": "external-query", "active": True}]

    def save_global_enabled(self, provider_names):
        self.saved_global.append(list(provider_names))

    def create_project(self, name):
        self.created_project.append(name)
        return True

    def delete_project(self, name):
        self.deleted_project.append(name)
        return self.delete_project_result

    def get_external_memory(self, project_name, target="memory"):
        return "content"

    def list_project_entries(self, project_name, target="memory"):
        return ["entry1", "entry2"]

    def add_project_entry(self, project_name, content, target="memory"):
        self.added_entry.append((project_name, content, target))
        return True

    def update_project_entry(self, project_name, entry_index, content, target="memory"):
        self.updated_entry.append((project_name, entry_index, content, target))
        return True

    def delete_project_entry(self, project_name, entry_index, target="memory"):
        self.deleted_entry.append((project_name, entry_index, target))
        return True


def _args(**kw):
    base = {"memory_command": None, "json": False, "form": False, "yaml": False, "id": None, "name": None,
            "type": None, "base_url": None, "api_key": None,
            "clear_api_key": False, "extra_config": None,
            "providers": None, "project": None, "content": None,
            "index": None, "target": "memory"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_memory_list_providers_sync_no_await(monkeypatch, capsys):
    fake = _FakeProviderService()
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="list-providers"))
    assert rc == 0
    assert fake.list_called


def test_memory_create_provider(monkeypatch, capsys):
    fake = _FakeProviderService()
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="create-provider", name="N", type="mem0",
                               base_url="http://x", api_key="k"))
    assert rc == 0
    assert fake.created_kwargs["name"] == "N"
    assert fake.created_kwargs["provider_type"].value == "mem0"


def test_memory_create_provider_invalid_type(monkeypatch, capsys):
    fake = _FakeProviderService()
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="create-provider", name="N", type="invalid",
                               base_url="http://x", api_key="k"))
    assert rc == 2


def test_memory_update_provider_returns_tuple(monkeypatch, capsys):
    fake = _FakeProviderService()
    fake.update_result = (_FakeProviderConfig(pid="p1"), True)
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="update-provider", id="p1", name="New"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "refresh_failed" in out or "True" in out


def test_memory_delete_provider_false(monkeypatch, capsys):
    fake = _FakeProviderService()
    fake.delete_result = False
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="delete-provider", id="p1"))
    assert rc == 1


def test_memory_activate_provider(monkeypatch, capsys):
    fake = _FakeProviderService()
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="activate-provider", id="p1"))
    assert rc == 0
    assert fake.activated == ["p1"]


def test_memory_deactivate_provider(monkeypatch, capsys):
    fake = _FakeProviderService()
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="deactivate-provider", id="p1"))
    assert rc == 0
    assert fake.deactivated == ["p1"]


def test_memory_probe_provider(monkeypatch, capsys):
    fake = _FakeProviderService()
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="probe-provider", id="p1"))
    assert rc == 0
    assert fake.probed == ["p1"]


def test_memory_global_providers_list(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="global", providers="a,b"))
    assert rc == 0
    assert fake.saved_global == [["a", "b"]]


def test_memory_global_empty_list(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="global", providers=""))
    assert rc == 0
    assert fake.saved_global == [[]]


def test_memory_list_projects(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="list-projects"))
    assert rc == 0


def test_memory_create_project(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="create-project", name="proj1"))
    assert rc == 0
    assert fake.created_project == ["proj1"]


def test_memory_delete_project_false(monkeypatch, capsys):
    fake = _FakeMemoryService()
    fake.delete_project_result = False
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="delete-project", name="proj1"))
    assert rc == 1


def test_memory_list_entries(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="list-entries", project="proj1"))
    assert rc == 0


def test_memory_add_entry(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="add-entry", project="proj1", content="hello"))
    assert rc == 0
    assert fake.added_entry == [("proj1", "hello", "memory")]


def test_memory_update_entry(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="update-entry", project="proj1", index=0, content="new"))
    assert rc == 0
    assert fake.updated_entry == [("proj1", 0, "new", "memory")]


def test_memory_delete_entry(monkeypatch, capsys):
    fake = _FakeMemoryService()
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="delete-entry", project="proj1", index=0))
    assert rc == 0
    assert fake.deleted_entry == [("proj1", 0, "memory")]


def test_memory_provider_service_is_sync():
    """ExternalMemory 两个 service 是同步的，CLI 不应 asyncio.run/await。"""
    from app.application.external_memory_provider_service import ExternalMemoryProviderService
    from app.application.external_memory_service import ExternalMemoryService
    for name in ("list", "get", "create", "update", "delete", "activate", "deactivate", "probe"):
        method = getattr(ExternalMemoryProviderService, name, None)
        if method:
            assert not inspect.iscoroutinefunction(method), f"{name} must be sync"
    for name in ("list_providers", "save_global_enabled", "create_project", "delete_project",
                 "get_external_memory", "list_project_entries", "add_project_entry",
                 "update_project_entry", "delete_project_entry"):
        method = getattr(ExternalMemoryService, name, None)
        if method:
            assert not inspect.iscoroutinefunction(method), f"{name} must be sync"


def test_memory_provider_service_none_returns_error(monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: None)
    rc = memory_cmd.run(_args(memory_command="list-providers"))
    assert rc == 1


def test_memory_service_none_returns_error(monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_load_memory_service", lambda: None)
    rc = memory_cmd.run(_args(memory_command="list-projects"))
    assert rc == 1


def test_memory_list_providers_json_no_secret(monkeypatch, capsys):
    fake = _FakeProviderService()
    monkeypatch.setattr(memory_cmd, "_load_provider_service", lambda: fake)
    rc = memory_cmd.run(_args(memory_command="list-providers", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert "api_key" not in data[0]
    assert data[0]["api_key_present"] is True
