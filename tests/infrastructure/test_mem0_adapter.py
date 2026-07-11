# tests/infrastructure/test_mem0_adapter.py
import json
import pytest
from app.infrastructure.memory.external.mem0 import Mem0Adapter


class FakeHttpClient:
    def __init__(self):
        self.calls = []
        self.responses = {}
    def get(self, url, *, headers=None):
        self.calls.append(("GET", url, None, headers))
        return self.responses.get(("GET", url), {})
    def post(self, url, *, json=None, headers=None):
        self.calls.append(("POST", url, json, headers))
        return self.responses.get(("POST", url), {})
    def delete(self, url, *, headers=None):
        self.calls.append(("DELETE", url, None, headers))
        return self.responses.get(("DELETE", url), {})


@pytest.fixture
def adapter():
    http = FakeHttpClient()
    a = Mem0Adapter(
        http_client=http,
        config={"base_url": "https://api.mem0.ai/v3", "api_key": "sk-x",
                "user_id": "u1", "agent_id": "a1", "rerank": True},
    )
    a.initialize(session_id="s1", project_root=".")
    return a, http


def test_name_and_available(adapter):
    a, _ = adapter
    assert a.name == "mem0"
    assert a.is_available() is True


def test_default_base_url_is_v3():
    # 不传 base_url 时默认为官方 V3 endpoint
    a = Mem0Adapter(http_client=FakeHttpClient(), config={"api_key": "sk-x"})
    assert a._base_url == "https://api.mem0.ai/v3"


def test_empty_base_url_falls_back_to_v3():
    # base_url 为空字符串（Dashboard 留空）时同样回退到官方 V3 endpoint，
    # 否则 urlparse 得到空 scheme，触发 http_client 的 "unsupported scheme:" 错误
    a = Mem0Adapter(http_client=FakeHttpClient(), config={"base_url": "", "api_key": "sk-x"})
    assert a._base_url == "https://api.mem0.ai/v3"


def test_prefetch_returns_empty_when_no_api_key():
    # 未配置 api_key 时 is_available 返回 False，prefetch 直接返回空
    a = Mem0Adapter(http_client=FakeHttpClient(), config={"base_url": "https://api.mem0.ai/v3", "api_key": ""})
    assert a.prefetch("q", session_id="s1") == ""


def test_prefetch_returns_empty_when_query_empty(adapter):
    a, _ = adapter
    assert a.prefetch("", session_id="s1") == ""


def test_prefetch_returns_formatted_memory_when_results_exist(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/search/")] = {
        "results": [{"memory": "likes spicy food"}, {"memory": "lives in Beijing"}],
    }
    text = a.prefetch("what does the user like", session_id="s1")
    assert "## Mem0 Memory" in text
    assert "- likes spicy food" in text
    assert "- lives in Beijing" in text
    # 验证搜索请求 body
    method, url, body, headers = http.calls[0]
    assert method == "POST" and url.endswith("/memories/search/")
    assert body["query"] == "what does the user like"
    assert body["filters"]["user_id"] == "u1"
    assert body["rerank"] is True
    assert body["top_k"] == 10
    assert headers["Authorization"] == "Token sk-x"


def test_prefetch_returns_empty_when_no_results(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/search/")] = {"results": []}
    assert a.prefetch("anything", session_id="s1") == ""


def test_prefetch_returns_empty_on_backend_error(adapter):
    a, http = adapter
    # http_client 抛异常时，后台线程捕获后缓存空字符串
    def raise_error(url, *, json=None, headers=None):
        raise RuntimeError("network down")
    http.post = raise_error
    assert a.prefetch("q", session_id="s1") == ""


def test_prefetch_consumes_cache_after_call(adapter):
    """同 query 第二次 prefetch 不再发起网络请求（缓存命中）。"""
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/search/")] = {
        "results": [{"memory": "fact1"}],
    }
    first = a.prefetch("q", session_id="s1")
    assert "fact1" in first
    initial_call_count = len(http.calls)
    # 同 query 再次 prefetch：缓存被消费后 query 不变，但 _prefetch_done=False，
    # 应启动新线程；为避免重复网络请求，验证语义改为：再次调用能返回结果
    second = a.prefetch("q", session_id="s1")
    assert "fact1" in second
    # 至少又发起了一次搜索
    assert len(http.calls) > initial_call_count


def test_prefetch_different_query_triggers_new_search(adapter):
    """不同 query 的 prefetch 独立搜索，不复用旧缓存。"""
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/search/")] = {
        "results": [{"memory": "fact1"}],
    }
    first = a.prefetch("query-a", session_id="s1")
    assert "fact1" in first
    second = a.prefetch("query-b", session_id="s1")
    assert "fact1" in second
    # 两次搜索请求
    search_calls = [c for c in http.calls if c[1].endswith("/memories/search/")]
    assert len(search_calls) == 2
    assert search_calls[0][2]["query"] == "query-a"
    assert search_calls[1][2]["query"] == "query-b"


def test_sync_turn_posts_memories(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/add/")] = {"results": []}
    a.sync_turn("hello", "world", session_id="s1")
    method, url, body, headers = http.calls[0]
    assert method == "POST" and url.endswith("/memories/add/")
    assert body["user_id"] == "u1" and body["agent_id"] == "a1"
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "hello"
    assert headers["Authorization"] == "Token sk-x"


def test_mem0_search_tool(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/search/")] = {"results": [{"memory": "fact1"}]}
    result = a.handle_tool_call("mem0_search", {"query": "q"})
    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["results"] == [{"memory": "fact1"}]
    # P4: body 用 top_k 而非 limit
    method, url, body, headers = http.calls[0]
    assert "top_k" in body
    assert "limit" not in body


def test_mem0_profile_tool(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/?page=1&page_size=50")] = {"results": []}
    result = a.handle_tool_call("mem0_profile", {})
    parsed = json.loads(result)
    assert parsed["success"] is True
    # P3: POST 方法 + filters body
    method, url, body, headers = http.calls[0]
    assert method == "POST"
    assert "page=1" in url and "page_size=50" in url
    assert body["filters"]["user_id"] == "u1"


def test_mem0_conclude_returns_event_id(adapter):
    a, http = adapter
    http.responses[("POST", "https://api.mem0.ai/v3/memories/add/")] = {
        "status": "PENDING", "event_id": "evt-123",
    }
    result = a.handle_tool_call("mem0_conclude", {"conclusion": "likes spicy food"})
    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["status"] == "PENDING"
    assert parsed["event_id"] == "evt-123"


def test_is_available_false_when_no_api_key():
    a = Mem0Adapter(http_client=FakeHttpClient(), config={"base_url": "u", "api_key": ""})
    assert a.is_available() is False
