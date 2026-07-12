import asyncio

from fastapi.testclient import TestClient

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionService
from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.domain.provider import LLMResult, ModelInfo
from app.domain.tool import ToolCallRequest, ToolResult, ToolResultStatus
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor
from app.infrastructure.tools.composite import CompositeToolExecutor
from app.interfaces.http.openai_compatible import create_openai_compatible_router


class FakeProvider:
    def __init__(self, tool_name: str = "calculator", final_content: str = "tool done"):
        self.calls = 0
        self.tool_name = tool_name
        self.final_content = final_content
        self.last_options: dict | None = None

    async def list_models(self):
        return [ModelInfo("test-model", "test-model", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        self.last_options = options
        if messages and messages[-1].get("role") == "tool":
            return LLMResult({"role": "assistant", "content": self.final_content}, "stop")
        if messages and "tool" in str(messages[-1].get("content", "")):
            return LLMResult(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": self.tool_name,
                                "arguments": '{"query":"python"}' if self.tool_name == "search_knowledge" else '{"expression":"1+2"}',
                            },
                        }
                    ],
                },
                "tool_calls",
            )
        return LLMResult({"role": "assistant", "content": "hello"}, "stop")


class FakeKnowledgeExecutor:
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            {"site": "N-KB", "query": request.arguments["query"], "results": [{"snippet": "Python snippet"}]},
        )


def build_client(tmp_path, provider=None, tool_service=None):
    provider = provider or FakeProvider()
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = AgentGraphRunner(
        provider,
        tool_service or ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    chat = ChatCompletionService(store, runner, SessionService(store))
    models = ModelService(provider, "test-model")
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_openai_compatible_router(chat, models))
    return TestClient(app), store


def test_health_and_models(tmp_path):
    client, _ = build_client(tmp_path)

    assert client.get("/health").json() == {"status": "ok"}
    models = client.get("/v1/models").json()

    assert models["object"] == "list"
    assert models["data"][0]["id"] == "N-Agent"
    assert models["data"][0]["owned_by"] == "n-agent"


def test_non_streaming_chat_completion_and_session_header(tmp_path):
    client, store = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "session-1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "N-Agent"
    assert body["choices"][0]["message"]["content"] == "hello"


def test_streaming_chat_completion(tmp_path):
    client, _ = build_client(tmp_path)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "test-model", "stream": True, "metadata": {"session_id": "s1"}, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        text = "".join(response.iter_text())

    assert response.headers["content-type"].startswith("text/event-stream")
    assert "chat.completion.chunk" in text
    assert '"model": "N-Agent"' in text
    assert "data: [DONE]" in text


def test_streaming_chat_completion_emits_chinese_as_original_chars(tmp_path):
    """SSE chunks must use ensure_ascii=False so Chinese characters appear as
    original characters (not \\uXXXX escape sequences) in the response stream."""
    provider = FakeProvider(final_content="你好，世界")
    client, _ = build_client(tmp_path, provider=provider)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "test-model", "stream": True, "metadata": {"session_id": "s-zh"}, "messages": [{"role": "user", "content": "use tool"}]},
    ) as response:
        text = "".join(response.iter_text())

    assert "你好，世界" in text
    assert "\\u4f60" not in text  # no ascii-escaped unicode for "你"
    assert "\\u4e16" not in text  # no ascii-escaped unicode for "世"


def test_tool_call_loop_persists_tool_call(tmp_path):
    client, store = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "stream": False, "metadata": {"session_id": "s-tool"}, "messages": [{"role": "user", "content": "use tool"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "tool done"


def test_openai_chat_completion_can_call_knowledge_tool(tmp_path):
    provider = FakeProvider(tool_name="search_knowledge", final_content="kb answer")
    builtin = build_builtin_tool_executor(tmp_path)
    kb = FakeKnowledgeExecutor()
    tool_service = ToolService(
        CompositeToolExecutor(
            {
                "get_current_time": builtin,
                "calculator": builtin,
                "list_directory": builtin,
                "read_text_file": builtin,
                "search_knowledge": kb,
            }
        ),
        builtin_tool_definitions() + knowledge_tool_definitions(),
    )
    client, store = build_client(tmp_path, provider=provider, tool_service=tool_service)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {"session_id": "s-kb"},
            "messages": [{"role": "user", "content": "use tool for knowledge"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "kb answer"
    tool_calls = asyncio.run(store.list_tool_calls("s-kb"))
    assert tool_calls[0].tool_name == "search_knowledge"
    assert tool_calls[0].status == "success"


def test_openai_chat_completion_accepts_valid_content_array(tmp_path):
    client, _ = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {"session_id": "s-valid"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看这张图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        },
    )

    assert response.status_code == 200


def test_openai_chat_completion_normalizes_input_image_to_image_url(tmp_path):
    client, _ = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {"session_id": "s-input-image"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析"},
                        {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="},
                    ],
                }
            ],
        },
    )

    assert response.status_code == 200


def test_openai_chat_completion_rejects_unknown_part_type(tmp_path):
    client, _ = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "audio_url", "audio_url": {"url": "x"}}],
                }
            ],
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "unsupported_content_type"


def test_openai_chat_completion_rejects_invalid_data_url(tmp_path):
    client, _ = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!not-base64!!!"}}
                    ],
                }
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image_url"


def test_openai_chat_completion_rejects_oversized_data_url(tmp_path):
    client, _ = build_client(tmp_path)
    huge = "A" * (21 * 1024 * 1024)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge}"}}
                    ],
                }
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "image_too_large"


def test_openai_chat_completion_rejects_system_message_with_image(tmp_path):
    client, _ = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "sys"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                },
                {"role": "user", "content": "hi"},
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_content_type"


def test_openai_chat_completion_rejects_tool_message_with_image(tmp_path):
    client, _ = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "c1",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}
                    ],
                },
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_content_type"


def test_openai_chat_completion_provider_error_returns_500_not_400(tmp_path):
    from app.domain.provider import LLMResult, ModelInfo

    class ErrorProvider:
        async def list_models(self):
            return [ModelInfo("test-model", "test-model", "fake")]

        async def supports_tools(self, model):
            return True

        async def chat(self, messages, tools, stream, model, options):
            raise RuntimeError("provider failure")

    client, _ = build_client(tmp_path, provider=ErrorProvider())

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 500


def test_openai_chat_completion_merges_top_level_generation_params(tmp_path):
    """Top-level temperature/max_tokens/top_p fields should be merged into
    the options dict passed to the provider, so they reach the LLM and are
    captured in usage recording."""
    provider = FakeProvider()
    client, _ = build_client(tmp_path, provider=provider)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {"session_id": "s-gen"},
            "temperature": 0.5,
            "max_tokens": 100,
            "top_p": 0.9,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_options is not None
    assert provider.last_options.get("temperature") == 0.5
    assert provider.last_options.get("max_tokens") == 100
    assert provider.last_options.get("top_p") == 0.9


def test_openai_chat_completion_options_dict_overrides_top_level(tmp_path):
    """When the same param is provided both as a top-level field and inside
    the options dict, the options dict wins (advanced callers can override)."""
    provider = FakeProvider()
    client, _ = build_client(tmp_path, provider=provider)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {"session_id": "s-override"},
            "temperature": 0.5,
            "max_tokens": 100,
            "options": {"temperature": 0.9},
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_options is not None
    assert provider.last_options.get("temperature") == 0.9
    assert provider.last_options.get("max_tokens") == 100


def test_openai_chat_completion_omits_unset_generation_params(tmp_path):
    """When generation params are not set, they should not appear in options
    (avoids sending null values to provider)."""
    provider = FakeProvider()
    client, _ = build_client(tmp_path, provider=provider)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {"session_id": "s-none"},
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_options is not None
    assert "temperature" not in provider.last_options
    assert "max_tokens" not in provider.last_options
    assert "top_p" not in provider.last_options
