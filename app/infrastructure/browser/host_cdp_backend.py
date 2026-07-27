"""Host CDP browser backend (T12a).

Agent-side restricted client: talks to the Host Bridge over loopback HTTP
with a file-based bearer token. Does NOT accept arbitrary CDP endpoint/method/
target/profile path from the caller. The public API surface is exactly the
BrowserBackend Protocol: create_session / close_session / execute_action /
get_state / begin_takeover / end_takeover. No raw-CDP method is exposed.

Security model (mirrors host_terminal):
  - Loopback-only HTTP (http://127.0.0.1:PORT or http://host.docker.internal:PORT).
  - File-based bearer token (perm-checked: 0600, regular file, owned by
    current user, no symlinks).
  - Fail-closed on: token missing/insecure, grant expired/revoked, policy
    version mismatch, unknown capability, target disappeared, bridge
    unavailable -> stable error + signal session degraded.
  - The backend trusts the BrowserService state machine (session is ACTIVE
    before create_session is called); the Bridge re-checks the grant and
    BrowserPolicy independently.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.domain.browser import (
    BrowserActionResult,
    BrowserElementSummary,
    BrowserSession,
    BrowserSessionStatus,
    BrowserState,
)


AUTH_HEADER = "X-N-Agent-Browser-Token"
_PROTOCOL_VERSION = "1"

_CANONICAL_URL_RE = re.compile(
    r"http://(127\.0\.0\.1|host\.docker\.internal):([1-9][0-9]{0,4})/?"
)

# Error codes that signal the session should be degraded (raised, not
# returned as a normal error result). The BrowserService catches the
# exception and transitions the session to DEGRADED.
_DEGRADED_ERROR_CODES = frozenset({
    "target_closed",
    "host_bridge_unhealthy",
    "host_bridge_unavailable",
})

# Error codes that are fail-closed policy/protocol denials. These are
# returned as stable BrowserActionResult errors (no session degradation).
_DENY_ERROR_CODES = frozenset({
    "grant_not_found",
    "grant_expired",
    "grant_revoked",
    "host_policy_version_mismatch",
    "host_grant_required",
    "session_not_active",
    "unknown_capability",
    "session_not_found",
    "host_bridge_invalid_request",
})


class HostCdpBackendError(RuntimeError):
    """Raised when the host CDP backend encounters an infrastructure failure.

    Carries a stable ``error_code`` (never raw exception text). The
    BrowserService catches this and transitions the session to DEGRADED.
    """

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class HostCdpBackendConfig:
    """Configuration for the host CDP browser backend."""

    base_url: str
    token: bytes | str | None = None
    token_path: str | os.PathLike[str] | None = None
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 65.0
    max_response_bytes: int = 2_097_152
    transport: httpx.AsyncBaseTransport | None = None

    def __post_init__(self) -> None:
        canonical = _CANONICAL_URL_RE.fullmatch(self.base_url)
        parsed = urlsplit(self.base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("host_bridge_url_invalid") from exc
        if (
            canonical is None
            or parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "host.docker.internal"}
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("host_bridge_url_invalid")
        if (self.token is None) == (self.token_path is None):
            raise ValueError("host_bridge_token_invalid")
        for value in (self.connect_timeout_seconds, self.read_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("host_bridge_timeout_invalid")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes <= 0
        ):
            raise ValueError("host_bridge_response_limit_invalid")


class HostCdpBrowserBackend:
    """Agent-side restricted client for the host CDP browser bridge.

    Implements the BrowserBackend Protocol. Talks to the Host Bridge over
    loopback HTTP with a file-based bearer token. Does NOT expose any raw
    CDP method/endpoint/target/profile path -- the API surface is exactly
    create / close / execute_action / get_state / begin_takeover /
    end_takeover.

    The backend trusts the BrowserService state machine (session is ACTIVE
    before create_session is called). The Bridge independently re-checks
    the grant and BrowserPolicy per request.
    """

    def __init__(self, config: HostCdpBackendConfig) -> None:
        self._config = config
        self._token = (
            _validate_token(config.token)
            if config.token is not None
            else load_secure_token(Path(config.token_path))  # type: ignore[arg-type]
        )
        # Locally registered session ids (set of create_session calls).
        self._sessions: set[str] = set()

    # ------------------------------------------------------------------
    # BrowserBackend Protocol
    # ------------------------------------------------------------------

    async def create_session(self, session: BrowserSession) -> None:
        """Register a session with the bridge.

        Only proceeds when the BrowserService has already transitioned the
        session to ACTIVE via a valid host grant. If the session is still
        PENDING_AUTHORIZATION (or any non-ACTIVE state), fail-closed.
        """
        if session.status is not BrowserSessionStatus.ACTIVE:
            raise HostCdpBackendError("session_not_active")
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "session_id": session.id,
            "n_agent_session_id": session.bound_n_agent_session_id,
            "profile_ref": session.profile_ref,
            "status": session.status.value,
        }
        body = await self._post("/v1/browser/session/create", payload)
        _require_ok(body)
        self._sessions.add(session.id)

    async def close_session(self, session_id: str) -> None:
        """Notify the bridge to release the session's target/capability."""
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            body = await self._post("/v1/browser/session/close", payload)
        except HostCdpBackendError:
            # Best-effort close: do not raise on bridge errors, just remove
            # the local registration.
            self._sessions.discard(session_id)
            return
        self._sessions.discard(session_id)

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        """Send an action to the bridge for proxying to the managed Chrome.

        The bridge re-evaluates BrowserPolicy (same version snapshot) and
        proxies to the registered CDP target. Fail-closed on grant
        expired/revoked, policy version mismatch, unknown capability, or
        target disappeared.
        """
        if session_id not in self._sessions:
            return BrowserActionResult(
                action_type=_action_type_name(action),
                status="error",
                error_code="session_not_found",
            )
        action_type, action_fields = _serialize_action(action)
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "session_id": session_id,
            "action_type": action_type,
            "action": action_fields,
            "document_revision": _action_document_revision(action),
        }
        try:
            body = await self._post("/v1/browser/session/action", payload)
        except HostCdpBackendError as exc:
            # Infrastructure failure (bridge unavailable, auth failed) ->
            # signal degraded by raising.
            raise
        return _parse_action_result(body, action_type)

    async def get_state(self, session_id: str) -> BrowserState:
        """Return the current browser state for the session."""
        if session_id not in self._sessions:
            return BrowserState(
                safe_url=None,
                title=None,
                status=BrowserSessionStatus.CLOSED,
                document_revision=0,
                latest_screenshot_ref=None,
            )
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            body = await self._post("/v1/browser/session/state", payload)
        except HostCdpBackendError:
            return BrowserState(
                safe_url=None,
                title=None,
                status=BrowserSessionStatus.DEGRADED,
                document_revision=0,
                latest_screenshot_ref=None,
            )
        return _parse_state(body)

    async def begin_takeover(self, session_id: str) -> str | None:
        """Host takeover: the user directly operates the managed Chrome window.

        Returns None (not an interactive view URL). Notifies the bridge
        for observability but does not issue a capability URL.
        """
        if session_id not in self._sessions:
            return None
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            await self._post("/v1/browser/session/takeover/begin", payload)
        except HostCdpBackendError:
            pass
        return None

    async def end_takeover(self, session_id: str) -> None:
        """Release the host takeover (no-op for host backend)."""
        if session_id not in self._sessions:
            return
        payload = {
            "protocol_version": _PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            await self._post("/v1/browser/session/takeover/end", payload)
        except HostCdpBackendError:
            pass

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a POST to the bridge and return the parsed response body.

        Raises HostCdpBackendError on network errors, auth failures, and
        degraded error codes. Returns the body dict on success.
        """
        url = f"{self._config.base_url.rstrip('/')}{path}"
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_seconds,
            read=self._config.read_timeout_seconds,
            write=self._config.connect_timeout_seconds,
            pool=self._config.connect_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=self._config.transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    url,
                    headers={AUTH_HEADER: self._token.decode("utf-8")},
                    json=payload,
                )
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
            raise HostCdpBackendError("host_bridge_unavailable") from exc
        if response.status_code == 401:
            raise HostCdpBackendError("host_bridge_auth_failed")
        if response.status_code >= 500:
            raise HostCdpBackendError("host_bridge_unavailable")
        if response.status_code >= 400:
            raise HostCdpBackendError("host_bridge_invalid_response")
        try:
            body = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostCdpBackendError("host_bridge_invalid_response") from exc
        if not isinstance(body, dict):
            raise HostCdpBackendError("host_bridge_invalid_response")
        # Check for degraded error codes that should signal session degraded.
        error_code = body.get("error_code") if body.get("status") == "error" else None
        if error_code in _DEGRADED_ERROR_CODES:
            raise HostCdpBackendError(error_code)
        return body


# ------------------------------------------------------------------
# Serialization helpers
# ------------------------------------------------------------------


def _action_type_name(action: Any) -> str:
    return type(action).__name__.replace("Action", "").lower()


def _action_document_revision(action: Any) -> int:
    rev = getattr(action, "document_revision", 0)
    if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0:
        return rev
    return 0


def _serialize_action(action: Any) -> tuple[str, dict[str, Any]]:
    """Serialize an action dataclass to (action_type, fields_dict).

    Only carries action_type + action fields. Does NOT carry raw CDP
    method/endpoint/target/profile path -- the API surface is the
    BrowserBackend Protocol, not raw CDP.
    """
    action_type = _action_type_name(action)
    fields: dict[str, Any] = {}
    if hasattr(action, "__dataclass_fields__"):
        for key in action.__dataclass_fields__:
            value = getattr(action, key)
            if isinstance(value, tuple):
                value = list(value)
            fields[key] = value
    return action_type, fields


def _parse_action_result(body: dict[str, Any], action_type: str) -> BrowserActionResult:
    """Parse the bridge response into a BrowserActionResult.

    If the bridge returned an error, map it to a stable error_code. If
    the bridge returned a degraded error code, the caller already raised.
    """
    status = body.get("status")
    if status == "error":
        error_code = body.get("error_code", "host_bridge_invalid_response")
        return BrowserActionResult(
            action_type=action_type,
            status="error",
            error_code=error_code,
            document_revision=body.get("document_revision", 0),
        )
    elements_data = body.get("elements") or ()
    elements = tuple(
        BrowserElementSummary(
            element_ref=e.get("element_ref", ""),
            role=e.get("role", ""),
            accessible_name=e.get("accessible_name", ""),
            text_excerpt=e.get("text_excerpt", ""),
            disabled=e.get("disabled", False),
        )
        for e in elements_data
    )
    return BrowserActionResult(
        action_type=body.get("action_type", action_type),
        status=body.get("status", "error"),
        url=body.get("url"),
        title=body.get("title"),
        text=body.get("text"),
        elements=elements,
        screenshot_ref=body.get("screenshot_ref"),
        warning_code=body.get("warning_code"),
        error_code=body.get("error_code"),
        duration_ms=body.get("duration_ms", 0),
        document_revision=body.get("document_revision", 0),
    )


def _parse_state(body: dict[str, Any]) -> BrowserState:
    status_str = body.get("status", "closed")
    try:
        status = BrowserSessionStatus(status_str)
    except ValueError:
        status = BrowserSessionStatus.DEGRADED
    return BrowserState(
        safe_url=body.get("safe_url"),
        title=body.get("title"),
        status=status,
        document_revision=body.get("document_revision", 0),
        latest_screenshot_ref=body.get("latest_screenshot_ref"),
    )


def _require_ok(body: dict[str, Any]) -> None:
    """Check that the bridge returned a success response."""
    status = body.get("status")
    if status == "ok":
        return
    error_code = body.get("error_code", "host_bridge_invalid_response")
    if error_code in _DEGRADED_ERROR_CODES:
        raise HostCdpBackendError(error_code)
    raise HostCdpBackendError(error_code)


# ------------------------------------------------------------------
# Secure token loading (mirrors host_terminal pattern)
# ------------------------------------------------------------------


def load_secure_token(path: Path) -> bytes:
    """Load a bearer token from a file with strict permission checks.

    Fail-closed if: file is a symlink, not a regular file, not owned by
    the current user, or has any permission bits beyond 0600.
    """
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostCdpBackendError("host_bridge_token_invalid") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & ~0o600
    ):
        raise HostCdpBackendError("host_bridge_token_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) & ~0o600
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise HostCdpBackendError("host_bridge_token_invalid")
            raw = os.read(fd, 4097)
            if os.read(fd, 1):
                raise HostCdpBackendError("host_bridge_token_invalid")
        finally:
            os.close(fd)
    except HostCdpBackendError:
        raise
    except OSError as exc:
        raise HostCdpBackendError("host_bridge_token_invalid") from exc
    return _validate_token(raw)


def _validate_token(value: bytes | str | None) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(raw, bytes):
        raise HostCdpBackendError("host_bridge_token_invalid")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if len(raw) < 32 or b"\n" in raw or b"\r" in raw:
        raise HostCdpBackendError("host_bridge_token_invalid")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostCdpBackendError("host_bridge_token_invalid") from exc
    return raw


__all__ = [
    "AUTH_HEADER",
    "HostCdpBackendConfig",
    "HostCdpBackendError",
    "HostCdpBrowserBackend",
    "load_secure_token",
]
