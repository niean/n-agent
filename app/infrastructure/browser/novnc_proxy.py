"""Bounded same-origin reverse proxy for the container noVNC service."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import websockets


_FORWARDED_RESPONSE_HEADERS = frozenset(
    {"content-type", "cache-control", "etag", "last-modified"}
)


class BrowserNoVncProxy:
    """Proxy noVNC assets and its WebSocket without exposing port 6080."""

    def __init__(
        self,
        base_url: str,
        *,
        max_http_response_bytes: int = 8 * 1024 * 1024,
        http_transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("novnc_proxy_base_url_invalid")
        if max_http_response_bytes <= 0:
            raise ValueError("novnc_proxy_response_limit_invalid")
        self._http_base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        ).rstrip("/")
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        self._ws_base_url = urlunsplit(
            (ws_scheme, parsed.netloc, "", "", "")
        ).rstrip("/")
        self._max_http_response_bytes = max_http_response_bytes
        self._http_transport = http_transport
        self._ws_connect = ws_connect or websockets.connect

    async def fetch(
        self, asset_path: str, query_string: str
    ) -> tuple[int, dict[str, str], bytes]:
        self._validate_asset_path(asset_path)
        safe_query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(
                    query_string, keep_blank_values=True, strict_parsing=False
                )
                if key not in {"cap", "n_agent_session_id"}
            ],
            doseq=True,
        )
        url = f"{self._http_base_url}/{asset_path}"
        if safe_query:
            url = f"{url}?{safe_query}"
        async with httpx.AsyncClient(
            transport=self._http_transport,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self._max_http_response_bytes:
                        raise RuntimeError("novnc_proxy_response_too_large")
                    body.extend(chunk)
                headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in _FORWARDED_RESPONSE_HEADERS
                }
                return response.status_code, headers, bytes(body)

    async def bridge(self, websocket: Any, asset_path: str) -> None:
        if asset_path != "websockify":
            raise ValueError("novnc_proxy_path_invalid")
        requested = websocket.headers.get("sec-websocket-protocol", "")
        subprotocols = [item.strip() for item in requested.split(",") if item.strip()]
        async with self._ws_connect(
            f"{self._ws_base_url}/websockify",
            subprotocols=subprotocols or None,
            compression=None,
            open_timeout=10,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
            proxy=None,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def downstream_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    message_type = message.get("type")
                    if message_type == "websocket.disconnect":
                        return
                    if message_type != "websocket.receive":
                        continue
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_downstream() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(downstream_to_upstream()),
                asyncio.create_task(upstream_to_downstream()),
            }
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done | pending:
                with suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    def _validate_asset_path(asset_path: str) -> None:
        if (
            not asset_path
            or asset_path.startswith("/")
            or "\\" in asset_path
            or "\x00" in asset_path
            or any(part in {"", ".", ".."} for part in asset_path.split("/"))
        ):
            raise ValueError("novnc_proxy_path_invalid")


__all__ = ["BrowserNoVncProxy"]
