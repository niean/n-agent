from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import httpx
import pytest

from app.domain.host_terminal import (
    HostCommandTarget,
    HostTerminalBridgeRequest,
    HostTerminalExecutionLimits,
    HostTerminalStatus,
)
from app.infrastructure.host_terminal.http_client import (
    AUTH_HEADER,
    HostTerminalBridgeClientError,
    HostTerminalHttpClient,
    HostTerminalHttpClientConfig,
    load_secure_token,
)


TOKEN = "t" * 32


def _request() -> HostTerminalBridgeRequest:
    return HostTerminalBridgeRequest(
        protocol_version="1",
        request_id="req-1",
        target=HostCommandTarget("/bin/echo", ("hello",)),
        n_agent_policy_version="v1",
        n_agent_content_digest="a" * 64,
        limits=HostTerminalExecutionLimits(2, 100, 100, 1),
    )


def _envelope(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "request_id": "req-1",
        "status": "success",
        "exit_code": 0,
        "stdout": "hello\n",
        "stderr": "",
        "duration_ms": 1,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "error_code": None,
    }
    result.update(updates)
    return result


@pytest.mark.parametrize(
    "base_url",
    [
        "https://host.docker.internal:8765",
        "http://host.docker.internal",
        "http://host.docker.internal:0",
        "http://host.docker.internal:8765/path",
        "http://host.docker.internal:8765?query=1",
        "http://host.docker.internal:8765#fragment",
        "http://user@host.docker.internal:8765",
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "http://bridge.example:8765",
        " http://host.docker.internal:8765",
        "http://host.docker.internal:8765 ",
        "HTTP://host.docker.internal:8765",
        "http://HOST.DOCKER.INTERNAL:8765",
        "http://host.docker.internal:08765",
        "http://host.docker.internal:8765//",
    ],
)
def test_rejects_every_url_outside_exact_local_docker_route(base_url: str) -> None:
    with pytest.raises(ValueError, match="host_bridge_url_invalid"):
        HostTerminalHttpClientConfig(base_url, token=TOKEN)


@pytest.mark.parametrize(
    "base_url",
    ["http://host.docker.internal:8765", "http://host.docker.internal:8765/"],
)
def test_accepts_only_exact_local_docker_route(base_url: str) -> None:
    HostTerminalHttpClientConfig(base_url, token=TOKEN)


@pytest.mark.asyncio
async def test_sends_dedicated_auth_and_fixed_policy_envelope() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers[AUTH_HEADER] == TOKEN
        payload = json.loads(request.content)
        assert payload["request_id"] == "req-1"
        assert payload["n_agent_policy_version"] == "v1"
        assert payload["n_agent_content_digest"] == "a" * 64
        assert payload["target"] == {
            "type": "command",
            "executable": "/bin/echo",
            "args": ["hello"],
        }
        return httpx.Response(200, json=_envelope())

    client = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765",
            token=TOKEN,
            transport=httpx.MockTransport(handler),
        )
    )
    response = await client.execute(_request())
    assert response.status is HostTerminalStatus.SUCCESS
    assert response.duration_ms == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={**_envelope(), "unknown": True}),
        httpx.Response(200, json=_envelope(request_id="wrong")),
        httpx.Response(200, json=_envelope(duration_ms=True)),
        httpx.Response(200, json=_envelope(duration_ms=-1)),
        httpx.Response(
            200,
            json={key: value for key, value in _envelope().items() if key != "duration_ms"},
        ),
    ],
)
async def test_maps_malformed_response_to_stable_error(response: httpx.Response) -> None:
    client = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765",
            token=TOKEN,
            transport=httpx.MockTransport(lambda request: response),
        )
    )
    with pytest.raises(HostTerminalBridgeClientError) as exc:
        await client.execute(_request())
    assert exc.value.error_code == "host_bridge_invalid_response"
    assert TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_auth_and_network_errors_are_stable() -> None:
    auth_client = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765",
            token=TOKEN,
            transport=httpx.MockTransport(lambda request: httpx.Response(401)),
        )
    )
    with pytest.raises(HostTerminalBridgeClientError) as auth:
        await auth_client.execute(_request())
    assert auth.value.error_code == "host_bridge_auth_failed"

    async def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-free")

    network_client = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765", token=TOKEN, transport=httpx.MockTransport(fail)
        )
    )
    with pytest.raises(HostTerminalBridgeClientError) as unavailable:
        await network_client.execute(_request())
    assert unavailable.value.error_code == "host_bridge_unavailable"


@pytest.mark.asyncio
async def test_preserves_authenticated_bridge_error_envelope() -> None:
    client = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765",
            token=TOKEN,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    409,
                    json=_envelope(
                        status="error",
                        exit_code=None,
                        stdout="",
                        error_code="host_bridge_busy",
                    ),
                )
            ),
        )
    )
    response = await client.execute(_request())
    assert response.status is HostTerminalStatus.ERROR
    assert response.error_code == "host_bridge_busy"


@pytest.mark.asyncio
async def test_rejects_bounded_response_and_preserves_cancellation() -> None:
    oversized = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765",
            token=TOKEN,
            max_response_bytes=10,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * 11)
            ),
        )
    )
    with pytest.raises(HostTerminalBridgeClientError) as exc:
        await oversized.execute(_request())
    assert exc.value.error_code == "host_bridge_invalid_response"

    async def cancelled(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    cancelling = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765", token=TOKEN, transport=httpx.MockTransport(cancelled)
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelling.execute(_request())


def test_token_rejects_special_bits_and_post_open_metadata_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token"
    path.write_bytes(TOKEN.encode())
    path.chmod(0o4600)
    with pytest.raises(HostTerminalBridgeClientError, match="host_bridge_token_invalid"):
        load_secure_token(path)

    path.chmod(0o600)
    real_fstat = os.fstat

    def mismatched(fd: int) -> os.stat_result:
        values = list(real_fstat(fd))
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", mismatched)
    with pytest.raises(HostTerminalBridgeClientError, match="host_bridge_token_invalid"):
        load_secure_token(path)


@pytest.mark.asyncio
async def test_proxy_environment_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            encoded = json.dumps(_envelope()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    real_getaddrinfo = socket.getaddrinfo

    def local_bridge_dns(host, port, *args, **kwargs):
        if host in {"host.docker.internal", b"host.docker.internal"}:
            host = "127.0.0.1" if isinstance(host, str) else b"127.0.0.1"
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_bridge_dns)
    try:
        client = HostTerminalHttpClient(
            HostTerminalHttpClientConfig(
                f"http://host.docker.internal:{server.server_port}", token=TOKEN
            )
        )
        response = await client.execute(_request())
        assert response.status is HostTerminalStatus.SUCCESS
    finally:
        server.shutdown()
        server.server_close()
