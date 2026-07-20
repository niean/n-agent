from types import SimpleNamespace

import pytest

from app.domain.provider import LLMEventType
from app.domain.tool import ToolExecutionContext
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider


class FakeModels:
    def __init__(self, fail=False):
        self.fail = fail

    async def list(self):
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(data=[SimpleNamespace(id="model-a")])


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("stream"):
            async def gen():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="hi", tool_calls=None), finish_reason=None)]
                )
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None), finish_reason="stop")]
                )
            return gen()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content="hello", tool_calls=None), finish_reason="stop")],
            usage=None,
        )


class FakeClient:
    def __init__(self, fail_models=False):
        self.models = FakeModels(fail_models)
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.mark.asyncio
async def test_provider_lists_models_and_falls_back_to_default():
    provider = OpenAICompatibleProvider("http://test", "key", "default", client=FakeClient())
    fallback = OpenAICompatibleProvider("http://test", "key", "default", client=FakeClient(fail_models=True))

    assert (await provider.list_models())[0].id == "model-a"
    assert (await fallback.list_models())[0].id == "default"


@pytest.mark.asyncio
async def test_provider_supports_tools():
    provider = OpenAICompatibleProvider("http://test", "key", "default", client=FakeClient())

    assert await provider.supports_tools("model-a") is True
    assert await provider.supports_tools("unknown") is True


@pytest.mark.asyncio
async def test_provider_non_streaming_chat_returns_llm_result():
    provider = OpenAICompatibleProvider("http://test", "key", "default", client=FakeClient())

    result = await provider.chat([], [], False, "model-a", {})

    assert result.message["content"] == "hello"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_provider_strips_internal_runtime_options():
    client = FakeClient()
    provider = OpenAICompatibleProvider("http://test", "key", "default", client=client)

    await provider.chat(
        [],
        [],
        False,
        "model-a",
        {
            "temperature": 0.2,
            "tool_execution_context": ToolExecutionContext(),
            "tool_exposure_policy": "safe_only",
            "execution_context_mode": "unattended",
        },
    )

    assert client.completions.kwargs["temperature"] == 0.2
    assert "tool_execution_context" not in client.completions.kwargs
    assert "tool_exposure_policy" not in client.completions.kwargs
    assert "execution_context_mode" not in client.completions.kwargs


@pytest.mark.asyncio
async def test_provider_streaming_chat_returns_llm_events():
    provider = OpenAICompatibleProvider("http://test", "key", "default", client=FakeClient())

    stream = await provider.chat([], [], True, "model-a", {})
    events = [event async for event in stream]

    assert events[0].type == LLMEventType.MESSAGE_START
    assert events[1].type == LLMEventType.CONTENT_DELTA
    assert events[-1].type == LLMEventType.MESSAGE_DONE


@pytest.mark.parametrize("placeholder", ["", "N-Agent", "n-agent", "model"])
@pytest.mark.asyncio
async def test_provider_falls_back_to_default_when_model_is_placeholder(placeholder):
    # 占位 id（如定时任务硬编码的 "N-Agent"）透传会导致 Ark 等 provider 报
    # "does not support agent plan feature"。provider 层必须回退到 default_model。
    client = FakeClient()
    provider = OpenAICompatibleProvider("http://test", "key", "real-model", client=client)

    await provider.chat([], [], False, placeholder, {})

    assert client.completions.kwargs["model"] == "real-model"


@pytest.mark.asyncio
async def test_provider_passes_through_multimodal_content_array():
    # 多模态内容（content array）必须原样透传，不能被字符串化
    client = FakeClient()
    provider = OpenAICompatibleProvider("http://test", "key", "default", client=client)

    content = [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]
    await provider.chat(
        [{"role": "user", "content": content}],
        [],
        False,
        "model-a",
        {},
    )

    messages = client.completions.kwargs["messages"]
    assert messages[0]["content"] is content
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][1]["type"] == "image_url"


def test_tool_call_to_dict_normalizes_unicode_escapes_in_arguments():
    """tool_call arguments 的 \\uXXXX 转义归一化为可读中文（/v1/chat/completions 响应 + SSE）。"""
    from app.infrastructure.llm.openai_compatible import _tool_call_to_dict

    # 对象形式（provider 返回的 tool_call）
    tc = SimpleNamespace(
        id="c1", type="function",
        function=SimpleNamespace(name="task_complete", arguments='{"summary": "\\u5df2\\u5b8c\\u6210"}'),
    )
    d = _tool_call_to_dict(tc)
    assert "\\u" not in d["function"]["arguments"], "still escaped: " + d["function"]["arguments"]
    assert "已完成" in d["function"]["arguments"]

    # dict 形式
    d2 = _tool_call_to_dict({
        "id": "c2", "type": "function",
        "function": {"name": "x", "arguments": '{"a": "\\u5df2"}'},
    })
    assert "已" in d2["function"]["arguments"]
    assert "\\u" not in d2["function"]["arguments"]

    # 非法 JSON 原样返回（best-effort）
    d3 = _tool_call_to_dict({"id": "c3", "type": "function", "function": {"name": "x", "arguments": "not json"}})
    assert d3["function"]["arguments"] == "not json"
