from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.provider import ModelInfo
from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall
from app.domain.tool import ToolCallRequest, ToolExecutor, ToolResult, ToolResultStatus
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router


class _StubExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


class _StubProvider:
    def __init__(self, models=None):
        self._models = models if models is not None else [
            ModelInfo("real-1", "Real 1", "openai-compatible", True, True),
            ModelInfo("real-2", "Real 2", "openai-compatible", False, True),
        ]

    async def list_models(self):
        return list(self._models)

    async def supports_tools(self, model: str):
        return True

    async def chat(self, *args, **kwargs):
        raise NotImplementedError


def _default_health() -> dict:
    return {
        "provider": {"status": "ok"},
        "memory": {"status": "ok"},
        "knowledge": {"status": "disabled", "enabled": False},
    }


def _build_app(store, health=None, model_service=None):
    tool_service = ToolService(_StubExecutor(), builtin_tool_definitions())
    model_service = model_service or ModelService(_StubProvider(), "real-1")
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_dashboard_router(
        SessionService(store),
        tool_service,
        model_service,
        health or _default_health,
    ))
    return app


def test_chat_page_and_apis(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio

    async def seed():
        await store.create_session(ConversationSession(id="s1", title="S1"))
        await store.append_message("s1", ConversationMessage(role="user", content="hello"))
        await store.save_summary(Summary(session_id="s1", summary="summary"))
        await store.save_task_state(TaskState(session_id="s1", status="completed", iteration_count=1))
        await store.save_tool_call(ToolCall(id="call-1", session_id="s1", tool_name="calculator", arguments={}, status="success"))

    asyncio.run(seed())
    app = _build_app(store)
    client = TestClient(app)

    html = client.get("/chat")
    sessions = client.get("/chat/sessions")
    detail = client.get("/chat/sessions/s1")
    tool_calls = client.get("/chat/sessions/s1/tool-calls")

    assert html.status_code == 200
    assert "<aside" in html.text and "sidebar" in html.text
    assert "/static/app.js" in html.text
    assert sessions.json()[0]["id"] == "s1"
    assert detail.json()["summary"]["summary"] == "summary"
    assert tool_calls.json()[0]["tool_name"] == "calculator"


def test_chat_can_create_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.post("/chat/sessions?session_id=s2")

    assert response.status_code == 200
    assert response.json()["id"] == "s2"


def test_chat_tools_endpoint_returns_definitions(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.get("/chat/tools")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    names = [item["name"] for item in payload]
    assert "get_current_time" in names
    sample = next(item for item in payload if item["name"] == "calculator")
    assert "description" in sample and "risk_level" in sample
    assert "enabled" in sample and "input_schema" in sample


def test_chat_health_dependencies_endpoint(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    snapshot = {
        "provider": {"status": "ok", "base_url": "http://localhost:11434/v1", "model": "qwen2.5"},
        "memory": {"status": "ok", "path": str(tmp_path / "sessions.db")},
        "knowledge": {"status": "ok", "base_url": "http://localhost:8202", "enabled": True},
    }
    client = TestClient(_build_app(store, health=lambda: snapshot))

    response = client.get("/chat/health/dependencies")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"]["status"] == "ok"
    assert payload["memory"]["status"] == "ok"
    assert payload["knowledge"]["enabled"] is True


def test_admin_models_endpoint_returns_real_provider_models(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    provider = _StubProvider([
        ModelInfo("real-1", "Real 1", "openai-compatible", True, True),
        ModelInfo("real-2", "Real 2", "openai-compatible", False, True),
    ])
    model_service = ModelService(provider, "real-1")
    client = TestClient(_build_app(store, model_service=model_service))

    response = client.get("/chat/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["default_model"] == "real-1"
    assert isinstance(payload["data"], list) and len(payload["data"]) == 2

    by_id = {item["id"]: item for item in payload["data"]}
    assert set(by_id.keys()) == {"real-1", "real-2"}
    for item in payload["data"]:
        assert set(item.keys()) == {
            "id", "display_name", "provider", "supports_tools", "supports_streaming", "is_default",
        }
    assert by_id["real-1"]["is_default"] is True
    assert by_id["real-1"]["display_name"] == "Real 1"
    assert by_id["real-1"]["provider"] == "openai-compatible"
    assert by_id["real-1"]["supports_tools"] is True
    assert by_id["real-2"]["is_default"] is False
    assert by_id["real-2"]["supports_tools"] is False
