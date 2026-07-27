"""Tests for the Host CDP browser backend (T12a) and Host Bridge (T12b).

Uses FAKE bridge/CDP targets. No real Chrome is connected. The HostCdpBrowserBackend
tests use httpx.MockTransport as a fake bridge. The HostBridge tests use fake
GrantStore + CdpTargetController implementations.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.domain.browser import (
    BrowserActionResult,
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
    GrantStore,
    HostBridge,
    HostBridgeConfig,
    TargetClosed,
)


# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

TOKEN = b"a" * 32  # 32 bytes, meets minimum length
BASE_URL = "http://127.0.0.1:8766"
POLICY_VERSION = "v1"


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
    revoked: bool = False,
    actor_id: str = "actor-1",
) -> dict[str, Any]:
    if expired:
        expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    return {
        "browser_session_id": session_id,
        "n_agent_session_id": n_agent_id,
        "actor_id": actor_id,
        "policy_version": policy_version,
        "expires_at": expires_at.isoformat(),
        "revoked": revoked,
    }


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


# ---------------------------------------------------------------------------
# Fake grant store + CDP controller (for HostBridge direct tests)
# ---------------------------------------------------------------------------


class FakeGrantStore:
    def __init__(self, grants: dict[str, dict] | None = None) -> None:
        self._grants = dict(grants or {})
        self.load_count = 0

    def load_grant(self, session_id: str) -> dict[str, Any] | None:
        self.load_count += 1
        return self._grants.get(session_id)

    def set_grant(self, session_id: str, grant: dict[str, Any]) -> None:
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

    def create_target(self, profile_ref: str) -> str:
        if self._create_error:
            raise RuntimeError("cdp unavailable")
        target_id = f"target-{self._next_id}"
        self._next_id += 1
        self.targets[target_id] = {
            "profile_ref": profile_ref,
            "closed": False,
        }
        return target_id

    def close_target(self, target_id: str) -> None:
        if target_id in self.targets:
            self.targets[target_id]["closed"] = True

    def execute_action(
        self,
        target_id: str,
        action_type: str,
        action: dict[str, Any],
        document_revision: int,
    ) -> dict[str, Any]:
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

    def get_state(self, target_id: str) -> dict[str, Any]:
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

    @property
    def create_error(self) -> bool:
        return self._create_error

    @create_error.setter
    def create_error(self, value: bool) -> None:
        self._create_error = value


def _make_bridge(
    tmp_path: Path,
    *,
    grant_store: FakeGrantStore | None = None,
    cdp: FakeCdpController | None = None,
    policy_version: str = POLICY_VERSION,
) -> tuple[HostBridge, FakeGrantStore, FakeCdpController]:
    token_path = tmp_path / "private" / "bridge_token"
    _write_token(token_path)
    gs = grant_store or FakeGrantStore()
    controller = cdp or FakeCdpController()
    config = HostBridgeConfig(
        token_path=token_path,
        policy_version=policy_version,
        cdp_endpoint="ws://127.0.0.1:9222",
    )
    bridge = HostBridge(
        config,
        grant_store=gs,
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


def test_token_file_too_short_fails_closed(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_bytes(b"short\n")
    token_path.chmod(0o600)
    config = HostCdpBackendConfig(base_url=BASE_URL, token_path=token_path)
    with pytest.raises(HostCdpBackendError, match="host_bridge_token_invalid"):
        HostCdpBrowserBackend(config)


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
            "error_code": None,
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
            "error_code": None,
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
            policy_version=POLICY_VERSION,
            cdp_endpoint="ws://127.0.0.1:9222",
            bind_host="0.0.0.0",
        )


def test_bridge_config_requires_policy_version(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    _write_token(token_path)
    with pytest.raises(ValueError, match="host_bridge_policy_version_required"):
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
    bridge, gs, _ = _make_bridge(tmp_path)
    gs.set_grant("b-1", _make_grant(revoked=True))
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
    assert body["error_code"] == "grant_revoked"


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
    tmp_path: Path, grant: dict | None = None
) -> tuple[HostBridge, FakeGrantStore, FakeCdpController]:
    bridge, gs, cdp = _make_bridge(tmp_path)
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
    return bridge, gs, cdp


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
    bridge, _, _ = _bridge_with_session(tmp_path)
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
    gs.set_grant("b-1", _make_grant(revoked=True))
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
    assert body["error_code"] == "grant_revoked"


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

    class FailingGrantStore:
        def load_grant(self, session_id: str) -> dict[str, Any] | None:
            raise RuntimeError("store unavailable")

    bridge._grant_store = FailingGrantStore()
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
    bridge, _, _ = _bridge_with_session(tmp_path)
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


def test_bridge_none_payload(tmp_path: Path) -> None:
    bridge, _, _ = _make_bridge(tmp_path)
    status, body = bridge.handle_request("/v1/browser/session/create", None)
    assert status == 400


def test_bridge_shutdown_closes_targets(tmp_path: Path) -> None:
    bridge, _, cdp = _bridge_with_session(tmp_path)
    assert len(cdp.targets) == 1
    bridge.shutdown()
    target_id = list(cdp.targets.keys())[0]
    assert cdp.targets[target_id]["closed"] is True
    assert bridge.healthy is False
