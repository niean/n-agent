from __future__ import annotations

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


def test_skill_evolution_wired(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.skill_service is not None
    assert services.skill_usage_store is not None
    assert services.skill_pending_store is not None
    assert services.skill_backup_store is not None
    assert services.skill_evolution_service is not None
    names = [d.name for d in services.tool_service.list_definitions()]
    assert "skill_manage" in names


def test_skill_evolution_service_has_expected_config(tmp_path: Path):
    """SkillEvolutionService should be wired with settings-driven config."""
    services = build_application_services(_settings(tmp_path))
    evo = services.skill_evolution_service
    assert evo is not None
    assert evo.tool_service is services.tool_service
    assert evo.enabled is True  # default skills_background_review_enabled
    assert evo.nudge_interval == 10  # default skills_creation_nudge_interval


def test_skill_manage_route_registered(tmp_path: Path):
    """skill_manage tool route should be registered in the composite executor."""
    services = build_application_services(_settings(tmp_path))
    # The route is wired into CompositeToolExecutor's routes dict. Verify the
    # tool definition is exposed via tool_service.list_definitions().
    names = {d.name for d in services.tool_service.list_definitions()}
    assert "skill_manage" in names
    # Verify the definition is also in the builtin set (registered at startup)
    defn = services.tool_service.get_definition("skill_manage")
    assert defn is not None
    assert defn.toolset == "skills"


def test_build_filtered_definitions_filters_correctly(tmp_path: Path):
    """ToolService.build_filtered_definitions should filter by toolset and name."""
    services = build_application_services(_settings(tmp_path))
    filtered = services.tool_service.build_filtered_definitions(
        allow_toolsets={"skills"},
        allow_tool_names={"skill_manage", "skills_list", "skill_view"},
    )
    names = {d.name for d in filtered}
    assert names == {"skill_manage", "skills_list", "skill_view"}


def test_agent_graph_runner_has_evolution_service(tmp_path: Path):
    """AgentGraphRunner should have evolution_service and nudge_interval wired."""
    services = build_application_services(_settings(tmp_path))
    runner = services.chat_service.graph_runner
    assert runner.evolution_service is services.skill_evolution_service
    assert runner.nudge_interval == 10
