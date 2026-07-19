from __future__ import annotations

import logging

import pytest

from app.application.plugin_service import (
    HookRegistration,
    PluginCliCommand,
    PluginContext,
    VALID_HOOKS,
)
from app.domain.plugin import PluginValidationError


# ---- Value objects ----

def test_hook_registration_fields_and_frozen():
    cb = lambda: None  # noqa: E731
    reg = HookRegistration(
        plugin_key="p1",
        hook_name="pre_tool_call",
        callback=cb,
        registration_index=0,
    )
    assert reg.plugin_key == "p1"
    assert reg.hook_name == "pre_tool_call"
    assert reg.callback is cb
    assert reg.registration_index == 0
    with pytest.raises(Exception):
        reg.plugin_key = "p2"  # type: ignore[misc]
    with pytest.raises(Exception):
        reg.registration_index = 99  # type: ignore[misc]


def test_plugin_cli_command_fields_and_frozen():
    setup = lambda parser: None  # noqa: E731
    reg = PluginCliCommand(
        plugin_key="p1",
        name="hello",
        help="greet",
        description="desc",
        setup_fn=setup,
        handler_fn=None,
        registration_index=0,
    )
    assert reg.plugin_key == "p1"
    assert reg.name == "hello"
    assert reg.help == "greet"
    assert reg.description == "desc"
    assert reg.setup_fn is setup
    assert reg.handler_fn is None
    assert reg.registration_index == 0
    with pytest.raises(Exception):
        reg.name = "bye"  # type: ignore[misc]
    with pytest.raises(Exception):
        reg.registration_index = 99  # type: ignore[misc]


# ---- VALID_HOOKS ----

def test_valid_hooks_contains_exactly_12_fixed_names():
    expected = {
        "on_session_start",
        "on_session_end",
        "on_turn_start",
        "on_turn_end",
        "pre_llm_call",
        "post_llm_call",
        "pre_tool_call",
        "post_tool_call",
        "transform_tool_result",
        "transform_llm_output",
        "on_pre_compress",
        "pre_finalize",
    }
    assert set(VALID_HOOKS) == expected
    assert len(VALID_HOOKS) == 12


def test_valid_hooks_is_immutable():
    with pytest.raises((AttributeError, TypeError)):
        VALID_HOOKS.add("evil")  # type: ignore[attr-defined]


# ---- register_hook ----

def test_register_hook_valid_stores_registration():
    ctx = PluginContext(plugin_key="p1")
    cb = lambda: None  # noqa: E731
    ctx.register_hook("pre_tool_call", cb)
    assert len(ctx.hook_registrations) == 1
    reg = ctx.hook_registrations[0]
    assert isinstance(reg, HookRegistration)
    assert reg.plugin_key == "p1"
    assert reg.hook_name == "pre_tool_call"
    assert reg.callback is cb
    assert reg.registration_index == 0
    assert "hook" not in ctx.unsupported_capabilities


def test_register_hook_unknown_non_empty_warns_but_stores(caplog):
    ctx = PluginContext(plugin_key="p1")
    cb = lambda: None  # noqa: E731
    with caplog.at_level(logging.WARNING, logger="app.application.plugin_service"):
        ctx.register_hook("unknown_hook_name", cb)
    assert len(ctx.hook_registrations) == 1
    reg = ctx.hook_registrations[0]
    assert reg.hook_name == "unknown_hook_name"
    assert reg.callback is cb
    assert "hook" not in ctx.unsupported_capabilities
    assert any(
        "unknown" in record.message.lower() and "unknown_hook_name" in record.message
        for record in caplog.records
    )


def test_register_hook_empty_name_raises():
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_hook("", lambda: None)
    assert len(ctx.hook_registrations) == 0


@pytest.mark.parametrize("bad_name", [123, [], 0.5, True])
def test_register_hook_non_string_name_raises(bad_name):
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_hook(bad_name, lambda: None)  # type: ignore[arg-type]
    assert len(ctx.hook_registrations) == 0


def test_register_hook_non_string_name_does_not_consume_index():
    ctx = PluginContext(plugin_key="p1")
    ctx.register_hook("pre_tool_call", lambda: None)   # index 0
    with pytest.raises(PluginValidationError):
        ctx.register_hook(123, lambda: None)           # type: ignore[arg-type]
    ctx.register_hook("post_tool_call", lambda: None)  # index 1
    assert len(ctx.hook_registrations) == 2
    assert ctx.hook_registrations[0].registration_index == 0
    assert ctx.hook_registrations[1].registration_index == 1


def test_register_hook_non_callable_callback_raises():
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_hook("pre_tool_call", "not a callable")  # type: ignore[arg-type]
    assert len(ctx.hook_registrations) == 0


def test_register_hook_none_callback_raises():
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_hook("pre_tool_call", None)  # type: ignore[arg-type]
    assert len(ctx.hook_registrations) == 0


# ---- register_cli_command ----

def test_register_cli_command_hermes_defaults():
    ctx = PluginContext(plugin_key="p1")
    setup = lambda parser: None  # noqa: E731
    ctx.register_cli_command("hello", "greet user", setup)
    assert len(ctx.cli_command_registrations) == 1
    reg = ctx.cli_command_registrations[0]
    assert isinstance(reg, PluginCliCommand)
    assert reg.plugin_key == "p1"
    assert reg.name == "hello"
    assert reg.help == "greet user"
    assert reg.description == ""
    assert reg.setup_fn is setup
    assert reg.handler_fn is None
    assert reg.registration_index == 0
    assert "cli_command" not in ctx.unsupported_capabilities


def test_register_cli_command_with_handler_and_description():
    ctx = PluginContext(plugin_key="p1")
    setup = lambda parser: None  # noqa: E731
    handler = lambda args: 0  # noqa: E731
    ctx.register_cli_command(
        "hello", "greet", setup, handler_fn=handler, description="desc"
    )
    reg = ctx.cli_command_registrations[0]
    assert reg.handler_fn is handler
    assert reg.description == "desc"


def test_register_cli_command_positional_order_matches_hermes():
    # Hermes positional order: name, help, setup_fn, handler_fn, description
    ctx = PluginContext(plugin_key="p1")
    setup = lambda parser: None  # noqa: E731
    handler = lambda args: 0  # noqa: E731
    ctx.register_cli_command("hello", "greet", setup, handler, "desc")
    reg = ctx.cli_command_registrations[0]
    assert reg.name == "hello"
    assert reg.help == "greet"
    assert reg.setup_fn is setup
    assert reg.handler_fn is handler
    assert reg.description == "desc"


@pytest.mark.parametrize(
    "bad_name",
    [
        "Hello",        # uppercase
        "1hello",       # starts with digit
        "hello_world",  # underscore
        "hello!",       # special char
        "hello world",  # space
        "",             # empty
        "-hello",       # leading dash
        "hello.cmd",    # dot
    ],
)
def test_register_cli_command_invalid_name_raises(bad_name):
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_cli_command(bad_name, "help", lambda parser: None)
    assert len(ctx.cli_command_registrations) == 0


@pytest.mark.parametrize("good_name", ["h", "hello", "hello-world", "a1", "a-1-b"])
def test_register_cli_command_valid_names(good_name):
    ctx = PluginContext(plugin_key="p1")
    ctx.register_cli_command(good_name, "help", lambda parser: None)
    assert ctx.cli_command_registrations[0].name == good_name


def test_register_cli_command_empty_help_raises():
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_cli_command("hello", "", lambda parser: None)
    assert len(ctx.cli_command_registrations) == 0


def test_register_cli_command_non_string_help_raises():
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_cli_command("hello", None, lambda parser: None)  # type: ignore[arg-type]
    assert len(ctx.cli_command_registrations) == 0


def test_register_cli_command_non_callable_setup_raises():
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_cli_command("hello", "help", "not callable")  # type: ignore[arg-type]
    assert len(ctx.cli_command_registrations) == 0


def test_register_cli_command_non_callable_handler_raises():
    ctx = PluginContext(plugin_key="p1")
    with pytest.raises(PluginValidationError):
        ctx.register_cli_command(
            "hello", "help", lambda parser: None, handler_fn="not callable"  # type: ignore[arg-type]
        )
    assert len(ctx.cli_command_registrations) == 0


def test_register_cli_command_none_handler_ok():
    ctx = PluginContext(plugin_key="p1")
    ctx.register_cli_command("hello", "help", lambda parser: None, handler_fn=None)
    assert ctx.cli_command_registrations[0].handler_fn is None


# ---- registration_index stability & per-Context independence ----

def test_registration_index_stable_across_hook_and_cli():
    ctx = PluginContext(plugin_key="p1")
    ctx.register_hook("pre_tool_call", lambda: None)            # index 0
    ctx.register_cli_command("cmd1", "help1", lambda p: None)   # index 1
    ctx.register_hook("post_tool_call", lambda: None)           # index 2
    ctx.register_cli_command("cmd2", "help2", lambda p: None)   # index 3

    assert ctx.hook_registrations[0].registration_index == 0
    assert ctx.cli_command_registrations[0].registration_index == 1
    assert ctx.hook_registrations[1].registration_index == 2
    assert ctx.cli_command_registrations[1].registration_index == 3


def test_registration_index_per_context_independent():
    ctx_a = PluginContext(plugin_key="a")
    ctx_b = PluginContext(plugin_key="b")
    ctx_a.register_hook("pre_tool_call", lambda: None)
    ctx_a.register_hook("post_tool_call", lambda: None)
    ctx_b.register_hook("pre_tool_call", lambda: None)

    assert ctx_a.hook_registrations[0].registration_index == 0
    assert ctx_a.hook_registrations[1].registration_index == 1
    assert ctx_b.hook_registrations[0].registration_index == 0


def test_failed_registration_does_not_consume_index():
    ctx = PluginContext(plugin_key="p1")
    ctx.register_hook("pre_tool_call", lambda: None)   # index 0
    with pytest.raises(PluginValidationError):
        ctx.register_hook("", lambda: None)            # fails, no index consumed
    ctx.register_hook("post_tool_call", lambda: None)  # index 1

    assert len(ctx.hook_registrations) == 2
    assert ctx.hook_registrations[0].registration_index == 0
    assert ctx.hook_registrations[1].registration_index == 1


def test_failed_cli_registration_does_not_consume_index():
    ctx = PluginContext(plugin_key="p1")
    ctx.register_hook("pre_tool_call", lambda: None)    # index 0
    with pytest.raises(PluginValidationError):
        ctx.register_cli_command("", "help", lambda p: None)  # fails
    ctx.register_cli_command("ok", "help", lambda p: None)    # index 1

    assert len(ctx.hook_registrations) == 1
    assert len(ctx.cli_command_registrations) == 1
    assert ctx.hook_registrations[0].registration_index == 0
    assert ctx.cli_command_registrations[0].registration_index == 1
