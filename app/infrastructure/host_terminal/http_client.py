"""Authenticated, bounded HTTP implementation of the host bridge port."""
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

from app.domain.host_terminal import (
    HostCommandTarget,
    HostSkillScriptTarget,
    HostTerminalBridgeRequest,
    HostTerminalBridgeResponse,
    HostTerminalStatus,
)


AUTH_HEADER = "X-N-Agent-Host-Token"
_RESPONSE_FIELDS = {
    "request_id",
    "status",
    "exit_code",
    "stdout",
    "stderr",
    "duration_ms",
    "stdout_truncated",
    "stderr_truncated",
    "error_code",
}
_CANONICAL_BASE_URL_RE = re.compile(
    r"http://host\.docker\.internal:([1-9][0-9]{0,4})/?"
)


class HostTerminalBridgeClientError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class HostTerminalHttpClientConfig:
    base_url: str
    token: bytes | str | None = None
    token_path: str | os.PathLike[str] | None = None
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 65.0
    max_response_bytes: int = 1_048_576
    transport: httpx.AsyncBaseTransport | None = None

    def __post_init__(self) -> None:
        canonical = _CANONICAL_BASE_URL_RE.fullmatch(self.base_url)
        parsed = urlsplit(self.base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("host_bridge_url_invalid") from exc
        if (
            canonical is None
            or parsed.scheme != "http"
            or parsed.hostname != "host.docker.internal"
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


class HostTerminalHttpClient:
    def __init__(self, config: HostTerminalHttpClientConfig) -> None:
        self._config = config
        self._token = (
            _validate_token(config.token)
            if config.token is not None
            else load_secure_token(Path(config.token_path))  # type: ignore[arg-type]
        )

    async def execute(
        self, request: HostTerminalBridgeRequest
    ) -> HostTerminalBridgeResponse:
        payload = _request_payload(request)
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
                    f"{self._config.base_url.rstrip('/')}/v1/execute",
                    headers={AUTH_HEADER: self._token.decode("utf-8")},
                    json=payload,
                ) as response:
                    if response.status_code == 401:
                        raise HostTerminalBridgeClientError("host_bridge_auth_failed")
                    body = await _read_bounded(response, self._config.max_response_bytes)
        except asyncio.CancelledError:
            raise
        except HostTerminalBridgeClientError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
            raise HostTerminalBridgeClientError("host_bridge_unavailable") from exc
        return _parse_response(body, request)


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise HostTerminalBridgeClientError("host_bridge_invalid_response")
        chunks.append(chunk)
    return b"".join(chunks)


def _request_payload(request: HostTerminalBridgeRequest) -> dict[str, Any]:
    target: dict[str, Any]
    if isinstance(request.target, HostCommandTarget):
        target = {
            "type": "command",
            "executable": request.target.executable,
            "args": list(request.target.args),
        }
    elif isinstance(request.target, HostSkillScriptTarget):
        target = {
            "type": "skill_script",
            "skill_name": request.target.skill_name,
            "script_relative_path": request.target.script_relative_path,
            "sha256": request.target.sha256,
            "args": list(request.target.args),
        }
    else:  # defensive against runtime Protocol violations
        raise HostTerminalBridgeClientError("host_bridge_invalid_request")
    return {
        "protocol_version": request.protocol_version,
        "request_id": request.request_id,
        "target": target,
        "n_agent_policy_version": request.n_agent_policy_version,
        "n_agent_content_digest": request.n_agent_content_digest,
        "limits": {
            "timeout_seconds": request.limits.timeout_seconds,
            "max_stdout_bytes": request.limits.max_stdout_bytes,
            "max_stderr_bytes": request.limits.max_stderr_bytes,
            "max_concurrency": request.limits.max_concurrency,
        },
    }


def _parse_response(
    raw: bytes, request: HostTerminalBridgeRequest
) -> HostTerminalBridgeResponse:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostTerminalBridgeClientError("host_bridge_invalid_response") from exc
    if not isinstance(data, dict) or set(data) != _RESPONSE_FIELDS:
        raise HostTerminalBridgeClientError("host_bridge_invalid_response")
    if data["request_id"] != request.request_id:
        raise HostTerminalBridgeClientError("host_bridge_invalid_response")
    duration = data["duration_ms"]
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise HostTerminalBridgeClientError("host_bridge_invalid_response")
    try:
        status = HostTerminalStatus(data["status"])
        result = HostTerminalBridgeResponse(
            protocol_version=request.protocol_version,
            request_id=data["request_id"],
            status=status,
            exit_code=data["exit_code"],
            stdout=data["stdout"],
            stderr=data["stderr"],
            duration_ms=duration,
            stdout_truncated=data["stdout_truncated"],
            stderr_truncated=data["stderr_truncated"],
            error_code=data["error_code"],
        )
        if (status is HostTerminalStatus.SUCCESS) != (result.error_code is None):
            raise ValueError("inconsistent_status")
        return result
    except (TypeError, ValueError) as exc:
        raise HostTerminalBridgeClientError("host_bridge_invalid_response") from exc


def load_secure_token(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostTerminalBridgeClientError("host_bridge_token_invalid") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & ~0o600
    ):
        raise HostTerminalBridgeClientError("host_bridge_token_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) & ~0o600
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise HostTerminalBridgeClientError("host_bridge_token_invalid")
            raw = os.read(fd, 4097)
            if os.read(fd, 1):
                raise HostTerminalBridgeClientError("host_bridge_token_invalid")
        finally:
            os.close(fd)
    except HostTerminalBridgeClientError:
        raise
    except OSError as exc:
        raise HostTerminalBridgeClientError("host_bridge_token_invalid") from exc
    return _validate_token(raw)


def _validate_token(value: bytes | str | None) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(raw, bytes):
        raise HostTerminalBridgeClientError("host_bridge_token_invalid")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if len(raw) < 32 or b"\n" in raw or b"\r" in raw:
        raise HostTerminalBridgeClientError("host_bridge_token_invalid")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostTerminalBridgeClientError("host_bridge_token_invalid") from exc
    return raw


__all__ = [
    "AUTH_HEADER",
    "HostTerminalBridgeClientError",
    "HostTerminalHttpClient",
    "HostTerminalHttpClientConfig",
    "load_secure_token",
]
