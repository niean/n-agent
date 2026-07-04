from __future__ import annotations

from app.interfaces.cli.commands import plugin, skill


def test_skill_list_does_not_call_build_application_services(monkeypatch):
    class _FakeSkill:
        async def list_skills(self, include_disabled: bool = False):
            return []

        async def render_view(self, name: str):
            return {"success": True, "content": ""}

    monkeypatch.setattr(skill, "_load_skill_service", lambda: _FakeSkill())
    args = type("A", (), {"skill_command": "list"})()
    rc = skill.run(args)
    assert rc == 0


def test_plugin_list_does_not_call_build_application_services(monkeypatch):
    class _FakePlugin:
        async def list_plugins(self):
            return []

        async def get_plugin(self, name: str):
            return None

    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: _FakePlugin())
    args = type("A", (), {"plugin_command": "list"})()
    rc = plugin.run(args)
    assert rc == 0
