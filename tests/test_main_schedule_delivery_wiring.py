from app.config import Settings
from app.main import build_application_services


def test_shared_schedule_delivery_resolver(tmp_path):
    services = build_application_services(Settings(
        provider_base_url="", provider_api_key="", provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"), workspace_root=str(tmp_path),
        skills_root=str(tmp_path / "skills"), plugins_root=str(tmp_path / "plugins"),
        scheduler_enabled=False, feishu_enabled=False, artifacts_enabled=False,
    ))
    resolver = services.schedule_delivery_service
    assert services.schedule_service.delivery_service is resolver
    assert services.scheduler_runner.run_service._delivery_service is resolver
    assert services.platform_service.delivery_service is resolver
    assert resolver.gateway_registry is services.gateway_registry
    assert resolver.schedule_registry is services.schedule_service.registry
    assert services.platform_service.gateway_registry is resolver.gateway_registry
    from app.domain.platform import Platform
    assert resolver.platform_status(Platform.FEISHU) != 'connected'
    _assert_missing_platform_denies_delivery(resolver, services.schedule_service.session_service)


def _assert_missing_platform_denies_delivery(resolver, session_service):
    import asyncio
    import pytest
    from app.application.schedule_delivery_service import ScheduleDeliveryError
    from app.domain.gateway import GatewaySessionKey
    from app.domain.platform import Platform

    async def check():
        key = GatewaySessionKey(Platform.FEISHU, 'unavailable-chat', '')
        await session_service.create_session('delivery-wiring-session')
        await resolver.gateway_registry.create_session_link(key, 'delivery-wiring-session')
        await resolver.gateway_registry.record_delivery_address(key, 'chat_id')
        rows = await resolver.gateway_registry.find_conversations(Platform.FEISHU, 'unavailable-chat')
        assert len(rows) == 1
        with pytest.raises(ScheduleDeliveryError, match='^platform_unavailable$'):
            await resolver.resolve_selection(rows[0].id)
    asyncio.run(check())


def test_dashboard_receives_shared_resolver_from_composition_root():
    import ast
    from pathlib import Path
    tree = ast.parse(Path("app/main.py").read_text())
    app_builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_app")
    calls = [node for node in ast.walk(app_builder) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "create_dashboard_router"]
    assert len(calls) == 1
    keyword = next(k for k in calls[0].keywords if k.arg == "schedule_delivery_service")
    assert ast.unparse(keyword.value) == "services.schedule_delivery_service"
