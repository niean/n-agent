from __future__ import annotations

import pytest

from app.domain.plugin import (
    Plugin,
    PluginManifest,
    PluginSource,
    PluginValidationError,
)


def _manifest(raw: dict, *, key: str = "p", path: str = "/p") -> PluginManifest:
    return PluginManifest.from_yaml(
        raw, source=PluginSource.BUNDLED, key=key, path=path
    )


# --- defaults: missing / null -> empty list ---

def test_pip_dependencies_default_when_missing():
    m = _manifest({"name": "p", "version": "1.0.0"})
    assert m.pip_dependencies == []


def test_pip_dependencies_default_when_null():
    m = _manifest({"name": "p", "version": "1.0.0", "pip_dependencies": None})
    assert m.pip_dependencies == []


def test_external_dependencies_default_when_missing():
    m = _manifest({"name": "p", "version": "1.0.0"})
    assert m.external_dependencies == []


def test_external_dependencies_default_when_null():
    m = _manifest({"name": "p", "version": "1.0.0", "external_dependencies": None})
    assert m.external_dependencies == []


def test_requires_plugins_default_when_missing():
    m = _manifest({"name": "p", "version": "1.0.0"})
    assert m.requires_plugins == []


def test_requires_plugins_default_when_null():
    m = _manifest({"name": "p", "version": "1.0.0", "requires_plugins": None})
    assert m.requires_plugins == []


# --- legal values ---

def test_pip_dependencies_legal_values():
    raw = {"name": "p", "version": "1.0.0", "pip_dependencies": ["mem0ai>=2.0.7,<3", "httpx"]}
    m = _manifest(raw)
    assert m.pip_dependencies == ["mem0ai>=2.0.7,<3", "httpx"]


def test_external_dependencies_legal_values_full():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "external_dependencies": [
            {"name": "ffmpeg", "install": "brew install ffmpeg", "check": "ffmpeg -version"},
            {"name": "redis"},
        ],
    }
    m = _manifest(raw)
    assert m.external_dependencies == [
        {"name": "ffmpeg", "install": "brew install ffmpeg", "check": "ffmpeg -version"},
        {"name": "redis"},
    ]


def test_requires_plugins_legal_values():
    raw = {"name": "p", "version": "1.0.0", "requires_plugins": ["web/exa", "memory/mem0"]}
    m = _manifest(raw)
    assert m.requires_plugins == ["web/exa", "memory/mem0"]


def test_requires_plugins_case_sensitive_distinct():
    raw = {"name": "p", "version": "1.0.0", "requires_plugins": ["web/exa", "WEB/Exa"]}
    m = _manifest(raw)
    # case-sensitive: both are distinct keys, both preserved (no lowercasing)
    assert m.requires_plugins == ["web/exa", "WEB/Exa"]


# --- non-list raw values: scalar string must NOT be split into chars ---

def test_pip_dependencies_scalar_string_rejected_not_split():
    raw = {"name": "p", "version": "1.0.0", "pip_dependencies": "mem0ai"}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_requires_plugins_scalar_string_rejected_not_split():
    raw = {"name": "p", "version": "1.0.0", "requires_plugins": "web/exa"}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_scalar_string_rejected_not_split():
    raw = {"name": "p", "version": "1.0.0", "external_dependencies": "ffmpeg"}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_pip_dependencies_non_list_int_rejected():
    raw = {"name": "p", "version": "1.0.0", "pip_dependencies": 42}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_requires_plugins_non_list_dict_rejected():
    raw = {"name": "p", "version": "1.0.0", "requires_plugins": {"a": 1}}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_non_list_int_rejected():
    raw = {"name": "p", "version": "1.0.0", "external_dependencies": 42}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


# --- pip / requires_plugins: item validation ---

def test_pip_dependencies_non_string_item_rejected():
    raw = {"name": "p", "version": "1.0.0", "pip_dependencies": ["httpx", 42]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_pip_dependencies_whitespace_item_rejected():
    raw = {"name": "p", "version": "1.0.0", "pip_dependencies": ["httpx", "   "]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_pip_dependencies_duplicates_deduped_preserve_order():
    raw = {"name": "p", "version": "1.0.0", "pip_dependencies": ["httpx", "mem0ai", "httpx"]}
    m = _manifest(raw)
    assert m.pip_dependencies == ["httpx", "mem0ai"]


def test_pip_dependencies_items_stripped():
    raw = {"name": "p", "version": "1.0.0", "pip_dependencies": ["  httpx  "]}
    m = _manifest(raw)
    assert m.pip_dependencies == ["httpx"]


def test_requires_plugins_non_string_item_rejected():
    raw = {"name": "p", "version": "1.0.0", "requires_plugins": ["web/exa", 42]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_requires_plugins_whitespace_item_rejected():
    raw = {"name": "p", "version": "1.0.0", "requires_plugins": ["web/exa", "  "]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_requires_plugins_duplicates_deduped_preserve_order():
    raw = {"name": "p", "version": "1.0.0", "requires_plugins": ["web/exa", "memory/mem0", "web/exa"]}
    m = _manifest(raw)
    assert m.requires_plugins == ["web/exa", "memory/mem0"]


# --- external_dependencies: item validation ---

def test_external_dependencies_non_mapping_string_item_rejected():
    raw = {"name": "p", "version": "1.0.0", "external_dependencies": ["ffmpeg"]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_non_mapping_int_item_rejected():
    raw = {"name": "p", "version": "1.0.0", "external_dependencies": [42]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_missing_name_rejected():
    raw = {"name": "p", "version": "1.0.0", "external_dependencies": [{"install": "x"}]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_empty_name_rejected():
    raw = {"name": "p", "version": "1.0.0", "external_dependencies": [{"name": "   "}]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_non_string_name_rejected():
    raw = {"name": "p", "version": "1.0.0", "external_dependencies": [{"name": 42}]}
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_non_string_install_rejected():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "external_dependencies": [{"name": "ffmpeg", "install": 42}],
    }
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_non_string_check_rejected():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "external_dependencies": [{"name": "ffmpeg", "check": 42}],
    }
    with pytest.raises(PluginValidationError):
        _manifest(raw)


def test_external_dependencies_install_null_treated_as_absent():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "external_dependencies": [{"name": "ffmpeg", "install": None}],
    }
    m = _manifest(raw)
    assert m.external_dependencies == [{"name": "ffmpeg"}]


def test_external_dependencies_check_null_treated_as_absent():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "external_dependencies": [{"name": "ffmpeg", "check": None}],
    }
    m = _manifest(raw)
    assert m.external_dependencies == [{"name": "ffmpeg"}]


# --- unknown external fields: dropped from public projection, preserved in raw ---

def test_external_dependencies_unknown_keys_dropped_from_public_field():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "external_dependencies": [
            {"name": "ffmpeg", "install": "x", "custom": "keep-in-raw"}
        ],
    }
    m = _manifest(raw)
    assert m.external_dependencies == [{"name": "ffmpeg", "install": "x"}]
    # unknown key preserved in raw manifest
    assert m.raw["external_dependencies"][0]["custom"] == "keep-in-raw"


def test_raw_manifest_preserves_unknown_top_level_fields():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "pip_dependencies": ["httpx"],
        "unknown_top": 123,
    }
    m = _manifest(raw)
    assert m.raw["unknown_top"] == 123
    assert m.raw["pip_dependencies"] == ["httpx"]


# --- to_public_detail preserves raw manifest (with new fields) ---

def test_to_public_detail_preserves_new_dependency_fields_in_manifest():
    raw = {
        "name": "p",
        "version": "1.0.0",
        "pip_dependencies": ["httpx"],
        "external_dependencies": [
            {"name": "ffmpeg", "install": "x", "custom": "y"}
        ],
        "requires_plugins": ["web/exa"],
    }
    m = _manifest(raw)
    plugin = Plugin(
        id="plg-1",
        key="p",
        name="p",
        source=PluginSource.BUNDLED,
        manifest=m.raw,
    )
    detail = plugin.to_public_detail()
    manifest = detail["manifest"]
    assert manifest["pip_dependencies"] == ["httpx"]
    assert manifest["requires_plugins"] == ["web/exa"]
    # raw manifest preserves unknown keys in external items
    assert manifest["external_dependencies"][0]["custom"] == "y"
