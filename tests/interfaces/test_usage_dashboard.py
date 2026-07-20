from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_observations_page_returns_html(client):
    r = client.get("/observations/sessions")
    assert r.status_code == 200
    assert "<aside" in r.text
    assert 'id="app-sidebar"' in r.text


def test_usage_session_stats(client):
    r = client.post("/chat/sessions?session_id=sess-test")
    assert r.status_code == 200
    r = client.get("/chat/usage/sessions/sess-test")
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == "sess-test"
    assert "input_tokens" in data
    assert "api_call_count" in data
    assert "estimated_cost_usd" in data
    assert "normalized_tokens" in data
    assert isinstance(data["normalized_tokens"], int)


def test_usage_records_empty(client):
    r = client.post("/chat/sessions?session_id=sess-test2")
    assert r.status_code == 200
    r = client.get("/chat/usage/sessions/sess-test2/records")
    assert r.status_code == 200
    assert r.json() == []


def test_usage_record_payload_normalizes_unicode_escaped_tool_arguments():
    """观测详情中的历史 tool arguments 应以可读中文返回。"""
    from app.interfaces.http.usage_routes import _normalize_observation_payload

    payload = '{"role":"assistant","content":"","tool_calls":[{"function":{"name":"task_complete","arguments":"{\\"summary\\": \\"\\\\u5df2\\\\u5b8c\\\\u6210\\"}"}}]}'

    normalized = _normalize_observation_payload(payload)

    assert "\\\\u" not in normalized
    assert "已完成" in normalized


def test_usage_compressions_empty(client):
    r = client.post("/chat/sessions?session_id=sess-test3")
    assert r.status_code == 200
    r = client.get("/chat/usage/sessions/sess-test3/compressions")
    assert r.status_code == 200
    assert r.json() == []


def test_usage_compressions_includes_before_after_messages(client):
    """compressions endpoint should expose before_messages/after_messages
    fields so the frontend modal can render the comparison."""
    r = client.post("/chat/sessions?session_id=sess-cm-api")
    assert r.status_code == 200
    from app.main import build_application_services
    services = build_application_services()
    import asyncio
    asyncio.run(services.usage_service.record_compression(
        "sess-cm-api", before_tokens=100, after_tokens=20,
        before_messages='[{"role":"user","content":"hi"}]',
        after_messages='[{"role":"user","content":"[CONTEXT SUMMARY]: s"}]',
    ))
    r = client.get("/chat/usage/sessions/sess-cm-api/compressions")
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 1
    item = data[0]
    assert "before_messages" in item
    assert "after_messages" in item
    assert item["before_messages"] is not None
    assert item["after_messages"] is not None


def test_usage_breakdown(client):
    r = client.post("/chat/sessions?session_id=sess-test4")
    assert r.status_code == 200
    r = client.get("/chat/usage/sessions/sess-test4/breakdown")
    assert r.status_code == 200
    data = r.json()
    assert "system_prompt" in data
    assert "tool_definitions" in data
    assert "memory" in data
    assert "conversation" in data
    assert "total" in data
    assert data["system_prompt"] > 0


def test_usage_overview_empty(client):
    r = client.get("/chat/usage/overview")
    assert r.status_code == 200
    data = r.json()
    # DB may have sessions from other tests; only assert shape, not exact counts.
    assert "input_tokens" in data
    assert "output_tokens" in data
    assert "total_tokens" in data
    assert "normalized_tokens" in data
    assert "api_call_count" in data
    assert "session_count" in data
    assert "estimated_cost_usd" in data
    assert "cache_read_tokens" in data
    assert "cache_write_tokens" in data
    assert "reasoning_tokens" in data
    assert isinstance(data["session_count"], int)
    assert data["session_count"] >= 0
    assert isinstance(data["normalized_tokens"], int)


def test_usage_overview_after_session(client):
    r = client.post("/chat/sessions?session_id=ov-1")
    assert r.status_code == 200
    r = client.get("/chat/usage/overview")
    assert r.status_code == 200
    data = r.json()
    # at least one session exists in the DB; session_count >= 1
    assert data["session_count"] >= 1


def test_usage_sessions_pagination(client):
    # create some sessions to ensure pagination returns a list and total
    for i in range(3):
        client.post(f"/chat/sessions?session_id=pg-{i}")
    r = client.get("/chat/usage/sessions?page=1&page_size=2")
    assert r.status_code == 200
    data = r.json()
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert data["total"] >= 3
    assert isinstance(data["items"], list)
    assert len(data["items"]) <= 2
    if data["items"]:
        item = data["items"][0]
        assert "session_id" in item
        assert "title" in item
        assert "total_tokens" in item
        assert "normalized_tokens" in item
        assert "api_call_count" in item
        assert "turn_count" in item
        assert isinstance(item["turn_count"], int)


def test_usage_sessions_pagination_validates_params(client):
    # page < 1 -> 422
    r = client.get("/chat/usage/sessions?page=0&page_size=10")
    assert r.status_code == 422
    # page_size > 100 -> 422
    r = client.get("/chat/usage/sessions?page=1&page_size=200")
    assert r.status_code == 422


def test_observations_detail_shell_route(client):
    r = client.get("/observations/sessions/sess-detail")
    assert r.status_code == 200
    assert "<aside" in r.text
    assert 'id="app-sidebar"' in r.text
