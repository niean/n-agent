from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any

import pytest


TOKEN = "a" * 32


class _Config:
    bind_host = "127.0.0.1"
    port = 0
    max_request_bytes = 4096
    default_request_timeout_seconds = 1.0
    expiry_grace_seconds = 0.2


class _Component:
    healthy = True


class _Bridge:
    def __init__(self, handler=None) -> None:
        self.config = _Config()
        self.healthy = True
        self._cdp = _Component()
        self._authorization_store = _Component()
        self.calls: list[tuple[str, dict[str, Any], float, threading.Event]] = []
        self.shutdown_count = 0
        self.handler = handler

    def authenticate(self, supplied: str | None) -> bool:
        return supplied == TOKEN

    def handle_request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((path, payload, deadline_monotonic, cancel_event))
        if self.handler is not None:
            return self.handler(path, payload, deadline_monotonic, cancel_event)
        return 200, {"status": "ok"}

    def shutdown(self) -> bool:
        self.shutdown_count += 1
        self.healthy = False
        return True


def _receive_all(connection: socket.socket) -> bytes:
    connection.settimeout(2)
    chunks = []
    try:
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    except ConnectionResetError:
        pass
    return b"".join(chunks)


def _exchange(
    address: tuple[str, int], request: bytes, *, half_close: bool = False
) -> bytes:
    with socket.create_connection(address, timeout=2) as connection:
        connection.sendall(request)
        if half_close:
            connection.shutdown(socket.SHUT_WR)
        return _receive_all(connection)


def _start(bridge: _Bridge):
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    server = make_server(
        bridge,
        BrowserHostBridgeServerConfig(port=0, request_timeout_seconds=1),
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    deadline = time.monotonic() + 2
    while server.lifecycle != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    return server, thread


def test_loopback_config_and_health_payload_are_exact() -> None:
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
    )

    for host in ("0.0.0.0", "::", "::1", "localhost"):
        with pytest.raises(ValueError, match="loopback"):
            BrowserHostBridgeServerConfig(bind_host=host)
    bridge = _Bridge()
    server, thread = _start(bridge)
    try:
        response = _exchange(
            server.server_address,
            b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n",
        )
        assert response.endswith(b'{"status":"ok"}')
        assert b"Cache-Control: no-store\r\n" in response
        assert b"Server:" not in response
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()
    assert bridge.shutdown_count == 1
    assert server.lifecycle == "stopped"


def test_authentication_precedes_route_and_body_reading() -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    try:
        response = _exchange(
            server.server_address,
            b"POST /secret-unknown HTTP/1.1\r\n"
            b"Host: x\r\nExpect: 100-continue\r\nContent-Length: 999999\r\n\r\n",
        )
        assert response.startswith(b"HTTP/1.1 401 ")
        assert b"100 Continue" not in response
        assert not bridge.calls
    finally:
        server.shutdown()
        thread.join(2)


def test_authenticated_json_delegates_with_same_deadline_and_cancel_event() -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    body = json.dumps({"protocol_version": "1", "session_id": "s"}).encode()
    try:
        response = _exchange(
            server.server_address,
            b"POST /v1/browser/session/state HTTP/1.1\r\n"
            b"Host: x\r\n"
            + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
            + b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body,
        )
        assert response.startswith(b"HTTP/1.1 200 ")
        assert len(bridge.calls) == 1
        assert bridge.calls[0][0] == "/v1/browser/session/state"
        assert bridge.calls[0][1]["session_id"] == "s"
        assert bridge.calls[0][2] > time.monotonic() - 1
        assert isinstance(bridge.calls[0][3], threading.Event)
    finally:
        server.shutdown()
        thread.join(2)


def test_visible_ascii_token_is_authenticated_byte_for_byte_over_raw_http() -> None:
    expected = bytes(range(0x21, 0x7F))

    class ExactTokenBridge(_Bridge):
        def authenticate(self, supplied: str | None) -> bool:
            return (
                supplied is not None
                and supplied.encode("ascii", errors="strict") == expected
            )

    bridge = ExactTokenBridge()
    server, thread = _start(bridge)
    body = b'{"protocol_version":"1","session_id":"s"}'
    try:
        response = _exchange(
            server.server_address,
            b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
            + b"X-N-Agent-Browser-Token: "
            + expected
            + b"\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body,
        )
        assert response.startswith(b"HTTP/1.1 200 ")
        assert len(bridge.calls) == 1
    finally:
        server.shutdown()
        thread.join(2)


def test_protocol_response_bound_formula() -> None:
    from app.infrastructure.browser.host_protocol import (
        HOST_CDP_MAX_SCREENSHOT_BYTES,
        MAX_JSON_METADATA_BYTES,
        MAX_JSON_RESPONSE_BYTES,
        PROTOCOL_VERSION,
        max_json_response_bytes,
    )

    assert PROTOCOL_VERSION == "1"
    assert HOST_CDP_MAX_SCREENSHOT_BYTES == 1_048_576
    assert MAX_JSON_METADATA_BYTES > 0
    assert max_json_response_bytes(3) == MAX_JSON_METADATA_BYTES + 4
    assert MAX_JSON_RESPONSE_BYTES == max_json_response_bytes(
        HOST_CDP_MAX_SCREENSHOT_BYTES
    )


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"X-N-Agent-Browser-Token: wrong\r\n",
        f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
        f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode(),
        b"X-N-Agent-Browser-Token: bad\x00token\r\n",
    ],
)
def test_auth_failures_are_uniform_before_route_or_expect(header: bytes) -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    try:
        response = _exchange(
            server.server_address,
            b"POST /unknown HTTP/1.1\r\nHost: x\r\n"
            + header
            + b"Expect: 100-continue\r\n"
            + b"Transfer-Encoding: chunked\r\n\r\n",
        )
        assert response.startswith(b"HTTP/1.1 401 ")
        assert response.endswith(
            b'{"status":"error","error_code":"host_bridge_auth_failed"}'
        )
        assert b"100 Continue" not in response
        assert not bridge.calls
    finally:
        server.shutdown()
        thread.join(2)


def test_authenticated_expect_continue_is_sent_only_before_body_read() -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    try:
        with socket.create_connection(server.server_address, timeout=2) as conn:
            conn.sendall(
                b"POST /v1/browser/session/state HTTP/1.1\r\n"
                b"Host: x\r\n"
                + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
                + b"Expect: 100-continue\r\n"
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: 41\r\n\r\n"
            )
            interim = conn.recv(4096)
            assert interim == b"HTTP/1.1 100 Continue\r\n\r\n"
            conn.sendall(b'{"protocol_version":"1","session_id":"s"}')
            final = _receive_all(conn)
            assert final.startswith(b"HTTP/1.1 200 ")
    finally:
        server.shutdown()
        thread.join(2)


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Length: 2\r\n".encode(),
            b"{}",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\n".encode(),
            b"{}",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\nContent-Length: 2\r\n".encode(),
            b"{}",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: -1\r\n".encode(),
            b"",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2x\r\n".encode(),
            b"",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\nTransfer-Encoding: chunked\r\n".encode(),
            b"{}",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: text/json\r\nContent-Length: 2\r\n".encode(),
            b"{}",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\n".encode(),
            b"[]",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\n".encode(),
            b"\xff\xff",
            400,
        ),
        (
            f"X-N-Agent-Browser-Token: {TOKEN}\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\n".encode(),
            b"{]",
            400,
        ),
    ],
)
def test_authenticated_framing_and_json_rejections(
    headers: bytes, body: bytes, status: int
) -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    try:
        response = _exchange(
            server.server_address,
            b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
            + headers
            + b"\r\n"
            + body,
        )
        assert response.startswith(f"HTTP/1.1 {status} ".encode())
        assert not bridge.calls
    finally:
        server.shutdown()
        thread.join(2)


def test_oversized_and_extra_bodies_are_rejected() -> None:
    bridge = _Bridge()
    bridge.config.max_request_bytes = 4
    server, thread = _start(bridge)
    common = (
        b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
        + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
        + b"Content-Type: application/json\r\n"
    )
    try:
        oversized = _exchange(
            server.server_address,
            common + b"Content-Length: 5\r\n\r\n12345",
        )
        assert oversized.startswith(b"HTTP/1.1 413 ")
        extra = _exchange(
            server.server_address,
            common + b"Content-Length: 2\r\n\r\n{}x",
        )
        assert extra.startswith(b"HTTP/1.1 400 ")
        assert not bridge.calls
    finally:
        server.shutdown()
        thread.join(2)


def test_request_line_header_bytes_and_header_count_are_bounded() -> None:
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    bridge = _Bridge()
    config = BrowserHostBridgeServerConfig(
        port=0,
        request_line_bytes=256,
        max_header_bytes=1024,
        max_header_count=2,
        request_timeout_seconds=1,
    )
    server = make_server(bridge, config)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        long_line = _exchange(
            server.server_address,
            b"GET /" + b"x" * 300 + b" HTTP/1.1\r\n\r\n",
        )
        assert long_line.startswith(b"HTTP/1.1 414 ")
        too_many = _exchange(
            server.server_address,
            b"GET /healthz HTTP/1.1\r\nA: 1\r\nB: 2\r\nC: 3\r\n\r\n",
        )
        assert too_many.startswith(b"HTTP/1.1 431 ")
        huge = _exchange(
            server.server_address,
            b"GET /healthz HTTP/1.1\r\nX: " + b"a" * 1100 + b"\r\n\r\n",
        )
        assert huge.startswith(b"HTTP/1.1 431 ")
    finally:
        server.shutdown()
        thread.join(2)


def test_slow_header_and_body_share_one_absolute_deadline() -> None:
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    bridge = _Bridge()
    server = make_server(
        bridge,
        BrowserHostBridgeServerConfig(
            port=0, request_timeout_seconds=0.15
        ),
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    started = time.monotonic()
    try:
        with socket.create_connection(server.server_address, timeout=1) as conn:
            conn.sendall(b"POST /v1/browser/session/state HTTP/1.1\r\n")
            time.sleep(0.09)
            conn.sendall(
                b"Host: x\r\n"
                + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: 42\r\n\r\n"
            )
            time.sleep(0.09)
            try:
                conn.sendall(
                    b'{"protocol_version":"1","session_id":"s"}'
                )
            except OSError:
                pass
            _receive_all(conn)
        assert time.monotonic() - started < 0.5
        assert not bridge.calls
    finally:
        server.shutdown()
        thread.join(2)


def test_handler_capacity_returns_stable_busy_without_new_thread() -> None:
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    entered = threading.Event()
    release = threading.Event()

    def blocking(*args):
        entered.set()
        release.wait(2)
        return 200, {"status": "ok"}

    bridge = _Bridge(blocking)
    server = make_server(
        bridge,
        BrowserHostBridgeServerConfig(
            port=0,
            max_handler_threads=1,
            accept_queue_size=1,
            request_timeout_seconds=1,
        ),
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    request = (
        b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
        + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
        + b"Content-Type: application/json\r\nContent-Length: 41\r\n\r\n"
        + b'{"protocol_version":"1","session_id":"s"}'
    )
    first_box: list[bytes] = []
    first = threading.Thread(
        target=lambda: first_box.append(_exchange(server.server_address, request))
    )
    first.start()
    assert entered.wait(1)
    try:
        busy = _exchange(server.server_address, request)
        assert busy.startswith(b"HTTP/1.1 503 ")
        assert busy.endswith(
            b'{"status":"error","error_code":"host_bridge_busy"}'
        )
    finally:
        release.set()
        first.join(2)
        server.shutdown()
        thread.join(2)


def test_client_disconnect_sets_shared_cancellation_event() -> None:
    cancelled = threading.Event()

    def await_cancel(path, payload, deadline, event):
        del path, payload, deadline
        if event.wait(1):
            cancelled.set()
        return 200, {"status": "ok"}

    bridge = _Bridge(await_cancel)
    server, thread = _start(bridge)
    body = b'{"protocol_version":"1","session_id":"s"}'
    try:
        conn = socket.create_connection(server.server_address, timeout=1)
        conn.sendall(
            b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
            + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        conn.close()
        assert cancelled.wait(1)
        assert bridge.calls[0][3].is_set()
    finally:
        server.shutdown()
        thread.join(2)


@pytest.mark.parametrize(
    "route",
    [
        "/v1/browser/session/create",
        "/v1/browser/session/close",
        "/v1/browser/session/action",
        "/v1/browser/session/state",
        "/v1/browser/session/takeover/begin",
        "/v1/browser/session/takeover/end",
    ],
)
def test_exact_six_business_routes_delegate_with_shared_context(
    route: str,
) -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    body = b'{"protocol_version":"1"}'
    try:
        response = _exchange(
            server.server_address,
            f"POST {route} HTTP/1.1\r\nHost: x\r\n".encode()
            + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body,
        )
        assert response.startswith(b"HTTP/1.1 200 ")
        assert bridge.calls[0][0] == route
        assert isinstance(bridge.calls[0][3], threading.Event)
        assert bridge.calls[0][2] > time.monotonic()
    finally:
        server.shutdown()
        thread.join(2)


@pytest.mark.parametrize("action_type", ["screenshot", "observe"])
def test_oversized_complete_screenshot_is_removed_not_truncated(
    action_type: str,
) -> None:
    def large(path, payload, deadline, event):
        del path, payload, deadline, event
        return 200, {
            "action_type": action_type,
            "status": "success",
            "document_revision": 7,
            "screenshot_base64": "A" * 1000,
        }

    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    bridge = _Bridge(large)
    server = make_server(
        bridge,
        BrowserHostBridgeServerConfig(port=0, max_response_bytes=256),
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    body = json.dumps(
        {
            "protocol_version": "1",
            "session_id": "s",
            "action_type": action_type,
        }
    ).encode()
    try:
        response = _exchange(
            server.server_address,
            b"POST /v1/browser/session/action HTTP/1.1\r\nHost: x\r\n"
            + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body,
        )
        payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
        assert "screenshot_base64" not in payload
        if action_type == "screenshot":
            assert payload["status"] == "error"
            assert payload["error_code"] == "screenshot_unavailable"
        else:
            assert payload["warning_code"] == "screenshot_unavailable"
    finally:
        server.shutdown()
        thread.join(2)


def test_internal_exception_details_are_redacted() -> None:
    secret = "/private/token-path"

    def explode(*args):
        raise RuntimeError(secret)

    bridge = _Bridge(explode)
    server, thread = _start(bridge)
    body = b'{"protocol_version":"1","session_id":"s"}'
    try:
        response = _exchange(
            server.server_address,
            b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
            + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body,
        )
        assert secret.encode() not in response
        assert b"host_bridge_internal_error" in response
    finally:
        server.shutdown()
        thread.join(2)


@pytest.mark.parametrize(
    "result",
    [
        ("200", []),
        (200, {"unsafe": object()}),
    ],
)
def test_invalid_bridge_response_is_stable_500(result) -> None:
    bridge = _Bridge(lambda *args: result)
    server, thread = _start(bridge)
    body = b'{"protocol_version":"1","session_id":"s"}'
    try:
        response = _exchange(
            server.server_address,
            b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
            + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body,
        )
        assert response.startswith(b"HTTP/1.1 500 ")
        assert response.endswith(
            b'{"status":"error","error_code":"host_bridge_invalid_response"}'
        )
    finally:
        server.shutdown()
        thread.join(2)


def test_health_aggregates_lifecycle_bridge_controller_and_store() -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    try:
        for component in (
            bridge,
            bridge._cdp,
            bridge._authorization_store,
        ):
            component.healthy = False
            response = _exchange(
                server.server_address,
                b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n",
            )
            assert response.startswith(b"HTTP/1.1 503 ")
            assert response.endswith(b'{"status":"unhealthy"}')
            component.healthy = True
        server.begin_draining()
        assert server.lifecycle == "draining"
    finally:
        server.shutdown()
        thread.join(2)
    assert server.lifecycle == "stopped"


def test_health_actively_probes_store_without_health_property() -> None:
    class Store:
        def load_authorization(self, session_id):
            raise RuntimeError("private sqlite detail")

    bridge = _Bridge()
    bridge._authorization_store = Store()
    server, thread = _start(bridge)
    try:
        response = _exchange(
            server.server_address,
            b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n",
        )
        assert response.startswith(b"HTTP/1.1 503 ")
        assert response.endswith(b'{"status":"unhealthy"}')
        assert b"sqlite" not in response
    finally:
        server.shutdown()
        thread.join(2)


def test_disconnect_watchers_do_not_outlive_completed_requests() -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    body = b'{"protocol_version":"1","session_id":"s"}'
    request = (
        b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
        + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    try:
        for _ in range(5):
            assert _exchange(server.server_address, request).startswith(
                b"HTTP/1.1 200 "
            )
        time.sleep(0.05)
        assert not [
            item
            for item in threading.enumerate()
            if item.name == "browser-host-disconnect"
        ]
    finally:
        server.shutdown()
        thread.join(2)


def test_shutdown_uses_one_shared_grace_for_bridge_and_handlers() -> None:
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    entered = threading.Event()

    def handler(path, payload, deadline, event):
        del path, payload, deadline
        entered.set()
        event.wait(1)
        time.sleep(0.12)
        return 200, {"status": "ok"}

    class SlowShutdownBridge(_Bridge):
        def shutdown(self) -> bool:
            self.shutdown_count += 1
            time.sleep(0.12)
            return True

    bridge = SlowShutdownBridge(handler)
    server = make_server(
        bridge,
        BrowserHostBridgeServerConfig(
            port=0,
            shutdown_grace_seconds=0.15,
            request_timeout_seconds=1,
        ),
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    body = b'{"protocol_version":"1","session_id":"s"}'
    request = (
        b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
        + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    client = socket.create_connection(server.server_address, timeout=1)
    client.sendall(request)
    assert entered.wait(1)
    started = time.monotonic()
    server.shutdown()
    elapsed = time.monotonic() - started
    client.close()
    thread.join(2)
    assert elapsed < 0.21
    assert bridge.shutdown_count == 1


def test_shutdown_is_idempotent_and_reports_bridge_cleanup_failure() -> None:
    class FailingBridge(_Bridge):
        def shutdown(self) -> bool:
            self.shutdown_count += 1
            raise RuntimeError("private cleanup detail")

    bridge = FailingBridge()
    server, thread = _start(bridge)
    server.shutdown()
    server.shutdown()
    thread.join(2)
    assert bridge.shutdown_count == 1
    assert server.lifecycle == "stopped"
    assert server.cleanup_confirmed is False


def test_shutdown_requires_exact_true_cleanup_outcome() -> None:
    class UnconfirmedBridge(_Bridge):
        def shutdown(self) -> bool:
            self.shutdown_count += 1
            return False

    bridge = UnconfirmedBridge()
    server, thread = _start(bridge)
    server.shutdown()
    thread.join(2)
    assert bridge.shutdown_count == 1
    assert server.cleanup_confirmed is False


def test_shutdown_request_is_nonblocking_while_cleanup_lock_is_held() -> None:
    bridge = _Bridge()
    server, thread = _start(bridge)
    server._shutdown_finish_lock.acquire()
    try:
        started = time.monotonic()
        server.request_shutdown()
        server.request_shutdown()
        assert time.monotonic() - started < 0.1
    finally:
        server._shutdown_finish_lock.release()
    thread.join(2)
    server.server_close()
    assert not thread.is_alive()
    assert bridge.shutdown_count == 1
    assert server.cleanup_confirmed is True


def test_late_bridge_response_is_dropped_and_cancelled() -> None:
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    entered = threading.Event()
    release = threading.Event()
    observed_cancel: list[threading.Event] = []

    def late(path, payload, deadline, cancel_event):
        del path, payload, deadline
        entered.set()
        assert release.wait(1)
        observed_cancel.append(cancel_event)
        return 200, {"status": "ok"}

    bridge = _Bridge(late)
    server = make_server(
        bridge,
        BrowserHostBridgeServerConfig(
            port=0, request_timeout_seconds=0.08
        ),
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    body = b'{"protocol_version":"1","session_id":"s"}'
    request = (
        b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
        + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    client = socket.create_connection(server.server_address, timeout=1)
    try:
        client.sendall(request)
        assert entered.wait(1)
        time.sleep(0.1)
        release.set()
        response = _receive_all(client)
        assert b"HTTP/1.1 200" not in response
        assert observed_cancel and observed_cancel[0].is_set()
    finally:
        client.close()
        server.shutdown()
        thread.join(2)


def test_draining_wins_between_capacity_and_registration() -> None:
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )

    bridge = _Bridge()
    server = make_server(
        bridge,
        BrowserHostBridgeServerConfig(port=0),
    )
    real_slots = server._handler_slots
    acquired = threading.Event()
    release = threading.Event()

    class PausedSlots:
        def acquire(self, blocking=False):
            result = real_slots.acquire(blocking=blocking)
            acquired.set()
            assert release.wait(1)
            return result

        def release(self):
            real_slots.release()

    server._handler_slots = PausedSlots()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    body = b'{"protocol_version":"1","session_id":"s"}'
    request = (
        b"POST /v1/browser/session/state HTTP/1.1\r\nHost: x\r\n"
        + f"X-N-Agent-Browser-Token: {TOKEN}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    client = socket.create_connection(server.server_address, timeout=1)
    client.sendall(request)
    assert acquired.wait(1)
    drainer = threading.Thread(target=server.begin_draining)
    drainer.start()
    drainer.join(1)
    assert not drainer.is_alive()
    release.set()
    response = _receive_all(client)
    client.close()
    thread.join(2)
    server.shutdown()
    assert b"host_bridge_busy" in response
    assert bridge.calls == []
    assert server._inflight == {}
