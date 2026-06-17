import asyncio
from pathlib import Path

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


def test_application_services_register_schedule_tools(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    names = {d.name for d in services.tool_service.list_definitions()}
    assert {"manage_schedule", "schedule_query"} <= names
    seeded = tmp_path / "skills" / "n-agent" / "SKILL.md"
    assert seeded.exists()


def test_application_services_seed_makes_n_agent_skill_visible(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))

    async def _scan():
        report = await services.skill_service.scan_now()
        skills = await services.skill_service.list_for_llm()
        return report, skills

    report, skills = asyncio.run(_scan())
    assert any(s.name == "n-agent" for s in skills), report
