from __future__ import annotations

from app.interfaces.cli.commands import plugin


def _make_args(**kw):
    defaults = {"plugin_command": None, "name": None}
    defaults.update(kw)
    return type("A", (), defaults)()


class _FakePluginService:
    def __init__(self):
        self.listed = False
        self.viewed: list[str] = []

    async def list_plugins(self):
        self.listed = True
        return []

    async def get_plugin(self, name: str):
        self.viewed.append(name)
        return None


def test_plugin_list_returns_zero(monkeypatch):
    fake = _FakePluginService()
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    args = _make_args(plugin_command="list")
    rc = plugin.run(args)
    assert rc == 0
    assert fake.listed


def test_plugin_view_not_found_returns_one(monkeypatch):
    fake = _FakePluginService()
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    args = _make_args(plugin_command="view", name="missing")
    rc = plugin.run(args)
    assert rc == 1
    assert fake.viewed == ["missing"]
