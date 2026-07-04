from datetime import datetime, timezone

from app.domain.skill import Skill, SkillFrontmatter, SkillReadiness
from app.interfaces.cli import main
from app.interfaces.cli.commands import skill as skill_cmd


def _skill(name):
    fm = SkillFrontmatter(
        name=name, description="d", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={"name": name},
    )
    return Skill(
        id=f"id-{name}", name=name, relative_path=f"{name}/SKILL.md",
        description="d", platforms=["linux"], frontmatter=fm,
        enabled=True, readiness=SkillReadiness.AVAILABLE,
        last_scan_status="ok", last_scan_error=None,
        last_seen_at=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )


class _FakeSkillService:
    async def list_skills(self, include_disabled=True):
        return [_skill("alpha"), _skill("beta")]

    async def render_view(self, name, session_id=""):
        return {
            "success": True,
            "name": name,
            "content": "BODY-" + name,
            "description": "d",
            "readiness": "available",
            "linked_files": {},
        }


def test_cli_skill_list_outputs_names(monkeypatch, capsys):
    monkeypatch.setattr(skill_cmd, "_load_skill_service", lambda: _FakeSkillService())
    rc = main(["skill", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out


def test_cli_skill_view_prints_content(monkeypatch, capsys):
    monkeypatch.setattr(skill_cmd, "_load_skill_service", lambda: _FakeSkillService())
    rc = main(["skill", "view", "alpha"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BODY-alpha" in out
