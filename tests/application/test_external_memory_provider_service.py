# tests/application/test_external_memory_provider_service.py
import pytest
from app.application.external_memory_provider_service import (
    ExternalMemoryProviderService, ActiveExternalMemoryReader,
)
from app.application.external_memory_manager import ExternalMemoryManager
from app.infrastructure.registry.sqlite_external_memory_provider_registry import (
    SQLiteExternalMemoryProviderRegistry,
)
from app.domain.external_memory_provider import (
    ExternalMemoryProviderType, ExternalMemoryProviderNotFoundError,
    ExternalMemoryProviderInUseError,
)
from app.infrastructure.memory.external.http_client import ExternalMemoryHttpClient
from app.infrastructure.memory.external.mem0 import Mem0Adapter
from app.infrastructure.memory.external.holographic import HolographicAdapter
from app.infrastructure.memory.external.honcho import HonchoAdapter


@pytest.fixture
def service(tmp_path):
    registry = SQLiteExternalMemoryProviderRegistry(tmp_path / "t.db")
    registry.create_tables()
    manager = ExternalMemoryManager()
    http = ExternalMemoryHttpClient()
    factories = {
        ExternalMemoryProviderType.MEM0: lambda cfg, secret: Mem0Adapter.factory(http_client=http, config=cfg, secret=secret),
        ExternalMemoryProviderType.HOLOGRAPHIC: lambda cfg, secret: HolographicAdapter.factory(config=cfg, secret=secret),
        ExternalMemoryProviderType.HONCHO: lambda cfg, secret: HonchoAdapter.factory(http_client=http, config=cfg, secret=secret),
    }
    return ExternalMemoryProviderService(
        registry=registry, manager=manager, factories=factories,
        workspace_root=tmp_path,
    )


def test_create_provider(service):
    cfg = service.create(
        name="my-mem0", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="https://app.mem0.ai/v1", api_key="sk-x",
        extra_config={"user_id": "u1"},
    )
    assert cfg.name == "my-mem0"
    assert cfg.api_key_present is True
    assert cfg.enabled is False


def test_activate_loads_adapter_into_manager(service, tmp_path):
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None, extra_config={"db_path": str(tmp_path / "h.db")},
    )
    pid = service.list()[0].id
    result = service.activate(pid)
    assert result.tool_surface_refresh_failed is False
    # manager external-query slot 已装载
    names = [p["name"] for p in service._manager.list_providers()]
    assert "holographic" in names
    # 工具面含 fact_store
    tool_names = [d.name for d in service._manager.get_tool_definitions()]
    assert "fact_store" in tool_names


def test_activate_deactivates_others(service, tmp_path):
    service.create(
        name="m1", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None, extra_config={"db_path": str(tmp_path / "h1.db")},
    )
    service.create(
        name="m2", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None, extra_config={"db_path": str(tmp_path / "h2.db")},
    )
    p1, p2 = service.list()
    service.activate(p1.id)
    service.activate(p2.id)
    # p1 已 deactivate
    assert service.get(p1.id).enabled is False
    assert service.get(p2.id).enabled is True
    # manager 中只有 holographic（p2）
    tool_names = [d.name for d in service._manager.get_tool_definitions()]
    assert "fact_store" in tool_names  # 仍有一个 holographic


def test_delete_active_swaps_none(service, tmp_path):
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None, extra_config={"db_path": str(tmp_path / "h.db")},
    )
    pid = service.list()[0].id
    service.activate(pid)
    service.delete(pid)
    tool_names = [d.name for d in service._manager.get_tool_definitions()]
    assert "fact_store" not in tool_names


def test_get_active_provider_names(service, tmp_path):
    assert service.get_active_provider_names() == []
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None, extra_config={"db_path": str(tmp_path / "h.db")},
    )
    pid = service.list()[0].id
    service.activate(pid)
    assert service.get_active_provider_names() == ["holographic"]


def test_probe_holographic(service, tmp_path):
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None, extra_config={"db_path": str(tmp_path / "probe.db")},
    )
    pid = service.list()[0].id
    status = service.probe(pid)
    assert status.value == "ok"


def test_probe_mem0_failure(service):
    # mem0 缺 api_key -> is_available() False -> FAILED
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="https://app.mem0.ai/v1", api_key=None, extra_config={},
    )
    pid = service.list()[0].id
    status = service.probe(pid)
    assert status.value == "failed"


def test_probe_honcho_failure(service):
    # honcho 缺 api_key -> is_available() False -> probe FAILED
    service.create(
        name="h", provider_type=ExternalMemoryProviderType.HONCHO,
        base_url="https://api.honcho.dev", api_key=None,
        extra_config={"workspace_id": "ws1", "user_id": "u1"},
    )
    pid = service.list()[0].id
    status = service.probe(pid)
    assert status.value == "failed"


def test_delete_missing_raises(service):
    with pytest.raises(ExternalMemoryProviderNotFoundError):
        service.delete("missing")


def _service_with_spy_factory(tmp_path, provider_type, spy):
    """构造 service，factory 捕获传入的 config dict 到 spy，返回 fake adapter。"""
    registry = SQLiteExternalMemoryProviderRegistry(tmp_path / "t.db")
    registry.create_tables()
    manager = ExternalMemoryManager()

    class FakeAdapter:
        def __init__(self, config):
            self._config = config
        def initialize(self, *args, **kwargs): pass
        def is_available(self): return True
        def handle_tool_call(self, *args, **kwargs):
            import json as _j
            return _j.dumps({"success": True, "results": []})
        def get_tool_schemas(self): return []
        @property
        def name(self): return "fake"

    factories = {provider_type: lambda cfg, secret: (spy.append(cfg), FakeAdapter(cfg))[1]}
    return ExternalMemoryProviderService(
        registry=registry, manager=manager, factories=factories,
        workspace_root=tmp_path,
    )


def test_activate_passes_base_url_to_factory(tmp_path):
    # P0：activate 构造 adapter 时必须把 cfg.base_url 合入 config，否则 Dashboard 配置被静默忽略
    spy = []
    service = _service_with_spy_factory(tmp_path, ExternalMemoryProviderType.MEM0, spy)
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="https://custom.mem0.ai/v3", api_key="sk-x",
        extra_config={"user_id": "u1"},
    )
    pid = service.list()[0].id
    service.activate(pid)
    assert spy, "factory 未被调用"
    assert spy[0].get("base_url") == "https://custom.mem0.ai/v3"
    assert spy[0].get("user_id") == "u1"  # extra_config 仍保留


def test_probe_passes_base_url_to_factory(tmp_path):
    # P0：probe 路径同样必须合入 base_url
    spy = []
    service = _service_with_spy_factory(tmp_path, ExternalMemoryProviderType.MEM0, spy)
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="https://custom.mem0.ai/v3", api_key="sk-x",
        extra_config={"user_id": "u1"},
    )
    pid = service.list()[0].id
    service.probe(pid)
    assert spy, "factory 未被调用"
    assert spy[0].get("base_url") == "https://custom.mem0.ai/v3"


def test_update_active_provider_reloads_adapter(service, tmp_path):
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None,
        extra_config={"db_path": str(tmp_path / "h.db"), "recall_mode": "hybrid"},
    )
    pid = service.list()[0].id
    service.activate(pid)
    cfg, refresh_failed = service.update(
        pid,
        extra_config={"db_path": str(tmp_path / "h.db"), "recall_mode": "context"},
    )
    assert cfg.extra_config["recall_mode"] == "context"
    assert refresh_failed is False
    tool_names = [d.name for d in service._manager.get_tool_definitions()]
    assert tool_names == []


def test_update_inactive_provider_does_not_swap(service, tmp_path):
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None,
        extra_config={"db_path": str(tmp_path / "h.db"), "recall_mode": "hybrid"},
    )
    pid = service.list()[0].id
    cfg, refresh_failed = service.update(
        pid,
        extra_config={"db_path": str(tmp_path / "h.db"), "recall_mode": "tools"},
    )
    assert cfg.extra_config["recall_mode"] == "tools"
    assert refresh_failed is None
    tool_names = [d.name for d in service._manager.get_tool_definitions()]
    assert tool_names == []


def test_update_active_provider_name_change_reloads(service, tmp_path):
    service.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None,
        extra_config={"db_path": str(tmp_path / "h.db"), "recall_mode": "hybrid"},
    )
    pid = service.list()[0].id
    service.activate(pid)
    cfg, refresh_failed = service.update(pid, name="m-renamed")
    assert cfg.name == "m-renamed"
    assert refresh_failed is False
    assert service._manager.get_active_external_query_provider_name() == "holographic"


def test_update_active_provider_initialize_failure_returns_refresh_failed(tmp_path):
    import json as _j
    registry = SQLiteExternalMemoryProviderRegistry(tmp_path / "t.db")
    registry.create_tables()
    manager = ExternalMemoryManager()

    class FailingAdapter:
        def __init__(self, config): self._config = config
        def initialize(self, *a, **kw): raise RuntimeError("init boom")
        def is_available(self): return True
        def handle_tool_call(self, *a, **kw): return _j.dumps({"success": True})
        def get_tool_schemas(self): return []
        @property
        def name(self): return "fake"

    class OkAdapter(FailingAdapter):
        def initialize(self, *a, **kw): pass

    factories = {ExternalMemoryProviderType.HOLOGRAPHIC: lambda cfg, secret: OkAdapter(cfg)}
    svc = ExternalMemoryProviderService(
        registry=registry, manager=manager, factories=factories, workspace_root=tmp_path,
    )
    svc.create(
        name="m", provider_type=ExternalMemoryProviderType.HOLOGRAPHIC,
        base_url="", api_key=None, extra_config={"db_path": str(tmp_path / "h.db")},
    )
    pid = svc.list()[0].id
    svc.activate(pid)
    svc._factories[ExternalMemoryProviderType.HOLOGRAPHIC] = lambda cfg, secret: FailingAdapter(cfg)
    cfg, refresh_failed = svc.update(pid, name="m2")
    assert refresh_failed is True
