from pathlib import Path

import pytest

from app.config import Settings
from app.domain.tool import ToolSourceType
from app.main import build_application_services


def _settings(tmp_path: Path, **updates):
    values = dict(
        provider_base_url="", provider_api_key="", provider_model="",
        sqlite_path=tmp_path / "sessions.db", workspace_root=tmp_path / "workspace",
        skills_root=tmp_path / "workspace" / "skills", plugins_root=tmp_path / "plugins",
        scheduler_enabled=False, feishu_enabled=False, sandbox_enabled=False,
        artifacts_enabled=False,
    )
    values.update(updates)
    return Settings(**values)


def _write_authority_files(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text("""schema_version: 1
version: v1
limits:
  default_timeout_seconds: 5
  max_timeout_seconds: 5
  max_stdout_bytes: 1024
  max_stderr_bytes: 1024
  max_args: 1
  max_arg_length: 32
  max_total_args_length: 32
  max_concurrency: 1
targets:
  - type: command
    rule_id: echo
    executable: /bin/echo
    args: [{exact: hello}]
""")
    policy.chmod(0o600)
    token = tmp_path / "token"
    token.write_text("x" * 32)
    token.chmod(0o600)
    return policy, token


def _host_mapping(tmp_path: Path) -> dict[str, Path]:
    host_workspace = tmp_path / "host-workspace"
    return {
        "host_terminal_host_workspace_root": host_workspace,
        "host_terminal_host_skills_root": host_workspace / "skills",
    }


@pytest.mark.asyncio
async def test_default_disabled_has_no_definition_or_route(tmp_path):
    services = build_application_services(_settings(tmp_path))
    assert services.tool_service.get_definition("host_terminal") is None
    assert "host_terminal" not in services.tool_service.executor.routes
    assert services.health_snapshot()["host_terminal"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_valid_authority_registers_definition_and_route_without_bridge_probe(tmp_path):
    policy, token = _write_authority_files(tmp_path)
    services = build_application_services(_settings(
        tmp_path, host_terminal_enabled=True,
        host_terminal_bridge_url="http://host.docker.internal:8765",
        host_terminal_policy_path=policy, host_terminal_token_path=token,
        **_host_mapping(tmp_path),
    ))
    definition = services.tool_service.get_definition("host_terminal")
    assert definition is not None and definition.source_type is ToolSourceType.AGENT
    assert "host_terminal" in services.tool_service.executor.routes
    health = services.health_snapshot()["host_terminal"]
    assert health == {"status": "degraded", "reason": "host_bridge_not_checked", "enabled": True}
    # photo-upload 图片持久化已装配：executor 注入 image_persister，Dashboard 暴露 image_store
    from app.infrastructure.image_store import LocalImageStore
    assert isinstance(services.image_store, LocalImageStore)
    executor = services.tool_service.executor.routes["host_terminal"]
    assert executor._image_persister is services.image_store


@pytest.mark.asyncio
async def test_invalid_policy_does_not_register_and_health_has_no_paths(tmp_path):
    policy, token = _write_authority_files(tmp_path)
    policy.write_text("broken: [")
    services = build_application_services(_settings(
        tmp_path, host_terminal_enabled=True,
        host_terminal_bridge_url="http://host.docker.internal:8765",
        host_terminal_policy_path=policy, host_terminal_token_path=token,
        **_host_mapping(tmp_path),
    ))
    assert services.tool_service.get_definition("host_terminal") is None
    health = services.health_snapshot()["host_terminal"]
    assert health["reason"] == "host_policy_yaml_invalid"
    assert str(tmp_path) not in str(health)


@pytest.mark.asyncio
async def test_dashboard_service_injected_in_all_three_states(tmp_path):
    # disabled
    services = build_application_services(_settings(tmp_path))
    assert services.host_terminal_dashboard_service is not None
    status = await services.host_terminal_dashboard_service.get_status()
    assert status["enabled"] is False
    assert status["health_code"] == "host_terminal_disabled"

    # valid authority -> enabled, bridge not yet checked
    policy, token = _write_authority_files(tmp_path)
    services = build_application_services(_settings(
        tmp_path, host_terminal_enabled=True,
        host_terminal_bridge_url="http://host.docker.internal:8765",
        host_terminal_policy_path=policy, host_terminal_token_path=token,
        **_host_mapping(tmp_path),
    ))
    assert services.host_terminal_dashboard_service is not None
    status = await services.host_terminal_dashboard_service.get_status()
    assert status["enabled"] is True
    assert status["health_code"] == "host_bridge_not_checked"

    # invalid policy yaml -> not enabled, stable reason; loader not retained
    policy.write_text("broken: [")
    services = build_application_services(_settings(
        tmp_path, host_terminal_enabled=True,
        host_terminal_bridge_url="http://host.docker.internal:8765",
        host_terminal_policy_path=policy, host_terminal_token_path=token,
        **_host_mapping(tmp_path),
    ))
    assert services.host_terminal_dashboard_service is not None
    status = await services.host_terminal_dashboard_service.get_status()
    assert status["enabled"] is False
    assert status["health_code"] == "host_policy_yaml_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("root_name", ["workspace_root", "skills_root", "sandbox_scratch_root"])
@pytest.mark.parametrize("authority_name", ["host_terminal_policy_path", "host_terminal_token_path"])
async def test_authority_must_not_overlap_any_container_model_writable_root(
    tmp_path: Path, root_name: str, authority_name: str
):
    policy, token = _write_authority_files(tmp_path)
    authority = policy if authority_name.endswith("policy_path") else token
    values = {
        "host_terminal_enabled": True,
        "host_terminal_bridge_url": "http://host.docker.internal:8765",
        "host_terminal_policy_path": policy,
        "host_terminal_token_path": token,
        **_host_mapping(tmp_path),
        root_name: authority.parent,
    }
    services = build_application_services(_settings(tmp_path, **values))
    assert services.tool_service.get_definition("host_terminal") is None
    assert services.health_snapshot()["host_terminal"]["reason"] == "host_terminal_authority_path_unsafe"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host_workspace", "host_skills"),
    [
        (Path("relative-workspace"), Path("relative-workspace/skills")),
        (Path("/deployment/workspace"), Path("/arbitrary/skills")),
        (Path("/deployment/workspace/../workspace"), Path("/deployment/workspace/skills")),
        (Path("/deployment/workspace"), Path("/deployment/workspace/skills/../skills")),
    ],
)
async def test_host_skill_mapping_descriptor_rejects_non_absolute_or_mismatched_roots(
    tmp_path: Path, host_workspace: Path, host_skills: Path
):
    policy, token = _write_authority_files(tmp_path)
    services = build_application_services(_settings(
        tmp_path,
        host_terminal_enabled=True,
        host_terminal_bridge_url="http://host.docker.internal:8765",
        host_terminal_policy_path=policy,
        host_terminal_token_path=token,
        host_terminal_host_workspace_root=host_workspace,
        host_terminal_host_skills_root=host_skills,
    ))

    assert services.tool_service.get_definition("host_terminal") is None
    health = services.health_snapshot()["host_terminal"]
    assert health["reason"] == "host_terminal_host_mapping_invalid"
    assert str(host_workspace) not in str(health)
    assert str(host_skills) not in str(health)


@pytest.mark.asyncio
async def test_current_deployment_host_skill_mapping_descriptor_is_accepted(tmp_path):
    policy, token = _write_authority_files(tmp_path)
    host_workspace = Path("/Users/niean/install/n-agent/workspace")
    services = build_application_services(_settings(
        tmp_path,
        host_terminal_enabled=True,
        host_terminal_bridge_url="http://host.docker.internal:8765",
        host_terminal_policy_path=policy,
        host_terminal_token_path=token,
        host_terminal_host_workspace_root=host_workspace,
        host_terminal_host_skills_root=host_workspace / "skills",
    ))

    assert services.tool_service.get_definition("host_terminal") is not None


@pytest.mark.asyncio
async def test_host_mapping_descriptor_must_not_overlap_authority_paths(tmp_path):
    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    policy, token = _write_authority_files(authority_dir)
    services = build_application_services(_settings(
        tmp_path,
        host_terminal_enabled=True,
        host_terminal_bridge_url="http://host.docker.internal:8765",
        host_terminal_policy_path=policy,
        host_terminal_token_path=token,
        host_terminal_host_workspace_root=authority_dir,
        host_terminal_host_skills_root=authority_dir / "skills",
    ))

    assert services.tool_service.get_definition("host_terminal") is None
    assert (
        services.health_snapshot()["host_terminal"]["reason"]
        == "host_terminal_host_mapping_invalid"
    )
