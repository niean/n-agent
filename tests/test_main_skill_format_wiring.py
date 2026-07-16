from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.main import build_application_services


def _settings(tmp_path: Path) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=False,
        feishu_enabled=False,
    )


def test_skill_service_has_format_validator_wired(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.skill_service is not None
    # T11: SkillFormatValidator is injected (not None) so manage_skill enforces
    # Anthropic format validation in production.
    assert services.skill_service.format_validator is not None


def test_skill_manage_definition_has_naming_guidance(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    defs = services.tool_service.list_definitions()
    manage = next((d for d in defs if d.name == "skill_manage"), None)
    assert manage is not None
    desc = manage.description
    assert "kebab-case" in desc
    assert "metadata" in desc
    assert "skill-creator" in desc


def test_skill_creator_seed_exists_and_is_copied(tmp_path: Path):
    # The bundled skill-creator seed must exist and be discoverable.
    seed_file = (
        Path(__file__).resolve().parents[1]
        / "app" / "infrastructure" / "skill" / "seeds"
        / "skill-creator" / "SKILL.md"
    )
    assert seed_file.exists(), "skill-creator seed must exist"
    skills_root = tmp_path / "skills"
    services = build_application_services(_settings(tmp_path))
    # seed_default_skills runs during build and copies the seed into skills_root.
    assert (skills_root / "skill-creator" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_skill_creator_seed_scans_compliant(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    report = await services.skill_service.scan_now()
    assert report.skills_count >= 1
    # Scanning the copied seed produces no format_warning for skill-creator.
    fmt_warnings = [
        w for w in report.warnings
        if "skill-creator" in (w.relative_path or "")
    ]
    assert fmt_warnings == [], [(w.reason, w.detail) for w in fmt_warnings]

