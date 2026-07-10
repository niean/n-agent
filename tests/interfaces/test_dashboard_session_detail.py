from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.context import CONTEXT_SUMMARY_PREFIX
from app.domain.provider import ModelInfo
from app.domain.session import ConversationMessage, ConversationSession
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router


class _StubExecutor:
    async def execute(self, request):
        from app.domain.tool import ToolResult, ToolResultStatus
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


class _StubProvider:
    async def list_models(self):
        return [ModelInfo("real-1", "Real 1", "openai-compatible", True, True)]

    async def supports_tools(self, model):
        return True

    async def chat(self, *args, **kwargs):
        raise NotImplementedError


def _build_app(store):
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_dashboard_router(
        SessionService(store),
        ToolService(_StubExecutor(), builtin_tool_definitions()),
        ModelService(_StubProvider(), "real-1"),
        {"provider": {"status": "ok"}, "memory": {"status": "ok"},
         "knowledge": {"status": "disabled", "enabled": False}},
    ))
    return app


def test_session_detail_response_includes_is_summary_field(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")

    async def seed():
        await store.create_session(ConversationSession(id="s1", title="t"))
        await store.append_message("s1", ConversationMessage(role="user", content="plain"))
        await store.append_message(
            "s1",
            ConversationMessage(
                role="user",
                content=f"{CONTEXT_SUMMARY_PREFIX}S1",
                is_summary=True,
            ),
        )

    asyncio.run(seed())
    client = TestClient(_build_app(store))
    resp = client.get("/chat/sessions/s1")
    assert resp.status_code == 200
    data = resp.json()
    msgs = data["messages"]
    assert len(msgs) == 2
    assert msgs[0]["is_summary"] is False
    assert msgs[1]["is_summary"] is True
