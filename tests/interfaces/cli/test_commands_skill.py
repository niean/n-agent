from __future__ import annotations

from app.interfaces.cli.commands import skill


def _make_args(**kw):
    defaults = {"skill_command": None, "name": None}
    defaults.update(kw)
    return type("A", (), defaults)()


class _FakeSkillService:
    def __init__(self):
        self.listed = False
        self.viewed: list[str] = []

    async def list_skills(self, include_disabled: bool = False):
        self.listed = True
        return []

    async def render_view(self, name: str):
        self.viewed.append(name)
        return {"success": True, "content": ""}


def test_skill_list_returns_zero(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="list")
    rc = skill.run(args)
    assert rc == 0
    assert fake.listed


def test_skill_view_returns_zero(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="view", name="demo")
    rc = skill.run(args)
    assert rc == 0
    assert fake.viewed == ["demo"]


def test_skill_view_not_found_returns_one(monkeypatch):
    class _NotFound:
        async def render_view(self, name: str):
            return {"success": False}

    monkeypatch.setattr(skill, "_load_skill_service", lambda: _NotFound())
    args = _make_args(skill_command="view", name="missing")
    rc = skill.run(args)
    assert rc == 1
