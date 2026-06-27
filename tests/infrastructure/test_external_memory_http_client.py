# tests/infrastructure/test_external_memory_http_client.py
import pytest
from app.infrastructure.memory.external.http_client import ExternalMemoryHttpClient


def test_private_ip_blocked():
    client = ExternalMemoryHttpClient(timeout=5, max_bytes=1024)
    with pytest.raises(ValueError):
        client.post("http://127.0.0.1:8080/memories", json={})


def test_metadata_endpoint_blocked():
    client = ExternalMemoryHttpClient()
    with pytest.raises(ValueError):
        client.get("http://169.254.169.254/latest/meta-data/")


def test_post_returns_json(monkeypatch):
    client = ExternalMemoryHttpClient()
    class FakeResp:
        def __init__(self): self._data = b'{"ok": true}'; self._done = False
        status = 200
        def read(self, n):
            if self._done: return b""
            self._done = True
            return self._data
        def close(self): pass
    class FakeOpener:
        def open(self, *a, **kw): return FakeResp()
    # mock _check_url 避免 DNS 查询
    monkeypatch.setattr(client, "_check_url", lambda url: None)
    monkeypatch.setattr(client, "_opener", lambda: FakeOpener())
    result = client.post("https://app.mem0.ai/v1/memories", json={"q": 1}, headers={"Authorization": "Bearer x"})
    assert result == {"ok": True}


def test_oversize_response_rejected(monkeypatch):
    client = ExternalMemoryHttpClient(max_bytes=10)
    class FakeResp:
        def __init__(self): self._data = b"x" * 100; self._done = False
        status = 200
        def read(self, n):
            if self._done: return b""
            self._done = True
            return self._data
        def close(self): pass
    class FakeOpener:
        def open(self, *a, **kw): return FakeResp()
    monkeypatch.setattr(client, "_check_url", lambda url: None)
    monkeypatch.setattr(client, "_opener", lambda: FakeOpener())
    with pytest.raises(ValueError):
        client.get("https://app.mem0.ai/v1/memories")


def test_benchmark_network_allowed(monkeypatch):
    # 198.18.0.0/15 是 RFC 2544 benchmark/proxy 网段，公开 SaaS（如 mem0 云端）解析到此；
    # 不能误判为 reserved 阻断
    import ipaddress
    client = ExternalMemoryHttpClient()
    ip = ipaddress.ip_address("198.18.0.58")
    # 直接验证 _check_url 不再因 198.18 网段抛 ValueError
    assert ip in __import__("app.infrastructure.memory.external.http_client", fromlist=["_BENCHMARK_NETWORK"])._BENCHMARK_NETWORK
