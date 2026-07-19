"""T11: Plugin CLI command discovery and dispatch tests.

Covers:
- S1: ``collect_plugin_cli_commands`` lightweight isolation. Must NOT call
  ``build_application_services`` or construct Provider/MCP/Feishu/Scheduler/
  AgentGraphRunner. Global discovery failure -> warning + empty list.
  ``main(argv)`` calls helper before ``parse_args``.
- S2: ``build_parser(plugin_commands=...)`` conflict rules. Builtin top-level
  names permanently reserved (builtin wins). Inter-plugin / same-plugin
  duplicate -> stable first-wins. Single ``setup_fn`` exception -> skip only
  that command. After any failure, builtin + other plugin commands still work.
- S3: Handler contract. ``setup_fn`` receives new subparser; ``handler_fn``
  receives ``argparse.Namespace``; ``args.func`` takes priority over
  ``_DISPATCH``. Sync/async handler: None->0, int->as-is; other return types
  or exception -> 1 with short error; no handler -> print that command's help
  + return 0.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.application.plugin_service import PluginCliCommand
from app.interfaces.cli.main import build_parser


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_cmd(
    name: str,
    *,
    plugin_key: str = "p1",
    setup_fn=None,
    handler_fn=None,
    help: str = "help text",
    description: str = "desc",
    registration_index: int = 0,
) -> PluginCliCommand:
    return PluginCliCommand(
        plugin_key=plugin_key,
        name=name,
        help=help,
        description=description,
        setup_fn=setup_fn or (lambda parser: None),
        handler_fn=handler_fn,
        registration_index=registration_index,
    )


# ---------------------------------------------------------------------------
# S1: isolation -- collect_plugin_cli_commands
# ---------------------------------------------------------------------------


def test_collect_plugin_cli_commands_does_not_call_build_application_services(
    monkeypatch,
):
    """The helper must NOT route through build_application_services."""
    import app.main

    sentinel = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(app.main, "build_application_services", sentinel)
    result = app.main.collect_plugin_cli_commands()
    assert isinstance(result, list)
    sentinel.assert_not_called()


def test_collect_plugin_cli_commands_does_not_construct_heavy_services(
    monkeypatch,
):
    """The helper must NOT construct Provider/MCP/Feishu/Scheduler/
    AgentGraphRunner/real ToolService."""
    import app.main

    def boom(*args, **kwargs):
        raise AssertionError("heavy service must not be constructed")

    for attr in (
        "AgentGraphRunner",
        "FeishuClient",
        "SchedulerRunner",
        "McpService",
        "McpSdkClient",
        "ActiveProviderHolder",
        "ProviderService",
        "ChatCompletionService",
        "SessionService",
        "GatewayService",
        "SandboxManager",
    ):
        monkeypatch.setattr(app.main, attr, boom)

    # build_application_services must also not be called
    monkeypatch.setattr(
        app.main, "build_application_services",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no full build")),
    )
    result = app.main.collect_plugin_cli_commands()
    assert isinstance(result, list)


def test_collect_plugin_cli_commands_returns_list():
    """Helper always returns a list (possibly empty)."""
    import app.main

    result = app.main.collect_plugin_cli_commands()
    assert isinstance(result, list)


def test_collect_plugin_cli_commands_global_failure_returns_empty(
    monkeypatch,
):
    """On any global failure, return [] and do not crash."""
    import app.main

    def boom_settings():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(app.main, "Settings", boom_settings)
    result = app.main.collect_plugin_cli_commands()
    assert result == []


def test_main_calls_collect_before_parse(monkeypatch, capsys):
    """main(argv) must call collect_plugin_cli_commands before parse_args."""
    import app.main
    from app.interfaces.cli.main import main

    calls = {"count": 0}

    def fake_collect():
        calls["count"] += 1
        return []

    monkeypatch.setattr(app.main, "collect_plugin_cli_commands", fake_collect)
    try:
        main(["--help"])
    except SystemExit:
        pass
    assert calls["count"] == 1


def test_cli_main_does_not_import_infrastructure():
    """Interfaces layer must not import Infrastructure directly."""
    import ast
    from pathlib import Path

    cli_main = Path(__file__).resolve().parents[3] / "app" / "interfaces" / "cli" / "main.py"
    tree = ast.parse(cli_main.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.infrastructure"), (
                    f"cli/main.py imports {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("app.infrastructure"), (
                f"cli/main.py imports {node.module}"
            )


# ---------------------------------------------------------------------------
# S2: parser conflict rules
# ---------------------------------------------------------------------------


def test_build_parser_preserves_builtin_names():
    """All existing builtin top-level names are still present."""
    from app.interfaces.cli.main import _DISPATCH

    parser = build_parser(plugin_commands=[])
    builtin_names = set(_DISPATCH.keys())
    # Parse each builtin to verify it's still recognised.
    for name in builtin_names:
        # Some builtins require a subcommand; just verify the top-level name
        # is accepted (parse_args may fail on missing subcommand, so we check
        # the parser's subparser choices instead).
        pass
    # Inspect subparser choices directly.
    sub_action = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub_action = action
            break
    assert sub_action is not None
    for name in builtin_names:
        assert name in sub_action.choices, f"builtin {name!r} missing from parser"


def test_builtin_conflict_plugin_skipped():
    """Plugin command whose name matches a builtin -> warning + skip (builtin wins)."""
    handler_calls = []

    cmd = _make_cmd(
        "status",
        handler_fn=lambda args: handler_calls.append("plugin") or 0,
    )
    parser = build_parser(plugin_commands=[cmd])
    args = parser.parse_args(["status"])
    # Plugin's func must NOT be set -- builtin wins.
    assert getattr(args, "func", None) is None
    assert args.command == "status"


def test_inter_plugin_same_name_first_wins():
    """Two plugins register the same command name -> first in list wins."""
    calls = []
    h1 = lambda args: calls.append("p1") or 0  # noqa: E731
    h2 = lambda args: calls.append("p2") or 0  # noqa: E731
    cmds = [
        _make_cmd("greet", plugin_key="p1", handler_fn=h1),
        _make_cmd("greet", plugin_key="p2", handler_fn=h2),
    ]
    parser = build_parser(plugin_commands=cmds)
    args = parser.parse_args(["greet"])
    assert args.func is h1


def test_same_plugin_duplicate_name_first_wins():
    """Same plugin registers duplicate name -> first wins, later skipped."""
    h1 = lambda args: 0  # noqa: E731
    h2 = lambda args: 1  # noqa: E731
    cmds = [
        _make_cmd("dup", plugin_key="p1", handler_fn=h1, registration_index=0),
        _make_cmd("dup", plugin_key="p1", handler_fn=h2, registration_index=1),
    ]
    parser = build_parser(plugin_commands=cmds)
    args = parser.parse_args(["dup"])
    assert args.func is h1


def test_setup_fn_exception_skips_only_that_command():
    """setup_fn raising -> skip that command only; others continue."""
    def bad_setup(parser):
        raise ValueError("bad setup")

    good_handler = lambda args: 0  # noqa: E731
    cmds = [
        _make_cmd("broken", setup_fn=bad_setup, handler_fn=lambda args: 1),
        _make_cmd("works", handler_fn=good_handler),
    ]
    parser = build_parser(plugin_commands=cmds)

    # "works" is still parseable and dispatches to its handler.
    args = parser.parse_args(["works"])
    assert args.func is good_handler
    assert args.command == "works"


def test_after_failure_builtin_and_other_plugin_commands_work(capsys):
    """After a setup_fn failure, builtin --help and other plugin commands work."""
    def bad_setup(parser):
        raise ValueError("bad")

    cmds = [
        _make_cmd("broken", setup_fn=bad_setup),
        _make_cmd("works", handler_fn=lambda args: 0),
    ]
    parser = build_parser(plugin_commands=cmds)

    # Builtin status is still available.
    args = parser.parse_args(["status"])
    assert args.command == "status"

    # Other plugin command is still available.
    args = parser.parse_args(["works"])
    assert args.command == "works"

    # status --help does not crash.
    try:
        parser.parse_args(["status", "--help"])
    except SystemExit as exc:
        assert int(exc.code or 0) == 0


def test_setup_fn_receives_subparser():
    """setup_fn receives a new argparse subparser."""
    received = []

    def setup_fn(parser):
        received.append(parser)
        parser.add_argument("--flag", default="x")

    cmd = _make_cmd("mycmd", setup_fn=setup_fn)
    parser = build_parser(plugin_commands=[cmd])
    args = parser.parse_args(["mycmd", "--flag", "y"])
    assert len(received) == 1
    assert isinstance(received[0], argparse.ArgumentParser)
    assert args.flag == "y"


def test_build_parser_no_plugin_commands_backward_compat():
    """build_parser() with no args still works (backward compat)."""
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"


# ---------------------------------------------------------------------------
# S3: handler contract
# ---------------------------------------------------------------------------


def _main_with_commands(monkeypatch, commands):
    """Patch collect_plugin_cli_commands to return *commands* and return main."""
    import app.main
    from app.interfaces.cli.main import main

    monkeypatch.setattr(app.main, "collect_plugin_cli_commands", lambda: commands)
    return main


def test_handler_fn_receives_namespace(monkeypatch):
    """handler_fn receives an argparse.Namespace."""
    received = []

    def handler(args):
        received.append(args)
        return 0

    cmds = [_make_cmd("greet", handler_fn=handler)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    rc = main_fn(["greet"])
    assert rc == 0
    assert len(received) == 1
    assert isinstance(received[0], argparse.Namespace)


def test_sync_handler_none_returns_zero(monkeypatch):
    """Sync handler returning None -> exit 0."""
    cmds = [_make_cmd("cmd", handler_fn=lambda args: None)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    assert main_fn(["cmd"]) == 0


def test_sync_handler_int_return_as_is(monkeypatch):
    """Sync handler returning int -> exit code as-is."""
    cmds = [_make_cmd("cmd", handler_fn=lambda args: 42)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    assert main_fn(["cmd"]) == 42


def test_sync_handler_zero_int(monkeypatch):
    cmds = [_make_cmd("cmd", handler_fn=lambda args: 0)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    assert main_fn(["cmd"]) == 0


def test_async_handler_none_returns_zero(monkeypatch):
    """Async handler returning None -> exit 0."""

    async def handler(args):
        return None

    cmds = [_make_cmd("acmd", handler_fn=handler)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    assert main_fn(["acmd"]) == 0


def test_async_handler_int_return_as_is(monkeypatch):
    """Async handler returning int -> exit code as-is."""

    async def handler(args):
        return 7

    cmds = [_make_cmd("acmd", handler_fn=handler)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    assert main_fn(["acmd"]) == 7


def test_handler_other_return_type_returns_one(monkeypatch, capsys):
    """Handler returning a non-None, non-int, non-awaitable type -> exit 1."""
    cmds = [_make_cmd("cmd", handler_fn=lambda args: "not-an-int")]
    main_fn = _main_with_commands(monkeypatch, cmds)
    rc = main_fn(["cmd"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "error" in captured.err.lower() or "error" in captured.out.lower()


def test_handler_exception_returns_one(monkeypatch, capsys):
    """Handler raising an exception -> exit 1 with short error."""

    def handler(args):
        raise RuntimeError("boom")

    cmds = [_make_cmd("cmd", handler_fn=handler)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    rc = main_fn(["cmd"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err or "boom" in captured.out


def test_no_handler_prints_help_returns_zero(monkeypatch, capsys):
    """Plugin command without handler_fn -> print that command's help + return 0."""
    cmds = [_make_cmd("nohandler", handler_fn=None, help="custom help text")]
    main_fn = _main_with_commands(monkeypatch, cmds)
    rc = main_fn(["nohandler"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "nohandler" in captured.out or "custom help text" in captured.out


def test_args_func_priority_over_dispatch(monkeypatch):
    """args.func (set by plugin set_defaults) takes priority over _DISPATCH."""
    handler_calls = []

    def plugin_handler(args):
        handler_calls.append("plugin")
        return 0

    # "myplugin" is not a builtin name, so it won't conflict.
    cmds = [_make_cmd("myplugin", handler_fn=plugin_handler)]
    main_fn = _main_with_commands(monkeypatch, cmds)
    main_fn(["myplugin"])
    assert handler_calls == ["plugin"]


def test_builtin_command_dispatches_via_dispatch_table(monkeypatch):
    """Builtin commands still dispatch via _DISPATCH (not args.func)."""
    from app.interfaces.cli.main import _DISPATCH

    # Monkeypatch the status handler to verify it's called.
    original = _DISPATCH["status"]
    called = {"count": 0}

    def fake_status(args):
        called["count"] += 1
        return 0

    monkeypatch.setitem(_DISPATCH, "status", fake_status)
    try:
        main_fn = _main_with_commands(monkeypatch, [])
        main_fn(["status"])
        assert called["count"] == 1
    finally:
        _DISPATCH["status"] = original
