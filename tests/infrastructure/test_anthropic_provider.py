from types import SimpleNamespace

import pytest

from app.domain.provider import LLMEventType
from app.domain.tool import ToolExecutionContext
from app.infrastructure.llm.anthropic_provider import AnthropicProvider


class FakeModels:
    def __init__(self, fail=False):
        self.fail = fail

    async def list(self):
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(data=[SimpleNamespace(id="claude-a", display_name="Claude A")])


class FakeMessages:
    def __init__(self, response=None, stream=None):
        self.kwargs = None
        self.response = response or SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )
        self.stream = stream

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("stream"):
            return self.stream
        return self.response


class FakeClient:
    def __init__(self, response=None, stream=None, fail_models=False):
        self.models = FakeModels(fail_models)
        self.messages = FakeMessages(response, stream)


@pytest.mark.asyncio
async def test_provider_lists_models_and_falls_back_to_default():
    provider = AnthropicProvider("http://test", "key", "default", client=FakeClient())
    fallback = AnthropicProvider("http://test", "key", "default", client=FakeClient(fail_models=True))

    assert (await provider.list_models())[0].id == "claude-a"
    assert (await fallback.list_models())[0].id == "default"


@pytest.mark.asyncio
async def test_provider_supports_tools():
    provider = AnthropicProvider("http://test", "key", "default", client=FakeClient())

    assert await provider.supports_tools("claude-a") is True


@pytest.mark.asyncio
async def test_request_splits_system_messages_and_strips_internal_options():
    client = FakeClient()
    provider = AnthropicProvider("http://test", "key", "default", client=client)

    await provider.chat(
        [
            {"role": "system", "content": "sys1"},
            {"role": "system", "content": "sys2"},
            {"role": "user", "content": "hi"},
        ],
        [],
        False,
        "claude-opus-4-6",
        {
            "temperature": 0.2,
            "tool_execution_context": ToolExecutionContext(),
            "tool_exposure_policy": "safe_only",
            "execution_context_mode": "unattended",
        },
    )

    assert client.messages.kwargs["system"] == "sys1\nsys2"
    assert client.messages.kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert client.messages.kwargs["temperature"] == 0.2
    assert "tool_execution_context" not in client.messages.kwargs
    assert "tool_exposure_policy" not in client.messages.kwargs
    assert "execution_context_mode" not in client.messages.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 4096), ("bad", 4096), (0, 4096), (-1, 4096), (999999, 32768), (128, 128)],
)
async def test_request_resolves_legal_max_tokens(value, expected):
    client = FakeClient()
    provider = AnthropicProvider("http://test", "key", "default", client=client)
    options = {} if value is None else {"max_tokens": value}

    await provider.chat([{"role": "user", "content": "hi"}], [], False, "claude-opus-4-7", options)

    assert client.messages.kwargs["max_tokens"] == expected


@pytest.mark.asyncio
async def test_opus_47_does_not_forward_sampling_parameters():
    client = FakeClient()
    provider = AnthropicProvider("http://test", "key", "default", client=client)

    await provider.chat(
        [{"role": "user", "content": "hi"}],
        [],
        False,
        "claude-opus-4-7",
        {"temperature": 0.2, "top_p": 0.9, "top_k": 10},
    )

    assert "temperature" not in client.messages.kwargs
    assert "top_p" not in client.messages.kwargs
    assert "top_k" not in client.messages.kwargs


@pytest.mark.asyncio
async def test_tool_schema_conversion_normalizes_schemas_and_deduplicates():
    client = FakeClient()
    provider = AnthropicProvider("http://test", "key", "default", client=client)

    await provider.chat(
        [{"role": "user", "content": "hi"}],
        [
            {"type": "function", "function": {"name": "a", "description": "A", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "parameters": {"oneOf": [{"type": "string"}]}}},
            {"type": "function", "function": {"name": "c", "parameters": {"type": ["string", "null"]}}},
            {"type": "function", "function": {"name": "a", "description": "duplicate", "parameters": {}}},
        ],
        False,
        "claude-opus-4-6",
        {},
    )

    tools = client.messages.kwargs["tools"]
    assert [tool["name"] for tool in tools] == ["a", "b", "c"]
    assert tools[0]["input_schema"] == {"type": "object", "properties": {}}
    assert tools[1]["input_schema"]["type"] == "object"
    assert tools[2]["input_schema"]["type"] == "string"


@pytest.mark.asyncio
async def test_tool_turn_normalization_merges_tool_results_after_assistant_tool_use():
    client = FakeClient()
    provider = AnthropicProvider("http://test", "key", "default", client=client)

    await provider.chat(
        [
            {"role": "user", "content": "calc"},
            {
                "role": "assistant",
                "content": "using tools",
                "tool_calls": [
                    {"id": "call-1", "type": "function", "function": {"name": "calculator", "arguments": "{\"expression\":\"1+2\"}"}},
                    {"id": "call-2", "type": "function", "function": {"name": "calculator", "arguments": "{\"expression\":\"2+3\"}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "3"},
            {"role": "tool", "tool_call_id": "call-2", "content": "5"},
        ],
        [],
        False,
        "claude-opus-4-6",
        {},
    )

    messages = client.messages.kwargs["messages"]
    assert messages[1]["role"] == "assistant"
    assert [block["type"] for block in messages[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert messages[2]["role"] == "user"
    assert [block["type"] for block in messages[2]["content"]] == ["tool_result", "tool_result"]


@pytest.mark.asyncio
async def test_tool_message_without_previous_tool_use_raises_diagnostic_error():
    provider = AnthropicProvider("http://test", "key", "default", client=FakeClient())

    with pytest.raises(ValueError, match="orphan tool message"):
        await provider.chat([{"role": "tool", "tool_call_id": "call-1", "content": "3"}], [], False, "claude-opus-4-6", {})


@pytest.mark.asyncio
async def test_intervening_message_between_tool_use_and_tool_result_raises_diagnostic_error():
    provider = AnthropicProvider("http://test", "key", "default", client=FakeClient())

    with pytest.raises(ValueError, match="tool result must immediately follow"):
        await provider.chat(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call-1", "type": "function", "function": {"name": "calculator", "arguments": "{}"}},
                    ],
                },
                {"role": "user", "content": "intervening"},
                {"role": "tool", "tool_call_id": "call-1", "content": "3"},
            ],
            [],
            False,
            "claude-opus-4-6",
            {},
        )


@pytest.mark.asyncio
async def test_response_normalizes_text_tool_calls_finish_reason_and_usage():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Use tool"),
            SimpleNamespace(type="tool_use", id="tu-1", name="calculator", input={"expression": "1+2"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=2, cache_creation_input_tokens=1),
    )
    provider = AnthropicProvider("http://test", "key", "default", client=FakeClient(response=response))

    result = await provider.chat([{"role": "user", "content": "hi"}], [], False, "claude-opus-4-6", {})

    assert result.message["content"] == "Use tool"
    assert result.message["tool_calls"][0]["function"]["arguments"] == '{"expression": "1+2"}'
    assert result.finish_reason == "tool_calls"
    assert result.usage["cache_read_input_tokens"] == 2


@pytest.mark.asyncio
async def test_streaming_chat_returns_llm_events():
    async def gen():
        yield SimpleNamespace(type="message_start")
        yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hi"))
        yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use", id="tu-1", name="calculator"))
        yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="input_json_delta", partial_json='{"expression"'))
        yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="input_json_delta", partial_json=": ""1+2""}"))
        yield SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="tool_use"))

    provider = AnthropicProvider("http://test", "key", "default", client=FakeClient(stream=gen()))

    stream = await provider.chat([{"role": "user", "content": "hi"}], [], True, "claude-opus-4-6", {})
    events = [event async for event in stream]

    assert events[0].type == LLMEventType.MESSAGE_START
    assert any(event.type == LLMEventType.CONTENT_DELTA for event in events)
    assert any(event.type == LLMEventType.TOOL_CALL_DELTA for event in events)
    assert events[-1].type == LLMEventType.MESSAGE_DONE
