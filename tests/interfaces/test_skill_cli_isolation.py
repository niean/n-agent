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
    from app.interfaces import cli as cli_module

    def fake_build(*args, **kwargs):
        raise AssertionError(
            "build_application_services must not be invoked for `skill` commands"
            " when _load_skill_service is patched"
        )

    monkeypatch.setattr(cli_module, "build_application_services", fake_build)
    monkeypatch.setattr(cli_module, "_load_skill_service", lambda: _StubSkillService())
    rc = cli_module.main(["skill", "list"])
    assert rc == 0
