"""Tests for the Host CDP browser backend (T12a) and Host Bridge (T12b).

Uses FAKE bridge/CDP targets. No real Chrome is connected. The HostCdpBrowserBackend
tests use httpx.MockTransport as a fake bridge. The HostBridge tests use fake
AuthorizationStore + CdpTargetController implementations.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

import app.infrastructure.browser.host_bridge as host_bridge_module
import app.infrastructure.browser.host_grant_store as host_grant_store_module
from app.infrastructure.browser import host_protocol
from app.domain.browser import (
    BrowserActionResult,
    BrowserBackendType,
    BrowserSession,
    BrowserSessionStatus,
    BrowserState,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScreenshotAction,
    ScrollAction,
    TypeAction,
)
from app.infrastructure.browser.host_cdp_backend import (
    AUTH_HEADER,
    HostCdpBackendConfig,
    HostCdpBackendError,
    HostCdpBrowserBackend,
    load_secure_token,
)
from app.infrastructure.browser.host_bridge import (
    AuthorizationStore,
    HostBridge,
    HostBridgeConfig,
    TargetClosed,
)
from app.infrastructure.browser.host_grant_store import (
    BrowserAuthorizationStoreError,
    HostAuthorizationSnapshot,
)
from app.domain.browser_policy import BROWSER_POLICY_VERSION


# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

TOKEN = b"a" * 32  # 32 bytes, meets minimum length
BASE_URL = "http://127.0.0.1:8766"
POLICY_VERSION = BROWSER_POLICY_VERSION


def _write_token(path: Path, token: bytes = TOKEN) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(token + b"\n")
    path.chmod(0o600)


def _active_session(
    sid: str = "b-1", nagent: str = "n-1", profile: str = "p-1"
) -> BrowserSession:
    s = BrowserSession.create_for_host(sid, nagent, profile)
    return s.transition_to(BrowserSessionStatus.ACTIVE)


def _pending_session(
    sid: str = "b-1", nagent: str = "n-1", profile: str = "p-1"
) -> BrowserSession:
    return BrowserSession.create_for_host(sid, nagent, profile)


def _make_grant(
    session_id: str = "b-1",
    n_agent_id: str = "n-1",
    policy_version: str = POLICY_VERSION,
    *,
    expired: bool = False,
    actor_id: str = "actor-1",
    status: BrowserSessionStatus = BrowserSessionStatus.ACTIVE,
    profile_ref: str | None = None,
    backend_type: BrowserBackendType = BrowserBackendType.HOST_CDP,
) -> HostAuthorizationSnapshot:
    if expired:
        expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    return HostAuthorizationSnapshot(
        browser_session_id=session_id,
        n_agent_session_id=n_agent_id,
        backend_type=backend_type,
        status=status,
        profile_ref=profile_ref or (
            "p-1" if session_id == "b-1" else f"p-{session_id.removeprefix('b-')}"
        ),
        actor_id=actor_id,
        policy_version=policy_version,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Fake bridge (httpx.MockTransport handler)
# ---------------------------------------------------------------------------


class FakeBridge:
    """Fake HTTP bridge for HostCdpBrowserBackend tests.

    Records all requests and returns canned responses based on path.
    Responses can be overridden per-path via set_response().
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._responses: dict[str, tuple[int, Any]] = {}

    def set_response(
        self, path: str, body: dict[str, Any], status_code: int = 200
    ) -> None:
        self._responses[path] = (status_code, body)

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            payload = json.loads(request.content) if request.content else {}
            self.requests.append({
                "path": path,
                "payload": payload,
                "headers": dict(request.headers),
            })
            if path in self._responses:
                status_code, body = self._responses[path]
                if callable(body):
                    body = body(payload)
                return httpx.Response(status_code, json=body)
            if path == "/v1/browser/session/action":
                return httpx.Response(
                    200,
                    json={
                        "action_type": payload["action_type"],
                        "status": "success",
                        "document_revision": payload[
                            "document_revision"
                        ],
                    },
                )
            if path == "/v1/browser/session/state":
                return httpx.Response(
                    200,
                    json={
                        "safe_url": None,
                        "title": None,
                        "status": "active",
                        "document_revision": 0,
                        "latest_screenshot_ref": None,
                    },
                )
            if path == "/v1/browser/session/takeover/begin":
                return httpx.Response(
                    200,
                    json={"status": "ok", "takeover_url": None},
                )
            return httpx.Response(200, json={"status": "ok"})

        return _handler


def _make_backend(
    tmp_path: Path, fake: FakeBridge | None = None
) -> HostCdpBrowserBackend:
    fake = fake or FakeBridge()
    token_path = tmp_path / "private" / "token"
    _write_token(token_path)
    config = HostCdpBackendConfig(
        base_url=BASE_URL,
        token_path=token_path,
        transport=httpx.MockTransport(fake.handler()),
    )
    return HostCdpBrowserBackend(config)


class _TrackedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _streaming_backend(
    tmp_path: Path,
    *,
    body: bytes,
    status_code: int = 200,
    headers: list[tuple[str, str]] | None = None,
    chunks: list[bytes] | None = None,
    max_screenshot_bytes: int = (
        host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
    ),
    max_response_bytes: int | None = None,
) -> tuple[HostCdpBrowserBackend, _TrackedByteStream]:
    stream = _TrackedByteStream(chunks or [body])
    response_headers = (
        [("Content-Length", str(len(body)))]
        if headers is None
        else headers
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers=response_headers,
            stream=stream,
        )

    token_path = tmp_path / "stream-private" / "token"
    _write_token(token_path)
    response_limit = (
        host_protocol.max_json_response_bytes(max_screenshot_bytes)
        if max_response_bytes is None
        else max_response_bytes
    )
    backend = HostCdpBrowserBackend(
        HostCdpBackendConfig(
            base_url=BASE_URL,
            token_path=token_path,
            max_screenshot_bytes=max_screenshot_bytes,
            max_response_bytes=response_limit,
            transport=httpx.MockTransport(handler),
        )
    )
    return backend, stream


def test_config_default_screenshot_limit_and_shared_response_formula() -> None:
    config = HostCdpBackendConfig(base_url=BASE_URL, token=TOKEN)
    assert (
        config.max_screenshot_bytes
        == host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
    )
    assert config.max_response_bytes >= host_protocol.max_json_response_bytes(
        config.max_screenshot_bytes
    )
    explicit = HostCdpBackendConfig(
        base_url=BASE_URL,
        token=TOKEN,
        max_screenshot_bytes=(
            host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
        ),
    )
    assert explicit.max_screenshot_bytes == config.max_screenshot_bytes


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("max_screenshot_bytes", True, "host_bridge_screenshot_limit_invalid"),
        ("max_screenshot_bytes", 0, "host_bridge_screenshot_limit_invalid"),
        ("max_screenshot_bytes", -1, "host_bridge_screenshot_limit_invalid"),
        ("max_screenshot_bytes", 1.5, "host_bridge_screenshot_limit_invalid"),
        ("max_response_bytes", True, "host_bridge_response_limit_invalid"),
        ("max_response_bytes", 0, "host_bridge_response_limit_invalid"),
        ("max_response_bytes", 1.5, "host_bridge_response_limit_invalid"),
        ("connect_timeout_seconds", float("inf"), "host_bridge_timeout_invalid"),
        ("read_timeout_seconds", float("nan"), "host_bridge_timeout_invalid"),
    ],
)
def test_config_rejects_non_positive_non_integer_or_non_finite_limits(
    field: str, value: Any, error: str
) -> None:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "token": TOKEN,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=error):
        HostCdpBackendConfig(**kwargs)


def test_config_rejects_response_limit_below_shared_formula() -> None:
    required = host_protocol.max_json_response_bytes(
        host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
    )
    with pytest.raises(
        ValueError, match="host_bridge_response_limit_invalid"
    ):
        HostCdpBackendConfig(
            base_url=BASE_URL,
            token=TOKEN,
            max_response_bytes=required - 1,
        )


@pytest.mark.parametrize(
    "value",
    [
        host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES - 1,
        host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES + 1,
    ],
)
def test_config_rejects_host_cdp_screenshot_limit_drift(
    value: int,
) -> None:
    with pytest.raises(
        ValueError, match="host_bridge_screenshot_limit_invalid"
    ):
        HostCdpBackendConfig(
            base_url=BASE_URL,
            token=TOKEN,
            max_screenshot_bytes=value,
            max_response_bytes=host_protocol.max_json_response_bytes(
                value
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Content-Length", "15"), ("Content-Length", "15")],
        [("Content-Length", "-1")],
        [("Content-Length", "1.0")],
        [("Content-Length", "+15")],
    ],
)
async def test_streaming_response_rejects_invalid_content_length(
    tmp_path: Path, headers: list[tuple[str, str]]
) -> None:
    body = b'{"status":"ok"}'
    backend, stream = _streaming_backend(
        tmp_path, body=body, headers=headers
    )
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend.create_session(_active_session())
    assert backend._sessions == set()
    assert stream.read_count == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_streaming_response_rejects_declared_length_over_limit_without_read(
    tmp_path: Path,
) -> None:
    maximum = host_protocol.max_json_response_bytes()
    backend, stream = _streaming_backend(
        tmp_path,
        body=b'{"status":"ok"}',
        headers=[("Content-Length", str(maximum + 1))],
        max_response_bytes=maximum,
    )
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend.create_session(_active_session())
    assert stream.read_count == 0
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        [b'{"status":"ok"}', b"x", b"must-not-read"],
        [
            b"x" * host_protocol.MAX_JSON_RESPONSE_BYTES,
            b"x",
            b"must-not-read",
        ],
    ],
)
async def test_streaming_response_stops_when_actual_or_declared_bound_exceeded(
    tmp_path: Path, chunks: list[bytes]
) -> None:
    maximum = host_protocol.max_json_response_bytes()
    declared = (
        len(chunks[0])
        if len(chunks[0]) < maximum
        else maximum
    )
    backend, stream = _streaming_backend(
        tmp_path,
        body=b"",
        headers=[("Content-Length", str(declared))],
        chunks=chunks,
        max_response_bytes=maximum,
    )
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend.create_session(_active_session())
    assert stream.read_count == 2
    assert stream.closed is True
    assert backend._sessions == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, b'{"status":"error","error_code":"auth"}'),
        (500, b'{"status":"error","error_code":"internal"}'),
        (200, b""),
        (200, b"\xff"),
        (200, b"{"),
        (200, b"[]"),
    ],
)
async def test_streaming_response_rejects_status_empty_utf8_and_json_errors(
    tmp_path: Path, status_code: int, body: bytes
) -> None:
    backend, stream = _streaming_backend(
        tmp_path, body=body, status_code=status_code
    )
    with pytest.raises(HostCdpBackendError):
        await backend.create_session(_active_session())
    assert backend._sessions == set()
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok", "unknown": True},
        {"status": "ok", "error_code": "grant_not_found"},
        {"status": "error"},
        {"status": "error", "error_code": "x", "unknown": True},
        {"status": "success"},
        {"status": True},
    ],
)
async def test_create_schema_is_exact_and_never_registers_invalid_response(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    encoded = json.dumps(body).encode()
    backend, _ = _streaming_backend(tmp_path, body=encoded)
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend.create_session(_active_session())
    assert backend._sessions == set()


def _action_success(**updates: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action_type": "navigate",
        "status": "success",
        "document_revision": 1,
    }
    body.update(updates)
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _action_success(unknown=True),
        _action_success(error_code="bad"),
        _action_success(action_type=True),
        _action_success(document_revision=True),
        _action_success(document_revision=-1),
        _action_success(duration_ms=True),
        _action_success(url="x" * 8193),
        _action_success(title="x" * 4097),
        _action_success(text="x" * 20001),
        _action_success(warning_code="x" * 257),
        _action_success(elements=[{}]),
        _action_success(
            elements=[
                {
                    "element_ref": "e",
                    "role": "button",
                    "accessible_name": "go",
                    "text_excerpt": "",
                    "disabled": False,
                    "unknown": True,
                }
            ]
        ),
        _action_success(
            elements=[
                {
                    "element_ref": "e" * 513,
                    "role": "button",
                    "accessible_name": "go",
                    "text_excerpt": "",
                    "disabled": False,
                }
            ]
        ),
        _action_success(
            elements=[
                {
                    "element_ref": "e",
                    "role": "button",
                    "accessible_name": "go",
                    "text_excerpt": "",
                    "disabled": 1,
                }
            ]
        ),
        _action_success(
            elements=[
                {
                    "element_ref": "e",
                    "role": "button",
                    "accessible_name": "go",
                    "text_excerpt": "",
                    "disabled": False,
                }
            ]
            * 201
        ),
        {
            "action_type": "navigate",
            "status": "error",
            "error_code": "x" * 257,
            "document_revision": 0,
        },
        {
            "action_type": "navigate",
            "status": "error",
            "error_code": "denied",
            "url": "https://example.com",
        },
        {"action_type": "navigate", "status": "timeout", "document_revision": 0},
    ],
)
async def test_action_schema_rejects_unknown_mixed_unbounded_and_wrong_types(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    backend, _ = _streaming_backend(
        tmp_path, body=json.dumps(body).encode()
    )
    backend._sessions.add("b-1")
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend.execute_action(
            "b-1", NavigateAction(url="https://example.com/")
        )
    assert backend.last_screenshot_bytes("b-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {
            "safe_url": "https://example.com/",
            "title": "Example",
            "status": "unknown",
            "document_revision": 0,
            "latest_screenshot_ref": None,
        },
        {
            "safe_url": "x" * 8193,
            "title": "Example",
            "status": "active",
            "document_revision": 0,
            "latest_screenshot_ref": None,
        },
        {
            "safe_url": None,
            "title": "x" * 4097,
            "status": "active",
            "document_revision": 0,
            "latest_screenshot_ref": None,
        },
        {
            "safe_url": None,
            "title": None,
            "status": "active",
            "document_revision": True,
            "latest_screenshot_ref": None,
        },
        {
            "safe_url": None,
            "title": None,
            "status": "active",
            "document_revision": 0,
            "latest_screenshot_ref": None,
            "unknown": True,
        },
        {"status": "error", "error_code": "x", "safe_url": None},
    ],
)
async def test_state_schema_invalid_response_returns_no_partial_state(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    backend, _ = _streaming_backend(
        tmp_path, body=json.dumps(body).encode()
    )
    backend._sessions.add("b-1")
    with backend._screenshot_lock:
        backend._screenshot_cache["b-1"] = b"stale"
    state = await backend.get_state("b-1")
    assert state == BrowserState(
        safe_url=None,
        title=None,
        status=BrowserSessionStatus.DEGRADED,
        document_revision=0,
        latest_screenshot_ref=None,
    )
    assert backend.last_screenshot_bytes("b-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "body"),
    [
        ("close", {"status": "ok", "unknown": True}),
        ("takeover_begin", {"status": "ok"}),
        (
            "takeover_begin",
            {"status": "ok", "takeover_url": "http://leak"},
        ),
        (
            "takeover_begin",
            {"status": "error", "error_code": "x", "unknown": True},
        ),
        ("takeover_end", {"status": "success"}),
        (
            "takeover_end",
            {"status": "ok", "screenshot_base64": "YQ=="},
        ),
    ],
)
async def test_close_and_takeover_schemas_reject_unknown_or_mixed_fields(
    tmp_path: Path, operation: str, body: dict[str, Any]
) -> None:
    backend, stream = _streaming_backend(
        tmp_path, body=json.dumps(body).encode()
    )
    backend._sessions.add("b-1")
    with backend._screenshot_lock:
        backend._screenshot_cache["b-1"] = b"stale"
    if operation == "close":
        await backend.close_session("b-1")
        assert "b-1" not in backend._sessions
    elif operation == "takeover_begin":
        assert await backend.begin_takeover("b-1") is None
    else:
        await backend.end_takeover("b-1")
    assert stream.closed is True
    assert backend.last_screenshot_bytes("b-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoded",
    [
        "YQ",
        "YQ=",
        "YQ===",
        "YWJj=",
        "YWJj\n",
        "YWJ_",
        "YWJ-",
        "!!!!",
    ],
)
async def test_action_screenshot_rejects_noncanonical_base64(
    tmp_path: Path, encoded: str
) -> None:
    body = json.dumps(_action_success(screenshot_base64=encoded)).encode()
    backend, _ = _streaming_backend(tmp_path, body=body)
    backend._sessions.add("b-1")
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend.execute_action(
            "b-1", NavigateAction(url="https://example.com/")
        )
    assert backend.last_screenshot_bytes("b-1") is None


@pytest.mark.asyncio
async def test_action_screenshot_enforces_raw_limit_before_and_after_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    maximum = host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
    oversized = base64.b64encode(b"x" * (maximum + 1)).decode()
    body = json.dumps(_action_success(screenshot_base64=oversized)).encode()
    backend, _ = _streaming_backend(tmp_path, body=body)
    backend._sessions.add("b-1")
    decode_calls: list[str] = []
    original_decode = base64.b64decode

    def tracked_decode(value: Any, **kwargs: Any) -> bytes:
        decode_calls.append("called")
        return original_decode(value, **kwargs)

    monkeypatch.setattr(base64, "b64decode", tracked_decode)
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend.execute_action(
            "b-1", NavigateAction(url="https://example.com/")
        )
    assert decode_calls == []

    valid = base64.b64encode(b"ab").decode()
    body2 = json.dumps(_action_success(screenshot_base64=valid)).encode()
    backend2, _ = _streaming_backend(tmp_path, body=body2)
    backend2._sessions.add("b-1")
    monkeypatch.setattr(
        base64,
        "b64decode",
        lambda *args, **kwargs: b"x" * (maximum + 1),
    )
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_invalid_response"
    ):
        await backend2.execute_action(
            "b-1", NavigateAction(url="https://example.com/")
        )
    assert backend2.last_screenshot_bytes("b-1") is None


@pytest.mark.asyncio
async def test_success_without_screenshot_never_reuses_stale_frame(
    tmp_path: Path,
) -> None:
    body = json.dumps(_action_success()).encode()
    backend, _ = _streaming_backend(tmp_path, body=body)
    backend._sessions.add("b-1")
    with backend._screenshot_lock:
        backend._screenshot_cache["b-1"] = b"stale"
    result = await backend.execute_action(
        "b-1", NavigateAction(url="https://example.com/")
    )
    assert result.status == "success"
    assert backend.last_screenshot_bytes("b-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
    ],
)
async def test_network_or_timeout_failure_clears_action_screenshot(
    tmp_path: Path, transport_error: Exception
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise transport_error

    token_path = tmp_path / "failure-private" / "token"
    _write_token(token_path)
    backend = HostCdpBrowserBackend(
        HostCdpBackendConfig(
            base_url=BASE_URL,
            token_path=token_path,
            transport=httpx.MockTransport(handler),
        )
    )
    backend._sessions.add("b-1")
    with backend._screenshot_lock:
        backend._screenshot_cache["b-1"] = b"stale"
    with pytest.raises(
        HostCdpBackendError, match="host_bridge_unavailable"
    ):
        await backend.execute_action(
            "b-1", NavigateAction(url="https://example.com/")
        )
    assert backend.last_screenshot_bytes("b-1") is None


@pytest.mark.asyncio
async def test_action_screenshot_is_fresh_side_channel_not_in_result(
    tmp_path: Path,
) -> None:
    screenshot = b"\x89PNG\r\n\x1a\nfresh"
    body = json.dumps(
        _action_success(
            screenshot_base64=base64.b64encode(screenshot).decode()
        )
    ).encode()
    backend, _ = _streaming_backend(tmp_path, body=body)
    backend._sessions.add("b-1")
    result = await backend.execute_action(
        "b-1", NavigateAction(url="https://example.com/")
    )
    assert result.status == "success"
    assert backend.last_screenshot_bytes("b-1") == screenshot
    assert "base64" not in repr(result).lower()
    assert not hasattr(result, "screenshot_base64")


@pytest.mark.asyncio
async def test_action_start_and_failure_clear_only_its_session_cache(
    tmp_path: Path,
) -> None:
    first = b"frame-one"
    responses = {
        "b-1": _action_success(
            screenshot_base64=base64.b64encode(first).decode()
        ),
        "b-2": _action_success(
            screenshot_base64=base64.b64encode(b"frame-two").decode()
        ),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        body = json.dumps(responses[payload["session_id"]]).encode()
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body))},
            content=body,
        )

    token_path = tmp_path / "isolated-private" / "token"
    _write_token(token_path)
    backend = HostCdpBrowserBackend(
        HostCdpBackendConfig(
            base_url=BASE_URL,
            token_path=token_path,
            max_response_bytes=host_protocol.max_json_response_bytes(),
            transport=httpx.MockTransport(handler),
        )
    )
    backend._sessions.update({"b-1", "b-2"})
    await asyncio.gather(
        backend.execute_action(
            "b-1", NavigateAction(url="https://example.com/1")
        ),
        backend.execute_action(
            "b-2", NavigateAction(url="https://example.com/2")
        ),
    )
    assert backend.last_screenshot_bytes("b-1") == first
    assert backend.last_screenshot_bytes("b-2") == b"frame-two"

    responses["b-1"] = _action_success(screenshot_base64="not-base64")
    with pytest.raises(HostCdpBackendError):
        await backend.execute_action(
            "b-1", NavigateAction(url="https://example.com/fail")
        )
    assert backend.last_screenshot_bytes("b-1") is None
    assert backend.last_screenshot_bytes("b-2") == b"frame-two"

    await backend.close_session("b-2")
    assert backend.last_screenshot_bytes("b-2") is None


@pytest.mark.asyncio
async def test_cancelled_action_clears_stale_screenshot(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    token_path = tmp_path / "cancel-private" / "token"
    _write_token(token_path)
    backend = HostCdpBrowserBackend(
        HostCdpBackendConfig(
            base_url=BASE_URL,
            token_path=token_path,
            transport=httpx.MockTransport(handler),
        )
    )
    backend._sessions.add("b-1")
    with backend._screenshot_lock:
        backend._screenshot_cache["b-1"] = b"stale"
    task = asyncio.create_task(
        backend.execute_action(
            "b-1", NavigateAction(url="https://example.com/")
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.last_screenshot_bytes("b-1") is None


# ---------------------------------------------------------------------------
# Fake grant store + CDP controller (for HostBridge direct tests)
# ---------------------------------------------------------------------------


class FakeAuthorizationStore:
    def __init__(
        self, grants: dict[str, HostAuthorizationSnapshot] | None = None
    ) -> None:
        self._grants = dict(grants or {})
        self.load_count = 0

    def load_authorization(
        self, session_id: str
    ) -> HostAuthorizationSnapshot | None:
        self.load_count += 1
        return self._grants.get(session_id)

    def set_grant(
        self, session_id: str, grant: HostAuthorizationSnapshot
    ) -> None:
        self._grants[session_id] = grant

    def remove_grant(self, session_id: str) -> None:
        self._grants.pop(session_id, None)


class FakeCdpController:
    def __init__(self) -> None:
        self.targets: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.actions: list[dict[str, Any]] = []
        self._force_closed: set[str] = set()
        self._create_error = False
        self.create_count = 0
        self.close_calls: list[str] = []
        self.shutdown_count = 0
        self.shutdown_result = True
        self.closed_event = threading.Event()
        self.last_deadline: float | None = None
        self.last_cancel_event: threading.Event | None = None

    def create_target(self, profile_ref: str) -> str:
        if self._create_error:
            raise RuntimeError("cdp unavailable")
        self.create_count += 1
        target_id = f"target-{self._next_id}"
        self._next_id += 1
        self.targets[target_id] = {
            "profile_ref": profile_ref,
            "closed": False,
        }
        return target_id

    def close_target(self, target_id: str) -> None:
        self.close_calls.append(target_id)
        if target_id in self.targets:
            self.targets[target_id]["closed"] = True
        self.closed_event.set()

    def execute_action(
        self,
        target_id: str,
        action_type: str,
        action: dict[str, Any],
        document_revision: int,
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        self.last_deadline = deadline_monotonic
        self.last_cancel_event = cancel_event
        if target_id not in self.targets or self.targets[target_id]["closed"]:
            raise TargetClosed()
        if target_id in self._force_closed:
            raise TargetClosed()
        self.actions.append({
            "target_id": target_id,
            "action_type": action_type,
            "action": action,
            "document_revision": document_revision,
        })
        return {
            "action_type": action_type,
            "status": "success",
            "url": "https://example.com/",
            "title": "Example",
            "text": "Page text",
            "elements": [],
            "screenshot_ref": None,
            "warning_code": None,
            "error_code": None,
            "duration_ms": 50,
            "document_revision": document_revision,
        }

    def get_state(
        self,
        target_id: str,
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        self.last_deadline = deadline_monotonic
        self.last_cancel_event = cancel_event
        if target_id not in self.targets or self.targets[target_id]["closed"]:
            raise TargetClosed()
        if target_id in self._force_closed:
            raise TargetClosed()
        return {
            "safe_url": "https://example.com/",
            "title": "Example",
            "status": "active",
            "document_revision": 1,
            "latest_screenshot_ref": None,
        }

    def force_target_closed(self, target_id: str) -> None:
        self._force_closed.add(target_id)

    def shutdown(self) -> bool:
        self.shutdown_count += 1
        return self.shutdown_result

    @property
    def create_error(self) -> bool:
        return self._create_error

    @create_error.setter
    def create_error(self, value: bool) -> None:
        self._create_error = value


def _make_bridge(
    tmp_path: Path,
    *,
    grant_store: FakeAuthorizationStore | None = None,
    cdp: FakeCdpController | None = None,
    max_concurrency: int = 8,
    max_sessions: int = 16,
    expiry_grace_seconds: float = 0.05,
) -> tuple[HostBridge, FakeAuthorizationStore, FakeCdpController]:
    token_path = tmp_path / "private" / "bridge_token"
    _write_token(token_path)
    gs = grant_store or FakeAuthorizationStore()
    controller = cdp or FakeCdpController()
    config = HostBridgeConfig(
        token_path=token_path,
        max_concurrency=max_concurrency,
        max_sessions=max_sessions,
        expiry_grace_seconds=expiry_grace_seconds,
    )
    bridge = HostBridge(
        config,
        authorization_store=gs,
        cdp_controller=controller,
    )
    return bridge, gs, controller


# ===========================================================================
# HostCdpBrowserBackend tests (T12a)
# ===========================================================================


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8766",
        "http://localhost:8766",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "http://bridge.example:8766",
        "http://user@127.0.0.1:8766",
        "http://127.0.0.1:8766/path",
        "http://127.0.0.1:8765?query=1",
        "http://127.0.0.1:8765#frag",
    ],
)
def test_rejects_non_loopback_or_invalid_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="host_bridge_url_invalid"):
        HostCdpBackendConfig(base_url, token=TOKEN)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8766",
        "http://127.0.0.1:8766/",
        "http://host.docker.internal:8766",
        "http://host.docker.internal:8766/",
    ],
)
def test_accepts_loopback_urls(base_url: str) -> None:
    HostCdpBackendConfig(base_url, token=TOKEN)


def test_rejects_both_token_and_token_path() -> None:
    with pytest.raises(ValueError, match="host_bridge_token_invalid"):
        HostCdpBackendConfig(BASE_URL, token=TOKEN, token_path="/tmp/x")


def test_rejects_neither_token_nor_token_path() -> None:
    with pytest.raises(ValueError, match="host_bridge_token_invalid"):
        HostCdpBackendConfig(BASE_URL)


# ---------------------------------------------------------------------------
# Token file security
# ---------------------------------------------------------------------------


def test_token_file_missing_fails_closed(tmp_path: Path) -> None:
    token_path = tmp_path / "nonexistent" / "token"
    config = HostCdpBackendConfig(base_url=BASE_URL, token_path=token_path)
    with pytest.raises(HostCdpBackendError, match="host_bridge_token_invalid"):
        HostCdpBrowserBackend(config)


def test_token_file_world_readable_fails_closed(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    _write_token(token_path)
    os.chmod(token_path, 0o644)
    config = HostCdpBackendConfig(base_url=BASE_URL, token_path=token_path)
    with pytest.raises(HostCdpBackendError, match="host_bridge_token_invalid"):
        HostCdpBrowserBackend(config)


def test_token_file_symlink_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write_token(real)
    link = tmp_path / "link"
    os.symlink(real, link)
    config = HostCdpBackendConfig(base_url=BASE_URL, token_path=link)
    with pytest.raises(HostCdpBackendError, match="host_bridge_token_invalid"):
        HostCdpBrowserBackend(config)


def test_token_file_empty_fails_closed(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_bytes(b"")
    token_path.chmod(0o600)
    config = HostCdpBackendConfig(base_url=BASE_URL, token_path=token_path)
    with pytest.raises(HostCdpBackendError, match="host_bridge_token_invalid"):
        HostCdpBrowserBackend(config)


@pytest.mark.parametrize(
    "raw",
    [
        b"contains space",
        b"contains\ttab",
        b"contains\x00nul",
        b"contains\x1fcontrol",
        b"contains\x7fdel",
        "contains-é".encode("utf-8"),
    ],
)
def test_token_file_rejects_non_header_safe_bytes(
    tmp_path: Path, raw: bytes
) -> None:
    token_path = tmp_path / "token"
    token_path.write_bytes(raw)
    token_path.chmod(0o600)
    with pytest.raises(HostCdpBackendError, match="host_bridge_token_invalid"):
        load_secure_token(token_path)


def test_token_file_accepts_visible_ascii_byte_for_byte(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    expected = bytes(range(0x21, 0x7F))
    token_path.write_bytes(expected + b"\n")
    token_path.chmod(0o600)
    assert load_secure_token(token_path) == expected


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(1, False), (31, False), (32, True), (4096, True), (4097, False)],
)
def test_token_file_length_boundaries(
    tmp_path: Path, length: int, accepted: bool
) -> None:
    token_path = tmp_path / "token"
    token_path.write_bytes(b"!" * length)
    token_path.chmod(0o600)
    if accepted:
        assert load_secure_token(token_path) == b"!" * length
    else:
        with pytest.raises(
            HostCdpBackendError, match="host_bridge_token_invalid"
        ):
            load_secure_token(token_path)


@pytest.mark.asyncio
async def test_client_uses_shared_protocol_version_for_every_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.infrastructure.browser import host_protocol

    fake = FakeBridge()
    backend = _make_backend(tmp_path, fake)
    monkeypatch.setattr(host_protocol, "PROTOCOL_VERSION", "shared-v-next")
    session = _active_session()

    await backend.create_session(session)
    await backend.execute_action(
        session.id, NavigateAction(url="https://example.com/")
    )
    await backend.get_state(session.id)
    await backend.begin_takeover(session.id)
    await backend.end_takeover(session.id)
    await backend.close_session(session.id)

    assert len(fake.requests) == 6
    assert {
        request["payload"]["protocol_version"]
        for request in fake.requests
    } == {"shared-v-next"}


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_with_active_session(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend = _make_backend(tmp_path, fake)
    session = _active_session()
    await backend.create_session(session)
    assert session.id in backend._sessions
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req["path"] == "/v1/browser/session/create"
    assert req["payload"]["session_id"] == session.id
    assert req["payload"]["n_agent_session_id"] == "n-1"
    assert req["payload"]["profile_ref"] == "p-1"
    assert req["payload"]["status"] == "active"


@pytest.mark.asyncio
async def test_create_session_with_pending_authorization_fails(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend = _make_backend(tmp_path, fake)
    session = _pending_session()
    with pytest.raises(HostCdpBackendError, match="session_not_active"):
        await backend.create_session(session)
    assert len(fake.requests) == 0


@pytest.mark.asyncio
async def test_create_session_bridge_returns_error(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/create",
        {"status": "error", "error_code": "grant_not_found"},
    )
    backend = _make_backend(tmp_path, fake)
    with pytest.raises(HostCdpBackendError, match="grant_not_found"):
        await backend.create_session(_active_session())
    assert backend._sessions == set()


# ---------------------------------------------------------------------------
# execute_action
# ---------------------------------------------------------------------------


async def _setup_created(
    tmp_path: Path, fake: FakeBridge | None = None
) -> tuple[HostCdpBrowserBackend, FakeBridge]:
    fake = fake or FakeBridge()
    backend = _make_backend(tmp_path, fake)
    await backend.create_session(_active_session())
    fake.requests.clear()
    return backend, fake


@pytest.mark.asyncio
async def test_execute_action_returns_success(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {
            "action_type": "navigate",
            "status": "success",
            "url": "https://example.com/",
            "title": "Example",
            "text": None,
            "elements": [],
            "screenshot_ref": None,
            "warning_code": None,
            "duration_ms": 100,
            "document_revision": 1,
        },
    )
    backend, fake = await _setup_created(tmp_path, fake)
    result = await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    assert result.status == "success"
    assert result.url == "https://example.com/"
    assert result.action_type == "navigate"
    assert result.document_revision == 1


@pytest.mark.asyncio
async def test_execute_action_unknown_session_returns_error(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    result = await backend.execute_action("unknown-sid", NavigateAction(url="https://example.com/"))
    assert result.status == "error"
    assert result.error_code == "session_not_found"
    assert len(fake.requests) == 0  # no request sent


@pytest.mark.asyncio
async def test_execute_action_grant_expired_returns_error(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {"status": "error", "error_code": "grant_expired"},
    )
    backend, _ = await _setup_created(tmp_path, fake)
    result = await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    assert result.status == "error"
    assert result.error_code == "grant_expired"


@pytest.mark.asyncio
async def test_execute_action_grant_revoked_returns_error(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {"status": "error", "error_code": "grant_revoked"},
    )
    backend, _ = await _setup_created(tmp_path, fake)
    result = await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    assert result.status == "error"
    assert result.error_code == "grant_revoked"


@pytest.mark.asyncio
async def test_execute_action_policy_version_mismatch_returns_error(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {"status": "error", "error_code": "host_policy_version_mismatch"},
    )
    backend, _ = await _setup_created(tmp_path, fake)
    result = await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    assert result.status == "error"
    assert result.error_code == "host_policy_version_mismatch"


@pytest.mark.asyncio
async def test_execute_action_unknown_capability_returns_error(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {"status": "error", "error_code": "unknown_capability"},
    )
    backend, _ = await _setup_created(tmp_path, fake)
    result = await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    assert result.status == "error"
    assert result.error_code == "unknown_capability"


@pytest.mark.asyncio
async def test_execute_action_target_closed_raises_degraded(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {"status": "error", "error_code": "target_closed"},
    )
    backend, _ = await _setup_created(tmp_path, fake)
    with pytest.raises(HostCdpBackendError, match="target_closed"):
        await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))


@pytest.mark.asyncio
async def test_execute_action_bridge_unavailable_raises_degraded(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {"status": "error", "error_code": "host_bridge_unavailable"},
    )
    backend, _ = await _setup_created(tmp_path, fake)
    with pytest.raises(HostCdpBackendError, match="host_bridge_unavailable"):
        await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))


@pytest.mark.asyncio
async def test_execute_action_network_error_raises_degraded(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    token_path = tmp_path / "private" / "token"
    _write_token(token_path)
    config = HostCdpBackendConfig(
        base_url=BASE_URL,
        token_path=token_path,
        transport=httpx.MockTransport(handler),
    )
    backend = HostCdpBrowserBackend(config)
    backend._sessions.add("b-1")
    with pytest.raises(HostCdpBackendError, match="host_bridge_unavailable"):
        await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))


@pytest.mark.asyncio
async def test_execute_action_auth_failed_raises(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/action",
        {"status": "error", "error_code": "auth"},
        status_code=401,
    )
    backend, _ = await _setup_created(tmp_path, fake)
    with pytest.raises(HostCdpBackendError, match="host_bridge_auth_failed"):
        await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_returns_state(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/state",
        {
            "safe_url": "https://example.com/",
            "title": "Example",
            "status": "active",
            "document_revision": 2,
            "latest_screenshot_ref": "ref-1",
        },
    )
    backend, _ = await _setup_created(tmp_path, fake)
    state = await backend.get_state("b-1")
    assert state.safe_url == "https://example.com/"
    assert state.title == "Example"
    assert state.status is BrowserSessionStatus.ACTIVE
    assert state.document_revision == 2
    assert state.latest_screenshot_ref == "ref-1"


@pytest.mark.asyncio
async def test_get_state_unknown_session_returns_closed(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    state = await backend.get_state("unknown-sid")
    assert state.status is BrowserSessionStatus.CLOSED
    assert len(fake.requests) == 0


@pytest.mark.asyncio
async def test_get_state_bridge_error_returns_degraded(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    token_path = tmp_path / "private" / "token"
    _write_token(token_path)
    config = HostCdpBackendConfig(
        base_url=BASE_URL,
        token_path=token_path,
        transport=httpx.MockTransport(handler),
    )
    backend = HostCdpBrowserBackend(config)
    backend._sessions.add("b-1")
    state = await backend.get_state("b-1")
    assert state.status is BrowserSessionStatus.DEGRADED


# ---------------------------------------------------------------------------
# begin_takeover / end_takeover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_takeover_returns_none(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    result = await backend.begin_takeover("b-1")
    assert result is None
    assert len(fake.requests) == 1
    assert fake.requests[0]["path"] == "/v1/browser/session/takeover/begin"


@pytest.mark.asyncio
async def test_begin_takeover_unknown_session_returns_none(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    result = await backend.begin_takeover("unknown-sid")
    assert result is None
    assert len(fake.requests) == 0


@pytest.mark.asyncio
async def test_end_takeover_sends_request(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    await backend.end_takeover("b-1")
    assert len(fake.requests) == 1
    assert fake.requests[0]["path"] == "/v1/browser/session/takeover/end"


@pytest.mark.asyncio
async def test_end_takeover_unknown_session_noop(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    await backend.end_takeover("unknown-sid")
    assert len(fake.requests) == 0


# ---------------------------------------------------------------------------
# close_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_session_sends_request(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    await backend.close_session("b-1")
    assert "b-1" not in backend._sessions
    assert len(fake.requests) == 1
    assert fake.requests[0]["path"] == "/v1/browser/session/close"


@pytest.mark.asyncio
async def test_close_session_bridge_error_swallowed(tmp_path: Path) -> None:
    fake = FakeBridge()
    fake.set_response(
        "/v1/browser/session/close",
        {"status": "error", "error_code": "target_closed"},
    )
    backend, fake = await _setup_created(tmp_path, fake)
    # Should not raise.
    await backend.close_session("b-1")
    assert "b-1" not in backend._sessions


# ---------------------------------------------------------------------------
# Valid grant operates only registered target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_grant_operates_only_registered_target(tmp_path: Path) -> None:
    backend, fake = await _setup_created(tmp_path)
    # Create session b-1 (registered).
    # Execute action for b-1 -> success.
    fake.set_response(
        "/v1/browser/session/action",
        {
            "action_type": "navigate",
            "status": "success",
            "url": "https://example.com/",
            "title": "Example",
            "text": None,
            "elements": [],
            "screenshot_ref": None,
            "warning_code": None,
            "duration_ms": 50,
            "document_revision": 1,
        },
    )
    result = await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    assert result.status == "success"

    # Execute action for unregistered session b-2 -> error.
    result2 = await backend.execute_action("b-2", NavigateAction(url="https://example.com/"))
    assert result2.status == "error"
    assert result2.error_code == "session_not_found"
    # Only one request was sent (for b-1); b-2 was rejected locally.
    assert len(fake.requests) == 1
    assert fake.requests[0]["payload"]["session_id"] == "b-1"


# ---------------------------------------------------------------------------
# Cannot submit arbitrary CDP method/endpoint/target/profile path
# ---------------------------------------------------------------------------


def test_backend_exposes_no_raw_cdp_methods(tmp_path: Path) -> None:
    backend = _make_backend(tmp_path)
    # The public API is exactly the BrowserBackend Protocol.
    public_methods = {
        attr for attr in dir(backend)
        if not attr.startswith("_") and callable(getattr(backend, attr))
    }
    # Must NOT expose raw CDP methods.
    forbidden = {
        "send_cdp_command",
        "connect_to_target",
        "set_profile_path",
        "set_cdp_endpoint",
        "raw_cdp",
        "execute_cdp",
        "connect_over_cdp",
        "new_browser_context",
    }
    leaked = forbidden & public_methods
    assert not leaked, f"backend exposes raw CDP methods: {leaked}"
    # Must expose exactly these.
    required = {
        "create_session",
        "close_session",
        "execute_action",
        "get_state",
        "begin_takeover",
        "end_takeover",
    }
    assert required <= public_methods


@pytest.mark.asyncio
async def test_action_payload_carries_no_cdp_fields(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend, fake = await _setup_created(tmp_path, fake)
    await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    assert len(fake.requests) == 1
    payload = fake.requests[0]["payload"]
    # The action payload must only have known fields.
    assert set(payload) == {"protocol_version", "session_id", "action_type", "action", "document_revision"}
    action = payload["action"]
    # NavigateAction only carries url.
    assert set(action) == {"url"}
    # Must NOT carry CDP-specific fields.
    forbidden_cdp_keys = {"cdp_method", "cdp_endpoint", "target_id", "target", "profile_path", "method", "params"}
    assert not (forbidden_cdp_keys & set(action))


@pytest.mark.asyncio
async def test_create_payload_carries_no_cdp_fields(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend, fake = await _setup_created(tmp_path, fake)
    # create was called in _setup_created; check the first request.
    # Actually _setup_created clears requests, so re-create.
    fake.requests.clear()
    await backend.create_session(_active_session(sid="b-2"))
    payload = fake.requests[0]["payload"]
    assert set(payload) == {"protocol_version", "session_id", "n_agent_session_id", "profile_ref", "status"}
    forbidden = {"cdp_endpoint", "cdp_method", "target_id", "target", "profile_path", "method", "params"}
    assert not (forbidden & set(payload))


# ---------------------------------------------------------------------------
# Bearer token in request header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_sent_in_header(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend, fake = await _setup_created(tmp_path, fake)
    await backend.execute_action("b-1", NavigateAction(url="https://example.com/"))
    req = fake.requests[0]
    assert req["headers"][AUTH_HEADER.lower()] == TOKEN.decode("utf-8")


# ---------------------------------------------------------------------------
# Action serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_action_serialized(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend, fake = await _setup_created(tmp_path, fake)
    await backend.execute_action("b-1", ClickAction(element_ref="ref-1", document_revision=3))
    payload = fake.requests[0]["payload"]
    assert payload["action_type"] == "click"
    assert payload["action"] == {"element_ref": "ref-1", "document_revision": 3}
    assert payload["document_revision"] == 3


@pytest.mark.asyncio
async def test_type_action_serialized(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend, fake = await _setup_created(tmp_path, fake)
    await backend.execute_action(
        "b-1", TypeAction(element_ref="ref-1", document_revision=0, text="hello", clear_first=True)
    )
    payload = fake.requests[0]["payload"]
    assert payload["action_type"] == "type"
    assert payload["action"]["text"] == "hello"
    assert payload["action"]["clear_first"] is True


@pytest.mark.asyncio
async def test_screenshot_action_serialized(tmp_path: Path) -> None:
    fake = FakeBridge()
    backend, fake = await _setup_created(tmp_path, fake)
    await backend.execute_action("b-1", ScreenshotAction(full_page=True))
    payload = fake.requests[0]["payload"]
    assert payload["action_type"] == "screenshot"
    assert payload["action"] == {"full_page": True}


# ===========================================================================
# HostBridge direct tests (T12b)
# ===========================================================================


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_bridge_config_rejects_non_loopback(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    _write_token(token_path)
    with pytest.raises(ValueError, match="host_bridge_loopback_required"):
        HostBridgeConfig(
            token_path=token_path,
            bind_host="0.0.0.0",
        )


def test_bridge_config_does_not_accept_policy_version_or_cdp_endpoint(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "token"
    _write_token(token_path)
    with pytest.raises(TypeError):
        HostBridgeConfig(
            token_path=token_path,
            policy_version="",
            cdp_endpoint="ws://127.0.0.1:9222",
        )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_bridge_authenticate_valid_token(tmp_path: Path) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    assert bridge.authenticate(TOKEN.decode("utf-8")) is True


def test_bridge_authenticate_invalid_token(tmp_path: Path) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    assert bridge.authenticate("wrong-token") is False


def test_bridge_authenticate_none_token(tmp_path: Path) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    assert bridge.authenticate(None) is False


# ---------------------------------------------------------------------------
# Session create via bridge
# ---------------------------------------------------------------------------


def test_bridge_create_session_with_valid_grant(tmp_path: Path) -> None:
    bridge, gs, cdp = _make_bridge(tmp_path)
    gs.set_grant("b-1", _make_grant())
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "active",
        },
    )
    assert status == 200
    assert body["status"] == "ok"
    assert len(cdp.targets) == 1


def test_bridge_create_session_with_pending_status_rejected(tmp_path: Path) -> None:
    bridge, gs, _ = _make_bridge(tmp_path)
    gs.set_grant("b-1", _make_grant())
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "pending_authorization",
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "session_not_active"


def test_bridge_create_session_grant_expired_fail_closed(tmp_path: Path) -> None:
    bridge, gs, _ = _make_bridge(tmp_path)
    gs.set_grant("b-1", _make_grant(expired=True))
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "active",
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "grant_expired"


def test_bridge_create_session_grant_revoked_fail_closed(tmp_path: Path) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "active",
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "grant_not_found"


def test_bridge_create_session_grant_not_found_fail_closed(tmp_path: Path) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "active",
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "grant_not_found"


def test_bridge_create_session_policy_version_mismatch(tmp_path: Path) -> None:
    bridge, gs, _ = _make_bridge(tmp_path)
    gs.set_grant("b-1", _make_grant(policy_version="v0"))
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "active",
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "host_policy_version_mismatch"


def test_bridge_create_session_cdp_unavailable(tmp_path: Path) -> None:
    bridge, gs, cdp = _make_bridge(tmp_path)
    cdp.create_error = True
    gs.set_grant("b-1", _make_grant())
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "active",
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "target_unavailable"


# ---------------------------------------------------------------------------
# Action execution via bridge
# ---------------------------------------------------------------------------


def _bridge_with_session(
    tmp_path: Path,
    grant: HostAuthorizationSnapshot | None = None,
    *,
    cdp: FakeCdpController | None = None,
) -> tuple[HostBridge, FakeAuthorizationStore, FakeCdpController]:
    bridge, gs, controller = _make_bridge(tmp_path, cdp=cdp)
    g = grant or _make_grant()
    gs.set_grant("b-1", g)
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "n_agent_session_id": "n-1",
            "profile_ref": "p-1",
            "status": "active",
        },
    )
    assert body["status"] == "ok"
    return bridge, gs, controller


def test_bridge_action_success(tmp_path: Path) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert status == 200
    assert body["status"] == "success"
    assert body["action_type"] == "navigate"
    assert len(cdp.actions) == 1
    assert cdp.actions[0]["action_type"] == "navigate"


def test_bridge_action_unknown_session_fail_closed(tmp_path: Path) -> None:
    bridge, store, _ = _bridge_with_session(tmp_path)
    store.set_grant(
        "unknown-sid",
        _make_grant(
            session_id="unknown-sid",
            n_agent_id="unknown-agent",
            profile_ref="unknown-profile",
        ),
    )
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "unknown-sid",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "session_not_found"


def test_bridge_action_unknown_capability_fail_closed(tmp_path: Path) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "raw_cdp",
            "action": {"method": "Page.navigate", "params": {}},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "unknown_capability"


def test_bridge_action_grant_expired_fail_closed(tmp_path: Path) -> None:
    bridge, gs, _ = _bridge_with_session(tmp_path)
    # Expire the grant after session creation.
    gs.set_grant("b-1", _make_grant(expired=True))
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "grant_expired"


def test_bridge_action_grant_revoked_fail_closed(tmp_path: Path) -> None:
    bridge, gs, _ = _bridge_with_session(tmp_path)
    gs.remove_grant("b-1")
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "grant_not_found"


def test_bridge_action_policy_version_mismatch_fail_closed(tmp_path: Path) -> None:
    bridge, gs, _ = _bridge_with_session(tmp_path)
    gs.set_grant("b-1", _make_grant(policy_version="v0"))
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "host_policy_version_mismatch"


def test_bridge_action_grant_removed_fail_closed(tmp_path: Path) -> None:
    bridge, gs, _ = _bridge_with_session(tmp_path)
    gs.remove_grant("b-1")
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "grant_not_found"


def test_bridge_action_target_closed_degraded(tmp_path: Path) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    # Force target closed.
    target_id = list(cdp.targets.keys())[0]
    cdp.force_target_closed(target_id)
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "target_closed"
    # Session should be removed (degraded).
    status2, body2 = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body2["error_code"] == "session_not_found"


def test_bridge_action_operates_only_registered_target(tmp_path: Path) -> None:
    """Effective grant only operates the target registered for that session."""
    bridge, gs, cdp = _bridge_with_session(tmp_path)
    # Create a second session with a different target.
    gs.set_grant("b-2", _make_grant(session_id="b-2", n_agent_id="n-2"))
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {
            "protocol_version": "1",
            "session_id": "b-2",
            "n_agent_session_id": "n-2",
            "profile_ref": "p-2",
            "status": "active",
        },
    )
    assert body["status"] == "ok"
    assert len(cdp.targets) == 2

    # Action for b-1 should operate target-1, not target-2.
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://a.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "success"
    assert cdp.actions[-1]["target_id"] == "target-1"

    # Action for b-2 should operate target-2.
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-2",
            "action_type": "navigate",
            "action": {"url": "https://b.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "success"
    assert cdp.actions[-1]["target_id"] == "target-2"


def test_bridge_grant_binding_mismatch_fail_closed(tmp_path: Path) -> None:
    """Grant with wrong n_agent_session_id binding is rejected."""
    bridge, gs, _ = _bridge_with_session(tmp_path)
    # Grant for b-1 but with wrong n_agent_session_id.
    gs.set_grant("b-1", _make_grant(n_agent_id="wrong"))
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "grant_not_found"


def test_bridge_grant_store_error_fail_closed(tmp_path: Path) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)

    class FailingAuthorizationStore:
        def load_authorization(
            self, session_id: str
        ) -> HostAuthorizationSnapshot | None:
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unhealthy"
            )

    bridge._authorization_store = FailingAuthorizationStore()
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["status"] == "error"
    assert body["error_code"] == "host_bridge_unhealthy"


# ---------------------------------------------------------------------------
# State and close via bridge
# ---------------------------------------------------------------------------


def test_bridge_get_state_success(tmp_path: Path) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/state",
        {"protocol_version": "1", "session_id": "b-1"},
    )
    assert status == 200
    assert body["safe_url"] == "https://example.com/"
    assert body["status"] == "active"


def test_bridge_get_state_unknown_session(tmp_path: Path) -> None:
    bridge, store, _ = _bridge_with_session(tmp_path)
    store.set_grant(
        "unknown",
        _make_grant(
            session_id="unknown",
            n_agent_id="unknown-agent",
            profile_ref="unknown-profile",
        ),
    )
    status, body = bridge.handle_request(
        "/v1/browser/session/state",
        {"protocol_version": "1", "session_id": "unknown"},
    )
    assert body["status"] == BrowserSessionStatus.CLOSED.value


def test_bridge_close_session_releases_target(tmp_path: Path) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    assert len(cdp.targets) == 1
    target_id = list(cdp.targets.keys())[0]
    assert cdp.targets[target_id]["closed"] is False

    status, body = bridge.handle_request(
        "/v1/browser/session/close",
        {"protocol_version": "1", "session_id": "b-1"},
    )
    assert body["status"] == "ok"
    assert cdp.targets[target_id]["closed"] is True

    # After close, actions for this session -> session_not_found.
    status, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["error_code"] == "session_not_found"


# ---------------------------------------------------------------------------
# Takeover via bridge
# ---------------------------------------------------------------------------


def test_bridge_takeover_begin_returns_null(tmp_path: Path) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/takeover/begin",
        {"protocol_version": "1", "session_id": "b-1"},
    )
    assert body["status"] == "ok"
    assert body["takeover_url"] is None


def test_bridge_takeover_end_ok(tmp_path: Path) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/takeover/end",
        {"protocol_version": "1", "session_id": "b-1"},
    )
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Invalid requests
# ---------------------------------------------------------------------------


def test_bridge_unknown_path_returns_not_found(tmp_path: Path) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    status, body = bridge.handle_request(
        "/v1/unknown",
        {"protocol_version": "1"},
    )
    assert status == 404
    assert body["error_code"] == "not_found"


def test_bridge_invalid_protocol_version(tmp_path: Path) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/create",
        {"protocol_version": "99", "session_id": "x"},
    )
    assert status == 400


def test_bridge_protocol_validation_uses_shared_protocol_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    monkeypatch.setattr(
        host_bridge_module, "PROTOCOL_VERSION", "shared-test-version"
    )

    shared_status, shared_body = bridge.handle_request(
        "/v1/unknown",
        {"protocol_version": "shared-test-version"},
    )
    stale_status, stale_body = bridge.handle_request(
        "/v1/unknown",
        {"protocol_version": "1"},
    )

    assert shared_status == 404
    assert shared_body["error_code"] == "not_found"
    assert stale_status == 400
    assert stale_body["error_code"] == "host_bridge_invalid_request"


def test_bridge_none_payload(tmp_path: Path) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    status, body = bridge.handle_request("/v1/browser/session/create", None)
    assert status == 400


def test_bridge_shutdown_closes_targets(tmp_path: Path) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    assert len(cdp.targets) == 1
    assert bridge.shutdown() is True
    target_id = list(cdp.targets.keys())[0]
    assert cdp.targets[target_id]["closed"] is True
    assert bridge.healthy is False
    assert cdp.shutdown_count == 1


def test_bridge_propagates_controller_cleanup_failure(
    tmp_path: Path,
) -> None:
    bridge, _, controller = _make_bridge(tmp_path)
    controller.shutdown_result = False

    assert bridge.shutdown() is False
    assert bridge.shutdown() is False
    assert controller.shutdown_count == 1


def test_bridge_reports_target_close_failure_even_if_controller_shutdown_succeeds(
    tmp_path: Path,
) -> None:
    class CloseFailingController(FakeCdpController):
        def close_target(self, target_id: str) -> None:
            raise RuntimeError("private close detail")

    controller = CloseFailingController()
    bridge, _, _ = _bridge_with_session(tmp_path, cdp=controller)

    assert bridge.shutdown() is False
    assert controller.shutdown_count == 1


# ---------------------------------------------------------------------------
# Authoritative authorization snapshots, concurrency, expiry, cancellation
# ---------------------------------------------------------------------------


def _create_payload(
    *,
    session_id: str = "b-1",
    n_agent_session_id: str = "n-1",
    profile_ref: str = "p-1",
    status: str = "active",
) -> dict[str, Any]:
    return {
        "protocol_version": "1",
        "session_id": session_id,
        "n_agent_session_id": n_agent_session_id,
        "profile_ref": profile_ref,
        "status": status,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("n_agent_session_id", "forged-agent"),
        ("profile_ref", "forged-profile"),
        ("status", "paused"),
    ],
)
def test_bridge_create_rejects_authoritative_conflicts_without_pollution(
    tmp_path: Path, field: str, value: str
) -> None:
    bridge, store, cdp = _bridge_with_session(tmp_path)
    original_target = next(iter(cdp.targets))
    payload = _create_payload()
    payload[field] = value

    _, body = bridge.handle_request("/v1/browser/session/create", payload)

    assert body["status"] == "error"
    assert cdp.create_count == 1
    assert cdp.targets[original_target]["closed"] is False
    assert set(cdp.targets) == {original_target}
    _, action = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert action["status"] == "success"
    assert store.load_count >= 3


@pytest.mark.parametrize(
    "forbidden",
    ["cdp_endpoint", "cdp_method", "target_id", "profile_path", "unknown"],
)
def test_bridge_forbidden_fields_never_reach_controller(
    tmp_path: Path, forbidden: str
) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    before = list(cdp.actions)
    payload = {
        "protocol_version": "1",
        "session_id": "b-1",
        "action_type": "navigate",
        "action": {"url": "https://example.com/"},
        "document_revision": 0,
        forbidden: "attacker-controlled",
    }
    _, body = bridge.handle_request("/v1/browser/session/action", payload)
    assert body["error_code"] == "host_bridge_invalid_request"
    assert cdp.actions == before

    nested = dict(payload)
    nested.pop(forbidden)
    nested["action"] = {
        "url": "https://example.com/",
        forbidden: "attacker-controlled",
    }
    _, nested_body = bridge.handle_request(
        "/v1/browser/session/action", nested
    )
    assert nested_body["error_code"] == "host_bridge_invalid_request"
    assert cdp.actions == before


@pytest.mark.parametrize(
    "status",
    [
        BrowserSessionStatus.PAUSED,
        BrowserSessionStatus.TAKEOVER,
        BrowserSessionStatus.DEGRADED,
        BrowserSessionStatus.CLOSED,
    ],
)
@pytest.mark.parametrize(
    "path,payload",
    [
        (
            "/v1/browser/session/action",
            {
                "protocol_version": "1",
                "session_id": "b-1",
                "action_type": "observe",
                "action": {"max_text_chars": 100, "max_elements": 10},
                "document_revision": 0,
            },
        ),
        (
            "/v1/browser/session/state",
            {"protocol_version": "1", "session_id": "b-1"},
        ),
    ],
)
def test_bridge_rereads_snapshot_and_rejects_non_active_state(
    tmp_path: Path,
    status: BrowserSessionStatus,
    path: str,
    payload: dict[str, Any],
) -> None:
    bridge, store, _ = _bridge_with_session(tmp_path)
    store.set_grant("b-1", _make_grant(status=status))
    _, body = bridge.handle_request(path, payload)
    assert body["error_code"] == "session_not_active"


def test_identical_serial_and_concurrent_create_is_idempotent(
    tmp_path: Path,
) -> None:
    bridge, store, cdp = _make_bridge(tmp_path, max_concurrency=8)
    store.set_grant("b-1", _make_grant())
    payload = _create_payload()

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(
                lambda _: bridge.handle_request(
                    "/v1/browser/session/create", dict(payload)
                ),
                range(6),
            )
        )
    assert all(body["status"] == "ok" for _, body in results)
    assert bridge.handle_request("/v1/browser/session/create", payload)[1][
        "status"
    ] == "ok"
    assert cdp.create_count == 1
    assert len(cdp.targets) == 1


def test_close_requires_exact_session_payload_and_no_valid_grant(
    tmp_path: Path,
) -> None:
    bridge, store, cdp = _bridge_with_session(tmp_path)
    target_id = next(iter(cdp.targets))
    store.remove_grant("b-1")
    _, rejected = bridge.handle_request(
        "/v1/browser/session/close",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "target_id": target_id,
        },
    )
    assert rejected["error_code"] == "host_bridge_invalid_request"
    assert cdp.targets[target_id]["closed"] is False

    _, closed = bridge.handle_request(
        "/v1/browser/session/close",
        {"protocol_version": "1", "session_id": "b-1"},
    )
    assert closed["status"] == "ok"
    assert cdp.close_calls.count(target_id) == 1


def test_deadline_and_cancel_event_are_forwarded_unchanged(
    tmp_path: Path,
) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    deadline = time.monotonic() + 10
    cancellation = threading.Event()

    _, action = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
        deadline_monotonic=deadline,
        cancel_event=cancellation,
    )
    assert action["status"] == "success"
    assert cdp.last_deadline is deadline
    assert cdp.last_cancel_event is cancellation

    _, state = bridge.handle_request(
        "/v1/browser/session/state",
        {"protocol_version": "1", "session_id": "b-1"},
        deadline_monotonic=deadline,
        cancel_event=cancellation,
    )
    assert state["status"] == "active"
    assert cdp.last_deadline is deadline
    assert cdp.last_cancel_event is cancellation


def test_action_timeout_waits_for_controller_exit_and_reports_unknown_outcome(
    tmp_path: Path,
) -> None:
    class CancellingController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.exited = threading.Event()

        def execute_action(
            self,
            target_id: str,
            action_type: str,
            action: dict[str, Any],
            document_revision: int,
            *,
            deadline_monotonic: float,
            cancel_event: threading.Event,
        ) -> dict[str, Any]:
            assert cancel_event.wait(timeout=1)
            self.exited.set()
            return super().execute_action(
                target_id,
                action_type,
                action,
                document_revision,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )

    controller = CancellingController()
    bridge, store, _ = _make_bridge(tmp_path, cdp=controller)
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    cancellation = threading.Event()
    _, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "click",
            "action": {"element_ref": "e-1", "document_revision": 0},
            "document_revision": 0,
        },
        deadline_monotonic=time.monotonic() + 0.02,
        cancel_event=cancellation,
    )
    assert controller.exited.is_set()
    assert body["error_code"] == "action_outcome_unknown"


def test_expiry_generation_renewal_invalidates_old_timer_then_reclaims_once(
    tmp_path: Path,
) -> None:
    bridge, store, cdp = _make_bridge(
        tmp_path, expiry_grace_seconds=0.02
    )
    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.08),
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    target_id = next(iter(cdp.targets))
    registered = bridge._sessions["b-1"]
    old_generation = registered.generation

    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.25),
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/state",
        {"protocol_version": "1", "session_id": "b-1"},
    )[1]["status"] == "active"
    bridge._expire_session("b-1", registered, old_generation)
    assert cdp.targets[target_id]["closed"] is False

    store.set_grant("b-1", _make_grant(expired=True))
    assert registered.expiry_timer is not None
    registered.expiry_timer.cancel()
    registered.expiry_deadline_monotonic = time.monotonic() - 1
    bridge._expire_session("b-1", registered, registered.generation)
    assert cdp.closed_event.wait(timeout=1)
    assert cdp.targets[target_id]["closed"] is True
    assert cdp.close_calls.count(target_id) == 1


def test_store_health_failure_is_non_diagnostic_bridge_unhealthy(
    tmp_path: Path,
) -> None:
    bridge, _, cdp = _make_bridge(tmp_path)

    class FailingAuthorizationStore:
        def load_authorization(
            self, session_id: str
        ) -> HostAuthorizationSnapshot | None:
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unhealthy"
            )

    bridge._authorization_store = FailingAuthorizationStore()
    _, body = bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )
    assert body == {"status": "error", "error_code": "host_bridge_unhealthy"}
    assert "sqlite" not in repr(body).lower()
    assert cdp.create_count == 0


def test_registry_global_lock_is_not_held_across_target_creation(
    tmp_path: Path,
) -> None:
    class ParallelCreateController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = threading.Barrier(2)

        def create_target(self, profile_ref: str) -> str:
            self.barrier.wait(timeout=1)
            return super().create_target(profile_ref)

    controller = ParallelCreateController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, max_concurrency=2
    )
    first_session_id = "b-1"
    first_lock = bridge._session_lock(first_session_id)
    second_session_id = next(
        f"parallel-{index}"
        for index in range(10_000)
        if bridge._session_lock(f"parallel-{index}") is not first_lock
    )
    second_n_agent_id = f"n-{second_session_id}"
    second_profile_ref = f"p-{second_session_id}"
    store.set_grant(first_session_id, _make_grant())
    store.set_grant(
        second_session_id,
        _make_grant(
            session_id=second_session_id,
            n_agent_id=second_n_agent_id,
            profile_ref=second_profile_ref,
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/create",
            _create_payload(),
        )
        second = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/create",
            _create_payload(
                session_id=second_session_id,
                n_agent_session_id=second_n_agent_id,
                profile_ref=second_profile_ref,
            ),
        )
        assert first.result(timeout=2)[1]["status"] == "ok"
        assert second.result(timeout=2)[1]["status"] == "ok"
    assert controller.create_count == 2


def test_shutdown_serializes_with_in_progress_create_and_unregisters_once(
    tmp_path: Path,
) -> None:
    class BlockingCreateController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def create_target(self, profile_ref: str) -> str:
            self.entered.set()
            assert self.release.wait(timeout=1)
            return super().create_target(profile_ref)

    controller = BlockingCreateController()
    bridge, store, _ = _make_bridge(tmp_path, cdp=controller)
    store.set_grant("b-1", _make_grant())
    with ThreadPoolExecutor(max_workers=2) as executor:
        creating = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/create",
            _create_payload(),
        )
        assert controller.entered.wait(timeout=1)
        shutting_down = executor.submit(bridge.shutdown)
        assert shutting_down.result(timeout=0.2) is False
        controller.release.set()
        _, body = creating.result(timeout=2)
    assert body["error_code"] == "host_bridge_unhealthy"
    assert controller.create_count == 1
    assert controller.close_calls == ["target-1"]
    assert controller.shutdown_count == 1


def test_expiry_cancels_one_in_flight_action_then_reclaims_once(
    tmp_path: Path,
) -> None:
    class ExpiryAwareController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.acknowledged = threading.Event()

        def execute_action(
            self,
            target_id: str,
            action_type: str,
            action: dict[str, Any],
            document_revision: int,
            *,
            deadline_monotonic: float,
            cancel_event: threading.Event,
        ) -> dict[str, Any]:
            self.entered.set()
            assert cancel_event.wait(timeout=1)
            self.acknowledged.set()
            return super().execute_action(
                target_id,
                action_type,
                action,
                document_revision,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )

    controller = ExpiryAwareController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, expiry_grace_seconds=0.05
    )
    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.05),
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    with ThreadPoolExecutor(max_workers=1) as executor:
        action = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/action",
            {
                "protocol_version": "1",
                "session_id": "b-1",
                "action_type": "click",
                "action": {"element_ref": "e-1", "document_revision": 0},
                "document_revision": 0,
            },
            deadline_monotonic=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )
        assert controller.entered.wait(timeout=1)
        _, body = action.result(timeout=2)
    assert controller.acknowledged.is_set()
    assert body["error_code"] == "action_outcome_unknown"
    assert controller.closed_event.wait(timeout=1)
    assert controller.close_calls == ["target-1"]


def test_expiry_rereads_database_only_renewal_without_request(
    tmp_path: Path,
) -> None:
    bridge, store, cdp = _make_bridge(
        tmp_path, expiry_grace_seconds=0.02
    )
    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.05),
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    target_id = next(iter(cdp.targets))
    registered = bridge._sessions["b-1"]
    old_generation = registered.generation
    assert registered.expiry_timer is not None
    registered.expiry_timer.cancel()
    registered.expiry_deadline_monotonic = time.monotonic() - 1

    # Renewal is committed only to the authoritative store. No bridge request
    # occurs before the old timer fires.
    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.22),
        ),
    )
    bridge._expire_session("b-1", registered, old_generation)
    assert cdp.targets[target_id]["closed"] is False
    assert cdp.close_calls == []

    store.set_grant("b-1", _make_grant(expired=True))
    assert registered.expiry_timer is not None
    registered.expiry_timer.cancel()
    registered.expiry_deadline_monotonic = time.monotonic() - 1
    bridge._expire_session("b-1", registered, registered.generation)
    assert cdp.closed_event.wait(timeout=1)
    assert cdp.close_calls == [target_id]


def test_early_expiry_callback_honors_stored_monotonic_deadline(
    tmp_path: Path,
) -> None:
    bridge, store, cdp = _make_bridge(tmp_path)
    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.25),
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    registered = bridge._sessions["b-1"]
    generation = registered.generation
    load_count = store.load_count

    bridge._expire_session("b-1", registered, generation)

    assert registered.generation == generation
    assert store.load_count == load_count
    assert cdp.close_calls == []


def test_reached_monotonic_deadline_ignores_wall_clock_rollback_without_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, cdp = _make_bridge(
        tmp_path, expiry_grace_seconds=0
    )
    original_expiry = datetime.now(timezone.utc) + timedelta(minutes=1)
    store.set_grant(
        "b-1",
        replace(_make_grant(), expires_at=original_expiry),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    registered = bridge._sessions["b-1"]
    generation = registered.generation
    assert registered.expiry_timer is not None
    registered.expiry_timer.cancel()
    registered.expiry_deadline_monotonic = time.monotonic() - 1

    rollback_now = datetime.now(timezone.utc) - timedelta(hours=1)

    class RollbackDateTime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            if tz is None:
                return rollback_now.replace(tzinfo=None)
            return rollback_now.astimezone(tz)

    monkeypatch.setattr(host_bridge_module, "datetime", RollbackDateTime)
    monkeypatch.setattr(host_grant_store_module, "datetime", RollbackDateTime)

    bridge._expire_session("b-1", registered, generation)

    assert cdp.closed_event.wait(timeout=1)
    assert cdp.close_calls == ["target-1"]
    assert "b-1" not in bridge._sessions


def test_request_cannot_extend_reached_deadline_after_wall_clock_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, cdp = _make_bridge(tmp_path)
    original_expiry = datetime.now(timezone.utc) + timedelta(minutes=1)
    store.set_grant(
        "b-1",
        replace(_make_grant(), expires_at=original_expiry),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    registered = bridge._sessions["b-1"]
    generation = registered.generation
    assert registered.expiry_timer is not None
    registered.expiry_timer.cancel()
    reached_deadline = time.monotonic() - 1
    registered.expiry_deadline_monotonic = reached_deadline

    rollback_now = datetime.now(timezone.utc) - timedelta(hours=1)

    class RollbackDateTime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            if tz is None:
                return rollback_now.replace(tzinfo=None)
            return rollback_now.astimezone(tz)

    monkeypatch.setattr(host_bridge_module, "datetime", RollbackDateTime)
    monkeypatch.setattr(host_grant_store_module, "datetime", RollbackDateTime)

    _, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )

    assert body["error_code"] == "grant_expired"
    assert registered.generation == generation
    assert registered.expiry_deadline_monotonic == reached_deadline
    assert cdp.actions == []


def test_unchanged_authoritative_expiry_preserves_generation_deadline_and_timer(
    tmp_path: Path,
) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    registered = bridge._sessions["b-1"]
    generation = registered.generation
    deadline = registered.expiry_deadline_monotonic
    timer = registered.expiry_timer

    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    assert bridge.handle_request(
        "/v1/browser/session/state",
        {"protocol_version": "1", "session_id": "b-1"},
    )[1]["status"] == "active"
    assert bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "observe",
            "action": {"max_text_chars": 100, "max_elements": 10},
            "document_revision": 0,
        },
    )[1]["status"] == "success"

    assert registered.generation == generation
    assert registered.expiry_deadline_monotonic == deadline
    assert registered.expiry_timer is timer


def test_earlier_authoritative_expiry_is_fail_closed_without_extension(
    tmp_path: Path,
) -> None:
    original_expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
    bridge, store, cdp = _make_bridge(tmp_path)
    store.set_grant(
        "b-1", replace(_make_grant(), expires_at=original_expiry)
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    registered = bridge._sessions["b-1"]
    generation = registered.generation
    deadline = registered.expiry_deadline_monotonic
    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=original_expiry - timedelta(seconds=30),
        ),
    )

    _, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "navigate",
            "action": {"url": "https://example.com/"},
            "document_revision": 0,
        },
    )
    assert body["error_code"] == "grant_expired"
    assert registered.generation == generation
    assert registered.expiry_deadline_monotonic == deadline
    assert cdp.actions == []


def test_genuine_renewal_maps_deadline_by_authoritative_delta_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
    bridge, store, _ = _make_bridge(tmp_path)
    store.set_grant(
        "b-1", replace(_make_grant(), expires_at=original_expiry)
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    registered = bridge._sessions["b-1"]
    old_generation = registered.generation
    old_deadline = registered.expiry_deadline_monotonic
    renewal_delta = timedelta(seconds=30)
    store.set_grant(
        "b-1",
        replace(
            _make_grant(), expires_at=original_expiry + renewal_delta
        ),
    )
    rollback_now = datetime.now(timezone.utc) - timedelta(hours=1)

    class RollbackDateTime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            if tz is None:
                return rollback_now.replace(tzinfo=None)
            return rollback_now.astimezone(tz)

    monkeypatch.setattr(host_bridge_module, "datetime", RollbackDateTime)
    monkeypatch.setattr(host_grant_store_module, "datetime", RollbackDateTime)

    _, body = bridge.handle_request(
        "/v1/browser/session/state",
        {"protocol_version": "1", "session_id": "b-1"},
    )
    assert body["status"] == "active"
    assert registered.generation == old_generation + 1
    assert registered.expiry_deadline_monotonic == pytest.approx(
        old_deadline + renewal_delta.total_seconds(), abs=0.01
    )


def test_invalid_session_id_churn_keeps_lock_pool_bounded(
    tmp_path: Path,
) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    for index in range(2_000):
        bridge.handle_request(
            "/v1/browser/session/close",
            {"protocol_version": "1", "session_id": f"invalid-{index}"},
        )
    assert len(bridge._session_locks) <= 64


@pytest.mark.parametrize(
    "action_type,action,revision",
    [
        ("navigate", {"url": ""}, 0),
        ("navigate", {"url": None}, 0),
        ("navigate", {"url": "bad\nurl"}, 0),
        ("click", {"element_ref": "", "document_revision": 0}, 0),
        ("click", {"element_ref": "e", "document_revision": True}, 0),
        ("click", {"element_ref": "e", "document_revision": 1}, 0),
        (
            "type",
            {
                "element_ref": "e",
                "document_revision": 0,
                "text": "",
                "clear_first": False,
            },
            0,
        ),
        (
            "type",
            {
                "element_ref": "e",
                "document_revision": 0,
                "text": "x",
                "clear_first": 1,
            },
            0,
        ),
        ("observe", {"max_text_chars": True, "max_elements": 10}, 0),
        ("observe", {"max_text_chars": 20_001, "max_elements": 10}, 0),
        ("observe", {"max_text_chars": 10, "max_elements": 0}, 0),
        (
            "scroll",
            {
                "element_ref": None,
                "document_revision": 0,
                "dx": True,
                "dy": 0,
            },
            0,
        ),
        (
            "scroll",
            {
                "element_ref": None,
                "document_revision": 0,
                "dx": 1_000_001,
                "dy": 0,
            },
            0,
        ),
        (
            "scroll",
            {
                "element_ref": None,
                "document_revision": 1,
                "dx": 0,
                "dy": 0,
            },
            0,
        ),
        ("screenshot", {"full_page": 1}, 0),
        ("navigate", {"url": "https://example.com/"}, True),
        ("navigate", {"url": "https://example.com/"}, -1),
    ],
)
def test_action_values_are_validated_before_authorization_and_controller(
    tmp_path: Path,
    action_type: str,
    action: dict[str, Any],
    revision: int,
) -> None:
    bridge, store, cdp = _bridge_with_session(tmp_path)
    loads_before = store.load_count
    _, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": action_type,
            "action": action,
            "document_revision": revision,
        },
    )
    assert body["error_code"] == "host_bridge_invalid_request"
    assert store.load_count == loads_before
    assert cdp.actions == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_bridge_config_rejects_non_finite_limits(
    tmp_path: Path, value: float
) -> None:
    token_path = tmp_path / "token"
    _write_token(token_path)
    with pytest.raises(ValueError, match="host_bridge_limits_invalid"):
        HostBridgeConfig(
            token_path=token_path, expiry_grace_seconds=value
        )
    with pytest.raises(ValueError, match="host_bridge_limits_invalid"):
        HostBridgeConfig(
            token_path=token_path,
            default_request_timeout_seconds=value,
        )


@pytest.mark.parametrize(
    "deadline", [True, float("nan"), float("inf"), float("-inf")]
)
def test_bridge_rejects_invalid_or_unbounded_request_deadline(
    tmp_path: Path, deadline: float
) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    status, body = bridge.handle_request(
        "/v1/browser/session/close",
        {"protocol_version": "1", "session_id": "b-1"},
        deadline_monotonic=deadline,
        cancel_event=threading.Event(),
    )
    assert status == 400
    assert body["error_code"] == "host_bridge_invalid_request"


def test_shutdown_returns_while_create_target_never_returns(
    tmp_path: Path,
) -> None:
    class NeverCreateController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def create_target(self, profile_ref: str) -> str:
            self.entered.set()
            self.release.wait()
            return super().create_target(profile_ref)

    controller = NeverCreateController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, expiry_grace_seconds=0.02
    )
    store.set_grant("b-1", _make_grant())
    creator = threading.Thread(
        target=bridge.handle_request,
        args=("/v1/browser/session/create", _create_payload()),
        daemon=True,
    )
    creator.start()
    assert controller.entered.wait(timeout=1)
    shutdown_done = threading.Event()
    shutdown_thread = threading.Thread(
        target=lambda: (bridge.shutdown(), shutdown_done.set()),
        daemon=True,
    )
    shutdown_thread.start()
    returned_in_time = shutdown_done.wait(timeout=0.2)
    controller.release.set()
    creator.join(timeout=1)
    shutdown_thread.join(timeout=1)
    assert returned_in_time
    assert controller.close_calls == ["target-1"]


def test_shutdown_returns_while_action_never_acknowledges_cancel(
    tmp_path: Path,
) -> None:
    class NeverActionController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def execute_action(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.entered.set()
            self.release.wait()
            return super().execute_action(*args, **kwargs)

    controller = NeverActionController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, expiry_grace_seconds=0.02
    )
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    worker = threading.Thread(
        target=bridge.handle_request,
        args=(
            "/v1/browser/session/action",
            {
                "protocol_version": "1",
                "session_id": "b-1",
                "action_type": "click",
                "action": {"element_ref": "e", "document_revision": 0},
                "document_revision": 0,
            },
        ),
        daemon=True,
    )
    worker.start()
    assert controller.entered.wait(timeout=1)
    shutdown_done = threading.Event()
    shutdown_thread = threading.Thread(
        target=lambda: (bridge.shutdown(), shutdown_done.set()),
        daemon=True,
    )
    shutdown_thread.start()
    returned_in_time = shutdown_done.wait(timeout=0.2)
    controller.release.set()
    worker.join(timeout=1)
    shutdown_thread.join(timeout=1)
    assert returned_in_time
    assert controller.close_calls == ["target-1"]


def test_shutdown_waits_for_delayed_close_worker_dispatch_after_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NeverActionController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.action_entered = threading.Event()
            self.release_action = threading.Event()
            self.order: list[str] = []
            self.checkpoint = threading.Event()

        def execute_action(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.action_entered.set()
            self.release_action.wait()
            return super().execute_action(*args, **kwargs)

        def close_target(self, target_id: str) -> None:
            self.order.append("close")
            super().close_target(target_id)

        def shutdown(self) -> bool:
            self.order.append("shutdown")
            self.checkpoint.set()
            return super().shutdown()

    controller = NeverActionController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, expiry_grace_seconds=0.01
    )
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    action = threading.Thread(
        target=bridge.handle_request,
        args=(
            "/v1/browser/session/action",
            {
                "protocol_version": "1",
                "session_id": "b-1",
                "action_type": "observe",
                "action": {"max_text_chars": 100, "max_elements": 10},
                "document_revision": 0,
            },
        ),
        daemon=True,
    )
    action.start()
    assert controller.action_entered.wait(timeout=1)

    real_thread = threading.Thread
    close_worker_captured = threading.Event()
    release_close_worker = threading.Event()
    prepared_captured = threading.Event()

    class WaitReportingEvent:
        def __init__(self) -> None:
            self._event = threading.Event()

        def is_set(self) -> bool:
            return self._event.is_set()

        def set(self) -> None:
            self._event.set()

        def wait(self, timeout: float | None = None) -> bool:
            controller.checkpoint.set()
            return self._event.wait(timeout)

    class DelayedStartThread:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._inner = real_thread(*args, **kwargs)

        def start(self) -> None:
            close_worker_captured.set()

            def delayed_start() -> None:
                assert release_close_worker.wait(timeout=1)
                self._inner.start()

            real_thread(target=delayed_start, daemon=True).start()

    monkeypatch.setattr(
        host_bridge_module.threading, "Thread", DelayedStartThread
    )
    real_prepare = bridge._prepare_reserved_target_close

    def recording_prepare(target_id: str) -> object:
        prepared = real_prepare(target_id)
        prepared.dispatched = WaitReportingEvent()  # type: ignore[assignment]
        prepared_captured.set()
        return prepared

    bridge._prepare_reserved_target_close = recording_prepare  # type: ignore[method-assign]
    shutdown_done = threading.Event()
    shutting_down = real_thread(
        target=lambda: (bridge.shutdown(), shutdown_done.set()),
        daemon=True,
    )
    shutting_down.start()
    assert close_worker_captured.wait(timeout=1)
    assert prepared_captured.wait(timeout=1)
    assert controller.checkpoint.wait(timeout=1)

    release_close_worker.set()
    assert shutdown_done.wait(timeout=1)
    assert controller.order == ["close", "shutdown"]
    assert controller.close_calls == ["target-1"]
    assert controller.shutdown_count == 1
    controller.release_action.set()
    action.join(timeout=1)


def test_shutdown_dispatch_ack_failure_preserves_registration_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, controller = _make_bridge(
        tmp_path, expiry_grace_seconds=0
    )
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"

    real_thread = threading.Thread
    delayed_workers: list[threading.Thread] = []
    worker_captured = threading.Event()

    class NeverScheduledThread:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._inner = real_thread(*args, **kwargs)

        def start(self) -> None:
            delayed_workers.append(self._inner)
            worker_captured.set()

    monkeypatch.setattr(
        host_bridge_module.threading, "Thread", NeverScheduledThread
    )
    bridge.shutdown()

    assert worker_captured.is_set()
    assert not bridge.healthy
    assert "b-1" in bridge._sessions
    assert controller.close_calls == []
    assert controller.shutdown_count == 0

    monkeypatch.undo()
    delayed_workers[0].start()
    assert bridge._controller_jobs_idle.wait(timeout=1)
    assert controller.close_calls == []

    bridge.shutdown()
    assert controller.closed_event.wait(timeout=1)
    assert bridge._sessions == {}
    assert controller.close_calls == ["target-1"]
    assert controller.shutdown_count == 1


def test_concurrent_shutdown_claims_each_target_cleanup_once(
    tmp_path: Path,
) -> None:
    bridge, store, controller = _make_bridge(tmp_path)
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"

    real_commit = bridge._commit_prepared_target_close
    calls_lock = threading.Lock()
    first_commit_entered = threading.Event()
    release_first_commit = threading.Event()
    commit_calls = 0

    def pause_first_commit(prepared: object) -> threading.Event:
        nonlocal commit_calls
        with calls_lock:
            commit_calls += 1
            call_number = commit_calls
        if call_number == 1:
            first_commit_entered.set()
            assert release_first_commit.wait(timeout=1)
        return real_commit(prepared)  # type: ignore[arg-type]

    bridge._commit_prepared_target_close = pause_first_commit  # type: ignore[method-assign]
    results: list[bool] = []
    first = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    second = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    first.start()
    assert first_commit_entered.wait(timeout=1)
    second.start()
    assert bridge._shutdown_waiter_present.wait(timeout=1)

    release_first_commit.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert sorted(results) == [True, True]
    assert controller.close_calls == ["target-1"]
    assert controller.shutdown_count == 1
    assert bridge._sessions == {}


def test_concurrent_shutdown_split_claims_tears_controller_down_once(
    tmp_path: Path,
) -> None:
    bridge, store, controller = _make_bridge(tmp_path)
    store.set_grant("b-1", _make_grant())
    store.set_grant(
        "b-2",
        _make_grant(
            session_id="b-2",
            n_agent_id="n-2",
            profile_ref="p-2",
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    assert bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(
            session_id="b-2",
            n_agent_session_id="n-2",
            profile_ref="p-2",
        ),
    )[1]["status"] == "ok"

    real_commit = bridge._commit_prepared_target_close
    first_commit_entered = threading.Event()
    release_first_commit = threading.Event()
    commit_lock = threading.Lock()
    commit_calls = 0

    def pause_first_commit(prepared: object) -> threading.Event:
        nonlocal commit_calls
        with commit_lock:
            commit_calls += 1
            call_number = commit_calls
        if call_number == 1:
            first_commit_entered.set()
            assert release_first_commit.wait(timeout=1)
        return real_commit(prepared)  # type: ignore[arg-type]

    bridge._commit_prepared_target_close = pause_first_commit  # type: ignore[method-assign]
    results: list[bool] = []
    first = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    second = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    first.start()
    assert first_commit_entered.wait(timeout=1)
    second.start()
    assert bridge._shutdown_waiter_present.wait(timeout=1)
    assert controller.close_calls == []
    assert controller.shutdown_count == 0

    release_first_commit.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert sorted(results) == [True, True]
    assert sorted(controller.close_calls) == ["target-1", "target-2"]
    assert controller.shutdown_count == 1
    assert bridge._sessions == {}


def test_overlapping_shutdown_callers_share_successful_bool_outcome(
    tmp_path: Path,
) -> None:
    bridge, store, controller = _make_bridge(tmp_path)
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    real_commit = bridge._commit_prepared_target_close
    owner_in_commit = threading.Event()
    release_owner = threading.Event()

    def blocked_commit(prepared: object) -> threading.Event:
        owner_in_commit.set()
        assert release_owner.wait(timeout=1)
        return real_commit(prepared)  # type: ignore[arg-type]

    bridge._commit_prepared_target_close = blocked_commit  # type: ignore[method-assign]
    results: list[bool] = []
    owner = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    waiter = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    owner.start()
    assert owner_in_commit.wait(timeout=1)
    waiter.start()
    try:
        assert bridge._shutdown_waiter_present.wait(timeout=1)
    finally:
        release_owner.set()
    owner.join(timeout=1)
    waiter.join(timeout=1)

    assert sorted(results) == [True, True]
    assert controller.close_calls == ["target-1"]
    assert controller.shutdown_count == 1


def test_overlapping_and_late_shutdown_callers_share_sticky_failure(
    tmp_path: Path,
) -> None:
    class CloseFailingController(FakeCdpController):
        def close_target(self, target_id: str) -> None:
            raise RuntimeError("close failed")

    controller = CloseFailingController()
    bridge, store, _ = _make_bridge(tmp_path, cdp=controller)
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    real_commit = bridge._commit_prepared_target_close
    owner_in_commit = threading.Event()
    release_owner = threading.Event()

    def blocked_commit(prepared: object) -> threading.Event:
        owner_in_commit.set()
        assert release_owner.wait(timeout=1)
        return real_commit(prepared)  # type: ignore[arg-type]

    bridge._commit_prepared_target_close = blocked_commit  # type: ignore[method-assign]
    results: list[bool] = []
    owner = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    waiter = threading.Thread(
        target=lambda: results.append(bridge.shutdown()),
        daemon=True,
    )
    owner.start()
    assert owner_in_commit.wait(timeout=1)
    waiter.start()
    assert bridge._shutdown_waiter_present.wait(timeout=1)
    release_owner.set()
    owner.join(timeout=1)
    waiter.join(timeout=1)

    assert sorted(results) == [False, False]
    assert bridge.shutdown() is False
    assert controller.shutdown_count == 1


def test_expiry_grace_is_bounded_when_controller_never_acknowledges(
    tmp_path: Path,
) -> None:
    class NonAcknowledgingController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def execute_action(
            self,
            target_id: str,
            action_type: str,
            action: dict[str, Any],
            document_revision: int,
            *,
            deadline_monotonic: float,
            cancel_event: threading.Event,
        ) -> dict[str, Any]:
            self.entered.set()
            assert self.release.wait(timeout=2)
            return super().execute_action(
                target_id,
                action_type,
                action,
                document_revision,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )

    controller = NonAcknowledgingController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, expiry_grace_seconds=0.03
    )
    store.set_grant(
        "b-1",
        replace(
            _make_grant(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.04),
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    with ThreadPoolExecutor(max_workers=1) as executor:
        action = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/action",
            {
                "protocol_version": "1",
                "session_id": "b-1",
                "action_type": "click",
                "action": {"element_ref": "e-1", "document_revision": 0},
                "document_revision": 0,
            },
            deadline_monotonic=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )
        assert controller.entered.wait(timeout=1)
        assert controller.closed_event.wait(timeout=1)
        assert controller.close_calls == ["target-1"]
        assert not action.done()
        controller.release.set()
        _, body = action.result(timeout=1)
    assert body["error_code"] == "action_outcome_unknown"
    assert controller.close_calls == ["target-1"]


def test_database_renewal_during_in_flight_grace_invalidates_old_cleanup(
    tmp_path: Path,
) -> None:
    class GraceController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.current_cancel: threading.Event | None = None

        def execute_action(
            self,
            target_id: str,
            action_type: str,
            action: dict[str, Any],
            document_revision: int,
            *,
            deadline_monotonic: float,
            cancel_event: threading.Event,
        ) -> dict[str, Any]:
            self.current_cancel = cancel_event
            self.entered.set()
            assert self.release.wait(timeout=2)
            return super().execute_action(
                target_id,
                action_type,
                action,
                document_revision,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )

    controller = GraceController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, expiry_grace_seconds=1
    )
    original_expiry = datetime.now(timezone.utc) + timedelta(minutes=1)
    store.set_grant(
        "b-1",
        replace(_make_grant(), expires_at=original_expiry),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    with ThreadPoolExecutor(max_workers=1) as executor:
        action = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/action",
            {
                "protocol_version": "1",
                "session_id": "b-1",
                "action_type": "observe",
                "action": {"max_text_chars": 100, "max_elements": 10},
                "document_revision": 0,
            },
            deadline_monotonic=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )
        assert controller.entered.wait(timeout=1)
        registered = bridge._sessions["b-1"]
        assert registered.expiry_timer is not None
        registered.expiry_timer.cancel()
        registered.expiry_deadline_monotonic = time.monotonic() - 1
        store.set_grant("b-1", _make_grant(expired=True))
        expiry = threading.Thread(
            target=bridge._expire_session,
            args=("b-1", registered, registered.generation),
            daemon=True,
        )
        expiry.start()
        assert controller.current_cancel is not None
        assert controller.current_cancel.wait(timeout=1)
        store.set_grant(
            "b-1",
            replace(
                _make_grant(),
                expires_at=original_expiry + timedelta(seconds=30),
            ),
        )
        controller.release.set()
        _, body = action.result(timeout=1)
        expiry.join(timeout=1)
        assert not expiry.is_alive()
    assert body["error_code"] == "host_bridge_timeout"
    assert controller.close_calls == []


@pytest.mark.parametrize(
    "path,payload",
    [
        (
            "/v1/browser/session/action",
            {
                "protocol_version": "1",
                "session_id": "not-registered",
                "action_type": "observe",
                "action": {"max_text_chars": 100, "max_elements": 10},
                "document_revision": 0,
            },
        ),
        (
            "/v1/browser/session/state",
            {"protocol_version": "1", "session_id": "not-registered"},
        ),
    ],
)
def test_absent_registration_still_exposes_authoritative_denials(
    tmp_path: Path, path: str, payload: dict[str, Any]
) -> None:
    bridge, store, _ = _make_bridge(tmp_path)

    _, missing = bridge.handle_request(path, payload)
    assert missing["error_code"] == "grant_not_found"

    store.set_grant(
        "not-registered",
        _make_grant(
            session_id="not-registered",
            policy_version="stale",
            profile_ref="profile-x",
        ),
    )
    _, stale = bridge.handle_request(path, payload)
    assert stale["error_code"] == "host_policy_version_mismatch"

    store.set_grant(
        "not-registered",
        _make_grant(
            session_id="not-registered",
            status=BrowserSessionStatus.PAUSED,
            profile_ref="profile-x",
        ),
    )
    _, paused = bridge.handle_request(path, payload)
    assert paused["error_code"] == "session_not_active"

    class FailingAuthorizationStore:
        def load_authorization(
            self, session_id: str
        ) -> HostAuthorizationSnapshot | None:
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unhealthy"
            )

    bridge._authorization_store = FailingAuthorizationStore()
    _, unhealthy = bridge.handle_request(path, payload)
    assert unhealthy["error_code"] == "host_bridge_unhealthy"


def test_cancelled_before_side_effecting_controller_invocation_is_timeout(
    tmp_path: Path,
) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    cancellation = threading.Event()
    cancellation.set()
    _, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "click",
            "action": {"element_ref": "e-1", "document_revision": 0},
            "document_revision": 0,
        },
        deadline_monotonic=time.monotonic() + 1,
        cancel_event=cancellation,
    )
    assert body["error_code"] == "host_bridge_timeout"
    assert cdp.actions == []


def test_cancelled_observe_after_controller_invocation_is_timeout_not_unknown(
    tmp_path: Path,
) -> None:
    class CancellingObserveController(FakeCdpController):
        def execute_action(
            self,
            target_id: str,
            action_type: str,
            action: dict[str, Any],
            document_revision: int,
            *,
            deadline_monotonic: float,
            cancel_event: threading.Event,
        ) -> dict[str, Any]:
            assert action_type == "observe"
            assert cancel_event.wait(timeout=1)
            return super().execute_action(
                target_id,
                action_type,
                action,
                document_revision,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )

    controller = CancellingObserveController()
    bridge, store, _ = _make_bridge(tmp_path, cdp=controller)
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    _, body = bridge.handle_request(
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "observe",
            "action": {"max_text_chars": 100, "max_elements": 10},
            "document_revision": 0,
        },
        deadline_monotonic=time.monotonic() + 0.02,
        cancel_event=threading.Event(),
    )
    assert body["error_code"] == "host_bridge_timeout"


def test_shutdown_wins_atomic_race_before_create_registration(
    tmp_path: Path,
) -> None:
    class SelectiveGateLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._create_acquires = 0
            self.before_publish = threading.Event()
            self.allow_publish = threading.Event()

        def __enter__(self) -> "SelectiveGateLock":
            if threading.current_thread().name == "creating":
                self._create_acquires += 1
                if self._create_acquires == 2:
                    self.before_publish.set()
                    assert self.allow_publish.wait(timeout=1)
            self._lock.acquire()
            return self

        def __exit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            self._lock.release()

    bridge, store, controller = _make_bridge(tmp_path)
    store.set_grant("b-1", _make_grant())
    gate = SelectiveGateLock()
    bridge._registry_lock = gate  # type: ignore[assignment]
    result: list[tuple[int, dict[str, Any]]] = []
    creating = threading.Thread(
        target=lambda: result.append(
            bridge.handle_request(
                "/v1/browser/session/create", _create_payload()
            )
        ),
        name="creating",
        daemon=True,
    )
    creating.start()
    assert gate.before_publish.wait(timeout=1)

    bridge.shutdown()
    gate.allow_publish.set()
    creating.join(timeout=1)

    assert not creating.is_alive()
    assert result[0][1]["error_code"] == "host_bridge_unhealthy"
    assert bridge._sessions == {}
    assert controller.closed_event.wait(timeout=1)
    assert controller.close_calls == ["target-1"]


def test_shutdown_wins_atomic_race_before_in_flight_admission(
    tmp_path: Path,
) -> None:
    bridge, _, controller = _bridge_with_session(tmp_path)
    before_begin = threading.Event()
    allow_begin = threading.Event()
    real_begin = bridge._begin_in_flight

    def paused_begin(
        registered: object, cancel_event: threading.Event
    ) -> None:
        before_begin.set()
        assert allow_begin.wait(timeout=1)
        real_begin(registered, cancel_event)  # type: ignore[arg-type]

    bridge._begin_in_flight = paused_begin  # type: ignore[method-assign]
    result: list[tuple[int, dict[str, Any]]] = []
    acting = threading.Thread(
        target=lambda: result.append(
            bridge.handle_request(
                "/v1/browser/session/action",
                {
                    "protocol_version": "1",
                    "session_id": "b-1",
                    "action_type": "observe",
                    "action": {
                        "max_text_chars": 100,
                        "max_elements": 10,
                    },
                    "document_revision": 0,
                },
            )
        ),
        daemon=True,
    )
    acting.start()
    assert before_begin.wait(timeout=1)

    bridge.shutdown()
    allow_begin.set()
    acting.join(timeout=1)

    assert not acting.is_alive()
    assert result[0][1]["error_code"] == "host_bridge_unhealthy"
    assert controller.last_deadline is None
    assert controller.actions == []


_ALL_ROUTE_CASES = [
    ("/v1/browser/session/create", _create_payload()),
    (
        "/v1/browser/session/close",
        {"protocol_version": "1", "session_id": "b-1"},
    ),
    (
        "/v1/browser/session/action",
        {
            "protocol_version": "1",
            "session_id": "b-1",
            "action_type": "observe",
            "action": {"max_text_chars": 100, "max_elements": 10},
            "document_revision": 0,
        },
    ),
    (
        "/v1/browser/session/state",
        {"protocol_version": "1", "session_id": "b-1"},
    ),
    (
        "/v1/browser/session/takeover/begin",
        {"protocol_version": "1", "session_id": "b-1"},
    ),
    (
        "/v1/browser/session/takeover/end",
        {"protocol_version": "1", "session_id": "b-1"},
    ),
]


@pytest.mark.parametrize("path,payload", _ALL_ROUTE_CASES)
def test_all_routes_preflight_expired_deadline_before_side_effects(
    tmp_path: Path, path: str, payload: dict[str, Any]
) -> None:
    bridge, store, controller = _bridge_with_session(tmp_path)
    loads_before = store.load_count
    creates_before = controller.create_count
    closes_before = list(controller.close_calls)
    actions_before = list(controller.actions)

    _, body = bridge.handle_request(
        path,
        payload,
        deadline_monotonic=time.monotonic() - 1,
        cancel_event=threading.Event(),
    )

    assert body["error_code"] == "host_bridge_timeout"
    assert store.load_count == loads_before
    assert controller.create_count == creates_before
    assert controller.close_calls == closes_before
    assert controller.actions == actions_before
    assert controller.last_deadline is None


@pytest.mark.parametrize("path,payload", _ALL_ROUTE_CASES)
def test_all_routes_preflight_cancellation_before_side_effects(
    tmp_path: Path, path: str, payload: dict[str, Any]
) -> None:
    bridge, store, controller = _bridge_with_session(tmp_path)
    loads_before = store.load_count
    creates_before = controller.create_count
    closes_before = list(controller.close_calls)
    actions_before = list(controller.actions)
    cancelled = threading.Event()
    cancelled.set()

    _, body = bridge.handle_request(
        path,
        payload,
        deadline_monotonic=time.monotonic() + 1,
        cancel_event=cancelled,
    )

    assert body["error_code"] == "host_bridge_timeout"
    assert store.load_count == loads_before
    assert controller.create_count == creates_before
    assert controller.close_calls == closes_before
    assert controller.actions == actions_before
    assert controller.last_deadline is None


def test_never_returning_create_times_out_frees_admission_and_closes_late_target(
    tmp_path: Path,
) -> None:
    class BlockingCreateController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def create_target(self, profile_ref: str) -> str:
            self.entered.set()
            self.release.wait()
            return super().create_target(profile_ref)

    controller = BlockingCreateController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, max_concurrency=1
    )
    store.set_grant("b-1", _make_grant())
    with ThreadPoolExecutor(max_workers=1) as executor:
        creating = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/create",
            _create_payload(),
            deadline_monotonic=time.monotonic() + 0.03,
            cancel_event=threading.Event(),
        )
        assert controller.entered.wait(timeout=1)
        try:
            _, body = creating.result(timeout=0.2)
        finally:
            controller.release.set()
    assert body["error_code"] == "host_bridge_timeout"

    _, admitted = bridge.handle_request(
        "/v1/browser/session/close",
        {"protocol_version": "1", "session_id": "other"},
    )
    assert admitted["status"] == "ok"
    assert controller.closed_event.wait(timeout=1)
    assert bridge._sessions == {}
    assert controller.close_calls == ["target-1"]


def test_colliding_session_lock_wait_honors_absolute_deadline(
    tmp_path: Path,
) -> None:
    bridge, _, _ = _bridge_with_session(tmp_path)
    first_id = "b-1"
    first_lock = bridge._session_lock(first_id)
    collision = next(
        f"collision-{index}"
        for index in range(10_000)
        if bridge._session_lock(f"collision-{index}") is first_lock
    )
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with first_lock:
            acquired.set()
            assert release.wait(timeout=1)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert acquired.wait(timeout=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/close",
            {"protocol_version": "1", "session_id": collision},
            deadline_monotonic=time.monotonic() + 0.03,
            cancel_event=threading.Event(),
        )
        try:
            _, body = waiting.result(timeout=0.2)
        finally:
            release.set()
    holder.join(timeout=1)
    assert body["error_code"] == "host_bridge_timeout"


def test_close_timeout_unpublishes_target_and_continues_cleanup(
    tmp_path: Path,
) -> None:
    class BlockingCloseController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.close_entered = threading.Event()
            self.release_close = threading.Event()

        def close_target(self, target_id: str) -> None:
            self.close_entered.set()
            self.release_close.wait()
            super().close_target(target_id)

    controller = BlockingCloseController()
    bridge, store, _ = _make_bridge(tmp_path, cdp=controller)
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    with ThreadPoolExecutor(max_workers=1) as executor:
        closing = executor.submit(
            bridge.handle_request,
            "/v1/browser/session/close",
            {"protocol_version": "1", "session_id": "b-1"},
            deadline_monotonic=time.monotonic() + 0.03,
            cancel_event=threading.Event(),
        )
        assert controller.close_entered.wait(timeout=1)
        try:
            _, body = closing.result(timeout=0.2)
        finally:
            controller.release_close.set()
    assert body["error_code"] == "host_bridge_timeout"
    assert "b-1" not in bridge._sessions
    assert controller.closed_event.wait(timeout=1)
    assert controller.close_calls == ["target-1"]


def test_repeated_hung_create_jobs_have_bounded_worker_capacity(
    tmp_path: Path,
) -> None:
    capacity = 8

    class HungCreateController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()
            self._counts_lock = threading.Lock()
            self.create_entered_count = 0
            self.closed_count = 0
            self.all_closed = threading.Event()

        def create_target(self, profile_ref: str) -> str:
            with self._counts_lock:
                self.create_entered_count += 1
            self.release.wait()
            return super().create_target(profile_ref)

        def close_target(self, target_id: str) -> None:
            super().close_target(target_id)
            with self._counts_lock:
                self.closed_count += 1
                if self.closed_count == capacity:
                    self.all_closed.set()

    controller = HungCreateController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, max_concurrency=1
    )
    store.set_grant("b-1", _make_grant())
    try:
        for _ in range(capacity + 3):
            _, body = bridge.handle_request(
                "/v1/browser/session/create",
                _create_payload(),
                deadline_monotonic=time.monotonic() + 0.02,
                cancel_event=threading.Event(),
            )
            assert body["error_code"] == "host_bridge_timeout"

        assert controller.create_entered_count == capacity
        assert bridge._controller_jobs_active == capacity
        assert bridge._active_requests == 0
        _, unrelated = bridge.handle_request(
            "/v1/browser/session/close",
            {"protocol_version": "1", "session_id": "missing"},
        )
        assert unrelated["status"] == "ok"
    finally:
        controller.release.set()

    assert controller.all_closed.wait(timeout=1)
    assert bridge._controller_jobs_idle.wait(timeout=1)
    assert bridge._controller_jobs_active == 0
    assert bridge._sessions == {}


def test_repeated_hung_close_jobs_keep_unreserved_target_owned(
    tmp_path: Path,
) -> None:
    capacity = 8

    class HungCloseController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.block_close = False
            self.release = threading.Event()
            self._counts_lock = threading.Lock()
            self.close_entered_count = 0
            self.close_completed_count = 0
            self.capacity_completed = threading.Event()

        def close_target(self, target_id: str) -> None:
            if self.block_close:
                with self._counts_lock:
                    self.close_entered_count += 1
                self.release.wait()
            super().close_target(target_id)
            with self._counts_lock:
                self.close_completed_count += 1
                if self.close_completed_count == capacity:
                    self.capacity_completed.set()

    controller = HungCloseController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, max_concurrency=1
    )
    session_ids = [f"bounded-close-{index}" for index in range(capacity + 1)]
    for session_id in session_ids:
        n_agent_id = f"n-{session_id}"
        profile_ref = f"p-{session_id}"
        store.set_grant(
            session_id,
            _make_grant(
                session_id=session_id,
                n_agent_id=n_agent_id,
                profile_ref=profile_ref,
            ),
        )
        assert bridge.handle_request(
            "/v1/browser/session/create",
            _create_payload(
                session_id=session_id,
                n_agent_session_id=n_agent_id,
                profile_ref=profile_ref,
            ),
        )[1]["status"] == "ok"

    controller.block_close = True
    try:
        for session_id in session_ids:
            _, body = bridge.handle_request(
                "/v1/browser/session/close",
                {"protocol_version": "1", "session_id": session_id},
                deadline_monotonic=time.monotonic() + 0.02,
                cancel_event=threading.Event(),
            )
            assert body["error_code"] == "host_bridge_timeout"

        retained_session_id = session_ids[-1]
        assert controller.close_entered_count == capacity
        assert bridge._controller_jobs_active == capacity
        assert retained_session_id in bridge._sessions
        _, unrelated = bridge.handle_request(
            "/v1/browser/session/takeover/begin",
            {"protocol_version": "1", "session_id": retained_session_id},
        )
        assert unrelated["status"] == "ok"

        shutdown_done = threading.Event()
        shutting_down = threading.Thread(
            target=lambda: (bridge.shutdown(), shutdown_done.set()),
            daemon=True,
        )
        shutting_down.start()
        assert shutdown_done.wait(timeout=0.2)
        assert not bridge.healthy
        assert retained_session_id in bridge._sessions
    finally:
        controller.release.set()

    assert controller.capacity_completed.wait(timeout=1)
    assert bridge._controller_jobs_idle.wait(timeout=1)
    _, retried = bridge.handle_request(
        "/v1/browser/session/close",
        {
            "protocol_version": "1",
            "session_id": session_ids[-1],
        },
    )
    assert retried["status"] == "ok"
    assert controller.closed_event.wait(timeout=1)
    assert bridge._sessions == {}


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, 65])
def test_bridge_config_rejects_invalid_or_unbounded_max_sessions(
    tmp_path: Path, value: object
) -> None:
    token_path = tmp_path / "token"
    _write_token(token_path)
    with pytest.raises(ValueError, match="host_bridge_limits_invalid"):
        HostBridgeConfig(token_path=token_path, max_sessions=value)  # type: ignore[arg-type]


def test_session_capacity_bounds_targets_timers_and_releases_on_close(
    tmp_path: Path,
) -> None:
    bridge, store, controller = _make_bridge(tmp_path, max_sessions=2)
    for index in range(1, 4):
        session_id = f"b-{index}"
        store.set_grant(
            session_id,
            _make_grant(
                session_id=session_id,
                n_agent_id=f"n-{index}",
                profile_ref=f"p-{index}",
            ),
        )
    for index in (1, 2):
        assert bridge.handle_request(
            "/v1/browser/session/create",
            _create_payload(
                session_id=f"b-{index}",
                n_agent_session_id=f"n-{index}",
                profile_ref=f"p-{index}",
            ),
        )[1]["status"] == "ok"

    _, duplicate = bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )
    _, full = bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(
            session_id="b-3",
            n_agent_session_id="n-3",
            profile_ref="p-3",
        ),
    )
    assert duplicate["status"] == "ok"
    assert full["error_code"] == "host_bridge_busy"
    assert controller.create_count == 2
    assert len(bridge._sessions) == 2
    assert all(
        registered.expiry_timer is not None
        for registered in bridge._sessions.values()
    )

    assert bridge.handle_request(
        "/v1/browser/session/close",
        {"protocol_version": "1", "session_id": "b-1"},
    )[1]["status"] == "ok"
    assert bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(
            session_id="b-3",
            n_agent_session_id="n-3",
            profile_ref="p-3",
        ),
    )[1]["status"] == "ok"
    assert len(bridge._sessions) == 2
    assert controller.create_count == 3


def test_session_capacity_remains_bounded_across_create_close_churn(
    tmp_path: Path,
) -> None:
    bridge, store, controller = _make_bridge(tmp_path, max_sessions=1)

    for index in range(20):
        session_id = f"churn-{index}"
        n_agent_id = f"n-{index}"
        profile_ref = f"p-{index}"
        store.set_grant(
            session_id,
            _make_grant(
                session_id=session_id,
                n_agent_id=n_agent_id,
                profile_ref=profile_ref,
            ),
        )
        assert bridge.handle_request(
            "/v1/browser/session/create",
            _create_payload(
                session_id=session_id,
                n_agent_session_id=n_agent_id,
                profile_ref=profile_ref,
            ),
        )[1]["status"] == "ok"
        assert len(bridge._sessions) == 1
        assert sum(
            not target["closed"] for target in controller.targets.values()
        ) == 1
        assert bridge.handle_request(
            "/v1/browser/session/close",
            {"protocol_version": "1", "session_id": session_id},
        )[1]["status"] == "ok"
        assert bridge._sessions == {}
        assert not bridge._session_reservations
        assert all(
            target["closed"] for target in controller.targets.values()
        )


def test_concurrent_create_reservations_never_exceed_session_capacity(
    tmp_path: Path,
) -> None:
    class BlockingCreateController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered_count = 0
            self.entered_lock = threading.Lock()
            self.capacity_entered = threading.Event()
            self.release = threading.Event()

        def create_target(self, profile_ref: str) -> str:
            with self.entered_lock:
                self.entered_count += 1
                if self.entered_count == 2:
                    self.capacity_entered.set()
            assert self.release.wait(timeout=1)
            return super().create_target(profile_ref)

    controller = BlockingCreateController()
    bridge, store, _ = _make_bridge(
        tmp_path,
        cdp=controller,
        max_concurrency=3,
        max_sessions=2,
    )
    session_ids: list[str] = []
    for candidate in range(
        1, host_bridge_module._SESSION_LOCK_STRIPES * 4 + 1
    ):
        session_id = f"b-{candidate}"
        if all(
            bridge._session_lock(session_id)
            is not bridge._session_lock(existing)
            for existing in session_ids
        ):
            session_ids.append(session_id)
            if len(session_ids) == 3:
                break
    assert len(session_ids) == 3, (
        "failed to find three session IDs on distinct lock stripes"
    )
    for index, session_id in enumerate(session_ids, start=1):
        store.set_grant(
            session_id,
            _make_grant(
                session_id=session_id,
                n_agent_id=f"n-{index}",
                profile_ref=f"p-{index}",
            ),
        )
    results: list[tuple[int, dict[str, Any]]] = []

    def create(index: int) -> None:
        results.append(
            bridge.handle_request(
                "/v1/browser/session/create",
                _create_payload(
                    session_id=session_ids[index - 1],
                    n_agent_session_id=f"n-{index}",
                    profile_ref=f"p-{index}",
                ),
            )
        )

    first = threading.Thread(target=create, args=(1,), daemon=True)
    second = threading.Thread(target=create, args=(2,), daemon=True)
    first.start()
    second.start()
    assert controller.capacity_entered.wait(timeout=1)
    _, full = bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(
            session_id=session_ids[2],
            n_agent_session_id="n-3",
            profile_ref="p-3",
        ),
    )
    assert full["error_code"] == "host_bridge_busy"
    assert controller.entered_count == 2

    controller.release.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert all(body["status"] == "ok" for _, body in results)
    assert len(bridge._sessions) == 2
    assert controller.create_count == 2


def test_timed_out_create_holds_capacity_until_late_target_is_closed(
    tmp_path: Path,
) -> None:
    class BlockingCreateController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def create_target(self, profile_ref: str) -> str:
            self.entered.set()
            assert self.release.wait(timeout=1)
            return super().create_target(profile_ref)

    controller = BlockingCreateController()
    bridge, store, _ = _make_bridge(
        tmp_path,
        cdp=controller,
        max_sessions=1,
    )
    for index in (1, 2):
        store.set_grant(
            f"b-{index}",
            _make_grant(
                session_id=f"b-{index}",
                n_agent_id=f"n-{index}",
                profile_ref=f"p-{index}",
            ),
        )

    _, timed_out = bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(),
        deadline_monotonic=time.monotonic() + 0.02,
        cancel_event=threading.Event(),
    )
    assert controller.entered.wait(timeout=1)
    _, at_capacity = bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(
            session_id="b-2",
            n_agent_session_id="n-2",
            profile_ref="p-2",
        ),
    )
    assert timed_out["error_code"] == "host_bridge_timeout"
    assert at_capacity["error_code"] == "host_bridge_busy"
    assert controller.create_count == 0
    assert bridge._session_reservations == {"b-1"}

    controller.release.set()
    assert controller.closed_event.wait(timeout=1)
    assert bridge._session_reservations == set()
    assert bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(
            session_id="b-2",
            n_agent_session_id="n-2",
            profile_ref="p-2",
        ),
    )[1]["status"] == "ok"
    assert controller.create_count == 2


def test_expiry_cleanup_retries_automatically_after_job_capacity_frees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CountingCloseController(FakeCdpController):
        def __init__(self) -> None:
            super().__init__()
            self.all_closed = threading.Event()

        def close_target(self, target_id: str) -> None:
            super().close_target(target_id)
            if len(self.close_calls) == 3:
                self.all_closed.set()

    controller = CountingCloseController()
    bridge, store, _ = _make_bridge(
        tmp_path, cdp=controller, max_sessions=3
    )
    original_timer = host_bridge_module.threading.Timer
    original_thread = host_bridge_module.threading.Thread
    retry_timer_creations = 0
    scheduler_thread_creations = 0

    def counting_timer(
        interval: float,
        function: Callable[..., Any],
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> threading.Timer:
        nonlocal retry_timer_creations
        if getattr(function, "__name__", "") == "_retry_expiry_cleanup":
            retry_timer_creations += 1
        return original_timer(interval, function, args, kwargs)

    def counting_thread(*args: Any, **kwargs: Any) -> threading.Thread:
        nonlocal scheduler_thread_creations
        if kwargs.get("name") == "host-bridge-expiry-cleanup":
            scheduler_thread_creations += 1
        return original_thread(*args, **kwargs)

    target_ids: list[str] = []
    for index in range(1, 4):
        session_id = f"expiry-pressure-{index}"
        store.set_grant(
            session_id,
            _make_grant(
                session_id=session_id,
                n_agent_id=f"n-{index}",
                profile_ref=f"p-{index}",
            ),
        )
        assert bridge.handle_request(
            "/v1/browser/session/create",
            _create_payload(
                session_id=session_id,
                n_agent_session_id=f"n-{index}",
                profile_ref=f"p-{index}",
            ),
        )[1]["status"] == "ok"
        target_ids.append(bridge._sessions[session_id].target_id)

    monkeypatch.setattr(host_bridge_module.threading, "Timer", counting_timer)
    monkeypatch.setattr(
        host_bridge_module.threading, "Thread", counting_thread
    )
    for _ in range(8):
        assert bridge._try_reserve_controller_job()
    try:
        for index in range(1, 4):
            session_id = f"expiry-pressure-{index}"
            registered = bridge._sessions[session_id]
            store.set_grant(
                session_id,
                _make_grant(
                    session_id=session_id,
                    n_agent_id=f"n-{index}",
                    profile_ref=f"p-{index}",
                    expired=True,
                ),
            )
            assert registered.expiry_timer is not None
            registered.expiry_timer.cancel()
            registered.expiry_deadline_monotonic = time.monotonic() - 1
            bridge._expire_session(
                session_id, registered, registered.generation
            )

        assert len(bridge._sessions) == 3
        assert all(
            registered.expiring
            for registered in bridge._sessions.values()
        )
        assert bridge._expiry_cleanup_thread is not None
        assert bridge._expiry_cleanup_thread.is_alive()
        assert len(bridge._pending_expiry_cleanup) == 3
        assert not threading.Event().wait(timeout=0.08)
        assert retry_timer_creations == 0
        assert scheduler_thread_creations == 1
        monkeypatch.setattr(
            host_bridge_module.threading, "Timer", original_timer
        )
        monkeypatch.setattr(
            host_bridge_module.threading, "Thread", original_thread
        )
    finally:
        for _ in range(8):
            bridge._release_controller_job()

    assert controller.all_closed.wait(timeout=1)
    assert sorted(controller.close_calls) == sorted(target_ids)
    assert bridge._sessions == {}

    store.set_grant(
        "after-expiry",
        _make_grant(
            session_id="after-expiry",
            n_agent_id="n-after-expiry",
            profile_ref="p-after-expiry",
        ),
    )
    assert bridge.handle_request(
        "/v1/browser/session/create",
        _create_payload(
            session_id="after-expiry",
            n_agent_session_id="n-after-expiry",
            profile_ref="p-after-expiry",
        ),
    )[1]["status"] == "ok"


def test_shutdown_stops_and_joins_expiry_cleanup_scheduler(
    tmp_path: Path,
) -> None:
    bridge, store, controller = _make_bridge(tmp_path, max_sessions=1)
    store.set_grant("b-1", _make_grant())
    assert bridge.handle_request(
        "/v1/browser/session/create", _create_payload()
    )[1]["status"] == "ok"
    registered = bridge._sessions["b-1"]

    for _ in range(8):
        assert bridge._try_reserve_controller_job()
    registered.expiring = True
    bridge._unregister_expired(
        "b-1",
        registered,
        expected_generation=registered.generation,
    )
    scheduler = bridge._expiry_cleanup_thread
    assert scheduler is not None
    assert scheduler.is_alive()

    for _ in range(8):
        bridge._release_controller_job()
    assert controller.closed_event.wait(timeout=1)
    assert bridge._sessions == {}

    assert bridge.shutdown() is True
    assert not scheduler.is_alive()
    assert bridge._expiry_cleanup_thread is None
    assert not bridge._pending_expiry_cleanup


def test_shutdown_reports_scheduler_termination_failure(
    tmp_path: Path,
) -> None:
    class NonTerminatingScheduler:
        def __init__(self) -> None:
            self.join_timeouts: list[float | None] = []

        def join(self, timeout: float | None = None) -> None:
            self.join_timeouts.append(timeout)

        def is_alive(self) -> bool:
            return True

    bridge, _, controller = _make_bridge(tmp_path)
    scheduler = NonTerminatingScheduler()
    bridge._expiry_cleanup_thread = scheduler  # type: ignore[assignment]

    assert bridge.shutdown() is False
    assert scheduler.join_timeouts == [
        host_bridge_module._SHUTDOWN_DISPATCH_ACK_SECONDS
    ]
    assert controller.shutdown_count == 1
    assert bridge.shutdown() is False
    assert scheduler.join_timeouts == [
        host_bridge_module._SHUTDOWN_DISPATCH_ACK_SECONDS
    ]
