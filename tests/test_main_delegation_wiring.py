"""T14: main.py Delegation subsystem wiring + gating tests.

Covers:
  - ApplicationServices exposes delegation fields (default None).
  - delegation_enabled=False -> all delegation services None; delegate_agents
    not registered in tool definitions or composite routes.
  - delegation_enabled=True -> all components wired; delegate_agents present
    in composite routes and tool definitions.
  - Registry init failure -> fail-fast (propagates, NOT silent degrade).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.main import ApplicationServices, build_application_services


def _settings(tmp_path: Path, **updates) -> Settings:
    values = dict(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(tmp_path / "skills"),
        plugins_root=str(tmp_path / "plugins"),
        scheduler_enabled=False,
        feishu_enabled=False,
        sandbox_enabled=False,
        task_enabled=False,
        artifacts_enabled=False,
    )
    values.update(updates)
    return Settings(**values)


# ---------------------------------------------------------------------------
# ApplicationServices fields
# ---------------------------------------------------------------------------


def test_delegation_fields_exist():
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ApplicationServices)}
    assert "delegation_service" in fields
    assert "delegation_run_service" in fields
    assert "delegation_registry" in fields
    assert "delegation_tool_executor" in fields
    assert "child_agent_executor" in fields


# ---------------------------------------------------------------------------
# Disabled: subsystem skipped
# ---------------------------------------------------------------------------


def test_delegation_disabled_skips_subsystem(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.delegation_service is None
    assert services.delegation_run_service is None
    assert services.delegation_registry is None
    assert services.delegation_tool_executor is None
    assert services.child_agent_executor is None
    # delegate_agents is NOT registered.
    names = {d.name for d in services.tool_service.list_definitions()}
    assert "delegate_agents" not in names
    routes = getattr(services.tool_service.executor, "routes", {}) or {}
    assert "delegate_agents" not in routes


# ---------------------------------------------------------------------------
# Enabled: all components wired
# ---------------------------------------------------------------------------


def test_delegation_enabled_wires_all_components(tmp_path: Path):
    services = build_application_services(
        _settings(
            tmp_path,
            delegation_enabled=True,
            delegation_task_enabled=True,
            task_enabled=True,
        )
    )
    assert services.delegation_service is not None
    assert services.delegation_run_service is not None
    assert services.delegation_registry is not None
    assert services.delegation_tool_executor is not None
    assert services.child_agent_executor is not None
    # delegate_agents is registered in composite routes + definitions.
    routes = getattr(services.tool_service.executor, "routes", {}) or {}
    assert "delegate_agents" in routes
    names = {d.name for d in services.tool_service.list_definitions()}
    assert "delegate_agents" in names


def test_delegation_enabled_realtime_signs_capability(tmp_path: Path):
    """ChatCompletionService receives the realtime adapter + config when
    delegation is enabled."""
    services = build_application_services(
        _settings(tmp_path, delegation_enabled=True, delegation_realtime_enabled=True)
    )
    assert services.chat_service._delegation_adapter is not None
    assert services.chat_service._delegation_config is not None
    assert services.chat_service._delegation_config.enabled is True


# ---------------------------------------------------------------------------
# Fail-fast: registry init exception propagates
# ---------------------------------------------------------------------------


def test_delegation_enabled_initialize_failfast(tmp_path: Path, monkeypatch):
    from app.infrastructure.registry.sqlite_delegation_registry import (
        SQLiteDelegationRegistry,
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("registry init exploded")

    monkeypatch.setattr(SQLiteDelegationRegistry, "__init__", _boom)
    with pytest.raises(RuntimeError, match="registry init exploded"):
        build_application_services(
            _settings(tmp_path, delegation_enabled=True)
        )
