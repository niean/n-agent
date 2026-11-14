from __future__ import annotations

import dataclasses
from pathlib import Path

from app.config import Settings
from app.main import ApplicationServices, build_application_services, create_app


def _settings(tmp_path: Path) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        plugins_root=str(plugins_root),
        scheduler_enabled=False,
        feishu_enabled=False,
        artifacts_enabled=False,
    )


def test_policy_dashboard_service_assembled(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.policy_dashboard_service is not None
    from app.application.policy_dashboard_service import PolicyDashboardService
    assert isinstance(services.policy_dashboard_service, PolicyDashboardService)


def test_service_provider_uses_same_settings_instance(tmp_path: Path):
    settings = _settings(tmp_path)
    services = build_application_services(settings)
    # The dashboard service must reuse the single Settings instance, not build a second one.
    assert services.policy_dashboard_service._provider._settings is settings


def test_field_is_non_default_and_before_optional_fields():
    fields = dataclasses.fields(ApplicationServices)
    names = [f.name for f in fields]
    pdi = names.index("policy_dashboard_service")
    usage = names.index("usage_service")
    assert pdi < usage
    assert dataclasses.fields(ApplicationServices)[pdi].default is dataclasses.MISSING


def _route_paths(app) -> list[str]:
    # Starlette 1.x wraps included routers in _IncludedRouter (path=None,
    # real routes on .original_router); walk those + Mounts recursively.
    paths: list[str] = []

    def walk(routes):
        for r in routes:
            p = getattr(r, "path", None)
            if p:
                paths.append(p)
            orig = getattr(r, "original_router", None)
            if orig is not None and hasattr(orig, "routes"):
                walk(orig.routes)
            sub = getattr(r, "routes", None)
            if isinstance(sub, list):
                walk(sub)

    walk(app.routes)
    return paths


def test_create_app_registers_security_and_policy_routes(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    paths = set(_route_paths(app))
    for path in ("/security", "/security/sessions", "/security/memory", "/security/sandbox"):
        assert path in paths
    assert "/chat/policies" in paths


def test_production_app_serves_policies(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        res = client.get("/chat/policies")
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store"
        data = res.json()
        assert len(data["policies"]) == 11
        assert client.get("/security").status_code == 200
        assert client.post("/chat/policies").status_code == 405


def test_composition_without_evaluation_keeps_stopper_scoping(tmp_path: Path):
    """T11 S4 实测：递归防护（N_AGENT_EVALUATION_ENABLED=false）让 CLI
    子进程以 evaluation_enabled=False 组装服务，但组合根在 browser/task
    接线处无条件引用 _scenario_active_stopper（仅 evaluation 分支赋值）
    -> UnboundLocalError，CLI 通道整体不可用。evaluation 关闭时组合
    必须完整可用（stopper 接线随 evaluation 子域一起关闭）。"""
    settings = _settings(tmp_path)
    settings.evaluation_enabled = False
    # browser/task 接线分支都在 browser_enabled 块内，需显式开启才触发
    settings.browser_enabled = True
    assert settings.task_enabled is True
    services = build_application_services(settings)
    assert services is not None


def test_composition_acp_runner_session_cwd_maps_container_root(tmp_path: Path):
    """T12 S4 实测：session/new 的 cwd 由 ScenarioAcpRunner 自持
    （runner 以自身 _cwd 调 driver.new_session(cwd)），把映射 cwd
    传给 SdkAcpDriver 构造参数是死参数 -> 容器内 session/new 以默认
    /workspace 发起，map_cwd 拒绝（"cannot map host cwd to
    container"）。组合根必须把 acp 容器根传给 ScenarioAcpRunner
    本体，未配置容器根时退回工作区根。"""
    settings = _settings(tmp_path)
    settings.acp_container_workspace_root = Path("/workspace-code")
    services = build_application_services(settings)
    target_runner = services.evaluation_execution_service.runner._target_runner
    assert target_runner._acp_factory is not None
    runner = target_runner._acp_factory()
    assert runner._cwd == "/workspace-code"


def test_composition_acp_runner_cwd_falls_back_to_workspace_root(tmp_path: Path):
    settings = _settings(tmp_path)
    assert settings.acp_container_workspace_root is None
    services = build_application_services(settings)
    runner = (
        services.evaluation_execution_service.runner._target_runner._acp_factory()
    )
    assert runner._cwd == str(tmp_path)


def test_evaluation_ledger_maintenance_loop_isolates_failures():
    """T13 S3（D110/D111）：账本生产调度循环——每轮 expire_retention +
    recover_stalled；单步异常隔离为 warning 继续下一轮，CancelledError
    正常上抛（lifespan 取消语义）。"""
    import asyncio

    from app import main as app_main

    class _FakeLedger:
        def __init__(self):
            self.calls = []

        async def expire_retention(self):
            self.calls.append("expire")

        async def recover_stalled(self):
            self.calls.append("recover")

    class _BrokenLedger(_FakeLedger):
        async def expire_retention(self):
            raise RuntimeError("boom")

    async def main():
        ledger = _FakeLedger()
        task = asyncio.create_task(
            app_main._evaluation_ledger_maintenance_loop(
                ledger, interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert "expire" in ledger.calls and "recover" in ledger.calls

        broken = _BrokenLedger()
        task = asyncio.create_task(
            app_main._evaluation_ledger_maintenance_loop(
                broken, interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert "recover" in broken.calls  # expire 失败未阻断恢复步

    asyncio.run(main())


def test_main_wires_evaluation_ledger_maintenance():
    """组合根接线：评估执行服务存在时启动账本维护任务，并纳入
    lifespan 取消清单。"""
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main)
    assert "_evaluation_ledger_maintenance_loop" in source
    assert (
        "services.evaluation_execution_service.resource_ledger" in source
    ), "维护循环必须以执行服务的场景资源账本为调度对象"
