"""Tests for DockerSandbox.exec_command.

Covers T3 of plan-260707-terminal-in-sandbox:
- success path: returncode 0 -> SandboxStatus.SUCCESS
- non-zero returncode still SUCCESS (shell semantics; the command ran, it just failed)
- /scratch... workdir triggers `docker exec <container> mkdir -p <workdir>`
- /workspace... workdir does NOT trigger mkdir -p
- script write failure (write_proc returncode != 0) -> ERROR
- docker exec spawn OSError -> ERROR
- timeout: only exec-phase wait_for times out; drain returns partial output;
  pkill -9 -f <script_basename> invoked; best-effort rm -f /tmp/<script_name>
- stdout/stderr truncated by max_stdout_bytes / max_stderr_bytes
- _redact_secrets applied to stdout/stderr
- _ensure_container exception propagates to caller (not swallowed)
- cleanup (rm -f) runs on success, failure, and timeout
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.sandbox import SandboxStatus
from app.infrastructure.sandbox.docker import DockerSandbox


CONTAINER = "nagent-sandbox-test-abc123"


def _make_sandbox(
    *,
    max_stdout_bytes: int = 50000,
    max_stderr_bytes: int = 10000,
) -> DockerSandbox:
    return DockerSandbox(
        registry=SimpleNamespace(),
        workspace_root=Path("/workspace"),
        image="python:3.11-slim",
        cpus=1.0,
        memory_mb=512,
        session_container_name=CONTAINER,
        network=False,
        host_workspace_root=Path("/host/workspace"),
        host_scratch_root=Path("/host/scratch"),
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )


class _FakeProc:
    """Minimal asyncio.subprocess.Process stub for docker exec tests."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self.communicate = AsyncMock(return_value=(stdout, stderr))
        self.kill = MagicMock()
        self.wait = AsyncMock(return_value=0)


def _install_mocks(
    monkeypatch,
    sandbox: DockerSandbox,
    *,
    write_proc: _FakeProc | Exception,
    exec_proc: _FakeProc | Exception | None = None,
    run_docker_rc: int = 0,
    ensure_container_ok: bool = True,
    wait_for_side_effect=None,
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Patch DockerSandbox internals and asyncio functions for testing.

    Returns (mock_ensure, mock_run_docker, mock_create) for assertion.
    """
    if ensure_container_ok:
        sandbox._ensure_container = AsyncMock(return_value=None)
    else:
        sandbox._ensure_container = AsyncMock(
            side_effect=RuntimeError("container start failed")
        )

    mock_run_docker = AsyncMock(return_value=run_docker_rc)
    sandbox._run_docker = mock_run_docker

    if exec_proc is None:
        # Only write step happens
        if isinstance(write_proc, Exception):
            mock_create = AsyncMock(side_effect=write_proc)
        else:
            mock_create = AsyncMock(return_value=write_proc)
    else:
        if isinstance(write_proc, Exception) or isinstance(exec_proc, Exception):
            mock_create = AsyncMock(side_effect=[write_proc, exec_proc])
        else:
            mock_create = AsyncMock(side_effect=[write_proc, exec_proc])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create)

    if wait_for_side_effect is not None:
        # Build a side_effect function that closes the unused coroutine argument
        # to asyncio.wait_for before raising/returning, so the test does not
        # emit "coroutine never awaited" RuntimeWarnings.
        items_iter = iter(wait_for_side_effect)

        def _wait_for_side_effect(coro, *args, **kwargs):
            try:
                coro.close()
            except Exception:
                pass
            item = next(items_iter)
            if isinstance(item, BaseException):
                raise item
            return item

        mock_wait_for = AsyncMock(side_effect=_wait_for_side_effect)
        monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)

    return sandbox._ensure_container, mock_run_docker, mock_create


def _extract_run_docker_args(mock_run_docker: AsyncMock) -> list[list[str]]:
    """Return list of args-lists passed to _run_docker."""
    return [call.args[0] for call in mock_run_docker.call_args_list]


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_success_returns_success_status(monkeypatch):
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    exec_proc = _FakeProc(returncode=0, stdout=b"hello\n", stderr=b"")
    _, mock_run_docker, mock_create = _install_mocks(
        monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc,
    )

    result = await sandbox.exec_command("echo hello", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.SUCCESS
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.duration_seconds >= 0.0
    # Two create_subprocess_exec calls: write step + exec step
    assert mock_create.await_count == 2
    first_call = mock_create.await_args_list[0]
    assert first_call.kwargs["env"]["DOCKER_CLI_HINTS"] == "false"
    write_proc.communicate.assert_awaited_once()
    written_script = write_proc.communicate.await_args.kwargs["input"].decode("utf-8")
    assert written_script.startswith(
        'export DOCKER_CLI_HINTS="${DOCKER_CLI_HINTS:-false}"\n'
    )
    assert written_script.endswith("echo hello")
    # Best-effort cleanup: rm -f /tmp/cmd-*.sh
    rm_calls = [a for a in _extract_run_docker_args(mock_run_docker) if "rm" in a]
    assert len(rm_calls) == 1
    assert rm_calls[0][-1].startswith("/tmp/cmd-")
    assert rm_calls[0][-1].endswith(".sh")


@pytest.mark.asyncio
async def test_exec_command_nonzero_returncode_still_success(monkeypatch):
    """Shell semantics: non-zero exit means the command ran but failed."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=2, stdout=b"", stderr=b"boom\n")
    _install_mocks(monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc)

    result = await sandbox.exec_command("exit 2", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.SUCCESS
    assert result.returncode == 2
    assert result.stderr == "boom\n"


@pytest.mark.asyncio
async def test_exec_command_returncode_127_still_success(monkeypatch):
    """command-not-found is returncode 127, still a successful command execution."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=127, stdout=b"", stderr=b"sh: foo: not found\n")
    _install_mocks(monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc)

    result = await sandbox.exec_command("foo", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.SUCCESS
    assert result.returncode == 127


# ---------------------------------------------------------------------------
# workdir handling: /scratch mkdir, /workspace no mkdir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_scratch_workdir_triggers_mkdir(monkeypatch):
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    _, mock_run_docker, _ = _install_mocks(
        monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc,
    )

    await sandbox.exec_command("ls", "/scratch/sess-abc/sub", 30)

    mkdir_calls = [
        a for a in _extract_run_docker_args(mock_run_docker)
        if "mkdir" in a
    ]
    assert len(mkdir_calls) == 1
    args = mkdir_calls[0]
    # args = ["exec", container, "mkdir", "-p", workdir]
    assert args[0] == "exec"
    assert args[1] == CONTAINER
    assert args[2] == "mkdir"
    assert args[3] == "-p"
    assert args[4] == "/scratch/sess-abc/sub"


@pytest.mark.asyncio
async def test_exec_command_scratch_root_workdir_triggers_mkdir(monkeypatch):
    """workdir exactly /scratch also triggers mkdir -p (per spec S7)."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=0)
    _, mock_run_docker, _ = _install_mocks(
        monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc,
    )

    await sandbox.exec_command("ls", "/scratch", 30)

    mkdir_calls = [
        a for a in _extract_run_docker_args(mock_run_docker) if "mkdir" in a
    ]
    assert len(mkdir_calls) == 1
    assert mkdir_calls[0][4] == "/scratch"


@pytest.mark.asyncio
async def test_exec_command_workspace_workdir_does_not_mkdir(monkeypatch):
    """/workspace is read-only mount; no mkdir attempted."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=0)
    _, mock_run_docker, _ = _install_mocks(
        monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc,
    )

    await sandbox.exec_command("ls", "/workspace/project", 30)

    args_lists = _extract_run_docker_args(mock_run_docker)
    # Only rm cleanup call, no mkdir
    assert not any("mkdir" in a for a in args_lists)
    assert any("rm" in a for a in args_lists)


@pytest.mark.asyncio
async def test_exec_command_workspace_root_workdir_does_not_mkdir(monkeypatch):
    """/workspace exactly is read-only mount; no mkdir attempted."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=0)
    _, mock_run_docker, _ = _install_mocks(
        monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc,
    )

    await sandbox.exec_command("ls", "/workspace", 30)

    args_lists = _extract_run_docker_args(mock_run_docker)
    assert not any("mkdir" in a for a in args_lists)


# ---------------------------------------------------------------------------
# Error paths: script write failure, OSError on docker exec spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_script_write_failure_returns_error(monkeypatch):
    """write_proc.returncode != 0 -> ERROR (script wasn't successfully written)."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=1, stdout=b"", stderr=b"permission denied\n")
    # exec_proc is never reached, but provide a placeholder for side_effect list
    exec_proc = _FakeProc(returncode=0)
    _, mock_run_docker, mock_create = _install_mocks(
        monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc,
    )

    result = await sandbox.exec_command("echo hi", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.ERROR
    assert result.returncode == 1
    assert "write" in result.stderr.lower() or "permission denied" in result.stderr.lower()
    # Only write step should have spawned; exec step not reached
    assert mock_create.await_count == 1
    # Best-effort cleanup still runs
    rm_calls = [a for a in _extract_run_docker_args(mock_run_docker) if "rm" in a]
    assert len(rm_calls) == 1


@pytest.mark.asyncio
async def test_exec_command_write_step_oserror_returns_error(monkeypatch):
    """create_subprocess_exec raises OSError during write step -> ERROR."""
    sandbox = _make_sandbox()
    # First call (write) raises OSError; exec_proc never reached
    exec_proc = _FakeProc(returncode=0)
    _, mock_run_docker, mock_create = _install_mocks(
        monkeypatch,
        sandbox,
        write_proc=OSError("docker not found"),
        exec_proc=exec_proc,
    )

    result = await sandbox.exec_command("echo hi", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.ERROR
    assert result.returncode == -1
    assert "docker" in result.stderr.lower() or "spawn" in result.stderr.lower()
    assert mock_create.await_count == 1


@pytest.mark.asyncio
async def test_exec_command_exec_step_oserror_returns_error(monkeypatch):
    """create_subprocess_exec raises OSError during exec step -> ERROR."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    _, mock_run_docker, mock_create = _install_mocks(
        monkeypatch,
        sandbox,
        write_proc=write_proc,
        exec_proc=OSError("docker exec failed"),
    )

    result = await sandbox.exec_command("echo hi", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.ERROR
    assert result.returncode == -1
    assert "docker" in result.stderr.lower() or "exec" in result.stderr.lower()
    # Write step + exec step both attempted
    assert mock_create.await_count == 2
    # Cleanup still runs (script was written)
    rm_calls = [a for a in _extract_run_docker_args(mock_run_docker) if "rm" in a]
    assert len(rm_calls) == 1


# ---------------------------------------------------------------------------
# Timeout: pkill + drain + cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_timeout_pkills_drains_and_cleans(monkeypatch):
    """Only exec-phase wait_for times out; drain returns partial; pkill + rm invoked."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    exec_proc = _FakeProc(returncode=-1)  # returncode not used (wait_for mocked)
    _, mock_run_docker, mock_create = _install_mocks(
        monkeypatch,
        sandbox,
        write_proc=write_proc,
        exec_proc=exec_proc,
        # First wait_for (exec) times out; second wait_for (drain) returns partial
        wait_for_side_effect=[
            asyncio.TimeoutError(),
            (b"partial stdout", b"partial stderr"),
        ],
    )

    result = await sandbox.exec_command("sleep 100", "/scratch/sess-test", 5)

    assert result.status is SandboxStatus.TIMEOUT
    assert result.returncode == 124
    assert result.stdout == "partial stdout"
    assert "partial stderr" in result.stderr
    assert "timed out" in result.stderr.lower()

    # pkill -9 -f <script_name> invoked
    args_lists = _extract_run_docker_args(mock_run_docker)
    pkill_calls = [a for a in args_lists if "pkill" in a]
    assert len(pkill_calls) == 1
    pk_args = pkill_calls[0]
    # ["exec", container, "pkill", "-9", "-f", script_name]
    assert pk_args[0] == "exec"
    assert pk_args[1] == CONTAINER
    assert pk_args[2] == "pkill"
    assert pk_args[3] == "-9"
    assert pk_args[4] == "-f"
    script_name = pk_args[5]
    assert script_name.startswith("cmd-")
    assert script_name.endswith(".sh")

    # rm -f /tmp/<script_name> invoked (best-effort cleanup)
    rm_calls = [a for a in args_lists if "rm" in a]
    assert len(rm_calls) == 1
    rm_args = rm_calls[0]
    # ["exec", container, "rm", "-f", "/tmp/cmd-<uuid>.sh"]
    assert rm_args[2] == "rm"
    assert rm_args[3] == "-f"
    assert rm_args[4].startswith("/tmp/cmd-")
    assert rm_args[4].endswith(".sh")
    # rm path uses the same script_name as pkill
    assert script_name in rm_args[4]


@pytest.mark.asyncio
async def test_exec_command_timeout_drain_failure_still_returns_timeout(monkeypatch):
    """If drain also times out, still return TIMEOUT with diagnostic stderr."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=-1)
    _, mock_run_docker, _ = _install_mocks(
        monkeypatch,
        sandbox,
        write_proc=write_proc,
        exec_proc=exec_proc,
        # Both wait_for calls time out
        wait_for_side_effect=[
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ],
    )

    result = await sandbox.exec_command("sleep 100", "/scratch/sess-test", 5)

    assert result.status is SandboxStatus.TIMEOUT
    assert result.returncode == 124
    # pkill still invoked
    args_lists = _extract_run_docker_args(mock_run_docker)
    assert any("pkill" in a for a in args_lists)
    # rm cleanup still invoked
    assert any("rm" in a for a in args_lists)


# ---------------------------------------------------------------------------
# Truncation and redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_truncates_stdout_and_stderr_separately(monkeypatch):
    """stdout truncated to max_stdout_bytes; stderr truncated to max_stderr_bytes."""
    sandbox = _make_sandbox(
        max_stdout_bytes=10,
        max_stderr_bytes=5,
    )
    write_proc = _FakeProc(returncode=0)
    big_stdout = b"a" * 100
    big_stderr = b"b" * 100
    exec_proc = _FakeProc(returncode=0, stdout=big_stdout, stderr=big_stderr)
    _install_mocks(monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc)

    result = await sandbox.exec_command("yes", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.SUCCESS
    assert len(result.stdout) == 10
    assert result.stdout == "a" * 10
    assert len(result.stderr) == 5
    assert result.stderr == "b" * 5


@pytest.mark.asyncio
async def test_exec_command_redacts_secrets_in_stdout_and_stderr(monkeypatch):
    """_redact_secrets replaces api_key/token/secret/password/bearer patterns."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(
        returncode=0,
        stdout=b"api_key=abcdef token=xyz\n",
        stderr=b"Authorization: Bearer secret123\n",
    )
    _install_mocks(monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc)

    result = await sandbox.exec_command("env", "/scratch/sess-test", 30)

    assert result.status is SandboxStatus.SUCCESS
    assert "api_key" not in result.stdout
    assert "token" not in result.stdout
    assert "****" in result.stdout
    assert "Bearer" not in result.stderr
    assert "secret123" not in result.stderr
    assert "****" in result.stderr


# ---------------------------------------------------------------------------
# _ensure_container exception propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_ensure_container_exception_propagates(monkeypatch):
    """_ensure_container raising must NOT be swallowed; executor maps to 'sandbox unavailable'."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=0)
    _install_mocks(
        monkeypatch,
        sandbox,
        write_proc=write_proc,
        exec_proc=exec_proc,
        ensure_container_ok=False,
    )

    with pytest.raises(RuntimeError, match="container start failed"):
        await sandbox.exec_command("echo hi", "/scratch/sess-test", 30)


# ---------------------------------------------------------------------------
# Script write mechanism: stdin-based, not shell-escaped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_writes_script_via_stdin_not_shell_args(monkeypatch):
    """Command is passed as stdin to `cat > /tmp/cmd-*.sh`, not via shell escaping."""
    sandbox = _make_sandbox()
    write_proc = _FakeProc(returncode=0)
    exec_proc = _FakeProc(returncode=0, stdout=b"", stderr=b"")
    _, _, mock_create = _install_mocks(
        monkeypatch, sandbox, write_proc=write_proc, exec_proc=exec_proc,
    )

    # Command with shell metacharacters that would break naive shell concatenation
    tricky_command = "echo 'hello world'; echo $HOME; cat <<EOF\nfoo bar\nEOF"
    await sandbox.exec_command(tricky_command, "/scratch/sess-test", 30)

    # First call: write step uses stdin=PIPE
    write_call = mock_create.call_args_list[0]
    # The args tuple is ("docker", "exec", "-i", container, "sh", "-c", "cat > /tmp/cmd-*.sh")
    write_args = write_call.args
    assert write_args[0] == "docker"
    assert write_args[1] == "exec"
    assert write_args[2] == "-i"
    assert write_args[3] == CONTAINER
    assert write_args[4] == "sh"
    assert write_args[5] == "-c"
    assert "cat >" in write_args[6]
    assert "/tmp/cmd-" in write_args[6]
    # stdin must be PIPE so command is fed via stdin, not shell-escaped args
    assert write_call.kwargs.get("stdin") == asyncio.subprocess.PIPE

    # Verify the command was passed to write_proc.communicate(input=...)
    write_proc.communicate.assert_awaited_once()
    call_kwargs = write_proc.communicate.call_args.kwargs
    assert "input" in call_kwargs
    written_script = call_kwargs["input"].decode("utf-8")
    assert written_script.startswith(
        'export DOCKER_CLI_HINTS="${DOCKER_CLI_HINTS:-false}"\n'
    )
    assert written_script.endswith(tricky_command)
