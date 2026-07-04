from app.interfaces.cli import main
from app.interfaces.cli.commands import skill as skill_cmd


class _StubSkillService:
    async def list_skills(self, include_disabled=True):
        return []

    async def render_view(self, name, session_id=""):
        return {
            "success": True,
            "name": name,
            "content": "",
            "description": "",
            "readiness": "available",
            "linked_files": {},
        }


def test_skill_command_does_not_invoke_build_application_services(monkeypatch):
    def fake_build(*args, **kwargs):
        raise AssertionError(
            "build_application_services must not be invoked for `skill` commands"
            " when _load_skill_service is patched"
        )

    monkeypatch.setattr(skill_cmd, "_load_skill_service", lambda: _StubSkillService())
    monkeypatch.setattr(skill_cmd, "_load_skill_service", lambda: _StubSkillService())
    rc = main(["skill", "list"])
    assert rc == 0
