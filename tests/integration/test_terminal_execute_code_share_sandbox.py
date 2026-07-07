"""Integration test: terminal and execute_code share session sandbox.

Docker-only: skips when docker CLI unavailable or docker daemon down.
Verifies terminal and execute_code share the same session-level sandbox
container and scratch directory (T7 of plan-260707-terminal-in-sandbox).
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.application.sandbox_tool_executor import SandboxToolExecutor
from app.application.terminal_tool_executor import TerminalToolExecutor
from app.config import Settings
from app.domain.tool import ToolCallRequest, ToolExecutionContext, ToolResultStatus
from app.infrastructure.sandbox.history_registry import SQLiteSandboxExecutionHistoryRegistry
from app.infrastructure.sandbox.manager import SandboxManager, _safe_session_segment
from app.infrastructure.sandbox.registry import InMemorySandboxCallbackToolRegistry


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker not available")


def _make_context(session_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        execution_context_mode="realtime",
        trusted_metadata={},
    )


def _build_env(tmp_path: Path):
    # Use a short base path (/tmp) to keep UDS socket path under UNIX_PATH_MAX=108.
    # pytest's tmp_path on macOS is ~70 chars, which pushes rpc.sock over the limit.
    base = Path("/tmp") / f"nagent-int-{uuid.uuid4().hex[:8]}"
    workspace = base / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    scratch = base / "scratch"
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(base / "sessions.db"),
        workspace_root=str(workspace),
        skills_root=str(base / "skills"),
        scheduler_enabled=False,
        feishu_enabled=False,
        sandbox_enabled=True,
        sandbox_type="docker",
        sandbox_scratch_root=str(scratch),
    )
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    callback_registry = InMemorySandboxCallbackToolRegistry()
    manager = SandboxManager(
        sandbox_type="docker",
        workspace_root=workspace,
        idle_seconds=900,
        settings=settings,
        callback_registry=callback_registry,
        scratch_root=scratch,
        host_workspace_root=workspace,
        host_scratch_root=scratch,
    )
    terminal_exec = TerminalToolExecutor(manager, settings, history)
    code_exec = SandboxToolExecutor(manager, callback_registry, settings, history)
    session_id = f"int-{uuid.uuid4().hex[:8]}"
    return manager, terminal_exec, code_exec, session_id, base


async def _cleanup(manager, session_id: str, base: Path) -> None:
    try:
        await manager.release(session_id, reason="test cleanup")
    except Exception:
        pass
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_terminal_writes_then_reads_marker(tmp_path: Path):
    """S2: terminal writes marker.txt, then reads it back — scratch persistence."""
    manager, terminal_exec, _, session_id, base = _build_env(tmp_path)
    ctx = _make_context(session_id)
    try:
        write_result = await terminal_exec.execute(
            ToolCallRequest(
                id="tc-1", name="terminal",
                arguments={"command": "printf shared > marker.txt"},
            ),
            ctx,
        )
        assert write_result.status is ToolResultStatus.SUCCESS

        read_result = await terminal_exec.execute(
            ToolCallRequest(
                id="tc-2", name="terminal",
                arguments={"command": "cat marker.txt"},
            ),
            ctx,
        )
        assert read_result.status is ToolResultStatus.SUCCESS
        assert read_result.content["stdout"] == "shared"
    finally:
        await _cleanup(manager, session_id, base)


@pytest.mark.asyncio
async def test_terminal_and_execute_code_share_scratch(tmp_path: Path):
    """S3: terminal writes marker, execute_code reads it — shared sandbox/scratch."""
    manager, terminal_exec, code_exec, session_id, base = _build_env(tmp_path)
    ctx = _make_context(session_id)
    safe = _safe_session_segment(session_id)
    try:
        write_result = await terminal_exec.execute(
            ToolCallRequest(
                id="tc-1", name="terminal",
                arguments={"command": "printf shared > marker.txt"},
            ),
            ctx,
        )
        assert write_result.status is ToolResultStatus.SUCCESS

        code = f"print(open('/scratch/{safe}/marker.txt').read(), end='')"
        code_result = await code_exec.execute(
            ToolCallRequest(
                id="tc-2", name="execute_code",
                arguments={"code": code},
            ),
            ctx,
        )
        assert code_result.status is ToolResultStatus.SUCCESS
        assert "shared" in code_result.content["stdout"]
    finally:
        await _cleanup(manager, session_id, base)


@pytest.mark.asyncio
async def test_terminal_nonzero_returncode_is_success(tmp_path: Path):
    """S4: terminal(command='exit 7') returns SUCCESS with returncode=7."""
    manager, terminal_exec, _, session_id, base = _build_env(tmp_path)
    ctx = _make_context(session_id)
    try:
        result = await terminal_exec.execute(
            ToolCallRequest(
                id="tc-1", name="terminal",
                arguments={"command": "exit 7"},
            ),
            ctx,
        )
        assert result.status is ToolResultStatus.SUCCESS
        assert result.content["returncode"] == 7
    finally:
        await _cleanup(manager, session_id, base)


@pytest.mark.asyncio
async def test_terminal_timeout_then_recovery(tmp_path: Path):
    """S5: timeout returns TIMEOUT, then same session terminal still works."""
    manager, terminal_exec, _, session_id, base = _build_env(tmp_path)
    ctx = _make_context(session_id)
    try:
        timeout_result = await terminal_exec.execute(
            ToolCallRequest(
                id="tc-1", name="terminal",
                arguments={"command": "sleep 100", "timeout": 2},
            ),
            ctx,
        )
        assert timeout_result.status is ToolResultStatus.TIMEOUT

        ok_result = await terminal_exec.execute(
            ToolCallRequest(
                id="tc-2", name="terminal",
                arguments={"command": "printf ok"},
            ),
            ctx,
        )
        assert ok_result.status is ToolResultStatus.SUCCESS
        assert ok_result.content["stdout"] == "ok"
    finally:
        await _cleanup(manager, session_id, base)


@pytest.mark.asyncio
async def test_terminal_write_workspace_readonly(tmp_path: Path):
    """S6: write to /workspace fails (read-only mount), host workspace not modified."""
    manager, terminal_exec, _, session_id, base = _build_env(tmp_path)
    ctx = _make_context(session_id)
    try:
        result = await terminal_exec.execute(
            ToolCallRequest(
                id="tc-1", name="terminal",
                arguments={"command": "echo hi > /workspace/foo"},
            ),
            ctx,
        )
        assert result.status is ToolResultStatus.SUCCESS
        assert result.content["returncode"] != 0
        assert not (manager.workspace_root / "foo").exists()
    finally:
        await _cleanup(manager, session_id, base)
