"""T15 -- ACP end-to-end subprocess test.

Spawns the real ``python -m app.interfaces.cli acp`` subprocess with an
isolated environment and drives the full ACP JSON-RPC protocol flow via the
``agent-client-protocol`` SDK client helpers. Verifies protocol correctness,
SQLite persistence (source=acp, cwd mapping), and stdout purity (no log
text leaks into the JSON-RPC stream).

The SDK uses newline-delimited JSON framing; both server and client use the
same SDK so framing is consistent. A tee reader captures every stdout byte
so we can assert no log text corrupts the protocol stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["N_AGENT_SQLITE_PATH"] = str(tmp_path / "sessions.db")
    env["N_AGENT_WORKSPACE_ROOT"] = str(tmp_path)
    env["N_AGENT_SANDBOX_SCRATCH_ROOT"] = str(tmp_path / "sandbox-scratch")
    env["N_AGENT_SKILLS_ROOT"] = str(tmp_path / "skills")
    env["N_AGENT_PLUGINS_ROOT"] = str(tmp_path / "plugins")
    env["N_AGENT_GATEWAY_ENABLED"] = "false"
    env["N_AGENT_SCHEDULER_ENABLED"] = "false"
    env["N_AGENT_ACP_HOST_WORKSPACE_ROOT"] = str(tmp_path / "host-workspace")
    env["N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT"] = str(tmp_path / "container-workspace")
    return env


class _TeeStreamReader(asyncio.StreamReader):
    """Wraps a StreamReader, capturing every byte read for later inspection."""

    def __init__(self, base: asyncio.StreamReader) -> None:
        super().__init__()
        self._base = base
        self.captured = bytearray()

    async def readline(self) -> bytes:
        line = await self._base.readline()
        self.captured.extend(line)
        return line

    async def read(self, n: int = -1) -> bytes:
        data = await self._base.read(n)
        self.captured.extend(data)
        return data

    def at_eof(self) -> bool:
        return self._base.at_eof()

    def feed_eof(self) -> None:
        self._base.feed_eof()

    def feed_data(self, data: bytes) -> None:
        self._base.feed_data(data)


class _RecordingClient:
    """Minimal ACP Client that records session updates and denies permissions.

    The agent calls these methods on the client (server->client direction).
    Most are no-ops; ``session_update`` records events for assertions and
    ``request_permission`` denies so no tool actually executes.
    """

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.conn: Any = None

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, **kwargs: Any) -> Any:
        from acp.schema import DeniedOutcome, RequestPermissionResponse

        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def read_text_file(self, **kwargs: Any) -> Any:
        from acp.schema import ReadTextFileResponse

        return ReadTextFileResponse(content="")

    async def write_text_file(self, **kwargs: Any) -> None:
        return None

    async def create_terminal(self, **kwargs: Any) -> Any:
        from acp.schema import CreateTerminalResponse

        return CreateTerminalResponse(terminal_id="")

    async def terminal_output(self, **kwargs: Any) -> Any:
        from acp.schema import TerminalOutputResponse

        return TerminalOutputResponse(output="")

    async def release_terminal(self, **kwargs: Any) -> None:
        return None

    async def wait_for_terminal_exit(self, **kwargs: Any) -> Any:
        from acp.schema import WaitForTerminalExitResponse

        return WaitForTerminalExitResponse(exit_code=0)

    async def kill_terminal(self, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


@pytest.mark.asyncio
async def test_acp_e2e_full_protocol_flow(tmp_path: Path) -> None:
    from acp.core import connect_to_agent
    from acp.meta import PROTOCOL_VERSION
    from acp.transports import spawn_stdio_transport
    from acp.schema import TextContentBlock

    host_workspace = tmp_path / "host-workspace"
    project_dir = host_workspace / "project-a"
    project_dir.mkdir(parents=True)
    container_workspace = tmp_path / "container-workspace"
    container_workspace.mkdir(parents=True)

    env = _base_env(tmp_path)
    cmd = [sys.executable, "-m", "app.interfaces.cli", "acp"]

    async with spawn_stdio_transport(*cmd, env=env) as (reader, writer, process):
        tee = _TeeStreamReader(reader)
        client = _RecordingClient()
        conn = connect_to_agent(
            client,
            input_stream=writer,
            output_stream=tee,
            use_unstable_protocol=True,
        )

        stderr_task = asyncio.create_task(_drain_stream(process.stderr))

        try:
            init_resp = await asyncio.wait_for(
                conn.initialize(protocol_version=PROTOCOL_VERSION),
                timeout=15,
            )
            assert init_resp.protocol_version == PROTOCOL_VERSION
            assert init_resp.agent_info.name == "n-agent"
            assert init_resp.agent_capabilities.load_session is True
            assert len(init_resp.auth_methods) >= 1
            auth_method_id = init_resp.auth_methods[0].id

            auth_resp = await asyncio.wait_for(
                conn.authenticate(method_id=auth_method_id),
                timeout=15,
            )
            assert auth_resp is not None

            new_resp = await asyncio.wait_for(
                conn.new_session(cwd=str(project_dir)),
                timeout=15,
            )
            session_id = new_resp.session_id
            assert session_id
            assert session_id.startswith("acp-")

            prompt_resp = await asyncio.wait_for(
                conn.prompt(
                    prompt=[TextContentBlock(text="hi", type="text")],
                    session_id=session_id,
                ),
                timeout=30,
            )
            assert prompt_resp.stop_reason in {"end_turn", "refusal", "cancelled"}

            list_resp = await asyncio.wait_for(
                conn.list_sessions(),
                timeout=15,
            )
            session_ids = [s.session_id for s in list_resp.sessions]
            assert session_id in session_ids

            load_resp = await asyncio.wait_for(
                conn.load_session(cwd=str(project_dir), session_id=session_id),
                timeout=15,
            )
            assert load_resp is not None

            await asyncio.wait_for(conn.cancel(session_id=session_id), timeout=15)

            close_resp = await asyncio.wait_for(
                conn.close_session(session_id=session_id),
                timeout=15,
            )
            assert close_resp is not None

            await conn.close()
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                process.kill()
                await process.wait()

    stdout_bytes = bytes(tee.captured)
    _assert_stdout_purity(stdout_bytes)

    _assert_sqlite_state(tmp_path / "sessions.db", session_id, container_workspace)


def _assert_stdout_purity(stdout_bytes: bytes) -> None:
    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    for needle in ("INFO", "WARNING", "Loaded", "Starting"):
        assert needle not in stdout_text, (
            f"stdout leaked {needle!r}: stdout={stdout_text!r}"
        )

    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except (json.JSONDecodeError, ValueError):
            pytest.fail(f"stdout line is not valid JSON-RPC: {line!r}")


def _assert_sqlite_state(
    db_path: Path, session_id: str, container_root: Path,
) -> None:
    assert db_path.exists(), f"SQLite db not created at {db_path}"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT source, acp_metadata_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"session {session_id} not found in SQLite"
    assert row["source"] == "acp"
    assert session_id.startswith("acp-")

    metadata = json.loads(row["acp_metadata_json"]) if row["acp_metadata_json"] else {}
    cwd = metadata.get("cwd", "")
    assert cwd != "", "acp_metadata.cwd is empty"
    container_root_resolved = str(container_root.resolve())
    assert cwd.startswith(container_root_resolved), (
        f"cwd {cwd!r} should be under container root {container_root_resolved!r}"
    )
    assert "host-workspace" not in cwd, (
        f"cwd {cwd!r} should be container path, not host path"
    )


async def _drain_stream(stream: Any) -> str:
    """Drain a subprocess stream into a string, preventing pipe buffer deadlock."""
    if stream is None:
        return ""
    chunks: list[bytes] = []
    while True:
        try:
            chunk = await stream.read(4096)
        except (asyncio.CancelledError, Exception):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")
