from dataclasses import fields
from datetime import datetime, timezone
import inspect

from app.domain.agent import AgentState
from app.domain.provider import ModelInfo, ProviderConfig, ProviderRegistry
from app.domain.gateway import GatewayOutboundMessage, GatewaySessionKey, InteractionMessage
from app.domain.platform import Platform
from app.domain.session import SessionSource
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType


def test_session_sources_cover_entry_types():
    # 模式十六：source 取值与 session_id 前缀一一对应；curator 是 Curator 周期维护的内部触发来源
    assert SessionSource.CURATOR.value == "curator"
    assert {source.value for source in SessionSource} == {
        "dashboard",
        "api",
        "cli",
        "feishu",
        "dingtalk",
        "wecom",
        "acp",
        "schedule",
        "curator",
    }


def test_tool_definition_has_no_handler_field():
    names = {field.name for field in fields(ToolDefinition)}

    assert "handler" not in names


def test_risk_levels_cover_mvp_permissions():
    assert {level.value for level in RiskLevel} == {"safe", "confirm", "dangerous"}


def test_tool_source_types_cover_registered_and_future_sources():
    assert {source.value for source in ToolSourceType} == {
        "builtin",
        "knowledge",
        "skill",
        "mcp",
        "plugin",
        "agent",
    }


def test_tool_definition_defaults_to_builtin_source_and_toolset():
    definition = ToolDefinition("name", "desc", {"type": "object"})

    assert definition.source_type is ToolSourceType.BUILTIN
    assert definition.toolset == "builtin"


def test_tool_definition_preserves_positional_risk_level_argument():
    definition = ToolDefinition("confirm_tool", "confirm", {"type": "object"}, RiskLevel.CONFIRM)

    assert definition.risk_level is RiskLevel.CONFIRM
    assert definition.source_type is ToolSourceType.BUILTIN


def test_agent_state_defaults_iteration_count_to_zero():
    state = AgentState(session_id="session-1")

    assert state.iteration_count == 0


def test_model_info_describes_capabilities():
    model = ModelInfo(
        id="model-a",
        display_name="Model A",
        provider="test",
        supports_tools=True,
        supports_streaming=False,
    )

    assert model.supports_tools is True
    assert model.supports_streaming is False


def test_gateway_session_key_normalizes_thread_id():
    key = GatewaySessionKey(Platform.FEISHU, "chat-1")

    assert key.thread_id == ""
    assert key.conversation_parts == ("feishu", "chat-1", "")


def test_interaction_message_defaults_metadata():
    message = InteractionMessage(
        id="event-1",
        session_key=GatewaySessionKey("cli", "local"),
        text="hello",
    )

    assert message.metadata == {}


def test_gateway_outbound_message_defaults_metadata():
    message = GatewayOutboundMessage(content="done")

    assert message.metadata == {}


def _provider_config_kwargs(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        id="p1",
        name="n",
        provider_type="openai-compatible",
        base_url="http://example.test/v1",
        model="m",
        api_key_present=True,
        is_active=True,
        extra_headers=None,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return base


def test_provider_config_supports_vision_defaults_false():
    cfg = ProviderConfig(**_provider_config_kwargs())
    assert cfg.supports_vision is False


def test_provider_config_supports_vision_explicit_true():
    cfg = ProviderConfig(**_provider_config_kwargs(supports_vision=True))
    assert cfg.supports_vision is True


def test_provider_registry_update_provider_signature_has_supports_vision():
    sig = inspect.signature(ProviderRegistry.update_provider)
    assert "supports_vision" in sig.parameters


def test_interaction_message_images_defaults_empty():
    message = InteractionMessage(
        id="event-1",
        session_key=GatewaySessionKey("cli", "local"),
        text="hello",
    )
    assert message.images == []


def test_interaction_message_images_explicit():
    message = InteractionMessage(
        id="event-1",
        session_key=GatewaySessionKey("cli", "local"),
        text="",
        images=["data:image/png;base64,aGVsbG8="],
    )
    assert len(message.images) == 1
    assert message.images[0] == "data:image/png;base64,aGVsbG8="


def test_interaction_message_positional_construction_still_works():
    """旧位置参数构造不被 images 字段破坏。"""
    message = InteractionMessage(
        "event-1",
        GatewaySessionKey("cli", "local"),
        "hello",
    )
    assert message.text == "hello"
    assert message.images == []
