from pathlib import Path

from app.config import Settings
from app.main import build_application_services


def test_build_application_services_wires_sandbox(tmp_path: Path):
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(tmp_path / "skills"),
        scheduler_enabled=False,
        feishu_enabled=False,
        sandbox_enabled=True,
        sandbox_type="local",
        sandbox_scratch_root=str(tmp_path / "scratch"),
    )

    services = build_application_services(settings)

    assert services.sandbox_dashboard_service is not None
    assert services.sandbox_manager is not None
    names = {d.name for d in services.tool_service.list_definitions()}
    assert "execute_code" in names


def test_build_application_services_sandbox_disabled(tmp_path: Path):
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(tmp_path / "skills"),
        scheduler_enabled=False,
        feishu_enabled=False,
        sandbox_enabled=False,
    )

    services = build_application_services(settings)

    assert services.sandbox_dashboard_service is None
    assert services.sandbox_manager is None
    names = {d.name for d in services.tool_service.list_definitions()}
    assert "execute_code" not in names


def test_health_snapshot_includes_sandbox(tmp_path: Path):
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(tmp_path / "skills"),
        scheduler_enabled=False,
        feishu_enabled=False,
        sandbox_enabled=True,
        sandbox_type="local",
        sandbox_scratch_root=str(tmp_path / "scratch"),
    )

    services = build_application_services(settings)
    health = services.health_snapshot()
    assert "sandbox" in health
    assert health["sandbox"]["enabled"] is True
    assert health["sandbox"]["type"] == "local"
    assert health["sandbox"]["status"] == "warn"
