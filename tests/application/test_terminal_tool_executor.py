"""Unit tests for TerminalToolExecutor.

Covers T5 of plan-260707-terminal-in-sandbox:
- command validation (missing/empty/whitespace)
- timeout validation (missing/convertible/zero/negative/non-convertible)
- session_id resolution (context.session_id or request.id)
- session lock held during get_or_create and exec_command
- default workdir vs explicit workdir
- Docker workdir validation (posixpath.normpath, reject escape/relative/NUL)
- Local workdir validation (Path.resolve, reject escape/relative/NUL/symlink)
- Local scratch subdir creation, workspace subdir not created
- status mapping (SUCCESS/TIMEOUT/ERROR, nonzero returncode stays SUCCESS)
- content fields (exactly status/stdout/stderr/returncode/duration_seconds)
- sandbox exceptions caught and returned as ERROR
- history audit on all paths (success/error/timeout/param error/workdir error)
- history registry exception doesn't change tool result
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.application.terminal_tool_executor import TerminalToolExecutor
from app.domain.sandbox import SandboxExecResult, SandboxStatus
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from app.infrastructure.sandbox.history_registry import (
    SQLiteSandboxExecutionHistoryRegistry,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _TrackingLock:
    """asyncio.Lock wrapper that tracks whether it's currently held."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.held = False

    async def __aenter__(self) -> "_TrackingLock":
        await self._lock.acquire()
        self.held = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._lock.release()
        self.held = False


@dataclass
class _FakeSandbox:
    manager: "_FakeSandboxManager"
    next_result: SandboxExecResult | None = None
    raise_exc: Exception | None = None
    exec_calls: list[tuple[str, str, int]] = field(default_factory=list)

    async def exec_command(
        self, command: str, workdir: str, timeout_seconds: int
    ) -> SandboxExecResult:
        assert self.manager.lock.held, "exec_command called outside session lock"
        self.exec_calls.append((command, workdir, timeout_seconds))
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.next_result is not None
        return self.next_result


class _FakeSandboxManager:
    def __init__(
        self,
        scratch_root: Path,
        workspace_root: Path,
        sandbox_type: str = "docker",
    ) -> None:
        self.scratch_root = scratch_root
        self.workspace_root = workspace_root
        self.sandbox_type = sandbox_type
        self.lock = _TrackingLock()
        self.sandbox = _FakeSandbox(self)
        self.default_workdir_calls: list[str] = []
        self.get_or_create_calls: list[str] = []
        self.get_or_create_exc: Exception | None = None
        scratch_root.mkdir(parents=True, exist_ok=True)
        workspace_root.mkdir(parents=True, exist_ok=True)

    def acquire_session_lock(self, session_id: str) -> _TrackingLock:
        return self.lock

    async def get_or_create(self, session_id: str, grant=None) -> _FakeSandbox:
        assert self.lock.held, "get_or_create called outside session lock"
        self.get_or_create_calls.append(session_id)
        if self.get_or_create_exc is not None:
            raise self.get_or_create_exc
        return self.sandbox

    def default_workdir(self, session_id: str) -> str:
        self.default_workdir_calls.append(session_id)
        if self.sandbox_type == "docker":
            return f"/scratch/sess-{session_id}"
        return str(self.scratch_root / f"sess-{session_id}")


class _FakeSettings:
    sandbox_timeout_seconds = 30


class _RaisingHistoryRegistry:
    def record(self, entry: Any) -> None:
        raise RuntimeError("history write failed")

    def list_recent(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[Any]:
        return []

    def delete(self, entry_id: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor(
    tmp_path: Path,
    *,
    sandbox_type: str = "docker",
    sandbox: _FakeSandbox | None = None,
    history: bool = True,
) -> tuple[
    TerminalToolExecutor,
    _FakeSandboxManager,
    _FakeSandbox,
    SQLiteSandboxExecutionHistoryRegistry | None,
]:
    scratch_root = tmp_path / "scratch"
    workspace_root = tmp_path / "workspace"
    manager = _FakeSandboxManager(scratch_root, workspace_root, sandbox_type=sandbox_type)
    if sandbox is not None:
        manager.sandbox = sandbox
        sandbox.manager = manager
    history_registry = (
        SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db") if history else None
    )
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history_registry,
    )
    return executor, manager, manager.sandbox, history_registry


def _make_context(session_id: str, actor_id: str = "u1") -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        execution_context_mode="realtime",
        trusted_metadata={"gateway.platform": "feishu", "actor_id": actor_id},
    )


def _success_result(
    stdout: str = "ok\n", stderr: str = "", returncode: int = 0
) -> SandboxExecResult:
    return SandboxExecResult(
        status=SandboxStatus.SUCCESS,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        duration_seconds=0.01,
    )


def _timeout_result() -> SandboxExecResult:
    return SandboxExecResult(
        status=SandboxStatus.TIMEOUT,
        stdout="",
        stderr="execution timed out after 30s",
        returncode=124,
        duration_seconds=30.0,
    )


def _error_result() -> SandboxExecResult:
    return SandboxExecResult(
        status=SandboxStatus.ERROR,
        stdout="",
        stderr="boom",
        returncode=-1,
        duration_seconds=0.01,
    )


# ---------------------------------------------------------------------------
# S1: command validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_missing_returns_error(tmp_path: Path):
    executor, _, _, history = _make_executor(tmp_path)
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "command required"}
    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_command_none_returns_error(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path)
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": None})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "command required"}


@pytest.mark.asyncio
async def test_command_empty_returns_error(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path)
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": ""})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "command required"}


@pytest.mark.asyncio
async def test_command_whitespace_returns_error(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path)
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "   \t\n"})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "command required"}


# ---------------------------------------------------------------------------
# S2: timeout validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_missing_uses_settings_default(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("s1"))

    assert sandbox.exec_calls[0][2] == 30  # _FakeSettings.sandbox_timeout_seconds


@pytest.mark.asyncio
async def test_timeout_string_convertible(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "timeout": "15"}
    )

    await executor.execute(req, _make_context("s1"))

    assert sandbox.exec_calls[0][2] == 15


@pytest.mark.asyncio
async def test_timeout_int(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "timeout": 20}
    )

    await executor.execute(req, _make_context("s1"))

    assert sandbox.exec_calls[0][2] == 20


@pytest.mark.asyncio
async def test_timeout_zero_returns_error(tmp_path: Path):
    executor, _, _, history = _make_executor(tmp_path)
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "timeout": 0}
    )

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "timeout must be a positive integer"}
    rows = history.list_recent(limit=10)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_timeout_negative_returns_error(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path)
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "timeout": -5}
    )

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "timeout must be a positive integer"}


@pytest.mark.asyncio
async def test_timeout_non_convertible_returns_error(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path)
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "timeout": "abc"}
    )

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "timeout must be a positive integer"}


# ---------------------------------------------------------------------------
# S3: session_id resolution and lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_id_from_context(tmp_path: Path):
    executor, manager, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("ctx-session-1"))

    assert manager.get_or_create_calls == ["ctx-session-1"]


@pytest.mark.asyncio
async def test_session_id_falls_back_to_request_id(tmp_path: Path):
    executor, manager, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="req-id-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context(""))

    assert manager.get_or_create_calls == ["req-id-1"]


@pytest.mark.asyncio
async def test_session_id_falls_back_to_request_id_when_context_none(tmp_path: Path):
    executor, manager, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="req-id-2", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, None)

    assert manager.get_or_create_calls == ["req-id-2"]


@pytest.mark.asyncio
async def test_get_or_create_and_exec_command_inside_lock(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("s1"))

    # The fake sandbox asserts lock.held in both get_or_create and exec_command.
    # If the executor didn't hold the lock, the test would have errored.
    assert sandbox.exec_calls == [("ls", sandbox.exec_calls[0][1], 30)]


# ---------------------------------------------------------------------------
# S4: default workdir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_workdir_called_when_workdir_not_provided(tmp_path: Path):
    executor, manager, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("s1"))

    assert manager.default_workdir_calls == ["s1"]
    assert sandbox.exec_calls[0][1] == "/scratch/sess-s1"


@pytest.mark.asyncio
async def test_default_workdir_not_called_when_workdir_provided(tmp_path: Path):
    executor, manager, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "workdir": "/scratch"}
    )

    await executor.execute(req, _make_context("s1"))

    assert manager.default_workdir_calls == []
    assert sandbox.exec_calls[0][1] == "/scratch"


# ---------------------------------------------------------------------------
# S5: Docker workdir validation (direct helper tests)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workdir",
    [
        "",
        "relative",
        "../scratch",
        "scratch/foo",
        "\x00/scratch",
        "/scratch\x00foo",
        "/etc",
        "/etc/passwd",
        "/scratch/../../etc",
        "/",
        "/tmp",
        "/scratchfoo",
        "/scratchfoo/bar",
        "/workspacefoo",
    ],
)
def test_docker_workdir_reject_invalid(workdir: str, tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path, sandbox_type="docker")
    assert executor._validate_docker_workdir(workdir) is None


@pytest.mark.parametrize(
    "workdir, expected_norm",
    [
        ("/scratch", "/scratch"),
        ("/scratch/", "/scratch"),
        ("/scratch/./foo", "/scratch/foo"),
        ("/scratch/foo/bar", "/scratch/foo/bar"),
        ("/workspace", "/workspace"),
        ("/workspace/", "/workspace"),
        ("/workspace/foo", "/workspace/foo"),
        ("/scratch/../workspace", "/workspace"),
    ],
)
def test_docker_workdir_accept_valid(workdir: str, expected_norm: str, tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path, sandbox_type="docker")
    assert executor._validate_docker_workdir(workdir) == expected_norm


# ---------------------------------------------------------------------------
# S6: Docker workdir behavior via execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docker_explicit_scratch_passed_to_sandbox(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path, sandbox_type="docker")
    sandbox.next_result = _success_result()
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "workdir": "/scratch/sub"}
    )

    await executor.execute(req, _make_context("s1"))

    assert sandbox.exec_calls[0][1] == "/scratch/sub"


@pytest.mark.asyncio
async def test_docker_explicit_workspace_passed_to_sandbox(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path, sandbox_type="docker")
    sandbox.next_result = _success_result()
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "workdir": "/workspace/sub"}
    )

    await executor.execute(req, _make_context("s1"))

    assert sandbox.exec_calls[0][1] == "/workspace/sub"


# ---------------------------------------------------------------------------
# S7: Local workdir validation (direct helper tests)
# ---------------------------------------------------------------------------


def test_local_workdir_reject_relative(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path, sandbox_type="local")
    workdir, error = executor._prepare_local_workdir("relative/path")
    assert workdir is None
    assert error is not None


def test_local_workdir_reject_empty(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path, sandbox_type="local")
    workdir, error = executor._prepare_local_workdir("")
    assert workdir is None
    assert error is not None


def test_local_workdir_reject_nul(tmp_path: Path):
    executor, manager, _, _ = _make_executor(tmp_path, sandbox_type="local")
    workdir, error = executor._prepare_local_workdir(
        str(manager.scratch_root / "foo\x00bar")
    )
    assert workdir is None
    assert error is not None


def test_local_workdir_reject_outside_root(tmp_path: Path):
    executor, _, _, _ = _make_executor(tmp_path, sandbox_type="local")
    workdir, error = executor._prepare_local_workdir(str(tmp_path / "outside"))
    assert workdir is None
    assert error is not None


def test_local_workdir_reject_symlink_escape(tmp_path: Path):
    executor, manager, _, _ = _make_executor(tmp_path, sandbox_type="local")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = manager.scratch_root / "escape"
    link.symlink_to(outside)

    workdir, error = executor._prepare_local_workdir(str(link))

    assert workdir is None
    assert error is not None


def test_local_workdir_accept_scratch(tmp_path: Path):
    executor, manager, _, _ = _make_executor(tmp_path, sandbox_type="local")
    workdir, error = executor._prepare_local_workdir(str(manager.scratch_root))
    assert workdir is not None
    assert error is None


def test_local_workdir_accept_scratch_subdir(tmp_path: Path):
    executor, manager, _, _ = _make_executor(tmp_path, sandbox_type="local")
    workdir, error = executor._prepare_local_workdir(str(manager.scratch_root / "sub"))
    assert workdir is not None
    assert error is None
    assert Path(workdir).is_dir()


def test_local_workdir_accept_workspace(tmp_path: Path):
    executor, manager, _, _ = _make_executor(tmp_path, sandbox_type="local")
    workdir, error = executor._prepare_local_workdir(str(manager.workspace_root))
    assert workdir is not None
    assert error is None


def test_local_workdir_accept_workspace_subdir(tmp_path: Path):
    executor, manager, _, _ = _make_executor(tmp_path, sandbox_type="local")
    ws_sub = manager.workspace_root / "sub"
    ws_sub.mkdir()

    workdir, error = executor._prepare_local_workdir(str(ws_sub))

    assert workdir is not None
    assert error is None


# ---------------------------------------------------------------------------
# S8: Local workdir creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_explicit_scratch_subdir_created(tmp_path: Path):
    executor, manager, sandbox, _ = _make_executor(tmp_path, sandbox_type="local")
    sandbox.next_result = _success_result()
    target = manager.scratch_root / "newdir"
    assert not target.exists()
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "workdir": str(target)}
    )

    await executor.execute(req, _make_context("s1"))

    assert target.is_dir()
    assert sandbox.exec_calls[0][1] == str(target.resolve())


@pytest.mark.asyncio
async def test_local_explicit_workspace_subdir_not_created_returns_error(tmp_path: Path):
    executor, manager, sandbox, _ = _make_executor(tmp_path, sandbox_type="local")
    sandbox.next_result = _success_result()
    target = manager.workspace_root / "newdir"
    assert not target.exists()
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "workdir": str(target)}
    )

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert not target.exists()
    assert sandbox.exec_calls == []


# ---------------------------------------------------------------------------
# S9: status mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_maps_to_success(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _timeout_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "sleep 100"})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.TIMEOUT


@pytest.mark.asyncio
async def test_error_maps_to_error(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _error_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR


@pytest.mark.asyncio
async def test_nonzero_returncode_stays_success(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result(stdout="", returncode=7)
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "exit 7"})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content["returncode"] == 7


# ---------------------------------------------------------------------------
# S10: content fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_has_exact_fields(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path)
    sandbox.next_result = _success_result(stdout="out", stderr="err")
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    result = await executor.execute(req, _make_context("s1"))

    assert set(result.content.keys()) == {
        "status",
        "stdout",
        "stderr",
        "returncode",
        "duration_seconds",
    }
    assert result.content["status"] == "success"
    assert result.content["stdout"] == "out"
    assert result.content["stderr"] == "err"
    assert result.content["returncode"] == 0
    assert result.content["duration_seconds"] == 0.01


# ---------------------------------------------------------------------------
# S11: sandbox exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_raises_returns_error(tmp_path: Path):
    executor, manager, _, history = _make_executor(tmp_path)
    manager.get_or_create_exc = RuntimeError("docker unavailable")
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert str(result.content["error"]).startswith("sandbox unavailable:")
    assert "docker unavailable" in result.content["error"]
    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_exec_command_raises_returns_error(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.raise_exc = RuntimeError("exec failed")
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    result = await executor.execute(req, _make_context("s1"))

    assert result.status is ToolResultStatus.ERROR
    assert str(result.content["error"]).startswith("sandbox unavailable:")
    assert "exec failed" in result.content["error"]
    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "error"


# ---------------------------------------------------------------------------
# S12: history audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_records_command_and_hash(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    command = "printf hello"
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": command})

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].code == command
    assert rows[0].code_hash == hashlib.sha256(command.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_history_records_authorized_callback_tools_empty(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert rows[0].authorized_callback_tools == []


@pytest.mark.asyncio
async def test_history_records_tool_name_terminal(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert rows[0].result is not None
    assert rows[0].result["tool_name"] == "terminal"


@pytest.mark.asyncio
async def test_history_records_status_matching_result(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.next_result = _timeout_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "sleep 100"})

    result = await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert rows[0].status == result.status.value


@pytest.mark.asyncio
async def test_history_records_session_id(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("sess-history-1"))

    rows = history.list_recent(limit=10)
    assert rows[0].session_id == "sess-history-1"


# ---------------------------------------------------------------------------
# S13: history on all paths + registry exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_recorded_on_command_error(tmp_path: Path):
    executor, _, _, history = _make_executor(tmp_path)
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "  "})

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].code == "  "


@pytest.mark.asyncio
async def test_history_recorded_on_timeout_error(tmp_path: Path):
    executor, _, _, history = _make_executor(tmp_path)
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "ls", "timeout": 0}
    )

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_history_recorded_on_workdir_error(tmp_path: Path):
    executor, _, _, history = _make_executor(tmp_path)
    req = ToolCallRequest(
        id="tc-1",
        name="terminal",
        arguments={"command": "ls", "workdir": "/etc"},
    )

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_history_recorded_on_sandbox_error(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.raise_exc = RuntimeError("boom")
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_history_recorded_on_timeout(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.next_result = _timeout_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "sleep 100"})

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "timeout"


@pytest.mark.asyncio
async def test_history_recorded_on_success(tmp_path: Path):
    executor, _, sandbox, history = _make_executor(tmp_path)
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    await executor.execute(req, _make_context("sess-1"))

    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "success"


@pytest.mark.asyncio
async def test_history_registry_exception_does_not_change_result(tmp_path: Path):
    executor, _, sandbox, _ = _make_executor(tmp_path, history=False)
    executor.history_registry = _RaisingHistoryRegistry()
    sandbox.next_result = _success_result()
    req = ToolCallRequest(id="tc-1", name="terminal", arguments={"command": "ls"})

    result = await executor.execute(req, _make_context("sess-1"))

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content["stdout"] == "ok\n"
