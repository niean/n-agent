import asyncio
from pathlib import Path

from app.config import Settings
from app.main import build_application_services


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(tmp_path / "skills"),
        scheduler_enabled=False,
        feishu_enabled=False,
    )


def test_build_application_services_wires_vision_analyze_tool(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))

    definition = services.tool_service.get_definition("vision_analyze")
    assert definition is not None
    assert definition.toolset == "vision"


def test_build_application_services_vision_capability_follows_provider_config(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))

    runner = services.chat_service.graph_runner
    assert runner.vision_capability is not None

    config = services.provider_holder.current_config
    if config is None:
        assert runner.vision_capability() is False
    else:
        assert runner.vision_capability() is bool(config.supports_vision)


def test_build_application_services_routes_vision_analyze_to_vision_executor(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))

    from app.domain.tool import ToolCallRequest

    result = asyncio.run(
        services.tool_service.execute(
            ToolCallRequest(
                id="wiring-test",
                name="vision_analyze",
                arguments={"image_url": "not-a-url", "question": "?"},
            )
        )
    )

    from app.domain.tool import ToolResultStatus

    assert result.status is ToolResultStatus.ERROR
    assert result.content.get("error") == "invalid_image_url"
