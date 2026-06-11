from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.session_service import SessionService
from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.http.dashboard import create_dashboard_router


def test_chat_page_and_apis(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "agent.db")
    import asyncio

    async def seed():
        await store.create_session(ConversationSession(id="s1", title="S1"))
        await store.append_message("s1", ConversationMessage(role="user", content="hello"))
        await store.save_summary(Summary(session_id="s1", summary="summary"))
        await store.save_task_state(TaskState(session_id="s1", status="completed", iteration_count=1))
        await store.save_tool_call(ToolCall(id="call-1", session_id="s1", tool_name="calculator", arguments={}, status="success"))

    asyncio.run(seed())
    app = FastAPI()
    app.include_router(create_dashboard_router(SessionService(store)))
    client = TestClient(app)

    html = client.get("/chat")
    sessions = client.get("/chat/sessions")
    detail = client.get("/chat/sessions/s1")
    tool_calls = client.get("/chat/sessions/s1/tool-calls")

    assert html.status_code == 200
    assert "/v1/chat/completions" in html.text
    assert "metadata" in html.text
    assert sessions.json()[0]["id"] == "s1"
    assert detail.json()["summary"]["summary"] == "summary"
    assert tool_calls.json()[0]["tool_name"] == "calculator"


def test_chat_can_create_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "agent.db")
    app = FastAPI()
    app.include_router(create_dashboard_router(SessionService(store)))
    client = TestClient(app)

    response = client.post("/chat/sessions?session_id=s2")

    assert response.status_code == 200
    assert response.json()["id"] == "s2"


def test_chat_send_creates_session_when_none_selected(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "agent.db")
    app = FastAPI()
    app.include_router(create_dashboard_router(SessionService(store)))
    client = TestClient(app)

    html = client.get("/chat")

    assert "async function ensureSession()" in html.text
    assert "await ensureSession();" in html.text
    assert "if (!text) return;" in html.text
