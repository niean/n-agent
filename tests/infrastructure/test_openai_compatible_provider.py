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
