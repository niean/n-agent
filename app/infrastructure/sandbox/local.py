"""LocalSandbox — subprocess + UDS RPC. Trusted-dev only.

Per spec: subprocess + cwd + env scrub cannot constrain arbitrary Python
code. This backend exists for local development ergonomics, NOT as a
security boundary. `sandbox_type=local` logs a WARNING at startup.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.sandbox import (
    Sandbox,
    SandboxCallbackContext,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxStatus,
)
from app.infrastructure.sandbox.rpc_server import SandboxRpcServer
from app.infrastructure.sandbox.stub_generator import generate_stub


_SECRET_SUBSTRINGS = ("api_key", "token", "secret", "password")
_SAFE_ENV_PREFIXES = ("PATH", "HOME", "LANG", "LC_", "PYTHONPATH", "TMPDIR", "TMP", "TEMP")
_SAFE_ENV_PREFIXES_LOWER = tuple(p.lower() for p in _SAFE_ENV_PREFIXES)


def _scrub_env(base: dict | None = None) -> dict:
    env = dict(base or os.environ)
    scrubbed = {}
    for k, v in env.items():
        lk = k.lower()
        if any(s in lk for s in _SECRET_SUBSTRINGS):
            continue
        if lk.startswith(_SAFE_ENV_PREFIXES_LOWER) or lk in ("user", "shell"):
            scrubbed[k] = v
    scrubbed["PYTHONUNBUFFERED"] = "1"
    return scrubbed


_SECRET_RE = re.compile(r"(api[_-]?key|token|secret|password|bearer)", re.IGNORECASE)


def _redact_secrets(text: str) -> str:
    return _SECRET_RE.sub("****", text)


class LocalSandbox(Sandbox):
    def __init__(self, registry, workspace_root: Path) -> None:
        self.registry = registry
        self.workspace_root = workspace_root
        self.container_status = "local"

    async def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        start = datetime.now(timezone.utc)
        staging = request.scratch_dir
        staging.mkdir(parents=True, exist_ok=True)
        stub_path = staging / "nagent_tools.py"
        # Short suffix (8 hex) to keep UDS path under UNIX_PATH_MAX=108.
        # staging dir is already call-unique, so sock/script don't need full uuid.
        script_path = staging / f"script-{uuid4().hex[:8]}.py"
        sock_path = str(staging / "rpc.sock")
        enabled = sorted(request.enabled_callback_tools)
        stub_path.write_text(generate_stub(enabled, rpc_socket_path=sock_path), encoding="utf-8")
        # Watchdog: os._exit(124) when timeout elapses (kills the whole process group)
        script_path.write_text(
            "import sys; sys.path.insert(0, %r)\nfrom nagent_tools import *\n" % str(staging)
            + "NAGENT_TIMEOUT = %d\n" % request.timeout_seconds
            + "import threading, os, time\n"
            + "def _watchdog():\n"
            + "    time.sleep(NAGENT_TIMEOUT); os._exit(124)\n"
            + "threading.Thread(target=_watchdog, daemon=True).start()\n"
            + request.code,
            encoding="utf-8",
        )
        ctx = SandboxCallbackContext(
            workspace_root=request.workspace_root,
            trusted_metadata=request.trusted_metadata,
            session_id=request.session_id,
            scratch_dir=staging,
        )
        server = SandboxRpcServer(
            registry=self.registry,
            socket_path=sock_path,
            workspace_root=request.workspace_root,
            scratch_dir=staging,
            trusted_metadata=request.trusted_metadata,
            session_id=request.session_id,
            max_tool_calls=request.max_tool_calls,
            callback_context=ctx,
        )
        await server.start_async()
        env = _scrub_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(script_path),
                cwd=str(staging),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            await server.stop()
            return SandboxExecutionResult(
                status=SandboxStatus.ERROR,
                stdout="", stderr=f"subprocess spawn failed: {exc}",
                returncode=-1, tool_calls_made=0, duration_seconds=0.0,
                tool_call_log=[],
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout_seconds
            )
            returncode = proc.returncode if proc.returncode is not None else -1
            status = SandboxStatus.SUCCESS if returncode == 0 else SandboxStatus.ERROR
            if returncode == 124:
                status = SandboxStatus.TIMEOUT
        except asyncio.TimeoutError:
            # Kill the whole process group (watchdog may have raced)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Drain whatever the subprocess already wrote before it died,
            # so timeout diagnostics aren't silently lost.
            stdout_b = b""
            stderr_b = b""
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=2)
            except asyncio.TimeoutError:
                pass
            await server.stop()
            end = datetime.now(timezone.utc)
            partial_stdout = _redact_secrets(stdout_b.decode("utf-8", errors="replace")[:50000])
            partial_stderr = _redact_secrets(stderr_b.decode("utf-8", errors="replace")[:10000])
            return SandboxExecutionResult(
                status=SandboxStatus.TIMEOUT,
                stdout=partial_stdout,
                stderr=f"execution timed out after {request.timeout_seconds}s\n--- partial stderr ---\n{partial_stderr}",
                returncode=124,
                tool_calls_made=server.tool_calls_made,
                duration_seconds=(end - start).total_seconds(),
                tool_call_log=server.tool_call_log,
            )
        await server.stop()
        end = datetime.now(timezone.utc)
        stdout = _redact_secrets(stdout_b.decode("utf-8", errors="replace")[:50000])
        stderr = _redact_secrets(stderr_b.decode("utf-8", errors="replace")[:10000])
        return SandboxExecutionResult(
            status=status,
            stdout=stdout, stderr=stderr,
            returncode=returncode,
            tool_calls_made=server.tool_calls_made,
            duration_seconds=(end - start).total_seconds(),
            tool_call_log=server.tool_call_log,
        )
