"""UDS RPC server — parent-process side of the sandbox callback channel.

The sandboxed code connects to this socket as a pure client; it cannot forge
responses. The server dispatches incoming requests to the
`SandboxCallbackToolRegistry`, enforces `max_tool_calls`, and writes one JSON
response per request.

Designed for both Local and Docker:
- Local: socket_path is a host path under scratch_dir
- Docker: socket_path is bind-mounted into the sibling container (same path
  on both sides, e.g. /scratch/<safe>/call-<uuid>/rpc.sock)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.domain.sandbox import (
    SandboxCallbackContext,
    SandboxCallbackToolRegistry,
)

logger = logging.getLogger(__name__)


class SandboxRpcServer:
    def __init__(
        self,
        registry: SandboxCallbackToolRegistry,
        socket_path: str,
        workspace_root: Path,
        scratch_dir: Path,
        trusted_metadata: dict[str, Any],
        session_id: str | None,
        max_tool_calls: int,
        callback_context: SandboxCallbackContext,
    ) -> None:
        self._registry = registry
        self._socket_path = socket_path
        self._workspace_root = workspace_root
        self._scratch_dir = scratch_dir
        self._trusted_metadata = trusted_metadata
        self._session_id = session_id
        self._max_tool_calls = max_tool_calls
        self._ctx = callback_context
        self._server: asyncio.AbstractServer | None = None
        self._tool_calls_made = 0
        self._tool_call_log: list[dict] = []

    @property
    def tool_calls_made(self) -> int:
        return self._tool_calls_made

    @property
    def tool_call_log(self) -> list[dict]:
        return list(self._tool_call_log)

    def start(self) -> None:
        # Ensure parent dir exists and stale socket is removed
        Path(self._socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        loop = asyncio.get_event_loop()
        self._server = loop.run_until_complete(
            asyncio.start_unix_server(self._handle_client, path=self._socket_path)
        )

    async def start_async(self) -> None:
        Path(self._socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle_client, path=self._socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass

    def stop_sync(self) -> None:
        """Best-effort sync stop for non-async callers (e.g. LocalSandbox.execute cleanup)."""
        if self._server is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Schedule close; cannot await here
                    loop.create_task(self._server.close())
                else:
                    self._server.close()
                    loop.run_until_complete(self._server.wait_closed())
            except Exception:
                pass
            self._server = None
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            payload = await reader.read()
        except asyncio.IncompleteReadError:
            writer.close()
            return
        except OSError:
            writer.close()
            return
        try:
            request = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            await self._write_response(writer, {"status": "error", "error": f"invalid request: {exc}"})
            return
        response = await self._dispatch(request)
        await self._write_response(writer, response)

    async def _write_response(self, writer: asyncio.StreamWriter, response: dict) -> None:
        try:
            writer.write(json.dumps(response).encode("utf-8"))
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _dispatch(self, request: dict) -> dict:
        name = str(request.get("name", ""))
        arguments = request.get("arguments") or {}
        if not name:
            return {"status": "error", "error": "tool name required"}
        if self._tool_calls_made >= self._max_tool_calls:
            self._tool_call_log.append({
                "name": name,
                "status": "error",
                "error": "tool_call_limit_exceeded",
            })
            return {"status": "error", "error": "tool_call_limit_exceeded"}
        tool = self._registry.get(name)
        if tool is None or not tool.enabled:
            self._tool_calls_made += 1
            self._tool_call_log.append({
                "name": name,
                "status": "error",
                "error": f"tool not enabled: {name}",
            })
            return {"status": "error", "error": f"tool not enabled: {name}"}
        try:
            result = await tool.call(arguments, self._ctx)
        except Exception as exc:
            self._tool_calls_made += 1
            self._tool_call_log.append({
                "name": name,
                "status": "error",
                "error": str(exc),
            })
            return {"status": "error", "error": f"tool raised: {exc}"}
        self._tool_calls_made += 1
        self._tool_call_log.append({
            "name": name,
            "status": result.get("status", "ok"),
        })
        return result
