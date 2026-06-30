"""Unit tests for SandboxToolExecutor direct execution.

Covers the security-critical core (post-Hermes-baseline):
- execute_code is SAFE; runs directly, no confirmation gate
- Sandbox success/error/timeout all map to ToolResult statuses
- History is recorded on every path (success/error/timeout/exception)
- Sandbox exceptions are caught and returned as ERROR (graph not interrupted)
- enabled_tools filter intersects with callback_registry enabled set
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.application.sandbox_tool_executor import SandboxToolExecutor
from app.domain.sandbox import (
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxStatus,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
)
from app.infrastructure.sandbox.history_registry import SQLiteSandboxExecutionHistoryRegistry


@dataclass
class _FakeSandbox:
    next_result: SandboxExecutionResult | None = None
    raise_exc: Exception | None = None
    last_request: SandboxExecutionRequest | None = None

    async def execute(self, req: SandboxExecutionRequest) -> SandboxExecutionResult:
        self.last_request = req
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.next_result is not None
        return self.next_result


class _FakeSandboxManager:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self._sandbox = _FakeSandbox()
        self._lock = asyncio.Lock()

    def acquire_session_lock(self, session_id: str):
        return self._lock

    async def get_or_create(self, session_id: str):
        return self._sandbox

    def new_call_staging(self, session_id: str) -> Path:
        d = self.workspace_root / f"call-{session_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d


class _FakeCallbackTool:
    def __init__(self, name: str):
        self.name = name


class _FakeCallbackRegistry:
    def __init__(self, enabled_names: list[str] | None = None):
        self._enabled = [_FakeCallbackTool(n) for n in (enabled_names or [])]

    def list_enabled(self):
        return self._enabled


class _FakeSettings:
    sandbox_timeout_seconds = 30
    sandbox_max_tool_calls = 50
    sandbox_summary_max_stdout_bytes = 2000
    sandbox_summary_max_stderr_bytes = 500


def _make_executor(
    tmp_path: Path,
    sandbox: _FakeSandbox,
    *,
    enabled_callbacks: list[str] | None = None,
) -> tuple[SandboxToolExecutor, _FakeSandbox, SQLiteSandboxExecutionHistoryRegistry]:
    manager = _FakeSandboxManager(tmp_path)
    manager._sandbox = sandbox
    history_registry = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(enabled_callbacks),
        settings=_FakeSettings(),
        history_registry=history_registry,
    )
    return executor, sandbox, history_registry


def _make_context(session_id: str, actor_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        execution_context_mode="realtime",
        trusted_metadata={"gateway.platform": "feishu", "actor_id": actor_id},
    )


def _success_result() -> SandboxExecutionResult:
    return SandboxExecutionResult(
        status=SandboxStatus.SUCCESS,
        stdout="1\n",
        stderr="",
        returncode=0,
        tool_calls_made=0,
        tool_call_log=[],
        duration_seconds=0.01,
    )


def _error_result() -> SandboxExecutionResult:
    return SandboxExecutionResult(
        status=SandboxStatus.ERROR,
        stdout="",
        stderr="boom",
        returncode=1,
        tool_calls_made=0,
        tool_call_log=[],
        duration_seconds=0.01,
    )


def _timeout_result() -> SandboxExecutionResult:
    return SandboxExecutionResult(
        status=SandboxStatus.TIMEOUT,
        stdout="",
        stderr="",
        returncode=124,
        tool_calls_made=0,
        tool_call_log=[],
        duration_seconds=30.0,
    )


@pytest.mark.asyncio
async def test_success_returns_success_and_records_history(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    executor, _, history = _make_executor(tmp_path, sandbox)
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})

    result = await executor.execute(req, _make_context("s1", "u1"))

    assert result.status is ToolResultStatus.SUCCESS
    assert result.tool_call_id == "tc-1"
    assert result.tool_name == "execute_code"
    code_hash = hashlib.sha256(b"print(1)").hexdigest()
    rows = history.list_recent(limit=10)
    assert [row.id for row in rows] == ["tc-1"]
    assert rows[0].code_hash == code_hash
    assert rows[0].status == "success"


@pytest.mark.asyncio
async def test_error_returns_error_and_records_history(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_error_result())
    executor, _, history = _make_executor(tmp_path, sandbox)
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "raise Boom"})

    result = await executor.execute(req, _make_context("s1", "u1"))

    assert result.status is ToolResultStatus.ERROR
    rows = history.list_recent(limit=10)
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_timeout_returns_timeout_and_records_history(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_timeout_result())
    executor, _, history = _make_executor(tmp_path, sandbox)
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "while True: pass"})

    result = await executor.execute(req, _make_context("s1", "u1"))

    assert result.status is ToolResultStatus.TIMEOUT
    rows = history.list_recent(limit=10)
    assert rows[0].status == "timeout"


@pytest.mark.asyncio
async def test_sandbox_exception_caught_and_recorded_as_error(tmp_path: Path):
    sandbox = _FakeSandbox(raise_exc=RuntimeError("docker unavailable"))
    executor, _, history = _make_executor(tmp_path, sandbox)
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})

    result = await executor.execute(req, _make_context("s1", "u1"))

    assert result.status is ToolResultStatus.ERROR
    assert "sandbox unavailable" in str(result.content)
    rows = history.list_recent(limit=10)
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_enabled_tools_intersect_with_registry_enabled(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    executor, sandbox_obj, _ = _make_executor(
        tmp_path, sandbox, enabled_callbacks=["web_search", "read_file"]
    )
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={"code": "print(1)", "enabled_tools": ["web_search", "nonexistent"]},
    )

    await executor.execute(req, _make_context("s1", "u1"))

    executed_req = sandbox_obj.last_request
    assert executed_req is not None
    assert set(executed_req.enabled_callback_tools) == {"web_search"}


@pytest.mark.asyncio
async def test_no_enabled_tools_uses_registry_default(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    executor, sandbox_obj, _ = _make_executor(
        tmp_path, sandbox, enabled_callbacks=["web_search", "read_file"]
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})

    await executor.execute(req, _make_context("s1", "u1"))

    executed_req = sandbox_obj.last_request
    assert executed_req is not None
    assert set(executed_req.enabled_callback_tools) == {"web_search", "read_file"}


@pytest.mark.asyncio
async def test_code_hash_computed_correctly(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    executor, _, history = _make_executor(tmp_path, sandbox)
    code = "print('hello world')"
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": code})

    await executor.execute(req, _make_context("s1", "u1"))

    expected_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    rows = history.list_recent(limit=10)
    assert rows[0].code_hash == expected_hash


@pytest.mark.asyncio
async def test_history_records_authorized_callback_tools(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    executor, _, history = _make_executor(
        tmp_path, sandbox, enabled_callbacks=["web_search", "read_file"]
    )
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={"code": "print(1)", "enabled_tools": ["web_search"]},
    )

    await executor.execute(req, _make_context("s1", "u1"))

    rows = history.list_recent(limit=10)
    assert rows[0].authorized_callback_tools == ["web_search"]
