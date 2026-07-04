from __future__ import annotations

from app.interfaces.cli.slash import (
    GATEWAY_COMMANDS,
    LOCAL_COMMANDS,
    handle_local_command,
    is_local_command,
)


def test_local_commands_listed():
    assert "/help" in LOCAL_COMMANDS
    assert "/exit" in LOCAL_COMMANDS
    assert "/clear" in LOCAL_COMMANDS
    assert "/history" in LOCAL_COMMANDS
    assert "/confirm" in LOCAL_COMMANDS
    assert "/cancel" in LOCAL_COMMANDS


def test_gateway_commands_listed():
    for cmd in ("/new", "/rename", "/delete", "/tools", "/models", "/switch", "/sethome"):
        assert cmd in GATEWAY_COMMANDS
    for cmd in ("/sessions", "/status", "/schedule"):
        assert cmd not in GATEWAY_COMMANDS


def test_is_local_command():
    assert is_local_command("/help")
    assert is_local_command("/exit")
    assert is_local_command("/clear")
    assert is_local_command("/history")
    assert is_local_command("/confirm once")
    assert is_local_command("/cancel")
    assert not is_local_command("/new")
    assert is_local_command("/sessions")
    assert is_local_command("/status")
    assert is_local_command("/schedule")
    assert is_local_command("/provider list")


def test_handle_local_command_help_returns_zero(fake_console):
    rc = handle_local_command("/help", fake_console, history_path="/tmp/x")
    assert rc == 0


def test_handle_local_command_clear_returns_zero(fake_console):
    rc = handle_local_command("/clear", fake_console, history_path="/tmp/x")
    assert rc == 0


def test_handle_local_command_exit_returns_zero(fake_console):
    rc = handle_local_command("/exit", fake_console, history_path="/tmp/x")
    assert rc == 0
