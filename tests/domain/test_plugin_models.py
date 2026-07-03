from __future__ import annotations

from app.domain.plugin import (
    Plugin,
    PluginKind,
    PluginManifest,
    PluginSource,
    PluginValidationError,
)


def test_plugin_manifest_from_yaml_minimal():
    raw = {"name": "hello", "version": "1.0.0", "description": "demo"}
    m = PluginManifest.from_yaml(raw, source=PluginSource.BUNDLED, key="hello", path="/seeds/hello")
    assert m.key == "hello"
    assert m.kind is PluginKind.STANDALONE
    assert m.source is PluginSource.BUNDLED
    assert m.version == "1.0.0"


def test_plugin_manifest_category_key():
    raw = {"name": "exa", "version": "1.0.0", "description": "search"}
    m = PluginManifest.from_yaml(raw, source=PluginSource.USER, key="web/exa", path="/plugins/web/exa")
    assert m.key == "web/exa"


def test_plugin_manifest_invalid_kind():
    raw = {"name": "x", "version": "1.0.0", "description": "x", "kind": "tool"}
    try:
        PluginManifest.from_yaml(raw, source=PluginSource.USER, key="x", path="/x")
        raise AssertionError("expected PluginValidationError")
    except PluginValidationError:
        pass


def test_plugin_manifest_missing_name():
    try:
        PluginManifest.from_yaml({"version": "1.0.0"}, source=PluginSource.USER, key="x", path="/x")
        raise AssertionError("expected PluginValidationError")
    except PluginValidationError:
        pass


def test_plugin_manifest_missing_version():
    try:
        PluginManifest.from_yaml({"name": "x"}, source=PluginSource.USER, key="x", path="/x")
        raise AssertionError("expected PluginValidationError")
    except PluginValidationError:
        pass


def test_plugin_manifest_explicit_kind_backend():
    raw = {"name": "b1", "version": "1.0.0", "description": "b", "kind": "backend"}
    m = PluginManifest.from_yaml(raw, source=PluginSource.USER, key="b1", path="/b1")
    assert m.kind is PluginKind.BACKEND


def test_plugin_aggregate_secret_refs_redacted():
    plugin = Plugin(
        id="plg-1",
        key="hello",
        name="hello",
        source=PluginSource.BUNDLED,
        enabled=False,
        secret_refs={"api_key": "secret-value"},
    )
    view = plugin.to_public_view()
    assert view["secret_refs"]["api_key"] is True
    assert "secret-value" not in str(view)


def test_plugin_aggregate_public_view_no_secret_value():
    plugin = Plugin(
        id="plg-1",
        key="hello",
        name="hello",
        source=PluginSource.BUNDLED,
        config={"endpoint": "http://example.com"},
        secret_refs={"api_key": "super-secret"},
    )
    view = plugin.to_public_view()
    assert view["config"] == {"endpoint": "http://example.com"}
    assert view["secret_refs"] == {"api_key": True}
    assert "super-secret" not in str(view)


def test_plugin_with_secret_refs_returns_new_instance():
    plugin = Plugin(id="plg-1", key="hello", name="hello", source=PluginSource.BUNDLED)
    updated = plugin.with_secret_refs({"api_key": "v1"})
    assert updated.secret_refs == {"api_key": "v1"}
    assert plugin.secret_refs == {}


def test_plugin_to_public_detail_includes_manifest():
    plugin = Plugin(
        id="plg-1",
        key="hello",
        name="hello",
        source=PluginSource.BUNDLED,
        manifest={"name": "hello", "version": "1.0.0"},
    )
    detail = plugin.to_public_detail()
    assert detail["manifest"] == {"name": "hello", "version": "1.0.0"}
    assert "secret_refs" in detail
