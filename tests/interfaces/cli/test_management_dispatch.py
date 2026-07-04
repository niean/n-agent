from __future__ import annotations

import io
import sys
from types import SimpleNamespace

from app.interfaces.cli import management


class _FakeProviderService:
    async def list_providers(self):
        return []


class _FakeServices:
    provider_service = _FakeProviderService()


def _install_fake_dispatch(monkeypatch, handler):
    monkeypatch.setattr(management, "_get_dispatch", lambda: {"provider": handler})


def test_is_management_command_detects_domain_prefixes():
    assert management.is_management_command("/provider list")
    assert management.is_management_command("/knowledge get kb1")
    assert management.is_management_command("/mcp refresh s1")
    assert management.is_management_command("/schedule")
    assert management.is_management_command("/doctor --probe")
    assert management.is_management_command("/config --json")
    assert management.is_management_command("/skill list")
    assert management.is_management_command("/plugin view foo")
    assert management.is_management_command("/status")
    assert management.is_management_command("/sessions")
    assert management.is_management_command("/sessions --browse")
    assert not management.is_management_command("/new")
    assert not management.is_management_command("hello world")
    assert not management.is_management_command("/help")


def test_is_management_command_dynamic_via_dispatch(monkeypatch):
    """Adding a command to _DISPATCH auto-detects it; no hardcoded tuple to update."""
    monkeypatch.setattr(management, "_get_dispatch", lambda: {"newcmd": lambda args: 0})
    assert management.is_management_command("/newcmd")
    assert management.is_management_command("/newcmd sub --flag")
    assert not management.is_management_command("/other")
    assert not management.is_management_command("newcmd")


def test_run_management_command_dispatches_to_handler(monkeypatch, capsys):
    calls: list = []

    def _handler(args):
        calls.append(args)
        print("handler called", flush=True)
        return 0

    _install_fake_dispatch(monkeypatch, _handler)
    rc = management.run_management_command("/provider list --json")
    assert rc == 0
    assert len(calls) == 1
    assert calls[0].command == "provider"
    assert calls[0].provider_command == "list"
    assert calls[0].json is True
    assert "handler called" in capsys.readouterr().out


def test_run_management_command_returns_handler_rc(monkeypatch):
    def _handler(args):
        return 2

    _install_fake_dispatch(monkeypatch, _handler)
    rc = management.run_management_command("/provider get missing")
    assert rc == 2


def test_run_management_command_unknown_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(management, "_get_dispatch", lambda: {})
    rc = management.run_management_command("/provider list")
    assert rc == 2
    assert "unknown command" in capsys.readouterr().out


def test_run_management_command_help_does_not_crash(monkeypatch, capsys):
    def _handler(args):
        return 0

    _install_fake_dispatch(monkeypatch, _handler)
    rc = management.run_management_command("/provider --help")
    assert rc == 0


def test_run_management_command_handler_exception_returns_1(monkeypatch, capsys):
    def _handler(args):
        raise RuntimeError("boom")

    _install_fake_dispatch(monkeypatch, _handler)
    rc = management.run_management_command("/provider list")
    assert rc == 1
    out = capsys.readouterr().out
    assert "RuntimeError" in out
    assert "boom" in out


def test_run_management_command_empty_returns_0(monkeypatch):
    rc = management.run_management_command("")
    assert rc == 0


def test_run_management_command_quoted_args(monkeypatch, capsys):
    calls: list = []

    def _handler(args):
        calls.append(args)
        return 0

    _install_fake_dispatch(monkeypatch, _handler)
    rc = management.run_management_command('/provider create --name "test name" --type openai-compatible')
    assert rc == 0
    assert calls[0].name == "test name"


def test_run_management_command_sessions_browse_preserves_tty_stdout(monkeypatch):
    class _TtyStdout(io.StringIO):
        def isatty(self):
            return True

    tty_stdout = _TtyStdout()
    calls: list = []

    def _handler(args):
        calls.append(args)
        assert sys.stdout is tty_stdout
        assert args.command == "sessions"
        assert args.browse is True
        assert args.no_interactive is False
        return 0

    monkeypatch.setattr(sys, "stdout", tty_stdout)
    monkeypatch.setattr(management, "_get_dispatch", lambda: {"sessions": _handler})

    rc = management.run_management_command("/sessions --browse")

    assert rc == 0
    assert len(calls) == 1


def test_get_management_completions_includes_all_domains():
    completions = management.get_management_completions()
    for cmd in (
        "provider", "knowledge", "mcp", "schedule", "sandbox", "memory",
        "platform", "doctor", "config", "logs", "skill", "plugin", "status", "sessions",
    ):
        assert f"/{cmd}" in completions
    assert isinstance(completions["/provider"], dict)
    assert "list" in completions["/provider"]
    assert "create" in completions["/provider"]
    assert "list" in completions["/skill"]
    assert "view" in completions["/skill"]
    assert "list" in completions["/plugin"]
    assert "view" in completions["/plugin"]


def test_repl_rewrite_sessions_injects_conversation_id():
    from app.interfaces.cli.repl import ReplRunner

    runner = ReplRunner(gateway_client=None, console=None, conversation_id="conv-123", is_tty=False)
    assert runner._rewrite_sessions_command("/sessions") == "/sessions --conversation-id conv-123"
    assert runner._rewrite_sessions_command("/sessions --browse") == "/sessions --browse --conversation-id conv-123"


def test_repl_rewrite_sessions_preserves_explicit_conversation_id():
    from app.interfaces.cli.repl import ReplRunner

    runner = ReplRunner(gateway_client=None, console=None, conversation_id="conv-123", is_tty=False)
    result = runner._rewrite_sessions_command("/sessions --conversation-id other-conv")
    assert result == "/sessions --conversation-id other-conv"


def test_repl_rewrite_sessions_preserves_explicit_no_interactive():
    from app.interfaces.cli.repl import ReplRunner

    runner = ReplRunner(gateway_client=None, console=None, conversation_id="conv-123", is_tty=False)
    result = runner._rewrite_sessions_command("/sessions --browse --no-interactive")
    assert "--conversation-id conv-123" in result
    assert result.count("--no-interactive") == 1


def test_repl_rewrite_sessions_does_not_touch_other_commands():
    from app.interfaces.cli.repl import ReplRunner

    runner = ReplRunner(gateway_client=None, console=None, conversation_id="conv-123", is_tty=False)
    assert runner._rewrite_sessions_command("/provider list") == "/provider list"
    assert runner._rewrite_sessions_command("/status") == "/status"
    assert runner._rewrite_sessions_command("/skill list") == "/skill list"
