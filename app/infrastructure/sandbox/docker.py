"""DockerSandbox — docker CLI + UDS RPC via bind-mount socket.

Security posture (per spec):
- cap-drop ALL + no-new-privileges + pids-limit + network=none + tmpfs /tmp
- workspace mounted :ro (writes must go through callback tools, not direct fs)
- scratch mounted :rw (per-call staging)
- sibling container per session, reused across calls; killed on release

UDS RPC (not file-based): the socket file is bind-mounted into the sibling
container at the same path, so host and container share it. The sandboxed
code is a pure UDS client and cannot forge server responses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.sandbox import (
    Sandbox,
    SandboxCallbackContext,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExecResult,
    SandboxStatus,
)
from app.infrastructure.sandbox.rpc_server import SandboxRpcServer
from app.infrastructure.sandbox.stub_generator import generate_stub


logger = logging.getLogger(__name__)

_SECRET_RE = re.compile(r"(api[_-]?key|token|secret|password|bearer)", re.IGNORECASE)


def _redact_secrets(text: str) -> str:
    return _SECRET_RE.sub("****", text)


def _docker_cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["DOCKER_CLI_HINTS"] = "false"
    return env


class DockerSandbox(Sandbox):
    def __init__(
        self,
        registry,
        workspace_root: Path,
        image: str,
        cpus: float,
        memory_mb: int,
        session_container_name: str,
        network: bool = False,
        host_workspace_root: Path | None = None,
        host_scratch_root: Path | None = None,
        max_stdout_bytes: int = 50000,
        max_stderr_bytes: int = 10000,
    ) -> None:
        self.registry = registry
        self.workspace_root = workspace_root
        self.image = image
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.session_container_name = session_container_name
        self.network = network
        self.host_workspace_root = host_workspace_root or workspace_root
        self.host_scratch_root = host_scratch_root or workspace_root
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.container_status: str | None = None  # None = not running

    def is_available(self) -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            r = asyncio.run(self._run_docker(["info"]))
            return r == 0
        except Exception:
            return False

    async def _run_docker(self, args: list[str], timeout: float = 30.0) -> int:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            env=_docker_cli_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return proc.returncode if proc.returncode is not None else -1

    async def _ensure_container(self) -> None:
        if self.container_status == "running":
            # Verify still alive
            rc = await self._run_docker(["inspect", "-f", "{{.State.Running}}", self.session_container_name])
            if rc == 0:
                return
            self.container_status = None
        network_flag = "--network=none" if not self.network else ""
        args = [
            "run", "-d", "--init",
            "--name", self.session_container_name,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--memory", f"{self.memory_mb}m",
            "--cpus", str(self.cpus),
            "--tmpfs", "/tmp:rw,nosuid,size=512m",
        ]
        if not self.network:
            args.append("--network=none")
        args.extend([
            "-v", f"{self.host_workspace_root}:/workspace:ro",
            "-v", f"{self.host_scratch_root}:/scratch",
            "-w", "/scratch",
            self.image, "sleep", "infinity",
        ])
        rc = await self._run_docker(args)
        if rc != 0:
            raise RuntimeError(f"docker run failed for {self.session_container_name} (rc={rc})")
        self.container_status = "running"

    async def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        start = datetime.now(timezone.utc)
        staging = request.scratch_dir
        staging.mkdir(parents=True, exist_ok=True)
        # staging.name is "call-<uuid>" set by SandboxManager.new_call_staging;
        # reuse it so container_staging path matches the host staging directory.
        call_uuid = staging.name  # call-<uuid>
        # Container-side paths (host staging is bind-mounted to /scratch)
        safe_session = staging.parent.name  # sess-<safe>
        container_staging = f"/scratch/{safe_session}/{call_uuid}"
        container_sock = f"{container_staging}/rpc.sock"
        stub_path = staging / "nagent_tools.py"
        script_basename = f"script-{staging.name.removeprefix('call-')}.py"
        script_path = staging / script_basename
        enabled = sorted(request.enabled_callback_tools)
        stub_path.write_text(generate_stub(enabled, rpc_socket_path=container_sock), encoding="utf-8")
        script_path.write_text(
            "import sys; sys.path.insert(0, %r)\nfrom nagent_tools import *\n" % container_staging
            + "NAGENT_TIMEOUT = %d\n" % request.timeout_seconds
            + "import threading, os, time\n"
            + "def _watchdog():\n"
            + "    time.sleep(NAGENT_TIMEOUT); os._exit(124)\n"
            + "threading.Thread(target=_watchdog, daemon=True).start()\n"
            + request.code,
            encoding="utf-8",
        )
        # Host-side socket path mirrors container-side (bind-mount same path)
        host_sock = str(staging / "rpc.sock")
        ctx = SandboxCallbackContext(
            workspace_root=request.workspace_root,
            trusted_metadata=request.trusted_metadata,
            session_id=request.session_id,
            scratch_dir=staging,
        )
        server = SandboxRpcServer(
            registry=self.registry,
            socket_path=host_sock,
            workspace_root=request.workspace_root,
            scratch_dir=staging,
            trusted_metadata=request.trusted_metadata,
            session_id=request.session_id,
            max_tool_calls=request.max_tool_calls,
            callback_context=ctx,
        )
        await server.start_async()
        try:
            await self._ensure_container()
        except RuntimeError as exc:
            await server.stop()
            end = datetime.now(timezone.utc)
            return SandboxExecutionResult(
                status=SandboxStatus.ERROR,
                stdout="", stderr=f"container start failed: {exc}",
                returncode=-1, tool_calls_made=0,
                duration_seconds=(end - start).total_seconds(),
                tool_call_log=[],
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "-w", container_staging,
                self.session_container_name, "python", "-u", script_basename,
                env=_docker_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            await server.stop()
            end = datetime.now(timezone.utc)
            return SandboxExecutionResult(
                status=SandboxStatus.ERROR,
                stdout="", stderr=f"docker exec failed: {exc}",
                returncode=-1, tool_calls_made=server.tool_calls_made,
                duration_seconds=(end - start).total_seconds(),
                tool_call_log=server.tool_call_log,
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
            # Kill by unique script name — avoid clobbering other scripts in the same container
            await self._run_docker(
                ["exec", self.session_container_name, "pkill", "-9", "-f", script_basename],
                timeout=5,
            )
            # Drain whatever the subprocess already wrote before it died,
            # so timeout diagnostics aren't silently lost.
            stdout_b = b""
            stderr_b = b""
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=2)
            except asyncio.TimeoutError:
                # Force-kill the container; mark for rebuild
                await self._run_docker(["kill", self.session_container_name], timeout=10)
                await self._run_docker(["rm", "-f", self.session_container_name], timeout=10)
                self.container_status = None
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

    async def exec_command(
        self, command: str, workdir: str, timeout_seconds: int
    ) -> SandboxExecResult:
        """Execute a shell command inside the existing sandbox container.

        The command is written to /tmp/cmd-<uuid>.sh via `docker exec -i ...
        cat > /tmp/cmd-<uuid>.sh` with the command fed as stdin (avoids shell
        escaping), then executed via `docker exec -w <workdir> <container>
        sh /tmp/cmd-<uuid>.sh`.

        Status semantics (shell): a non-zero returncode still maps to
        SandboxStatus.SUCCESS — the command ran, it just failed. Only timeout
        returns TIMEOUT; only spawn/write errors return ERROR.
        """
        start = datetime.now(timezone.utc)
        # S6: ensure container first; let exceptions propagate to executor
        # so it can uniformly map them to "sandbox unavailable".
        await self._ensure_container()

        # S7: mkdir -p for /scratch or /scratch/... only (scratch is rw mount).
        # /workspace or /workspace/... is read-only; let docker exec -w fail
        # at the command level rather than silently creating directories.
        norm = posixpath.normpath(workdir)
        if norm == "/scratch" or norm.startswith("/scratch/"):
            await self._run_docker(
                ["exec", self.session_container_name, "mkdir", "-p", workdir]
            )

        # S8: unique script name; write command via stdin to avoid shell escaping.
        script_name = f"cmd-{uuid4().hex}.sh"
        script_path = f"/tmp/{script_name}"

        try:
            # Write step: `docker exec -i <container> sh -c "cat > /tmp/cmd-*.sh"`
            try:
                write_proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", "-i", self.session_container_name,
                    "sh", "-c", f"cat > {script_path}",
                    env=_docker_cli_env(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                end = datetime.now(timezone.utc)
                return SandboxExecResult(
                    status=SandboxStatus.ERROR,
                    stdout="",
                    stderr=f"docker exec write spawn failed: {exc}",
                    returncode=-1,
                    duration_seconds=(end - start).total_seconds(),
                )

            # Feed command as stdin; this completes the `cat > /tmp/cmd-*.sh`.
            try:
                script_body = (
                    'export DOCKER_CLI_HINTS="${DOCKER_CLI_HINTS:-false}"\n'
                    + command
                )
                await write_proc.communicate(input=script_body.encode("utf-8"))
            except Exception as exc:
                end = datetime.now(timezone.utc)
                return SandboxExecResult(
                    status=SandboxStatus.ERROR,
                    stdout="",
                    stderr=f"script write communicate failed: {exc}",
                    returncode=-1,
                    duration_seconds=(end - start).total_seconds(),
                )

            if write_proc.returncode not in (0, None):
                end = datetime.now(timezone.utc)
                return SandboxExecResult(
                    status=SandboxStatus.ERROR,
                    stdout="",
                    stderr=(
                        f"docker exec script write failed (rc={write_proc.returncode})"
                    ),
                    returncode=(
                        write_proc.returncode
                        if write_proc.returncode is not None
                        else -1
                    ),
                    duration_seconds=(end - start).total_seconds(),
                )

            # S9: execute via `docker exec -w <workdir> <container> sh /tmp/cmd-*.sh`
            try:
                exec_proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", "-w", workdir, self.session_container_name,
                    "sh", script_path,
                    env=_docker_cli_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                end = datetime.now(timezone.utc)
                return SandboxExecResult(
                    status=SandboxStatus.ERROR,
                    stdout="",
                    stderr=f"docker exec failed: {exc}",
                    returncode=-1,
                    duration_seconds=(end - start).total_seconds(),
                )

            # S9: command completion returns SUCCESS regardless of returncode.
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    exec_proc.communicate(), timeout=timeout_seconds
                )
                returncode = (
                    exec_proc.returncode if exec_proc.returncode is not None else -1
                )
                status = SandboxStatus.SUCCESS
            except asyncio.TimeoutError:
                # S10: kill by unique script name, drain partial output, return TIMEOUT.
                await self._run_docker(
                    [
                        "exec", self.session_container_name,
                        "pkill", "-9", "-f", script_name,
                    ],
                    timeout=5,
                )
                stdout_b = b""
                stderr_b = b""
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        exec_proc.communicate(), timeout=2
                    )
                except asyncio.TimeoutError:
                    stderr_b = b"failed to drain subprocess output after timeout"
                    try:
                        exec_proc.kill()
                        await exec_proc.wait()
                    except ProcessLookupError:
                        pass
                end = datetime.now(timezone.utc)
                partial_stdout = _redact_secrets(
                    stdout_b.decode("utf-8", errors="replace")[: self.max_stdout_bytes]
                )
                partial_stderr = _redact_secrets(
                    stderr_b.decode("utf-8", errors="replace")[: self.max_stderr_bytes]
                )
                return SandboxExecResult(
                    status=SandboxStatus.TIMEOUT,
                    stdout=partial_stdout,
                    stderr=(
                        f"execution timed out after {timeout_seconds}s\n"
                        f"--- partial stderr ---\n{partial_stderr}"
                    ),
                    returncode=124,
                    duration_seconds=(end - start).total_seconds(),
                )

            # S12: UTF-8 replace decode, _redact_secrets, truncate by config.
            end = datetime.now(timezone.utc)
            stdout = _redact_secrets(
                stdout_b.decode("utf-8", errors="replace")[: self.max_stdout_bytes]
            )
            stderr = _redact_secrets(
                stderr_b.decode("utf-8", errors="replace")[: self.max_stderr_bytes]
            )
            return SandboxExecResult(
                status=status,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                duration_seconds=(end - start).total_seconds(),
            )
        finally:
            # S11: best-effort cleanup regardless of success/failure/timeout.
            # rm -f is safe even if the script was never written.
            try:
                await self._run_docker(
                    ["exec", self.session_container_name, "rm", "-f", script_path]
                )
            except Exception:
                pass

    async def cleanup_container(self) -> None:
        if self.container_status is None and not self.session_container_name:
            return
        for args in (["kill", self.session_container_name], ["rm", "-f", self.session_container_name]):
            try:
                await self._run_docker(args, timeout=15)
            except Exception:
                continue
        self.container_status = None
