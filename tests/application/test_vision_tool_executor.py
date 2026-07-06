import pytest

from app.application.vision_tool_executor import VisionAnalyzeToolExecutor
from app.domain.provider import LLMResult, ModelInfo
from app.domain.tool import ToolCallRequest, ToolResultStatus


class FakeProvider:
    def __init__(self, response: LLMResult | None = None, raises: Exception | None = None):
        self.response = response or LLMResult(
            message={"role": "assistant", "content": "image shows a cat"}, finish_reason="stop"
        )
        self.raises = raises
        self.calls = 0
        self.last_messages = None
        self.last_tools = None
        self.last_model = None

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        self.last_messages = list(messages)
        self.last_tools = list(tools) if tools else tools
        self.last_model = model
        if self.raises:
            raise self.raises
        return self.response


def _executor(provider, *, vision=True, model="test-model"):
    return VisionAnalyzeToolExecutor(
        provider=provider,
        vision_capability=lambda: vision,
        current_model=lambda: model,
    )


@pytest.mark.asyncio
async def test_vision_analyze_success_returns_assistant_content():
    provider = FakeProvider()
    executor = _executor(provider)

    result = await executor.execute(
        ToolCallRequest(
            id="call-1",
            name="vision_analyze",
            arguments={
                "image_url": "data:image/png;base64,aGVsbG8=",
                "question": "what is this",
            },
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == {"answer": "image shows a cat"}
    assert provider.calls == 1
    assert provider.last_tools == []
    assert provider.last_model == "test-model"
    user_msg = provider.last_messages[-1]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list)
    assert any(part.get("type") == "image_url" for part in user_msg["content"])
    assert any(part.get("type") == "text" for part in user_msg["content"])


@pytest.mark.asyncio
async def test_vision_analyze_unsupported_vision_returns_error():
    provider = FakeProvider()
    executor = _executor(provider, vision=False)

    result = await executor.execute(
        ToolCallRequest(
            id="call-2",
            name="vision_analyze",
            arguments={"image_url": "data:image/png;base64,aGVsbG8=", "question": "?"},
        )
    )

    assert result.status is ToolResultStatus.ERROR
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_vision_analyze_invalid_url_returns_error():
    provider = FakeProvider()
    executor = _executor(provider)

    result = await executor.execute(
        ToolCallRequest(
            id="call-3",
            name="vision_analyze",
            arguments={"image_url": "not-a-url", "question": "?"},
        )
    )

    assert result.status is ToolResultStatus.ERROR
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_vision_analyze_provider_exception_returns_error():
    provider = FakeProvider(raises=RuntimeError("provider down"))
    executor = _executor(provider)

    result = await executor.execute(
        ToolCallRequest(
            id="call-4",
            name="vision_analyze",
            arguments={"image_url": "data:image/png;base64,aGVsbG8=", "question": "?"},
        )
    )

    assert result.status is ToolResultStatus.ERROR
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_vision_analyze_does_not_recurse_when_provider_returns_tool_calls():
    provider = FakeProvider(
        response=LLMResult(
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "nested-call",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression":"1+1"}'},
                    }
                ],
            },
            finish_reason="tool_calls",
        )
    )
    executor = _executor(provider)

    result = await executor.execute(
        ToolCallRequest(
            id="call-5",
            name="vision_analyze",
            arguments={"image_url": "data:image/png;base64,aGVsbG8=", "question": "?"},
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert provider.calls == 1
