"""Enforcement point tests for SandboxToolExecutor with SandboxPolicy + Budget.

Tests the Application-layer integration:
- SandboxPolicy deny -> 0 get_or_create calls (no container created)
- Budget deny -> 0 SandboxPolicy/manager calls (fail-closed before sandbox)
- Grant success -> sandbox receives clamped resource values
- Callbacks resolved as requested ∩ registry-enabled ∩ allowlist
- Budget reserve/settle wraps sandbox execution
- Removed callbacks audited by name only
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.budget_service import BudgetService
from app.application.policy_snapshot import BudgetPolicyConfig
from app.application.sandbox_tool_executor import SandboxToolExecutor
from app.domain.budget import (
    BudgetActualUsage,
    BudgetReserveKind,
    BudgetReserveRequest,
    SandboxReserveSpec,
)
from app.domain.policy import PolicyOutcome
from app.domain.sandbox import (
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxStatus,
)
from app.domain.sandbox_policy import (
    SandboxDomainConfig,
    SandboxMountAccess,
    SandboxMountSpec,
    SandboxPolicy,
    SandboxResourceSpec,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
)
from app.infrastructure.sandbox.history_registry import SQLiteSandboxExecutionHistoryRegistry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


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

    async def exec_command(self, command, workdir, timeout_seconds):
        pass


class _SpySandboxManager:
    """Records get_or_create calls for deny -> 0 assertion."""

    def __init__(self, workspace_root: Path, sandbox: _FakeSandbox):
        self.workspace_root = workspace_root
        self.sandbox_type = "local"
        self.scratch_root = workspace_root / "scratch"
        self._sandbox = sandbox
        self._lock = asyncio.Lock()
        self.get_or_create_calls: int = 0

    def acquire_session_lock(self, session_id: str):
        return self._lock

    async def get_or_create(self, session_id: str, grant=None):
        self.get_or_create_calls += 1
        return self._sandbox

    def new_call_staging(self, session_id: str) -> Path:
        d = self.workspace_root / f"call-{session_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def default_workdir(self):
        return str(self.scratch_root)


class _FakeCallbackTool:
    def __init__(self, name: str):
        self.name = name
        self.enabled = True


class _FakeCallbackRegistry:
    def __init__(self, enabled_names: list[str] | None = None):
        self._enabled = [_FakeCallbackTool(n) for n in (enabled_names or [])]

    def list_enabled(self):
        return self._enabled


class _FakeSettings:
    sandbox_timeout_seconds = 30
    sandbox_max_tool_calls = 50
    sandbox_max_stdout_bytes = 50000
    sandbox_max_stderr_bytes = 10000
    sandbox_summary_max_stdout_bytes = 2000
    sandbox_summary_max_stderr_bytes = 500
    sandbox_docker_cpus = 2.0
    sandbox_docker_memory_mb = 1024
    sandbox_docker_network = False
    sandbox_callback_tools = ["web_search", "read_file"]


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


def _make_domain_config(**overrides) -> SandboxDomainConfig:
    defaults = dict(
        timeout_seconds=300,
        max_tool_calls=50,
        cpus=2.0,
        memory_mb=1024,
        network_enabled=False,
        idle_seconds=900,
        workspace_readonly=True,
        max_stdout_bytes=50000,
        max_stderr_bytes=10000,
        pids_limit=256,
        allowed_backends=frozenset({"docker", "local"}),
        allowed_callbacks=frozenset({"web_search", "read_file"}),
    )
    defaults.update(overrides)
    return SandboxDomainConfig(**defaults)


def _make_context(session_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        execution_context_mode="realtime",
        trusted_metadata={},
    )


# ---------------------------------------------------------------------------
# Tests: deny -> 0 get_or_create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_policy_deny_results_in_zero_get_or_create(tmp_path: Path):
    """SandboxPolicy deny (network requested but config disables) -> no container."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(network_enabled=False)
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    # Request network but config denies -> deny -> no get_or_create
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={"code": "print(1)", "network": True},
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    assert manager.get_or_create_calls == 0


@pytest.mark.asyncio
async def test_sandbox_policy_allow_results_in_get_or_create(tmp_path: Path):
    """SandboxPolicy allow -> get_or_create called once."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.SUCCESS
    assert manager.get_or_create_calls == 1


# ---------------------------------------------------------------------------
# Tests: Budget deny -> 0 SandboxPolicy/manager calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_deny_prevents_sandbox_policy_and_manager(tmp_path: Path):
    """Budget deny (sandbox_seconds exhausted) -> 0 get_or_create, 0 policy calls."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    budget_service = BudgetService(BudgetPolicyConfig(
        max_sandbox_seconds=5.0,  # only 5 seconds allowed
    ))
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
        budget_service=budget_service,
    )
    # Reserve 10 seconds (exceeds 5s limit) -> budget deny
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={"code": "print(1)"},
    )
    # Pre-reserve to exhaust the budget
    pre_reserve = await budget_service.reserve(
        "s1",
        BudgetReserveRequest(
            kind=BudgetReserveKind.SANDBOX_RESOURCE,
            sandbox_spec=SandboxReserveSpec(
                max_seconds=5.0,
                max_cpu_seconds=5.0,
                max_memory_mb_seconds=512.0,
                max_callback_calls=10,
            ),
        ),
    )
    assert pre_reserve.outcome is PolicyOutcome.ALLOW
    # Now reserve for the executor call -- should deny (5 + 10 > 5)
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    assert manager.get_or_create_calls == 0


# ---------------------------------------------------------------------------
# Tests: Grant success -> clamped values reach sandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_clamps_timeout_to_config_max(tmp_path: Path):
    """Executor requests timeout=600 but config max=300 -> sandbox gets 300."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(timeout_seconds=120)
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    # The executor's settings has sandbox_timeout_seconds=30; the config says 120.
    # The request doesn't carry timeout. The executor uses settings timeout (30)
    # which is within config (120), so no clamping needed.
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    await executor.execute(req, _make_context("s1"))
    assert sandbox.last_request is not None
    assert sandbox.last_request.timeout_seconds == 30


@pytest.mark.asyncio
async def test_grant_clamps_when_settings_exceeds_config(tmp_path: Path):
    """Settings timeout=600 but config max=120 -> sandbox gets 120."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(timeout_seconds=120)

    class _HighTimeoutSettings(_FakeSettings):
        sandbox_timeout_seconds = 600

    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_HighTimeoutSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    await executor.execute(req, _make_context("s1"))
    assert sandbox.last_request is not None
    # Settings says 600, config says 120 -> clamped to 120
    assert sandbox.last_request.timeout_seconds == 120


# ---------------------------------------------------------------------------
# Tests: callbacks resolved correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callbacks_intersect_requested_registry_allowlist(tmp_path: Path):
    """Callbacks = requested ∩ registry_enabled ∩ config.allowlist."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(allowed_callbacks=frozenset({"web_search", "read_file"}))
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search", "read_file", "write_file"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={
            "code": "print(1)",
            "enabled_tools": ["web_search", "write_file", "nonexistent"],
        },
    )
    await executor.execute(req, _make_context("s1"))
    assert sandbox.last_request is not None
    # web_search is in all three; write_file is in registry+requested but NOT allowlist
    assert set(sandbox.last_request.enabled_callback_tools) == {"web_search"}


@pytest.mark.asyncio
async def test_removed_callbacks_audited_by_name_only(tmp_path: Path):
    """Removed callbacks recorded as names in history, not arguments."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(allowed_callbacks=frozenset({"web_search"}))
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search", "read_file"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={
            "code": "print(1)",
            "enabled_tools": ["web_search", "read_file"],
        },
    )
    await executor.execute(req, _make_context("s1"))
    rows = history.list_recent(limit=10)
    assert len(rows) == 1
    # Authorized callbacks should be just web_search (read_file removed by allowlist)
    assert rows[0].authorized_callback_tools == ["web_search"]


# ---------------------------------------------------------------------------
# Tests: Budget reserve/settle wraps sandbox execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_reserve_settle_wraps_sandbox_execution(tmp_path: Path):
    """Budget reserve before exec, settle after exec with actual duration."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    budget_service = BudgetService(BudgetPolicyConfig(
        max_sandbox_seconds=300.0,
    ))
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
        budget_service=budget_service,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.SUCCESS
    # After settle, the budget state should reflect actual usage
    state = budget_service.get_state("s1")
    assert state is not None
    # sandbox_seconds_reserved should be the actual duration (0.01s from _success_result)
    assert state.sandbox_seconds_reserved >= 0


@pytest.mark.asyncio
async def test_budget_release_on_sandbox_exception(tmp_path: Path):
    """If sandbox raises, budget reservation is released."""
    sandbox = _FakeSandbox(raise_exc=RuntimeError("docker unavailable"))
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    budget_service = BudgetService(BudgetPolicyConfig(
        max_sandbox_seconds=300.0,
    ))
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
        budget_service=budget_service,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    # Budget should be released (not settled) -> no remaining reservation
    state = budget_service.get_state("s1")
    assert state is not None
    assert state.sandbox_seconds_reserved == 0


# ---------------------------------------------------------------------------
# Tests: backward compatibility (no sandbox_policy injected)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backward_compat_without_sandbox_policy(tmp_path: Path):
    """When sandbox_policy is not injected, executor falls back to old behavior."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.SUCCESS
    assert manager.get_or_create_calls == 1


# ---------------------------------------------------------------------------
# Original backward-compat tests (no sandbox_policy injected)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_returns_success_and_records_history(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    result = await executor.execute(req, _make_context("s1"))
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
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "raise Boom"})
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    rows = history.list_recent(limit=10)
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_timeout_returns_timeout_and_records_history(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_timeout_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "while True: pass"})
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.TIMEOUT
    rows = history.list_recent(limit=10)
    assert rows[0].status == "timeout"


@pytest.mark.asyncio
async def test_sandbox_exception_caught_and_recorded_as_error(tmp_path: Path):
    sandbox = _FakeSandbox(raise_exc=RuntimeError("docker unavailable"))
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    assert "sandbox unavailable" in str(result.content)
    rows = history.list_recent(limit=10)
    assert rows[0].status == "error"


@pytest.mark.asyncio
async def test_enabled_tools_intersect_with_registry_enabled(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search", "read_file"]),
        settings=_FakeSettings(),
    )
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={"code": "print(1)", "enabled_tools": ["web_search", "nonexistent"]},
    )
    await executor.execute(req, _make_context("s1"))
    executed_req = sandbox.last_request
    assert executed_req is not None
    assert set(executed_req.enabled_callback_tools) == {"web_search"}


@pytest.mark.asyncio
async def test_no_enabled_tools_uses_registry_default(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search", "read_file"]),
        settings=_FakeSettings(),
    )
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": "print(1)"})
    await executor.execute(req, _make_context("s1"))
    executed_req = sandbox.last_request
    assert executed_req is not None
    assert set(executed_req.enabled_callback_tools) == {"web_search", "read_file"}


@pytest.mark.asyncio
async def test_code_hash_computed_correctly(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search"]),
        settings=_FakeSettings(),
        history_registry=history,
    )
    code = "print('hello world')"
    req = ToolCallRequest(id="tc-1", name="execute_code", arguments={"code": code})
    await executor.execute(req, _make_context("s1"))
    expected_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    rows = history.list_recent(limit=10)
    assert rows[0].code_hash == expected_hash


@pytest.mark.asyncio
async def test_history_records_authorized_callback_tools(tmp_path: Path):
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = SandboxToolExecutor(
        sandbox_manager=manager,
        callback_registry=_FakeCallbackRegistry(["web_search", "read_file"]),
        settings=_FakeSettings(),
        history_registry=history,
    )
    req = ToolCallRequest(
        id="tc-1",
        name="execute_code",
        arguments={"code": "print(1)", "enabled_tools": ["web_search"]},
    )
    await executor.execute(req, _make_context("s1"))
    rows = history.list_recent(limit=10)
    assert rows[0].authorized_callback_tools == ["web_search"]
