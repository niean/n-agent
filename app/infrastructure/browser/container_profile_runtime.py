"""Client for the container browser's persistent-profile runtime manager."""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ContainerProfileRuntimeError(RuntimeError):
    """The isolated browser runtime could not prepare a profile."""


@dataclass(frozen=True)
class ContainerProfileRuntime:
    cdp_endpoint: str
    runtime_id: str


class ContainerProfileRuntimeClient:
    """Ask the browser container to start/reuse one persistent profile."""

    def __init__(self, endpoint: str, *, timeout_seconds: float = 10.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def ensure_profile(self, profile_ref: str) -> tuple[str, str]:
        return await asyncio.to_thread(self._ensure_profile_sync, profile_ref)

    def _ensure_profile_sync(self, profile_ref: str) -> tuple[str, str]:
        request = Request(
            f"{self._endpoint}/profiles/{profile_ref}",
            method="POST",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                if response.status != 200:
                    raise ContainerProfileRuntimeError("profile_runtime_unavailable")
                payload = json.loads(response.read().decode("utf-8"))
        except ContainerProfileRuntimeError:
            raise
        except Exception as exc:
            raise ContainerProfileRuntimeError("profile_runtime_unavailable") from exc
        port = payload.get("cdp_port")
        runtime_id = payload.get("runtime_id")
        if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(runtime_id, str):
            raise ContainerProfileRuntimeError("profile_runtime_invalid_response")
        parsed = urlsplit(self._endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ContainerProfileRuntimeError("profile_runtime_invalid_endpoint")
        host = parsed.hostname
        try:
            host = socket.gethostbyname(host)
        except OSError as exc:
            raise ContainerProfileRuntimeError("profile_runtime_host_unresolved") from exc
        if ":" in host:
            host = f"[{host}]"
        return (f"{parsed.scheme}://{host}:{port}", runtime_id)


__all__ = [
    "ContainerProfileRuntime",
    "ContainerProfileRuntimeClient",
    "ContainerProfileRuntimeError",
]
