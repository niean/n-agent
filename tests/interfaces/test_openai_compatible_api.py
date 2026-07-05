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

    async def list_models(self):
        return [ModelInfo("test-model", "test-model", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
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
