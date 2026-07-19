from __future__ import annotations

import asyncio

from app.application.plugin_service import (
    HookRegistration,
    PluginCliCommand,
    PluginContext,
    PluginToolRegistration,
)


def test_register_tool_signature_matches_hermes():
    ctx = PluginContext(plugin_key="hello", plugin_config={})
    schema = {"name": "hello", "description": "greet", "parameters": {"type": "object"}}

    def handler(args, **kwargs):
        return {"message": "hi"}

    ctx.register_tool(
        name="hello",
        toolset="hello",
        schema=schema,
        handler=handler,
        check_fn=None,
        requires_env=None,
        is_async=False,
        description="",
        emoji="",
        override=False,
    )
    assert len(ctx.tool_registrations) == 1
    reg = ctx.tool_registrations[0]
    assert isinstance(reg, PluginToolRegistration)
    assert reg.name == "hello"
    assert reg.toolset == "hello"
    assert reg.is_async is False
    assert reg.override is False
    assert reg.plugin_key == "hello"


def test_register_tool_accepts_openai_wrapped_schema():
    ctx = PluginContext(plugin_key="hello", plugin_config={})
    wrapped = {
        "type": "function",
        "function": {
            "name": "hello",
            "description": "g",
            "parameters": {"type": "object"},
        },
    }
    ctx.register_tool(name="hello", toolset="hello", schema=wrapped, handler=lambda a, **k: "")
    reg = ctx.tool_registrations[0]
    assert reg.schema["name"] == "hello"
    assert reg.schema["description"] == "g"


def test_register_tool_propagates_plugin_config_and_secret():
    ctx = PluginContext(
        plugin_key="hello",
        plugin_config={"endpoint": "http://x"},
        secret_config={"api_key": "secret"},
    )
    ctx.register_tool(
        name="hello",
        toolset="hello",
        schema={"name": "hello", "parameters": {"type": "object"}},
        handler=lambda a, **k: "",
    )
    reg = ctx.tool_registrations[0]
    assert reg.plugin_config == {"endpoint": "http://x"}
    assert reg.secret_config == {"api_key": "secret"}


def test_unsupported_api_does_not_raise():
    ctx = PluginContext(plugin_key="hello", plugin_config={})
    # T2: register_hook and register_cli_command are now real, not unsupported
    ctx.register_hook("pre_tool_call", lambda: None)
    ctx.register_cli_command("hello", "greet user", lambda parser: None)
    # these remain unsupported stubs
    ctx.register_command("cmd", lambda: None)
    ctx.register_platform("discord", lambda: None)
    ctx.register_web_search_provider(lambda: None)
    ctx.register_image_gen_provider(lambda: None)
    ctx.register_skill("s", lambda: None)
    assert "hook" not in ctx.unsupported_capabilities
    assert "cli_command" not in ctx.unsupported_capabilities
    assert "command" in ctx.unsupported_capabilities
    assert "platform" in ctx.unsupported_capabilities
    assert "web_search_provider" in ctx.unsupported_capabilities
    assert len(ctx.warnings) == len(ctx.unsupported_capabilities)
    # real registrations are stored, not unsupported
    assert len(ctx.hook_registrations) == 1
    assert isinstance(ctx.hook_registrations[0], HookRegistration)
    assert len(ctx.cli_command_registrations) == 1
    assert isinstance(ctx.cli_command_registrations[0], PluginCliCommand)


def test_dispatch_tool_returns_error():
    ctx = PluginContext(plugin_key="hello", plugin_config={})
    result = asyncio.run(ctx.dispatch_tool("missing", {}))
    assert "error" in result


def test_llm_facade_raises():
    ctx = PluginContext(plugin_key="hello", plugin_config={})
    try:
        _ = ctx.llm
        raise AssertionError("expected PluginValidationError")
    except Exception as exc:
        assert "llm" in str(exc).lower() or "unsupported" in str(exc).lower()
