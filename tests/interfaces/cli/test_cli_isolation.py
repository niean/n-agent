from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.interfaces.cli.commands import (
    config,
    doctor,
    knowledge,
    logs,
    mcp,
    memory,
    platform,
    provider,
    sandbox,
    schedule,
)


class _FakeProviderService:
    async def list_providers(self):
        return []


class _FakeKnowledgeService:
    async def list_bases(self):
        return []


class _FakeMcpService:
    async def list_sites(self):
        return []


class _FakeScheduleService:
    async def list(self):
        return []


class _FakePlatformService:
    async def list_platforms(self, include_local: bool = False):
        return []


class _FakeSandboxService:
    async def list_active_sandboxes(self):
        return []


class _FakeSessionService:
    async def list_tool_calls(self, session_id):
        return []

    async def get_session_detail(self, session_id):
        return {"session_id": session_id, "task_state": {}, "messages": []}


class _FakeScheduleExecService:
    async def list_executions(self, task_id, limit=10):
        return []


class _FakeExternalMemoryProviderService:
    def list(self):
        return []


class _FakeExternalMemoryService:
    def list_providers(self):
        return []


class _FakeSettings:
    provider_base_url = "http://x"
    provider_model = "m"
    provider_api_key = "sk-x"
    sqlite_path = "/tmp/x.db"
    workspace_root = "/tmp"


class _FakeApplicationServices:
    settings = _FakeSettings()
    session_service = _FakeSessionService()
    provider_service = _FakeProviderService()
    knowledge_service = _FakeKnowledgeService()
    mcp_service = _FakeMcpService()
    schedule_service = _FakeScheduleService()
    platform_service = _FakePlatformService()
    skill_service = _FakeSessionService()
    plugin_service = _FakeSessionService()
    sandbox_dashboard_service = None
    external_memory_provider_service = None
    external_memory_service = None


def test_provider_isolation(monkeypatch):
    monkeypatch.setattr(provider, "_load_provider_service", lambda: _FakeProviderService())
    args = SimpleNamespace(provider_command="list", json=True)
    assert provider.run(args) == 0


def test_knowledge_isolation(monkeypatch):
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: _FakeKnowledgeService())
    args = SimpleNamespace(knowledge_command="list", json=True)
    assert knowledge.run(args) == 0


def test_mcp_isolation(monkeypatch):
    monkeypatch.setattr(mcp, "_load_mcp_service", lambda: _FakeMcpService())
    args = SimpleNamespace(mcp_command="list", json=True)
    assert mcp.run(args) == 0


def test_schedule_isolation(monkeypatch):
    monkeypatch.setattr(schedule, "_load_schedule_service", lambda: _FakeScheduleService())
    args = SimpleNamespace(schedule_command="list", json=True)
    assert schedule.run(args) == 0


def test_sandbox_isolation_disabled(monkeypatch):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: None)
    args = SimpleNamespace(sandbox_command="list-active", json=True)
    assert sandbox.run(args) == 0


def test_sandbox_isolation_with_service(monkeypatch):
    monkeypatch.setattr(sandbox, "_load_sandbox_service", lambda: _FakeSandboxService())
    args = SimpleNamespace(sandbox_command="list-active", json=True)
    assert sandbox.run(args) == 0


def test_platform_isolation(monkeypatch):
    monkeypatch.setattr(platform, "_load_platform_service", lambda: _FakePlatformService())
    args = SimpleNamespace(platform_command="list", include_local=False, json=True)
    assert platform.run(args) == 0


def test_memory_provider_isolation_sync(monkeypatch):
    monkeypatch.setattr(memory, "_load_provider_service", lambda: _FakeExternalMemoryProviderService())
    args = SimpleNamespace(memory_command="list-providers", json=True)
    assert memory.run(args) == 0


def test_memory_projects_isolation_sync(monkeypatch):
    monkeypatch.setattr(memory, "_load_memory_service", lambda: _FakeExternalMemoryService())
    args = SimpleNamespace(memory_command="list-projects", json=True)
    assert memory.run(args) == 0


def test_doctor_isolation(monkeypatch):
    monkeypatch.setattr(doctor, "_load_services", lambda: _FakeApplicationServices())
    args = SimpleNamespace(probe=False)
    assert doctor.run(args) in (0, 1)


def test_config_isolation(monkeypatch):
    monkeypatch.setattr(config, "_load_settings", lambda: _FakeSettings())
    args = SimpleNamespace(json=True, section=None)
    assert config.run(args) == 0


def test_logs_sandbox_isolation(monkeypatch):
    monkeypatch.setattr(logs, "_load_sandbox_service", lambda: None)
    args = SimpleNamespace(logs_command="sandbox", session_id=None, limit=None, json=True)
    assert logs.run(args) == 0


def test_logs_tools_isolation(monkeypatch):
    monkeypatch.setattr(logs, "_load_session_service", lambda: _FakeSessionService())
    args = SimpleNamespace(logs_command="tools", session_id="s1", limit=None, json=True)
    assert logs.run(args) == 0


def test_logs_scheduled_isolation(monkeypatch):
    monkeypatch.setattr(logs, "_load_schedule_service", lambda: _FakeScheduleExecService())
    args = SimpleNamespace(logs_command="scheduled", task_id="t1", limit=10, json=True)
    assert logs.run(args) == 0


def test_logs_runs_isolation(monkeypatch):
    monkeypatch.setattr(logs, "_load_session_service", lambda: _FakeSessionService())
    args = SimpleNamespace(logs_command="runs", session_id="s1", json=True)
    assert logs.run(args) == 0


def test_memory_sync_service_not_awaited():
    from app.application.external_memory_provider_service import ExternalMemoryProviderService
    from app.application.external_memory_service import ExternalMemoryService

    for name in ("list", "get", "create", "update", "delete", "activate", "deactivate", "probe"):
        method = getattr(ExternalMemoryProviderService, name, None)
        if method:
            assert not inspect.iscoroutinefunction(method), f"{name} must be sync"
    for name in (
        "list_providers",
        "save_global_enabled",
        "create_project",
        "delete_project",
        "get_external_memory",
        "list_project_entries",
        "add_project_entry",
        "update_project_entry",
        "delete_project_entry",
    ):
        method = getattr(ExternalMemoryService, name, None)
        if method:
            assert not inspect.iscoroutinefunction(method), f"{name} must be sync"


def test_cli_commands_no_infrastructure_import():
    import ast
    from pathlib import Path

    cli_dir = Path(__file__).resolve().parents[2] / "app" / "interfaces" / "cli"
    forbidden = ("app.infrastructure", "sqlite3")
    for path in cli_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), f"{path.name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden), f"{path.name} imports {node.module}"


def test_acp_dispatch_registered():
    from app.interfaces.cli.main import _DISPATCH, build_parser

    parser = build_parser()
    args = parser.parse_args(["acp", "--check"])
    assert args.command == "acp"
    assert args.check is True
    assert args.setup is False
    args = parser.parse_args(["acp", "--setup"])
    assert args.setup is True
    assert "acp" in _DISPATCH
    assert callable(_DISPATCH["acp"])


def test_acp_check_returns_zero():
    from app.interfaces.cli.commands.acp import command

    args = SimpleNamespace(check=True, setup=False)
    assert command.run(args) == 0


def test_acp_setup_returns_zero(capsys):
    from app.interfaces.cli.commands.acp import command

    args = SimpleNamespace(check=False, setup=True)
    assert command.run(args) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ACP provider setup" in captured.err

