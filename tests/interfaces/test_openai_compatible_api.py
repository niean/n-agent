import json
from contextlib import ExitStack

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionResult, ChatCompletionService
from app.application.events import ChatEvent, ChatEventType
from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.domain.provider import LLMResult, ModelInfo
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
)
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


_test_client_stack: ExitStack | None = None


@pytest.fixture(autouse=True)
def test_client_lifecycle():
    global _test_client_stack
    with ExitStack() as stack:
        _test_client_stack = stack
        yield
    _test_client_stack = None


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
    app = FastAPI()
    app.include_router(create_openai_compatible_router(chat, models, store))
    if _test_client_stack is None:
        raise RuntimeError("TestClient lifecycle fixture is not active")
    client = _test_client_stack.enter_context(TestClient(app))
    return client, store


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
        headers={"X-Session-ID": "s1"},
        json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
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
        headers={"X-Session-ID": "s-zh"},
        json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "use tool"}]},
    ) as response:
        text = "".join(response.iter_text())

    assert "你好，世界" in text
    assert "\\u4f60" not in text  # no ascii-escaped unicode for "你"
    assert "\\u4e16" not in text  # no ascii-escaped unicode for "世"


def test_tool_call_loop_persists_tool_call(tmp_path):
    client, store = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "s-tool"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "use tool"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "tool done"


def test_http_metadata_cannot_inject_internal_tool_authorization(tmp_path):
    provider = FakeProvider(tool_name="managed_action")
    tool_service = ToolService(
        build_builtin_tool_executor(tmp_path),
        [
            ToolDefinition(
                name="managed_action",
                description="Managed action",
                input_schema={"type": "object"},
                risk_level=RiskLevel.CONFIRM,
                managed=True,
            )
        ],
    )
    client, store = build_client(
        tmp_path,
        provider=provider,
        tool_service=tool_service,
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "s-untrusted-grant"},
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {
                "allowed_confirm_tools": {"managed_action": "session"},
                "permitted_managed_tools": ["managed_action"],
            },
            "messages": [{"role": "user", "content": "use tool"}],
        },
    )

    assert response.status_code == 200
    assert client.portal is not None
    calls = client.portal.call(store.list_tool_calls, "s-untrusted-grant")
    assert calls[0].status == "permission_denied"


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
        headers={"X-Session-ID": "s-kb"},
        json={
            "model": "test-model",
            "stream": False,
            "messages": [{"role": "user", "content": "use tool for knowledge"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "kb answer"
    assert client.portal is not None
    tool_calls = client.portal.call(store.list_tool_calls, "s-kb")
    assert tool_calls[0].tool_name == "search_knowledge"
    assert tool_calls[0].status == "success"


def test_openai_chat_completion_accepts_valid_content_array(tmp_path):
    client, _ = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "s-valid"},
        json={
            "model": "test-model",
            "stream": False,
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
        headers={"X-Session-ID": "s-input-image"},
        json={
            "model": "test-model",
            "stream": False,
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
        headers={"X-Session-ID": "s-gen"},
        json={
            "model": "test-model",
            "stream": False,
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
        headers={"X-Session-ID": "s-override"},
        json={
            "model": "test-model",
            "stream": False,
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
        headers={"X-Session-ID": "s-none"},
        json={
            "model": "test-model",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_options is not None
    assert "temperature" not in provider.last_options
    assert "max_tokens" not in provider.last_options
    assert "top_p" not in provider.last_options


# ---------------------------------------------------------------------------
# S4: API bearer selector tests
# ---------------------------------------------------------------------------


def test_api_no_session_header_creates_new_api_session(tmp_path):
    """Without X-Session-ID, the route generates a new api- prefixed session."""
    client, store = build_client(tmp_path)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    # The session should have been created with source=api
    assert client.portal is not None
    sessions = client.portal.call(store.list_sessions)
    assert len(sessions) >= 1
    assert sessions[-1].source == "api"
    assert sessions[-1].id.startswith("api-")


def test_api_header_continues_existing_api_session(tmp_path):
    """X-Session-ID pointing to an existing api session continues it."""
    client, store = build_client(tmp_path)

    # First request creates the session
    client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "api-existing"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "first"}]},
    )

    # Second request continues the same session
    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "api-existing"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "second"}]},
    )

    assert response.status_code == 200
    assert client.portal is not None
    messages = client.portal.call(store.list_messages, "api-existing")
    assert len(messages) >= 2


def test_api_metadata_session_id_is_ignored(tmp_path):
    """metadata.session_id must NOT be used for session selection."""
    client, store = build_client(tmp_path)

    # Send with metadata.session_id but NO X-Session-ID header
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": False,
            "metadata": {"session_id": "should-be-ignored"},
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    # The session "should-be-ignored" should NOT exist; a new api- session was created
    assert client.portal is not None
    session = client.portal.call(store.get_session, "should-be-ignored")
    assert session is None


def test_api_header_pointing_to_dashboard_session_returns_409(tmp_path):
    """X-Session-ID pointing to a dashboard session returns 409 before content read."""
    client, store = build_client(tmp_path)

    # Create a dashboard session via the session service
    assert client.portal is not None
    from app.domain.session import ConversationSession
    client.portal.call(store.create_session, ConversationSession(id="dash-1", source="dashboard"))

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "dash-1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "api_session_scope_mismatch"

def test_api_header_pointing_to_feishu_session_returns_409(tmp_path):
    """X-Session-ID pointing to a feishu session returns 409."""
    client, store = build_client(tmp_path)

    assert client.portal is not None
    from app.domain.session import ConversationSession
    client.portal.call(store.create_session, ConversationSession(id="feishu-1", source="feishu"))

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "feishu-1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "api_session_scope_mismatch"


# ---------------------------------------------------------------------------
# T4: OpenAI-compatible Run 轻量入口 (run_footer) 响应合同
# ---------------------------------------------------------------------------

class _RunFooterChatService:
    """Stub ChatCompletionService：非流式返回预置结果，流式返回预置事件序列。"""

    def __init__(self, result=None, events=None):
        self._result = result
        self._events = events

    async def complete(self, request):
        if request.stream:
            async def _gen():
                for event in self._events:
                    yield event
            return _gen()
        return self._result


def _run_footer_result(run_id=None, finish_reason="stop"):
    return ChatCompletionResult(
        session_id="s-footer",
        model="test-model",
        message={"role": "assistant", "content": "hello"},
        finish_reason=finish_reason,
        usage={"total_tokens": 3},
        run_id=run_id,
    )


def _run_footer_client(chat_service):
    app = FastAPI()
    # memory_store 传 None：跳过 session bootstrap，run_footer 只关心响应合同。
    app.include_router(create_openai_compatible_router(chat_service, ModelService(FakeProvider(), "test-model"), None))
    return TestClient(app)


def _run_footer_stream_text(chat_service):
    client = _run_footer_client(chat_service)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Session-ID": "s-footer"},
        json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        return "".join(response.iter_text())


def _parse_sse(text: str):
    """解析 SSE 响应体为 (chunk 列表, done_seen)。"""
    chunks = []
    done_seen = False
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            done_seen = True
            continue
        chunks.append(json.loads(payload))
    return chunks, done_seen


def test_run_footer_non_stream_success_includes_run_id(tmp_path):
    """非流式成功：持久化 run_id 以顶层 run_footer 键透出，Open-WebUI 依赖的既有字段不回退。"""
    chat_service = _RunFooterChatService(result=_run_footer_result(run_id="run-abc-123"))
    client = _run_footer_client(chat_service)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "s-footer"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-abc-123"
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert body["model"] == "N-Agent"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert body["usage"] == {"total_tokens": 3}


def test_run_footer_non_stream_omits_run_id_without_identity(tmp_path):
    """无账本/无身份：run_id 键整体缺省，不输出空值。"""
    chat_service = _RunFooterChatService(result=_run_footer_result(run_id=None))
    client = _run_footer_client(chat_service)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "s-footer"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert "run_id" not in response.json()


def test_run_footer_non_stream_error_keeps_http_error_contract(tmp_path):
    """非流式异常：保留既有 500 状态与错误结构，不变成成功响应，也不透出 run_id。"""
    chat_service = _RunFooterChatService(
        result=_run_footer_result(run_id="run-abc-123", finish_reason="error"),
    )
    client = _run_footer_client(chat_service)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "s-footer"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "server_error"
    assert "run_id" not in body


def test_run_footer_stream_message_done_carries_run_id_and_mid_stream_does_not(tmp_path):
    """流式：仅 MESSAGE_DONE 顶层携带 run_id；中途事件 metadata 即使被人为注入也不输出。"""
    chat_service = _RunFooterChatService(events=[
        ChatEvent(ChatEventType.MESSAGE_START),
        ChatEvent(ChatEventType.CONTENT_DELTA, content="he", metadata={"run_id": "run-abc-123"}),
        ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop", metadata={"run_id": "run-abc-123"}),
        ChatEvent(ChatEventType.DONE),
    ])
    text = _run_footer_stream_text(chat_service)

    chunks, done_seen = _parse_sse(text)
    assert done_seen is True
    by_finish_reason = {}
    for chunk in chunks:
        assert chunk["id"].startswith("chatcmpl-")
        assert chunk["object"] == "chat.completion.chunk"
        by_finish_reason.setdefault(chunk["choices"][0]["finish_reason"], []).append(chunk)
    # 中途 CONTENT_DELTA chunk（finish_reason 为 None）不携带 run_id
    mid_chunks = by_finish_reason[None]
    assert any(c["choices"][0]["delta"] == {"content": "he"} for c in mid_chunks)
    for chunk in mid_chunks:
        assert "run_id" not in chunk
    # MESSAGE_DONE chunk 顶层携带 run_id
    done_chunks = by_finish_reason["stop"]
    assert len(done_chunks) == 1
    assert done_chunks[0]["run_id"] == "run-abc-123"
    # [DONE] 行保持原样
    assert "data: [DONE]" in text


def test_run_footer_stream_error_carries_run_id(tmp_path):
    """流式 ERROR：结束 chunk 顶层携带 run_id，finish_reason 保持 error。"""
    chat_service = _RunFooterChatService(events=[
        ChatEvent(ChatEventType.MESSAGE_START),
        ChatEvent(ChatEventType.ERROR, error="provider down", metadata={"run_id": "run-err-1"}),
        ChatEvent(ChatEventType.DONE),
    ])
    text = _run_footer_stream_text(chat_service)

    chunks, done_seen = _parse_sse(text)
    assert done_seen is True
    error_chunks = [c for c in chunks if c["choices"][0]["finish_reason"] == "error"]
    assert len(error_chunks) == 1
    assert error_chunks[0]["run_id"] == "run-err-1"
    assert error_chunks[0]["choices"][0]["delta"] == {"content": "provider down"}


def test_run_footer_stream_omits_run_id_without_identity(tmp_path):
    """流式无身份：MESSAGE_DONE 无 metadata 或 run_id 为空串时键缺省。"""
    chat_service = _RunFooterChatService(events=[
        ChatEvent(ChatEventType.MESSAGE_START),
        ChatEvent(ChatEventType.CONTENT_DELTA, content="he", metadata={"run_id": "run-abc-123"}),
        ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop"),
        ChatEvent(ChatEventType.DONE),
    ])
    text = _run_footer_stream_text(chat_service)
    chunks, _ = _parse_sse(text)
    assert all("run_id" not in chunk for chunk in chunks)

    chat_service_empty = _RunFooterChatService(events=[
        ChatEvent(ChatEventType.MESSAGE_START),
        ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop", metadata={"run_id": ""}),
        ChatEvent(ChatEventType.DONE),
    ])
    text_empty = _run_footer_stream_text(chat_service_empty)
    chunks_empty, _ = _parse_sse(text_empty)
    assert all("run_id" not in chunk for chunk in chunks_empty)
