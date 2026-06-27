from app.application.external_memory_service import ExternalMemoryService


class _StubManager:
    def __init__(self, providers):
        self._providers = providers

    def list_providers(self):
        return list(self._providers)


class _StubConfigRegistry:
    def get_enabled(self):
        return None

    def set_enabled(self, names):
        pass


def _make_service(manager_providers, catalog_entries, tmp_path):
    base_dir = tmp_path / "ext"
    base_dir.mkdir(parents=True, exist_ok=True)
    svc = ExternalMemoryService(
        external_memory_manager=_StubManager(manager_providers),
        config_registry=_StubConfigRegistry(),
        settings_default=None,
        base_dir=base_dir,
    )
    if catalog_entries is not None:
        svc.set_external_query_catalog(lambda: list(catalog_entries))
    return svc


def _cfg(name):
    return type("Cfg", (), {"name": name})()


def test_list_providers_merges_inactive_catalog_entries(tmp_path):
    manager_providers = [
        {"name": "builtin", "enabled_global": True, "slot": "builtin"},
        {"name": "holographic", "enabled_global": False, "slot": "external-query", "active": True},
    ]
    catalog = [_cfg("holographic"), _cfg("mem0")]
    svc = _make_service(manager_providers, catalog, tmp_path)
    items = {p["name"]: p for p in svc.list_providers()}
    assert items["holographic"]["active"] is True
    assert items["mem0"]["active"] is False
    assert items["mem0"]["slot"] == "external-query"
    assert items["mem0"]["enabled_global"] is False


def test_list_providers_without_catalog_backward_compatible(tmp_path):
    manager_providers = [
        {"name": "builtin", "enabled_global": True, "slot": "builtin"},
    ]
    svc = _make_service(manager_providers, None, tmp_path)
    names = [p["name"] for p in svc.list_providers()]
    assert names == ["builtin"]


def test_list_providers_catalog_empty(tmp_path):
    manager_providers = [
        {"name": "builtin", "enabled_global": True, "slot": "builtin"},
    ]
    svc = _make_service(manager_providers, [], tmp_path)
    names = [p["name"] for p in svc.list_providers()]
    assert names == ["builtin"]


def test_list_providers_catalog_exception_swallowed(tmp_path):
    manager_providers = [
        {"name": "builtin", "enabled_global": True, "slot": "builtin"},
    ]
    base_dir = tmp_path / "ext"
    base_dir.mkdir(parents=True, exist_ok=True)
    svc = ExternalMemoryService(
        external_memory_manager=_StubManager(manager_providers),
        config_registry=_StubConfigRegistry(),
        settings_default=None,
        base_dir=base_dir,
    )

    def boom():
        raise RuntimeError("registry unavailable")

    svc.set_external_query_catalog(boom)
    names = [p["name"] for p in svc.list_providers()]
    assert names == ["builtin"]
