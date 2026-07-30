"""Bounded loopback HTTP transport for the Browser Host Bridge."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import select
import socket
import socketserver
import threading
import time
from typing import Any, Mapping, Protocol

from app.infrastructure.browser.host_protocol import (
    AUTH_HEADER,
    MAX_JSON_RESPONSE_BYTES,
    PROTOCOL_VERSION,
)


BUSINESS_ROUTES = frozenset(
    {
        "/v1/browser/session/create",
        "/v1/browser/session/close",
        "/v1/browser/session/action",
        "/v1/browser/session/state",
        "/v1/browser/session/takeover/begin",
        "/v1/browser/session/takeover/end",
    }
)
_TOKEN_HEADER = AUTH_HEADER.lower()
_TOKEN_NAME_RE = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_CONTENT_TYPE_RE = re.compile(
    r"application/json(?:\s*;\s*charset=utf-8)?\Z", re.IGNORECASE
)
_CONTENT_LENGTH_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_HTTP_VERSION_RE = re.compile(r"HTTP/1\.[01]\Z")


class BrowserHostBridge(Protocol):
    config: Any

    @property
    def healthy(self) -> bool: ...

    def authenticate(self, supplied: str | None) -> bool: ...

    def handle_request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> tuple[int, dict[str, Any]]: ...

    def shutdown(self) -> bool: ...


@dataclass(frozen=True)
class BrowserHostBridgeServerConfig:
    bind_host: str = "127.0.0.1"
    port: int = 8766
    request_line_bytes: int = 4_096
    max_header_bytes: int = 16_384
    max_header_count: int = 64
    request_timeout_seconds: float = 65.0
    max_handler_threads: int = 16
    accept_queue_size: int = 16
    shutdown_grace_seconds: float = 3.0
    max_response_bytes: int = MAX_JSON_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.bind_host != "127.0.0.1":
            raise ValueError("host_bridge_loopback_required")
        if (
            type(self.port) is not int
            or not 0 <= self.port <= 65535
            or type(self.request_line_bytes) is not int
            or not 256 <= self.request_line_bytes <= 65_536
            or type(self.max_header_bytes) is not int
            or not 1_024 <= self.max_header_bytes <= 262_144
            or type(self.max_header_count) is not int
            or not 1 <= self.max_header_count <= 256
            or type(self.max_handler_threads) is not int
            or not 1 <= self.max_handler_threads <= 1_024
            or type(self.accept_queue_size) is not int
            or not 1 <= self.accept_queue_size <= 1_024
            or type(self.max_response_bytes) is not int
            or self.max_response_bytes < 256
        ):
            raise ValueError("host_bridge_limits_invalid")
        for value in (
            self.request_timeout_seconds,
            self.shutdown_grace_seconds,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                or value > 3_600
            ):
                raise ValueError("host_bridge_limits_invalid")


HostBridgeServerConfig = BrowserHostBridgeServerConfig


class BrowserHostBridgeServer(socketserver.TCPServer):
    """TCP server with a fixed handler pool admission bound."""

    allow_reuse_address = False

    def __init__(
        self,
        bridge: BrowserHostBridge,
        config: BrowserHostBridgeServerConfig | None = None,
    ) -> None:
        self.bridge = bridge
        self.config = config or BrowserHostBridgeServerConfig(
            bind_host=getattr(bridge.config, "bind_host", "127.0.0.1"),
            port=getattr(bridge.config, "port", 8766),
            request_timeout_seconds=getattr(
                bridge.config, "default_request_timeout_seconds", 65.0
            ),
            shutdown_grace_seconds=max(
                0.01,
                float(getattr(bridge.config, "expiry_grace_seconds", 3.0)),
            ),
        )
        self.request_queue_size = self.config.accept_queue_size
        self._handler_slots = threading.BoundedSemaphore(
            self.config.max_handler_threads
        )
        self._lifecycle_lock = threading.RLock()
        self._lifecycle = "starting"
        self._stop = threading.Event()
        self._serve_started = threading.Event()
        self._shutdown_complete = threading.Event()
        self._shutdown_finish_lock = threading.Lock()
        self._bridge_shutdown_called = False
        self._cleanup_confirmed = True
        self._socket_closed = False
        self._wakeup_reader, self._wakeup_writer = socket.socketpair()
        self._wakeup_reader.setblocking(False)
        self._wakeup_writer.setblocking(False)
        self._wakeup_closed = False
        self._inflight_lock = threading.Lock()
        self._inflight: dict[int, tuple[threading.Thread, threading.Event]] = {}
        try:
            super().__init__(
                (self.config.bind_host, self.config.port),
                _BrowserHostRequestHandler,
                bind_and_activate=True,
            )
        except Exception:
            self._wakeup_reader.close()
            self._wakeup_writer.close()
            self._wakeup_closed = True
            raise

    @property
    def lifecycle(self) -> str:
        with self._lifecycle_lock:
            return self._lifecycle

    @property
    def healthy(self) -> bool:
        if self.lifecycle != "running" or not _component_healthy(self.bridge):
            return False
        controller = getattr(self.bridge, "_cdp", None)
        if controller is not None and not _component_healthy(controller):
            return False
        store = getattr(self.bridge, "_authorization_store", None)
        if store is not None and not _store_healthy(store):
            return False
        return True

    @property
    def cleanup_confirmed(self) -> bool:
        with self._lifecycle_lock:
            return self._cleanup_confirmed

    def serve_forever(self, poll_interval: float = 0.05) -> None:
        with self._lifecycle_lock:
            if self._lifecycle == "starting":
                self._lifecycle = "running"
            if self._lifecycle != "running":
                return
        self._serve_started.set()
        try:
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select(
                        [self.socket, self._wakeup_reader],
                        [],
                        [],
                        max(0.001, poll_interval),
                    )
                except (OSError, ValueError):
                    break
                if self._wakeup_reader in readable:
                    self._drain_wakeup()
                if self.socket in readable and not self._stop.is_set():
                    try:
                        self._handle_request_noblock()
                    except OSError:
                        if not self._stop.is_set():
                            continue
        finally:
            self._finish_shutdown()

    def request_shutdown(self) -> None:
        """Request shutdown without acquiring cleanup or lifecycle locks."""
        self._stop.set()
        try:
            self._wakeup_writer.send(b"\0")
        except (BlockingIOError, OSError):
            pass

    def _drain_wakeup(self) -> None:
        while True:
            try:
                if not self._wakeup_reader.recv(4096):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def process_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        if self.lifecycle != "running":
            _send_busy_and_close(request)
            return
        if not self._handler_slots.acquire(blocking=False):
            _send_busy_and_close(request)
            return
        cancel_event = threading.Event()
        deadline = time.monotonic() + self.config.request_timeout_seconds
        thread = threading.Thread(
            target=self._process_request_thread,
            args=(request, client_address, deadline, cancel_event),
            name="browser-host-handler",
            daemon=True,
        )
        with self._lifecycle_lock:
            if self._lifecycle != "running":
                cancel_event.set()
                self._handler_slots.release()
                _send_busy_and_close(request)
                return
            with self._inflight_lock:
                self._inflight[id(thread)] = (thread, cancel_event)
        try:
            thread.start()
        except Exception:
            with self._inflight_lock:
                self._inflight.pop(id(thread), None)
            self._handler_slots.release()
            self.shutdown_request(request)
            raise

    def _process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        deadline: float,
        cancel_event: threading.Event,
    ) -> None:
        try:
            self.RequestHandlerClass(
                request, client_address, self
            ).handle_with_context(deadline, cancel_event)
        except Exception:
            pass
        finally:
            try:
                self.shutdown_request(request)
            finally:
                with self._inflight_lock:
                    self._inflight.pop(id(threading.current_thread()), None)
                self._handler_slots.release()

    def finish_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        # Request construction is handled explicitly so deadline/cancellation
        # context is installed before any byte is read.
        del request, client_address

    def shutdown(self) -> None:
        self.begin_draining()
        self._finish_shutdown()

    def begin_draining(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle in {"draining", "stopped"}:
                return
            self._lifecycle = "draining"
            self._stop.set()
            if not self._socket_closed:
                self._socket_closed = True
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.socket.close()
                except OSError:
                    pass
            with self._inflight_lock:
                entries = tuple(self._inflight.values())
        for _, cancel_event in entries:
            cancel_event.set()

    def _finish_shutdown(self) -> None:
        with self._shutdown_finish_lock:
            if self._shutdown_complete.is_set():
                return
            self.begin_draining()
            deadline = (
                time.monotonic() + self.config.shutdown_grace_seconds
            )
            bridge_done = threading.Event()
            bridge_failed = threading.Event()
            with self._lifecycle_lock:
                call_bridge = not self._bridge_shutdown_called
                self._bridge_shutdown_called = True

            def shutdown_bridge() -> None:
                try:
                    if self.bridge.shutdown() is not True:
                        bridge_failed.set()
                except Exception:
                    bridge_failed.set()
                finally:
                    bridge_done.set()

            if call_bridge:
                shutdown_thread = threading.Thread(
                    target=shutdown_bridge,
                    name="browser-host-shutdown",
                    daemon=True,
                )
                shutdown_thread.start()
            else:
                bridge_done.set()
            with self._inflight_lock:
                entries = tuple(self._inflight.values())
            bridge_done.wait(
                timeout=max(0.0, deadline - time.monotonic())
            )
            for thread, _ in entries:
                if thread is threading.current_thread():
                    continue
                thread.join(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            with self._lifecycle_lock:
                if not bridge_done.is_set() or bridge_failed.is_set():
                    self._cleanup_confirmed = False
                if any(
                    thread.is_alive()
                    for thread, _ in entries
                    if thread is not threading.current_thread()
                ):
                    self._cleanup_confirmed = False
                self._lifecycle = "stopped"
                self._shutdown_complete.set()

    def server_close(self) -> None:
        self._finish_shutdown()
        if not self._socket_closed:
            self._socket_closed = True
            try:
                super().server_close()
            except OSError:
                pass
        if not self._wakeup_closed:
            self._wakeup_closed = True
            for endpoint in (self._wakeup_reader, self._wakeup_writer):
                try:
                    endpoint.close()
                except OSError:
                    pass


class _BrowserHostRequestHandler:
    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: BrowserHostBridgeServer,
    ) -> None:
        self.request = request
        self.client_address = client_address
        self.server = server
        self.rfile = request.makefile("rb", buffering=0)

    def handle_with_context(
        self, deadline: float, cancel_event: threading.Event
    ) -> None:
        self._deadline = deadline
        try:
            method, path, headers = self._read_head(deadline)
            if method == "GET":
                self._handle_get(path)
            elif method == "POST":
                self._handle_post(path, headers, deadline, cancel_event)
            else:
                self._send(405, _error("method_not_allowed"))
        except _RequestFailure as exc:
            if exc.send_response:
                self._send(exc.status, _error(exc.error_code))
        except (OSError, TimeoutError, socket.timeout):
            return
        finally:
            try:
                self.rfile.close()
            except OSError:
                pass

    def _read_head(
        self, deadline: float
    ) -> tuple[str, str, dict[str, list[str]]]:
        raw_line = self._readline(
            self.server.config.request_line_bytes, deadline
        )
        if not raw_line:
            raise _RequestFailure(400)
        if len(raw_line) > self.server.config.request_line_bytes:
            raise _RequestFailure(414)
        try:
            line = raw_line.rstrip(b"\r\n").decode("ascii")
        except UnicodeDecodeError:
            raise _RequestFailure(400) from None
        parts = line.split(" ")
        if (
            len(parts) != 3
            or not parts[0]
            or not parts[1].startswith("/")
            or not _HTTP_VERSION_RE.fullmatch(parts[2])
            or any(ord(char) < 0x20 for char in line)
        ):
            raise _RequestFailure(400)
        headers: dict[str, list[str]] = {}
        total = 0
        count = 0
        while True:
            raw = self._readline(
                self.server.config.max_header_bytes + 1, deadline
            )
            total += len(raw)
            if total > self.server.config.max_header_bytes:
                raise _RequestFailure(431)
            if raw in {b"\r\n", b"\n"}:
                break
            if not raw:
                raise _RequestFailure(400)
            count += 1
            if count > self.server.config.max_header_count:
                raise _RequestFailure(431)
            if raw[:1] in {b" ", b"\t"} or b":" not in raw:
                raise _RequestFailure(400)
            raw_name, raw_value = raw.rstrip(b"\r\n").split(b":", 1)
            if not _TOKEN_NAME_RE.fullmatch(raw_name):
                raise _RequestFailure(400)
            name = raw_name.decode("ascii").lower()
            value = raw_value.strip(b" \t").decode("latin-1")
            headers.setdefault(name, []).append(value)
        return parts[0], parts[1], headers

    def _readline(self, maximum: int, deadline: float) -> bytes:
        self._set_remaining_timeout(deadline)
        raw = self.rfile.readline(maximum + 1)
        if len(raw) > maximum:
            return raw
        return raw

    def _set_remaining_timeout(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.request.settimeout(remaining)

    def _handle_get(self, path: str) -> None:
        if path != "/healthz":
            self._send(404, _error("not_found"))
            return
        healthy = self.server.healthy
        self._send(
            200 if healthy else 503,
            {"status": "ok" if healthy else "unhealthy"},
        )

    def _handle_post(
        self,
        path: str,
        headers: dict[str, list[str]],
        deadline: float,
        cancel_event: threading.Event,
    ) -> None:
        # Authentication is deliberately before route selection, Expect,
        # framing/content validation, and every body read.
        tokens = headers.get(_TOKEN_HEADER, [])
        supplied = tokens[0] if len(tokens) == 1 else None
        if (
            supplied is None
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in supplied)
            or not self.server.bridge.authenticate(supplied)
        ):
            self._send(401, _error("host_bridge_auth_failed"))
            return
        if path not in BUSINESS_ROUTES:
            self._send(404, _error("not_found"))
            return
        transfer_encodings = headers.get("transfer-encoding", [])
        lengths = headers.get("content-length", [])
        content_types = headers.get("content-type", [])
        if transfer_encodings:
            self._send(400, _error("host_bridge_invalid_request"))
            return
        if (
            len(lengths) != 1
            or not _CONTENT_LENGTH_RE.fullmatch(lengths[0])
            or len(content_types) != 1
            or not _CONTENT_TYPE_RE.fullmatch(content_types[0])
        ):
            self._send(400, _error("host_bridge_invalid_request"))
            return
        if len(lengths[0]) > 20:
            self._send(413, _error("host_bridge_invalid_request"))
            return
        length = int(lengths[0])
        maximum = int(
            getattr(self.server.bridge.config, "max_request_bytes", 262_144)
        )
        if length > maximum:
            self._send(413, _error("host_bridge_invalid_request"))
            return
        expects = headers.get("expect", [])
        if expects:
            if len(expects) != 1 or expects[0].lower() != "100-continue":
                self._send(417, _error("host_bridge_invalid_request"))
                return
            self._send_interim_continue()
        body = self._read_exact(length, deadline)
        if len(body) != length:
            self._send(400, _error("host_bridge_invalid_request"))
            return
        if _has_extra_body(self.request):
            self._send(400, _error("host_bridge_invalid_request"))
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, _error("host_bridge_invalid_request"))
            return
        if not isinstance(payload, dict):
            self._send(400, _error("host_bridge_invalid_request"))
            return
        if (
            payload.get("protocol_version") is not None
            and payload.get("protocol_version") != PROTOCOL_VERSION
        ):
            # HostBridge performs the authoritative shape check. This early
            # check only preserves a stable transport failure.
            self._send(400, _error("host_bridge_invalid_request"))
            return
        watcher_stop = threading.Event()
        if _peer_disconnected(self.request):
            cancel_event.set()
        watcher = threading.Thread(
            target=_watch_disconnect,
            args=(
                self.request,
                cancel_event,
                watcher_stop,
                deadline,
            ),
            name="browser-host-disconnect",
            daemon=True,
        )
        watcher.start()
        try:
            status, response = self.server.bridge.handle_request(
                path,
                payload,
                deadline_monotonic=deadline,
                cancel_event=cancel_event,
            )
        except Exception:
            status, response = 500, _error("host_bridge_internal_error")
        finally:
            watcher_stop.set()
            watcher.join(timeout=0.05)
        if time.monotonic() >= deadline:
            cancel_event.set()
            return
        if (
            type(status) is not int
            or not 100 <= status <= 599
            or not isinstance(response, dict)
        ):
            status, response = 500, _error(
                "host_bridge_invalid_response"
            )
        self._send(status, response, action_type=payload.get("action_type"))

    def _read_exact(self, length: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            self._set_remaining_timeout(deadline)
            chunk = self.request.recv(min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_interim_continue(self) -> None:
        try:
            self.request.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        except OSError:
            pass

    def _send(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        action_type: Any = None,
    ) -> None:
        encoded = _encode_bounded_response(
            payload,
            action_type=action_type,
            maximum=self.server.config.max_response_bytes,
        )
        if encoded is None:
            status = 500
            encoded = _encode_json(_error("host_bridge_invalid_response"))
        reason = {
            100: "Continue",
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            413: "Content Too Large",
            414: "URI Too Long",
            417: "Expectation Failed",
            431: "Request Header Fields Too Large",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            deadline = getattr(self, "_deadline", None)
            if isinstance(deadline, float):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.request.settimeout(remaining)
            self.request.sendall(head + encoded)
        except OSError:
            pass


class _RequestFailure(Exception):
    def __init__(
        self,
        status: int,
        error_code: str = "host_bridge_invalid_request",
        *,
        send_response: bool = True,
    ) -> None:
        self.status = status
        self.error_code = error_code
        self.send_response = send_response
        super().__init__(error_code)


def _component_healthy(component: Any) -> bool:
    try:
        healthy = getattr(component, "healthy", True)
        return bool(healthy() if callable(healthy) else healthy)
    except Exception:
        return False


def _store_healthy(store: Any) -> bool:
    if hasattr(store, "healthy"):
        return _component_healthy(store)
    loader = getattr(store, "load_authorization", None)
    if not callable(loader):
        return False
    try:
        loader("__browser_host_health__")
        return True
    except Exception:
        return False


def _error(code: str) -> dict[str, Any]:
    return {"status": "error", "error_code": code}


def _encode_json(payload: Mapping[str, Any]) -> bytes:
    encoded = _try_encode_json(payload)
    if encoded is not None:
        return encoded
    return b'{"status":"error","error_code":"host_bridge_invalid_response"}'


def _try_encode_json(payload: Mapping[str, Any]) -> bytes | None:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return None


def _encode_bounded_response(
    payload: Mapping[str, Any],
    *,
    action_type: Any,
    maximum: int,
) -> bytes | None:
    encoded = _try_encode_json(payload)
    if encoded is None:
        return None
    if len(encoded) <= maximum:
        return encoded
    if "screenshot_base64" not in payload:
        return None
    without = dict(payload)
    without.pop("screenshot_base64", None)
    effective_action = (
        action_type
        if isinstance(action_type, str)
        else without.get("action_type")
    )
    if effective_action == "screenshot":
        without = {
            "action_type": "screenshot",
            "status": "error",
            "error_code": "screenshot_unavailable",
            "document_revision": without.get("document_revision", 0),
        }
    else:
        without["warning_code"] = "screenshot_unavailable"
    encoded = _try_encode_json(without)
    return (
        encoded
        if encoded is not None and len(encoded) <= maximum
        else None
    )


def _has_extra_body(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return bool(connection.recv(1, socket.MSG_PEEK))
    except (BlockingIOError, OSError):
        return False


def _watch_disconnect(
    connection: socket.socket,
    cancel_event: threading.Event,
    stop_event: threading.Event,
    deadline: float,
) -> None:
    while (
        not stop_event.is_set()
        and not cancel_event.is_set()
        and time.monotonic() < deadline
    ):
        try:
            readable, _, _ = select.select([connection], [], [], 0.02)
            if readable and connection.recv(1, socket.MSG_PEEK) == b"":
                cancel_event.set()
                return
        except (BlockingIOError, OSError):
            cancel_event.set()
            return


def _peer_disconnected(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        return bool(
            readable
            and connection.recv(1, socket.MSG_PEEK) == b""
        )
    except (BlockingIOError, OSError):
        return True


def _send_busy_and_close(connection: socket.socket) -> None:
    payload = _encode_json(_error("host_bridge_busy"))
    response = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n".encode("ascii")
        + b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        + payload
    )
    try:
        connection.sendall(response)
    except OSError:
        pass
    finally:
        try:
            connection.close()
        except OSError:
            pass


def make_server(
    bridge: BrowserHostBridge,
    config: BrowserHostBridgeServerConfig | None = None,
) -> BrowserHostBridgeServer:
    return BrowserHostBridgeServer(bridge, config)


__all__ = [
    "BUSINESS_ROUTES",
    "BrowserHostBridgeServer",
    "BrowserHostBridgeServerConfig",
    "HostBridgeServerConfig",
    "make_server",
]
