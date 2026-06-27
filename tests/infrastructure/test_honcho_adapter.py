# tests/infrastructure/test_honcho_adapter.py
import json
import pytest
from app.infrastructure.memory.external.honcho import HonchoAdapter


class FakeHttpClient:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def get(self, url, *, headers=None, query=None):
        self.calls.append(("GET", url, None, headers, query))
        key = ("GET", url)
        if key in self.responses:
            v = self.responses[key]
            if isinstance(v, Exception):
                raise v
            return v
        return {}

    def post(self, url, *, json=None, headers=None, query=None):
        self.calls.append(("POST", url, json, headers, query))
        key = ("POST", url)
        if key in self.responses:
            v = self.responses[key]
            if isinstance(v, Exception):
                raise v
            return v
        return {}

    def put(self, url, *, json=None, headers=None, query=None):
        self.calls.append(("PUT", url, json, headers, query))
        key = ("PUT", url)
        if key in self.responses:
            v = self.responses[key]
            if isinstance(v, Exception):
                raise v
            return v
        return {}

    def delete(self, url, *, headers=None, query=None):
        self.calls.append(("DELETE", url, None, headers, query))
        key = ("DELETE", url)
        if key in self.responses:
            v = self.responses[key]
            if isinstance(v, Exception):
                raise v
            return v
        return {}


@pytest.fixture
def adapter():
    http = FakeHttpClient()
    a = HonchoAdapter(
        http_client=http,
        config={
            "base_url": "https://api.honcho.dev",
            "api_key": "sk-x",
            "workspace_id": "ws1",
            "user_id": "u1",
            "ai_peer_id": "n-agent",
            "session_strategy": "per-session",
            "recall_mode": "hybrid",
        },
    )
    a.initialize(session_id="s1", project_root=".")
    return a, http


def _seed_ensure(http, base="https://api.honcho.dev"):
    http.responses[("POST", f"{base}/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", f"{base}/v3/workspaces/ws1/peers")] = {"id": "x"}
    http.responses[("POST", f"{base}/v3/workspaces/ws1/sessions")] = {"id": "s1"}


# T1: 配置与可用性

def test_name_and_available(adapter):
    a, _ = adapter
    assert a.name == "honcho"
    assert a.is_available() is True


def test_is_available_false_when_missing_workspace_id():
    http = FakeHttpClient()
    a = HonchoAdapter(http_client=http, config={
        "base_url": "https://api.honcho.dev", "api_key": "sk-x",
        "user_id": "u1", "session_strategy": "per-session", "recall_mode": "hybrid",
    })
    assert a.is_available() is False


def test_is_available_false_when_missing_api_key():
    http = FakeHttpClient()
    a = HonchoAdapter(http_client=http, config={
        "base_url": "https://api.honcho.dev", "workspace_id": "ws1",
        "user_id": "u1", "session_strategy": "per-session", "recall_mode": "hybrid",
    })
    assert a.is_available() is False


# T2: ensure 资源

def test_ensure_workspace_posts_once(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    a._ensure_workspace()
    ws_calls = [c for c in http.calls if c[1].endswith("/v3/workspaces")]
    assert len(ws_calls) == 1
    assert ws_calls[0][2] == {"id": "ws1", "name": "ws1"}
    a._ensure_workspace()
    ws_calls2 = [c for c in http.calls if c[1].endswith("/v3/workspaces")]
    assert len(ws_calls2) == 1


def test_ensure_peers_posts_both(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces/ws1/peers")] = {"id": "x"}
    a._ensure_peers()
    peer_calls = [c for c in http.calls if c[1].endswith("/v3/workspaces/ws1/peers")]
    assert len(peer_calls) == 2
    peer_ids = [c[2]["id"] for c in peer_calls]
    assert "u1" in peer_ids and "n-agent" in peer_ids


def test_ensure_session_posts_session_with_peers(adapter):
    a, http = adapter
    _seed_ensure(http)
    a._ensure_session("s1")
    sess_calls = [c for c in http.calls if c[1].endswith("/v3/workspaces/ws1/sessions")]
    assert len(sess_calls) == 1
    body = sess_calls[0][2]
    assert body["id"] == "s1"
    assert "u1" in body["peers"] and "n-agent" in body["peers"]


def test_ensure_workspace_swallows_existing(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = Exception("already exists")
    a._ensure_workspace()  # 不应抛出


# T3: prefetch + sync_turn

def test_prefetch_hybrid_calls_v3_context(adapter):
    a, http = adapter
    _seed_ensure(http)
    ctx_url = "https://api.honcho.dev/v3/workspaces/ws1/sessions/s1/context"
    http.responses[("GET", ctx_url)] = {
        "summary": "sm", "peer_representation": "rep", "peer_card": ["c1"],
        "messages": [{"content": "m1", "peer_id": "u1"}],
    }
    result = a.prefetch("q", session_id="s1")
    assert "sm" in result and "rep" in result and "c1" in result and "m1" in result
    get_calls = [c for c in http.calls if c[0] == "GET" and "/v3/workspaces/ws1/sessions/s1/context" in c[1]]
    assert len(get_calls) == 1
    query = get_calls[0][4]
    assert query is not None and query.get("peer_target") == "u1" and query.get("summary") == "true"


def test_prefetch_tools_mode_returns_empty():
    http = FakeHttpClient()
    a = HonchoAdapter(http_client=http, config={
        "base_url": "https://api.honcho.dev", "api_key": "k", "workspace_id": "ws1",
        "user_id": "u1", "session_strategy": "per-session", "recall_mode": "tools",
    })
    a.initialize(session_id="s1", project_root=".")
    assert a.prefetch("q", session_id="s1") == ""


def test_prefetch_swallows_error(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = Exception("net")
    assert a.prefetch("q", session_id="s1") == ""


def test_sync_turn_posts_batched_messages(adapter):
    a, http = adapter
    _seed_ensure(http)
    msg_url = "https://api.honcho.dev/v3/workspaces/ws1/sessions/s1/messages"
    http.responses[("POST", msg_url)] = []
    a.sync_turn("hi", "hello", session_id="s1")
    msg_calls = [c for c in http.calls if c[1] == msg_url]
    assert len(msg_calls) == 1
    body = msg_calls[0][2]
    assert body["messages"] == [
        {"content": "hi", "peer_id": "u1"},
        {"content": "hello", "peer_id": "n-agent"},
    ]
    assert msg_calls[0][3]["Authorization"] == "Bearer sk-x"


def test_sync_turn_swallows_error(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = Exception("net")
    a.sync_turn("hi", "hello", session_id="s1")  # 不应抛出


# T4: get_tool_schemas + handle_tool_call

def test_get_tool_schemas_hybrid(adapter):
    a, _ = adapter
    names = [s["name"] for s in a.get_tool_schemas()]
    assert names == ["honcho_profile", "honcho_search", "honcho_reasoning", "honcho_context", "honcho_conclude"]


def test_get_tool_schemas_context_mode_empty():
    http = FakeHttpClient()
    a = HonchoAdapter(http_client=http, config={
        "base_url": "u", "api_key": "k", "workspace_id": "ws1",
        "user_id": "u1", "session_strategy": "per-session", "recall_mode": "context",
    })
    assert a.get_tool_schemas() == []


def test_honcho_profile_get(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces/ws1/peers")] = {"id": "x"}
    card_url = "https://api.honcho.dev/v3/workspaces/ws1/peers/u1/card"
    http.responses[("GET", card_url)] = {"peer_card": ["fact1"]}
    parsed = json.loads(a.handle_tool_call("honcho_profile", {"peer": "u1"}))
    assert parsed["success"] is True
    assert parsed["card"] == ["fact1"]


def test_honcho_profile_put(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces/ws1/peers")] = {"id": "x"}
    card_url = "https://api.honcho.dev/v3/workspaces/ws1/peers/u1/card"
    http.responses[("PUT", card_url)] = {"peer_card": ["new"]}
    parsed = json.loads(a.handle_tool_call("honcho_profile", {"peer": "u1", "card": ["new"]}))
    assert parsed["success"] is True
    put_calls = [c for c in http.calls if c[0] == "PUT" and c[1] == card_url]
    assert put_calls[0][2] == {"peer_card": ["new"]}


def test_honcho_search(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces/ws1/peers")] = {"id": "x"}
    search_url = "https://api.honcho.dev/v3/workspaces/ws1/peers/u1/search"
    http.responses[("POST", search_url)] = [{"content": "r1", "peer_id": "u1"}]
    parsed = json.loads(a.handle_tool_call("honcho_search", {"query": "q"}))
    assert parsed["success"] is True
    assert parsed["results"] == [{"content": "r1", "peer_id": "u1"}]
    body = [c for c in http.calls if c[1] == search_url][0][2]
    assert body == {"query": "q", "filters": None, "limit": 10}


def test_honcho_reasoning(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces/ws1/peers")] = {"id": "x"}
    chat_url = "https://api.honcho.dev/v3/workspaces/ws1/peers/u1/chat"
    http.responses[("POST", chat_url)] = {"content": "answer"}
    parsed = json.loads(a.handle_tool_call("honcho_reasoning", {"query": "q", "reasoning_level": "high"}))
    assert parsed["success"] is True
    assert parsed["result"] == "answer"
    body = [c for c in http.calls if c[1] == chat_url][0][2]
    assert body == {"query": "q", "stream": False, "reasoning_level": "high"}


def test_honcho_context(adapter):
    a, http = adapter
    _seed_ensure(http)
    ctx_url = "https://api.honcho.dev/v3/workspaces/ws1/sessions/s1/context"
    http.responses[("GET", ctx_url)] = {"summary": "sm"}
    parsed = json.loads(a.handle_tool_call("honcho_context", {}))
    assert parsed["success"] is True
    assert parsed["result"]["summary"] == "sm"


def test_honcho_conclude_create(adapter):
    a, http = adapter
    _seed_ensure(http)
    cc_url = "https://api.honcho.dev/v3/workspaces/ws1/conclusions"
    http.responses[("POST", cc_url)] = [{"id": "c1", "content": "x"}]
    parsed = json.loads(a.handle_tool_call("honcho_conclude", {"conclusion": "user likes tea"}))
    assert parsed["success"] is True
    body = [c for c in http.calls if c[1] == cc_url][0][2]
    assert body["conclusions"] == [{
        "content": "user likes tea",
        "observer_id": "u1",
        "observed_id": "u1",
        "session_id": "s1",
    }]


def test_honcho_conclude_query(adapter):
    a, http = adapter
    _seed_ensure(http)
    q_url = "https://api.honcho.dev/v3/workspaces/ws1/conclusions/query"
    http.responses[("POST", q_url)] = [{"id": "c1", "content": "x"}]
    parsed = json.loads(a.handle_tool_call("honcho_conclude", {"query": "preferences"}))
    assert parsed["success"] is True
    body = [c for c in http.calls if c[1] == q_url][0][2]
    assert body == {"query": "preferences"}


def test_honcho_conclude_delete(adapter):
    a, http = adapter
    _seed_ensure(http)
    del_url = "https://api.honcho.dev/v3/workspaces/ws1/conclusions/c1"
    parsed = json.loads(a.handle_tool_call("honcho_conclude", {"delete_id": "c1"}))
    assert parsed["success"] is True
    del_calls = [c for c in http.calls if c[0] == "DELETE" and c[1] == del_url]
    assert len(del_calls) == 1


def test_honcho_unknown_tool(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces/ws1/peers")] = {"id": "x"}
    parsed = json.loads(a.handle_tool_call("bogus", {}))
    assert parsed["success"] is False
    assert "unknown tool" in parsed["error"]


def test_honcho_tool_swallows_error(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces")] = {"id": "ws1"}
    http.responses[("POST", "https://api.honcho.dev/v3/workspaces/ws1/peers")] = {"id": "x"}
    card_url = "https://api.honcho.dev/v3/workspaces/ws1/peers/u1/card"
    http.responses[("GET", card_url)] = Exception("net error")
    parsed = json.loads(a.handle_tool_call("honcho_profile", {"peer": "u1"}))
    assert parsed["success"] is False
    assert parsed["error"] == "Exception"


# T5: probe

def test_probe_success(adapter):
    a, http = adapter
    ctx_url = "https://api.honcho.dev/v3/workspaces/ws1/sessions/probe/context"
    http.responses[("GET", ctx_url)] = {"summary": ""}
    parsed = json.loads(a.probe())
    assert parsed["success"] is True
    # probe 不应触发 ensure_session（不 POST sessions）
    sess_calls = [c for c in http.calls if c[0] == "POST" and c[1].endswith("/v3/workspaces/ws1/sessions")]
    assert len(sess_calls) == 0


def test_probe_failure_on_auth_error(adapter):
    a, http = adapter
    ctx_url = "https://api.honcho.dev/v3/workspaces/ws1/sessions/probe/context"
    http.responses[("GET", ctx_url)] = Exception("401 Unauthorized")
    parsed = json.loads(a.probe())
    assert parsed["success"] is False
    assert "401" in parsed["error"]


def test_probe_failure_when_not_configured():
    http = FakeHttpClient()
    a = HonchoAdapter(http_client=http, config={
        "base_url": "", "api_key": "", "workspace_id": "",
        "user_id": "u1", "session_strategy": "per-session", "recall_mode": "hybrid",
    })
    parsed = json.loads(a.probe())
    assert parsed["success"] is False
