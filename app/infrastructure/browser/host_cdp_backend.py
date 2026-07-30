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
import base64
import binascii
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx

import app.infrastructure.browser.host_protocol as host_protocol
from app.domain.browser import (
    BrowserActionResult,
    BrowserElementSummary,
    BrowserSession,
    BrowserSessionStatus,
    BrowserState,
)


AUTH_HEADER = host_protocol.AUTH_HEADER

_CANONICAL_URL_RE = re.compile(
    r"http://(127\.0\.0\.1|host\.docker\.internal):([1-9][0-9]{0,4})/?"
)
_CONTENT_LENGTH_RE = re.compile(r"[0-9]+\Z")
_BASE64_RE = re.compile(
    r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?\Z"
)
_MAX_URL_LENGTH = 8_192
_MAX_TITLE_LENGTH = 4_096
_MAX_TEXT_LENGTH = 20_000
_MAX_CODE_LENGTH = 256
_MAX_ACTION_TYPE_LENGTH = 64
_MAX_SCREENSHOT_REF_LENGTH = 4_096
_MAX_ELEMENTS = 200
_MAX_ELEMENT_REF_LENGTH = 512
_MAX_ELEMENT_ROLE_LENGTH = 128
_MAX_ELEMENT_NAME_LENGTH = 2_048
_MAX_ELEMENT_EXCERPT_LENGTH = 4_096
_MAX_DURATION_MS = 3_600_000
_MAX_DOCUMENT_REVISION = 2**63 - 1

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
    max_screenshot_bytes: int = (
        host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
    )
    max_response_bytes: int = host_protocol.MAX_JSON_RESPONSE_BYTES
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
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("host_bridge_timeout_invalid")
        if (
            isinstance(self.max_screenshot_bytes, bool)
            or not isinstance(self.max_screenshot_bytes, int)
            or self.max_screenshot_bytes
            != host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
        ):
            raise ValueError("host_bridge_screenshot_limit_invalid")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes <= 0
            or self.max_response_bytes
            < host_protocol.max_json_response_bytes(
                self.max_screenshot_bytes
            )
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
        self._screenshot_lock = threading.Lock()
        self._screenshot_cache: dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # BrowserBackend Protocol
    # ------------------------------------------------------------------

    async def create_session(self, session: BrowserSession) -> None:
        """Register a session with the bridge.

        Only proceeds when the BrowserService has already transitioned the
        session to ACTIVE via a valid host grant. If the session is still
        PENDING_AUTHORIZATION (or any non-ACTIVE state), fail-closed.
        """
        self._clear_screenshot(session.id)
        if session.status is not BrowserSessionStatus.ACTIVE:
            raise HostCdpBackendError("session_not_active")
        payload = {
            "protocol_version": host_protocol.PROTOCOL_VERSION,
            "session_id": session.id,
            "n_agent_session_id": session.bound_n_agent_session_id,
            "profile_ref": session.profile_ref,
            "status": session.status.value,
        }
        try:
            body = await self._post(
                "/v1/browser/session/create", payload, session.id
            )
            _require_ok(body, endpoint="create")
        except BaseException:
            self._clear_screenshot(session.id)
            raise
        self._sessions.add(session.id)

    async def close_session(self, session_id: str) -> None:
        """Notify the bridge to release the session's target/capability."""
        self._clear_screenshot(session_id)
        payload = {
            "protocol_version": host_protocol.PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            body = await self._post(
                "/v1/browser/session/close", payload, session_id
            )
            _require_ok(body, endpoint="close")
        except HostCdpBackendError:
            # Best-effort close: do not raise on bridge errors, just remove
            # the local registration.
            pass
        finally:
            self._sessions.discard(session_id)
            self._clear_screenshot(session_id)

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        """Send an action to the bridge for proxying to the managed Chrome.

        The bridge re-evaluates BrowserPolicy (same version snapshot) and
        proxies to the registered CDP target. Fail-closed on grant
        expired/revoked, policy version mismatch, unknown capability, or
        target disappeared.
        """
        self._clear_screenshot(session_id)
        if session_id not in self._sessions:
            return BrowserActionResult(
                action_type=_action_type_name(action),
                status="error",
                error_code="session_not_found",
            )
        action_type, action_fields = _serialize_action(action)
        payload = {
            "protocol_version": host_protocol.PROTOCOL_VERSION,
            "session_id": session_id,
            "action_type": action_type,
            "action": action_fields,
            "document_revision": _action_document_revision(action),
        }
        try:
            body = await self._post(
                "/v1/browser/session/action", payload, session_id
            )
            result, screenshot = _parse_action_result(
                body,
                action_type,
                max_screenshot_bytes=self._config.max_screenshot_bytes,
            )
        except BaseException:
            self._clear_screenshot(session_id)
            raise
        if screenshot is not None:
            with self._screenshot_lock:
                self._screenshot_cache[session_id] = screenshot
        return result

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
            "protocol_version": host_protocol.PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            body = await self._post(
                "/v1/browser/session/state", payload, session_id
            )
            return _parse_state(body)
        except HostCdpBackendError:
            self._clear_screenshot(session_id)
            return BrowserState(
                safe_url=None,
                title=None,
                status=BrowserSessionStatus.DEGRADED,
                document_revision=0,
                latest_screenshot_ref=None,
            )

    async def begin_takeover(self, session_id: str) -> str | None:
        """Host takeover: the user directly operates the managed Chrome window.

        Returns None (not an interactive view URL). Notifies the bridge
        for observability but does not issue a capability URL.
        """
        if session_id not in self._sessions:
            return None
        payload = {
            "protocol_version": host_protocol.PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            body = await self._post(
                "/v1/browser/session/takeover/begin",
                payload,
                session_id,
            )
            _require_ok(body, endpoint="takeover_begin")
        except HostCdpBackendError:
            self._clear_screenshot(session_id)
            pass
        return None

    async def end_takeover(self, session_id: str) -> None:
        """Release the host takeover (no-op for host backend)."""
        if session_id not in self._sessions:
            return
        payload = {
            "protocol_version": host_protocol.PROTOCOL_VERSION,
            "session_id": session_id,
        }
        try:
            body = await self._post(
                "/v1/browser/session/takeover/end",
                payload,
                session_id,
            )
            _require_ok(body, endpoint="takeover_end")
        except HostCdpBackendError:
            self._clear_screenshot(session_id)
            pass

    def last_screenshot_bytes(self, session_id: str) -> bytes | None:
        """Return the fresh screenshot captured by the last successful action."""
        with self._screenshot_lock:
            return self._screenshot_cache.get(session_id)

    def _clear_screenshot(self, session_id: str) -> None:
        with self._screenshot_lock:
            self._screenshot_cache.pop(session_id, None)

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
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
                async with client.stream(
                    "POST",
                    url,
                    headers={
                        AUTH_HEADER: self._token.decode("utf-8")
                    },
                    json=payload,
                ) as response:
                    lengths = response.headers.get_list(
                        "content-length"
                    )
                    if (
                        len(lengths) != 1
                        or len(lengths[0]) > 20
                        or not _CONTENT_LENGTH_RE.fullmatch(lengths[0])
                    ):
                        raise HostCdpBackendError(
                            "host_bridge_invalid_response"
                        )
                    declared_length = int(lengths[0])
                    if (
                        declared_length <= 0
                        or declared_length
                        > self._config.max_response_bytes
                    ):
                        raise HostCdpBackendError(
                            "host_bridge_invalid_response"
                        )
                    if response.status_code == 401:
                        raise HostCdpBackendError(
                            "host_bridge_auth_failed"
                        )
                    if not 200 <= response.status_code < 300:
                        if response.status_code >= 500:
                            raise HostCdpBackendError(
                                "host_bridge_unavailable"
                            )
                        raise HostCdpBackendError(
                            "host_bridge_invalid_response"
                        )
                    chunks: list[bytes] = []
                    actual_length = 0
                    async for chunk in response.aiter_bytes():
                        actual_length += len(chunk)
                        if (
                            actual_length > declared_length
                            or actual_length
                            > self._config.max_response_bytes
                        ):
                            raise HostCdpBackendError(
                                "host_bridge_invalid_response"
                            )
                        chunks.append(chunk)
                    if actual_length != declared_length:
                        raise HostCdpBackendError(
                            "host_bridge_invalid_response"
                        )
                    encoded_body = b"".join(chunks)
        except asyncio.CancelledError:
            self._clear_screenshot(session_id)
            raise
        except HostCdpBackendError:
            self._clear_screenshot(session_id)
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
            self._clear_screenshot(session_id)
            raise HostCdpBackendError("host_bridge_unavailable") from exc
        try:
            decoded_body = encoded_body.decode("utf-8")
            body = json.loads(decoded_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._clear_screenshot(session_id)
            raise HostCdpBackendError("host_bridge_invalid_response") from exc
        if not isinstance(body, dict):
            self._clear_screenshot(session_id)
            raise HostCdpBackendError("host_bridge_invalid_response")
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


def _parse_action_result(
    body: dict[str, Any],
    action_type: str,
    *,
    max_screenshot_bytes: int,
) -> tuple[BrowserActionResult, bytes | None]:
    """Parse the bridge response into a BrowserActionResult.

    If the bridge returned an error, map it to a stable error_code. If
    the bridge returned a degraded error code, the caller already raised.
    """
    status = body.get("status")
    if status == "error":
        _validate_action_error(body, action_type)
        error_code = body["error_code"]
        if error_code in _DEGRADED_ERROR_CODES:
            raise HostCdpBackendError(error_code)
        return BrowserActionResult(
            action_type=action_type,
            status="error",
            error_code=error_code,
            document_revision=body.get("document_revision", 0),
        ), None
    _validate_action_success(body, action_type)
    elements_data = body.get("elements") or ()
    elements = tuple(
        BrowserElementSummary(
            element_ref=e["element_ref"],
            role=e["role"],
            accessible_name=e["accessible_name"],
            text_excerpt=e["text_excerpt"],
            disabled=e["disabled"],
        )
        for e in elements_data
    )
    screenshot = None
    encoded_screenshot = body.get("screenshot_base64")
    if encoded_screenshot is not None:
        screenshot = _decode_screenshot(
            encoded_screenshot, max_screenshot_bytes
        )
    result = BrowserActionResult(
        action_type=body["action_type"],
        status=body["status"],
        url=body.get("url"),
        title=body.get("title"),
        text=body.get("text"),
        elements=elements,
        screenshot_ref=body.get("screenshot_ref"),
        warning_code=body.get("warning_code"),
        error_code=body.get("error_code"),
        duration_ms=body.get("duration_ms", 0),
        document_revision=body["document_revision"],
    )
    return result, screenshot


def _parse_state(body: dict[str, Any]) -> BrowserState:
    if body.get("status") == "error":
        _validate_error_envelope(body)
        error_code = body["error_code"]
        if error_code in _DEGRADED_ERROR_CODES:
            raise HostCdpBackendError(error_code)
        raise HostCdpBackendError(error_code)
    required = {
        "safe_url",
        "title",
        "status",
        "document_revision",
        "latest_screenshot_ref",
    }
    if set(body) != required:
        _invalid_response()
    _optional_bounded_text(
        body["safe_url"], _MAX_URL_LENGTH
    )
    _optional_bounded_text(
        body["title"], _MAX_TITLE_LENGTH
    )
    _optional_bounded_text(
        body["latest_screenshot_ref"],
        _MAX_SCREENSHOT_REF_LENGTH,
    )
    _bounded_int(
        body["document_revision"], _MAX_DOCUMENT_REVISION
    )
    try:
        status = BrowserSessionStatus(body["status"])
    except (TypeError, ValueError):
        _invalid_response()
    return BrowserState(
        safe_url=body["safe_url"],
        title=body["title"],
        status=status,
        document_revision=body["document_revision"],
        latest_screenshot_ref=body["latest_screenshot_ref"],
    )


def _require_ok(body: dict[str, Any], *, endpoint: str) -> None:
    """Check that the bridge returned a success response."""
    status = body.get("status")
    if status == "error":
        _validate_error_envelope(body)
        error_code = body["error_code"]
        if error_code in _DEGRADED_ERROR_CODES:
            raise HostCdpBackendError(error_code)
        raise HostCdpBackendError(error_code)
    if endpoint == "takeover_begin":
        if (
            status != "ok"
            or set(body) != {"status", "takeover_url"}
            or body["takeover_url"] is not None
        ):
            _invalid_response()
        return
    if endpoint not in {"create", "close", "takeover_end"}:
        _invalid_response()
    if status != "ok" or set(body) != {"status"}:
        _invalid_response()


def _validate_error_envelope(body: dict[str, Any]) -> None:
    if set(body) != {"status", "error_code"}:
        _invalid_response()
    if body.get("status") != "error":
        _invalid_response()
    _bounded_text(body.get("error_code"), _MAX_CODE_LENGTH)


def _validate_action_error(
    body: dict[str, Any], action_type: str
) -> None:
    if set(body) == {"status", "error_code"}:
        _validate_error_envelope(body)
        return
    if set(body) != {
        "action_type",
        "status",
        "error_code",
        "document_revision",
    }:
        _invalid_response()
    if body.get("status") != "error":
        _invalid_response()
    if body.get("action_type") != action_type:
        _invalid_response()
    _bounded_text(body.get("action_type"), _MAX_ACTION_TYPE_LENGTH)
    _bounded_text(body.get("error_code"), _MAX_CODE_LENGTH)
    _bounded_int(
        body.get("document_revision"), _MAX_DOCUMENT_REVISION
    )


def _validate_action_success(
    body: dict[str, Any], action_type: str
) -> None:
    required = {"action_type", "status", "document_revision"}
    allowed = required | {
        "url",
        "title",
        "text",
        "elements",
        "screenshot_ref",
        "warning_code",
        "duration_ms",
        "screenshot_base64",
    }
    if not required <= set(body) or not set(body) <= allowed:
        _invalid_response()
    if body.get("status") != "success":
        _invalid_response()
    if body.get("action_type") != action_type:
        _invalid_response()
    _bounded_text(body.get("action_type"), _MAX_ACTION_TYPE_LENGTH)
    _bounded_int(
        body.get("document_revision"), _MAX_DOCUMENT_REVISION
    )
    if "duration_ms" in body:
        _bounded_int(body["duration_ms"], _MAX_DURATION_MS)
    for field, maximum in (
        ("url", _MAX_URL_LENGTH),
        ("title", _MAX_TITLE_LENGTH),
        ("text", _MAX_TEXT_LENGTH),
        ("screenshot_ref", _MAX_SCREENSHOT_REF_LENGTH),
        ("warning_code", _MAX_CODE_LENGTH),
    ):
        if field in body:
            _optional_bounded_text(body[field], maximum)
    if "elements" in body:
        elements = body["elements"]
        if (
            not isinstance(elements, list)
            or len(elements) > _MAX_ELEMENTS
        ):
            _invalid_response()
        for element in elements:
            _validate_element(element)
    if "screenshot_base64" in body and not isinstance(
        body["screenshot_base64"], str
    ):
        _invalid_response()


def _validate_element(value: Any) -> None:
    fields = {
        "element_ref",
        "role",
        "accessible_name",
        "text_excerpt",
        "disabled",
    }
    if not isinstance(value, dict) or set(value) != fields:
        _invalid_response()
    _bounded_text(
        value["element_ref"],
        _MAX_ELEMENT_REF_LENGTH,
    )
    _bounded_text(
        value["role"], _MAX_ELEMENT_ROLE_LENGTH, allow_empty=True
    )
    _bounded_text(
        value["accessible_name"],
        _MAX_ELEMENT_NAME_LENGTH,
        allow_empty=True,
    )
    _bounded_text(
        value["text_excerpt"],
        _MAX_ELEMENT_EXCERPT_LENGTH,
        allow_empty=True,
    )
    if type(value["disabled"]) is not bool:
        _invalid_response()


def _decode_screenshot(value: str, maximum: int) -> bytes:
    if (
        not value
        or len(value) % 4 != 0
        or _BASE64_RE.fullmatch(value) is None
    ):
        _invalid_response()
    padding = 2 if value.endswith("==") else (
        1 if value.endswith("=") else 0
    )
    estimated = (len(value) // 4) * 3 - padding
    if estimated <= 0 or estimated > maximum:
        _invalid_response()
    try:
        decoded = base64.b64decode(
            value.encode("ascii"), validate=True
        )
    except (UnicodeEncodeError, binascii.Error, ValueError):
        _invalid_response()
    if (
        len(decoded) != estimated
        or len(decoded) > maximum
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        _invalid_response()
    return decoded


def _bounded_text(
    value: Any, maximum: int, *, allow_empty: bool = False
) -> None:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > maximum
    ):
        _invalid_response()


def _optional_bounded_text(value: Any, maximum: int) -> None:
    if value is not None:
        _bounded_text(value, maximum, allow_empty=True)


def _bounded_int(value: Any, maximum: int) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        _invalid_response()


def _invalid_response() -> None:
    raise HostCdpBackendError("host_bridge_invalid_response") from None


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
    try:
        raw = value.encode("ascii") if isinstance(value, str) else value
    except UnicodeEncodeError as exc:
        raise HostCdpBackendError("host_bridge_token_invalid") from exc
    if not isinstance(raw, bytes):
        raise HostCdpBackendError("host_bridge_token_invalid")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if (
        len(raw) < 32
        or len(raw) > 4096
        or any(byte < 0x21 or byte > 0x7E for byte in raw)
    ):
        raise HostCdpBackendError("host_bridge_token_invalid")
    return raw


__all__ = [
    "AUTH_HEADER",
    "HostCdpBackendConfig",
    "HostCdpBackendError",
    "HostCdpBrowserBackend",
    "load_secure_token",
]
