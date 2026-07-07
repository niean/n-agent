"""Tests for LocalSandbox.exec_command.

Covers T4 of plan-260707-terminal-in-sandbox:
- success path: returncode 0 -> SandboxStatus.SUCCESS
- non-zero returncode still SUCCESS (shell semantics; the command ran, it just failed)
- timeout: process group killed via os.killpg(SIGKILL), partial output drained,
  returns SandboxStatus.TIMEOUT with returncode=124
- spawn failure (OSError) returns SandboxStatus.ERROR
- workdir doesn't exist -> OSError -> SandboxStatus.ERROR
- stdout/stderr truncated by max_stdout_bytes / max_stderr_bytes
- _redact_secrets applied to stdout/stderr

Tests use REAL subprocess execution (no mocking for happy/error paths)
since `sh -c` is fast and safe. spawn-failure and killpg-spy tests use
monkeypatch to inject controlled failures and verify cleanup.

Shell output generation is /bin/sh-compatible (no bash brace expansion):
`printf '%s' '<literal>'` is used for fixed-length output.
"""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.sandbox import SandboxStatus
from app.infrastructure.sandbox.local import LocalSandbox


def _make_sandbox(
    tmp_path: Path,
    *,
    max_stdout_bytes: int = 50000,
    max_stderr_bytes: int = 10000,
) -> LocalSandbox:
    return LocalSandbox(
        registry=SimpleNamespace(),
        workspace_root=tmp_path / "workspace",
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_success_returns_success_status(tmp_path: Path):
    """returncode 0 -> SUCCESS, stdout captured."""
    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    result = await sandbox.exec_command("printf '%s' hello", str(workdir), 30)

    assert result.status is SandboxStatus.SUCCESS
    assert result.returncode == 0
    assert result.stdout == "hello"
    assert result.stderr == ""
    assert result.duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_exec_command_nonzero_returncode_still_success(tmp_path: Path):
    """Shell semantics: non-zero exit means the command ran but failed."""
    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    result = await sandbox.exec_command("exit 2", str(workdir), 30)

    assert result.status is SandboxStatus.SUCCESS
    assert result.returncode == 2


@pytest.mark.asyncio
async def test_exec_command_returncode_127_still_success(tmp_path: Path):
    """command-not-found is returncode 127, still a successful command execution."""
    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    result = await sandbox.exec_command(
        "this_command_does_not_exist_xyz", str(workdir), 30
    )

    assert result.status is SandboxStatus.SUCCESS
    assert result.returncode == 127


# ---------------------------------------------------------------------------
# Timeout: process group killed, partial output drained
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_timeout_kills_process_group_and_returns_timeout(
    tmp_path: Path, monkeypatch
):
    """Timeout triggers os.killpg(SIGKILL), returns TIMEOUT with returncode=124."""
    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    killpg_calls: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def spy_killpg(pid: int, sig: int) -> None:
        killpg_calls.append((pid, sig))
        try:
            real_killpg(pid, sig)
        except ProcessLookupError:
            pass

    monkeypatch.setattr(os, "killpg", spy_killpg)

    result = await sandbox.exec_command("sleep 100", str(workdir), 1)

    assert result.status is SandboxStatus.TIMEOUT
    assert result.returncode == 124
    assert len(killpg_calls) == 1
    assert killpg_calls[0][1] == signal.SIGKILL
    assert "timed out" in result.stderr.lower()


@pytest.mark.asyncio
async def test_exec_command_timeout_drains_partial_output(
    tmp_path: Path, monkeypatch
):
    """Timeout attempts to drain partial output via second communicate() call.

    Uses mocks to simulate: first wait_for times out, second wait_for (drain)
    returns partial output. Verifies the implementation propagates the drained
    output to the result. Real subprocess drain is unreliable because
    StreamReader cancellation may discard buffered data; the drain contract
    is best-effort, tested here via controlled mocks.
    """
    from unittest.mock import AsyncMock, MagicMock

    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.returncode = -1
    fake_proc.communicate = AsyncMock(
        return_value=(b"partial stdout", b"partial stderr")
    )
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock(return_value=0)

    async def fake_create(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    # Neutralize killpg so the test never sends SIGKILL to a real pgid
    # that might happen to equal fake_proc.pid on the host.
    monkeypatch.setattr(os, "killpg", lambda *a, **kw: None)

    side_effects = iter([
        asyncio.TimeoutError(),
        (b"partial stdout", b"partial stderr"),
    ])

    async def mock_wait_for(coro, *args, **kwargs):
        try:
            coro.close()
        except Exception:
            pass
        item = next(side_effects)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)

    result = await sandbox.exec_command("sleep 100", str(workdir), 1)

    assert result.status is SandboxStatus.TIMEOUT
    assert result.returncode == 124
    assert "partial stdout" in result.stdout
    assert "partial stderr" in result.stderr


@pytest.mark.asyncio
async def test_exec_command_timeout_drain_failure_reaps_subprocess(
    tmp_path: Path, monkeypatch
):
    """If drain also times out, proc.kill()+proc.wait() reaps the subprocess.

    Verifies the drain-failure path doesn't leak the subprocess, and returns
    TIMEOUT with diagnostic stderr mentioning the drain failure.
    """
    from unittest.mock import AsyncMock, MagicMock

    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.returncode = -1
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock(return_value=0)

    async def fake_create(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(os, "killpg", lambda *a, **kw: None)

    # Both wait_for calls (exec + drain) time out
    side_effects = iter([asyncio.TimeoutError(), asyncio.TimeoutError()])

    async def mock_wait_for(coro, *args, **kwargs):
        try:
            coro.close()
        except Exception:
            pass
        item = next(side_effects)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)

    result = await sandbox.exec_command("sleep 100", str(workdir), 1)

    assert result.status is SandboxStatus.TIMEOUT
    assert result.returncode == 124
    # proc.kill() and proc.wait() must be called to reap the subprocess
    fake_proc.kill.assert_called_once()
    fake_proc.wait.assert_awaited_once()
    # stderr should mention the drain failure
    assert "failed to drain" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Spawn failure / OSError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_nonexistent_workdir_returns_error(tmp_path: Path):
    """Nonexistent workdir triggers OSError (FileNotFoundError) -> ERROR."""
    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "does_not_exist"

    result = await sandbox.exec_command("echo hi", str(workdir), 30)

    assert result.status is SandboxStatus.ERROR
    assert result.returncode == -1
    assert result.stdout == ""
    # stderr should contain useful diagnostic text
    assert len(result.stderr) > 0


@pytest.mark.asyncio
async def test_exec_command_spawn_oserror_returns_error(
    tmp_path: Path, monkeypatch
):
    """create_subprocess_exec raising OSError -> ERROR."""

    async def fake_create(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    result = await sandbox.exec_command("echo hi", str(workdir), 30)

    assert result.status is SandboxStatus.ERROR
    assert result.returncode == -1
    assert "spawn" in result.stderr.lower() or "failed" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Truncation and redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_truncates_stdout_and_stderr_separately(tmp_path: Path):
    """stdout truncated to max_stdout_bytes; stderr truncated to max_stderr_bytes."""
    sandbox = _make_sandbox(
        tmp_path,
        max_stdout_bytes=10,
        max_stderr_bytes=5,
    )
    workdir = tmp_path / "wd"
    workdir.mkdir()

    # Generate 100 bytes of stdout and 100 bytes of stderr via printf
    # (sh-compatible, no bash brace expansion)
    stdout_gen = "x" * 100
    stderr_gen = "y" * 100
    cmd = f"printf '%s' '{stdout_gen}'; printf '%s' '{stderr_gen}' 1>&2"

    result = await sandbox.exec_command(cmd, str(workdir), 30)

    assert result.status is SandboxStatus.SUCCESS
    assert len(result.stdout) == 10
    assert result.stdout == "x" * 10
    assert len(result.stderr) == 5
    assert result.stderr == "y" * 5


@pytest.mark.asyncio
async def test_exec_command_redacts_secrets_in_stdout_and_stderr(tmp_path: Path):
    """_redact_secrets replaces api_key/token/secret/password/bearer patterns."""
    sandbox = _make_sandbox(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()

    cmd = (
        "printf '%s' 'api_key=abcdef token=xyz'; "
        "printf '%s' 'Authorization: Bearer secret123' 1>&2"
    )

    result = await sandbox.exec_command(cmd, str(workdir), 30)

    assert result.status is SandboxStatus.SUCCESS
    assert "api_key" not in result.stdout
    assert "token" not in result.stdout
    assert "****" in result.stdout
    assert "Bearer" not in result.stderr
    assert "secret123" not in result.stderr
    assert "****" in result.stderr
