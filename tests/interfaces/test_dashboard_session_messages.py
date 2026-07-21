from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.provider import ModelInfo
from app.domain.session import ConversationSession
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


def _client_with_session(tmp_path, session_id="s1"):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")

    async def seed():
        await store.create_session(ConversationSession(id=session_id))

    asyncio.run(seed())
    return TestClient(_build_app(store)), store


def test_post_session_message_201(tmp_path):
    client, _ = _client_with_session(tmp_path)
    r = client.post("/chat/sessions/s1/messages", json={"content": "[任务指令] 执行命令: /task list"})
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "system"
    assert body["name"] == "ui.task_command"
    assert body["content"].startswith("[任务指令]")


def test_post_session_message_404_when_absent(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))
    r = client.post("/chat/sessions/no-such/messages", json={"content": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


def test_post_session_message_422_extra_field(tmp_path):
    client, _ = _client_with_session(tmp_path)
    r = client.post("/chat/sessions/s1/messages", json={"content": "x", "role": "user"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "session_message_invalid"


def test_post_session_message_422_missing_field(tmp_path):
    client, _ = _client_with_session(tmp_path)
    r = client.post("/chat/sessions/s1/messages", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "session_message_invalid"


def test_post_session_message_422_blank(tmp_path):
    client, _ = _client_with_session(tmp_path)
    r = client.post("/chat/sessions/s1/messages", json={"content": "   "})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "session_message_invalid"


def test_post_session_message_422_oversize(tmp_path):
    client, _ = _client_with_session(tmp_path)
    r = client.post("/chat/sessions/s1/messages", json={"content": "a" * 65537})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "session_message_invalid"


def test_post_session_message_422_malformed_json(tmp_path):
    client, _ = _client_with_session(tmp_path)
    r = client.post(
        "/chat/sessions/s1/messages",
        content="not json{",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "session_message_invalid"


def test_post_session_message_422_wrong_content_type(tmp_path):
    """错误 Content-Type（即便 body 是合法 JSON）-> 422 session_message_invalid，零写入。"""
    client, store = _client_with_session(tmp_path)
    r = client.post(
        "/chat/sessions/s1/messages",
        content='{"content":"x"}',
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "session_message_invalid"
    # 零写入
    assert asyncio.run(store.list_messages("s1")) == []


def test_post_session_message_422_missing_content_type(tmp_path):
    client, store = _client_with_session(tmp_path)
    r = client.post(
        "/chat/sessions/s1/messages",
        content='{"content":"x"}',
        headers={"Content-Type": ""},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "session_message_invalid"


def test_post_session_message_boundary_65536_ok(tmp_path):
    client, _ = _client_with_session(tmp_path)
    r = client.post("/chat/sessions/s1/messages", json={"content": "a" * 65536})
    assert r.status_code == 201


def test_message_to_dict_normalizes_tool_call_arguments_unicode_escapes():
    """tool_call arguments 的 \\uXXXX 转义归一化为可读中文（部分 provider 返回转义）。"""
    from app.domain.session import ConversationMessage
    from app.interfaces.http.dashboard import _message_to_dict

    msg = ConversationMessage(
        role="assistant",
        content={
            "content": "",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "task_complete", "arguments": '{"summary": "\\u5df2\\u6210\\u529f"}'},
            }],
        },
    )
    data = _message_to_dict(msg)
    args = data["tool_calls"][0]["function"]["arguments"]
    assert "\\u" not in args  # 无转义
    assert "已成功" in args  # 可读中文


def test_normalize_tool_call_arguments_handles_non_string_and_invalid_json():
    """非字符串/非法 JSON 原样返回（best-effort）。"""
    from app.interfaces.http.dashboard import _normalize_tool_call_arguments

    assert _normalize_tool_call_arguments({"a": 1}) == {"a": 1}  # dict 原样
    assert _normalize_tool_call_arguments(None) is None
    assert _normalize_tool_call_arguments("not json") == "not json"  # 非法 JSON 原样
    assert _normalize_tool_call_arguments('{"x": 1}') == '{"x": 1}'  # 合法 JSON 保持


def test_message_to_dict_includes_card_field():
    from app.domain.session import ConversationMessage
    from app.interfaces.http.dashboard import _message_to_dict

    card = {"schema_version": 1, "kind": "task_lifecycle", "task_id": "t1",
            "status": "waiting_approval", "title": "T", "summary": "p",
            "available_actions": ["approve"]}
    msg = ConversationMessage(role="system", content="等待批准", name="ui.task_lifecycle", card=card)
    data = _message_to_dict(msg)
    assert data["card"] == card


def test_message_to_dict_card_null_when_absent():
    from app.domain.session import ConversationMessage
    from app.interfaces.http.dashboard import _message_to_dict

    msg = ConversationMessage(role="user", content="hi")
    assert _message_to_dict(msg)["card"] is None
