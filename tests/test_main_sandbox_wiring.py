from pathlib import Path

from app.config import Settings
from app.domain.tool import RiskLevel, ToolSourceType
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
        artifacts_enabled=False,
    )

    services = build_application_services(settings)

    assert services.sandbox_dashboard_service is not None
    assert services.sandbox_manager is not None
    names = {d.name for d in services.tool_service.list_definitions()}
    assert "execute_code" in names
    assert "terminal" in names


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
        artifacts_enabled=False,
    )

    services = build_application_services(settings)

    assert services.sandbox_dashboard_service is None
    assert services.sandbox_manager is None
    names = {d.name for d in services.tool_service.list_definitions()}
    assert "execute_code" not in names
    assert "terminal" not in names


def test_terminal_definition_fields_when_sandbox_enabled(tmp_path: Path):
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
        artifacts_enabled=False,
    )

    services = build_application_services(settings)
    definitions = {d.name: d for d in services.tool_service.list_definitions()}
    terminal = definitions["terminal"]

    assert terminal.risk_level == RiskLevel.SAFE
    assert terminal.source_type == ToolSourceType.AGENT
    assert terminal.toolset == "sandbox"
    assert terminal.managed is False
    assert terminal.enabled is True


def test_terminal_input_schema_structure(tmp_path: Path):
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
        artifacts_enabled=False,
    )

    services = build_application_services(settings)
    definitions = {d.name: d for d in services.tool_service.list_definitions()}
    terminal = definitions["terminal"]
    schema = terminal.input_schema

    assert set(schema["properties"].keys()) == {"command", "timeout", "workdir"}
    assert schema["required"] == ["command"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["timeout"]["minimum"] == 1


def test_terminal_route_registered_when_sandbox_enabled(tmp_path: Path):
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
        artifacts_enabled=False,
    )

    services = build_application_services(settings)
    routes = services.tool_service.executor.routes
    assert "execute_code" in routes
    assert "terminal" in routes


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
        artifacts_enabled=False,
    )

    services = build_application_services(settings)
    health = services.health_snapshot()
    assert "sandbox" in health
    assert health["sandbox"]["enabled"] is True
    assert health["sandbox"]["type"] == "local"
    assert health["sandbox"]["status"] == "warn"
