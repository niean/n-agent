"""Stub generator must expose callback tools so user code can call them bare.

Regression: previously user script did `import nagent_tools` but tools were
installed only into nagent_tools module globals, so `web_search(...)` in user
code raised NameError. Now user script does `from nagent_tools import *` and
stub declares `__all__` to limit exports to callback tool names only.
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

from app.infrastructure.sandbox.stub_generator import generate_stub


def _load_stub(tmp_path: Path, enabled: list[str]) -> object:
    src = generate_stub(enabled, rpc_socket_path="/tmp/fake.sock")
    stub_file = tmp_path / "nagent_tools.py"
    stub_file.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("nagent_tools_test", stub_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nagent_tools_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_stub_declares_all_with_only_tool_names(tmp_path: Path):
    src = generate_stub(["read_file", "web_search"], rpc_socket_path="/tmp/x.sock")
    assert "__all__ = ['read_file', 'web_search']" in src


def test_stub_install_creates_callable_proxies(tmp_path: Path):
    mod = _load_stub(tmp_path, ["read_file", "web_search"])
    assert hasattr(mod, "read_file")
    assert hasattr(mod, "web_search")
    assert callable(mod.read_file)
    assert callable(mod.web_search)


def test_stub_all_excludes_internals(tmp_path: Path):
    """`from nagent_tools import *` must not leak _rpc, _ToolProxy, Any, json, etc."""
    mod = _load_stub(tmp_path, ["read_file"])
    # __all__ controls star-import exports
    exported = set(getattr(mod, "__all__", []))
    assert exported == {"read_file"}
    # Internals should still exist as module attrs (just not star-exported)
    assert hasattr(mod, "_rpc")
    assert hasattr(mod, "_ToolProxy")


def test_user_script_star_import_gets_tools(tmp_path: Path):
    """End-to-end: user script `from nagent_tools import *` can bare-call tools."""
    stub_src = generate_stub(["web_search"], rpc_socket_path="/tmp/x.sock")
    (tmp_path / "nagent_tools.py").write_text(stub_src, encoding="utf-8")
    # User script that bare-calls web_search — should not raise NameError.
    # The call itself will fail (no socket), but NameError is what we're guarding.
    user_script = textwrap.dedent('''
        import sys
        sys.path.insert(0, %r)
        from nagent_tools import *
        # Reference web_search — if not imported, this raises NameError at compile
        assert callable(web_search), "web_search not imported"
    ''') % str(tmp_path)
    user_file = tmp_path / "user.py"
    user_file.write_text(user_script, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("user_test", user_file)
    mod = importlib.util.module_from_spec(spec)
    # Should execute without NameError
    spec.loader.exec_module(mod)
