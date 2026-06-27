# app/infrastructure/memory/external/http_client.py
from __future__ import annotations
import ipaddress
import json
import socket
import ssl
from typing import Any
from urllib import request as urlrequest
from urllib.parse import urlencode, urlparse

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_MAX_BYTES = 1024 * 1024  # 1MB

# RFC 2544 benchmark/proxy 网段：部分公开 SaaS API（如 mem0 云端 api.mem0.ai）解析到此网段，
# 对齐 web_fetch 工具的例外策略，允许放行
_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class ExternalMemoryHttpClient:
    """共享 HTTP 客户端：URL 安全校验 + timeout + 响应大小限制。"""

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes

    def _opener(self):
        ctx = ssl.create_default_context()
        return urlrequest.build_opener(urlrequest.HTTPSHandler(context=ctx))

    def _check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"unsupported scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise ValueError("missing hostname")
        try:
            infos = socket.getaddrinfo(parsed.hostname, None)
        except socket.gaierror as exc:
            raise ValueError(f"unresolvable host: {exc}") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip in _BENCHMARK_NETWORK:
                continue  # 放行公开 SaaS 代理网段（mem0 云端等）
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                raise ValueError(f"private/loopback/link-local IP blocked: {ip}")

    def _do(self, method: str, url: str, *, json_body: dict | None = None, headers: dict | None = None, query: dict | None = None) -> Any:
        final_url = url
        if query:
            sep = "&" if "?" in url else "?"
            final_url = url + sep + urlencode(query)
        self._check_url(final_url)
        data = None
        h = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        req = urlrequest.Request(final_url, data=data, method=method, headers=h)
        opener = self._opener()
        resp = opener.open(req, timeout=self._timeout)
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._max_bytes:
                    raise ValueError(f"response exceeds max_bytes={self._max_bytes}")
                chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            resp.close()
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return {"_raw": body.decode("utf-8", errors="replace")}

    def get(self, url: str, *, headers: dict | None = None, query: dict | None = None) -> Any:
        return self._do("GET", url, headers=headers, query=query)

    def post(self, url: str, *, json: dict | None = None, headers: dict | None = None, query: dict | None = None) -> Any:
        return self._do("POST", url, json_body=json, headers=headers, query=query)

    def put(self, url: str, *, json: dict | None = None, headers: dict | None = None, query: dict | None = None) -> Any:
        return self._do("PUT", url, json_body=json, headers=headers, query=query)

    def delete(self, url: str, *, headers: dict | None = None, query: dict | None = None) -> Any:
        return self._do("DELETE", url, headers=headers, query=query)
