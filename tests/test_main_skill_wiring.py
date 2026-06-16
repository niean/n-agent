from pathlib import Path

from app.config import Settings
from app.main import build_application_services


def test_build_application_services_wires_skill(tmp_path: Path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=False,
        feishu_enabled=False,
    )

    services = build_application_services(settings)

    assert services.skill_service is not None
    names = {d.name for d in services.tool_service.list_definitions()}
    assert {"skills_list", "skill_view"} <= names
