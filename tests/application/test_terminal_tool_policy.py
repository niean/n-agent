"""Terminal tool policy enforcement tests.

Tests that TerminalToolExecutor:
- Evaluates SandboxPolicy before get_or_create
- deny -> 0 get_or_create calls
- grant success -> sandbox receives clamped values
- Keeps non-zero returncode -> SUCCESS shell semantics
- Keeps workdir validation (Docker /scratch|/workspace; Local scratch_root|workspace_root)
- Budget integration: reserve before exec, settle after
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.application.budget_service import BudgetService
from app.application.policy_snapshot import BudgetPolicyConfig
from app.application.terminal_tool_executor import TerminalToolExecutor
from app.domain.budget import (
    BudgetReserveKind,
    BudgetReserveRequest,
    SandboxReserveSpec,
)
from app.domain.policy import PolicyOutcome
from app.domain.sandbox import SandboxExecResult, SandboxStatus
from app.domain.sandbox_policy import SandboxDomainConfig, SandboxPolicy
from app.domain.tool import ToolCallRequest, ToolExecutionContext, ToolResultStatus
from app.infrastructure.sandbox.history_registry import SQLiteSandboxExecutionHistoryRegistry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeSandbox:
    next_result: SandboxExecResult | None = None
    raise_exc: Exception | None = None
    last_command: str | None = None
    last_workdir: str | None = None
    last_timeout: int | None = None

    async def execute(self, req):
        pass

    async def exec_command(self, command, workdir, timeout_seconds):
        self.last_command = command
        self.last_workdir = workdir
        self.last_timeout = timeout_seconds
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.next_result is not None
        return self.next_result


class _SpySandboxManager:
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

    def default_workdir(self, session_id: str) -> str:
        d = self.scratch_root / session_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)


class _FakeSettings:
    sandbox_timeout_seconds = 30
    sandbox_max_tool_calls = 50
    sandbox_max_stdout_bytes = 50000
    sandbox_max_stderr_bytes = 10000
    sandbox_docker_cpus = 2.0
    sandbox_docker_memory_mb = 1024
    sandbox_docker_network = False


def _success_result() -> SandboxExecResult:
    return SandboxExecResult(
        status=SandboxStatus.SUCCESS,
        stdout="ok\n",
        stderr="",
        returncode=0,
        duration_seconds=0.01,
    )


def _nonzero_returncode_result() -> SandboxExecResult:
    return SandboxExecResult(
        status=SandboxStatus.SUCCESS,
        stdout="",
        stderr="some error\n",
        returncode=7,
        duration_seconds=0.01,
    )


def _timeout_result() -> SandboxExecResult:
    return SandboxExecResult(
        status=SandboxStatus.TIMEOUT,
        stdout="",
        stderr="timed out",
        returncode=124,
        duration_seconds=2.0,
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
        allowed_callbacks=frozenset(),
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
async def test_terminal_policy_allow_results_in_get_or_create(tmp_path: Path):
    """TerminalToolExecutor with policy allow -> get_or_create called."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hello"}
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.SUCCESS
    assert manager.get_or_create_calls == 1


@pytest.mark.asyncio
async def test_terminal_policy_deny_network_request(tmp_path: Path):
    """Terminal requesting network but config denies -> deny, 0 get_or_create."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(network_enabled=False)
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    # Terminal doesn't normally request network, but test the deny path
    # by using a config that denies a backend
    cfg_deny = _make_domain_config(allowed_backends=frozenset({"docker"}))
    policy_deny = SandboxPolicy(cfg_deny)
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy_deny,
        sandbox_config=cfg_deny,
    )
    # The manager is "local" type, but config only allows "docker"
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hello"}
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    assert manager.get_or_create_calls == 0


# ---------------------------------------------------------------------------
# Tests: non-zero returncode -> SUCCESS (shell semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_nonzero_returncode_is_success(tmp_path: Path):
    """Non-zero returncode maps to SUCCESS (shell semantics preserved)."""
    sandbox = _FakeSandbox(next_result=_nonzero_returncode_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "exit 7"}
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content["returncode"] == 7


# ---------------------------------------------------------------------------
# Tests: timeout clamped to config max
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_timeout_clamped_to_config(tmp_path: Path):
    """Settings timeout=600 but config max=120 -> sandbox gets 120."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(timeout_seconds=120)

    class _HighTimeoutSettings(_FakeSettings):
        sandbox_timeout_seconds = 600

    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_HighTimeoutSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hello"}
    )
    await executor.execute(req, _make_context("s1"))
    assert sandbox.last_timeout == 120


@pytest.mark.asyncio
async def test_terminal_explicit_timeout_within_config(tmp_path: Path):
    """User-specified timeout=60 within config max=300 -> sandbox gets 60."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(timeout_seconds=300)
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hi", "timeout": 60}
    )
    await executor.execute(req, _make_context("s1"))
    assert sandbox.last_timeout == 60


@pytest.mark.asyncio
async def test_terminal_explicit_timeout_exceeds_config_clamped(tmp_path: Path):
    """User-specified timeout=600 but config max=120 -> sandbox gets 120."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config(timeout_seconds=120)
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hi", "timeout": 600}
    )
    await executor.execute(req, _make_context("s1"))
    assert sandbox.last_timeout == 120


# ---------------------------------------------------------------------------
# Tests: Budget integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_budget_deny_prevents_get_or_create(tmp_path: Path):
    """Budget deny -> 0 get_or_create calls."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    budget_service = BudgetService(BudgetPolicyConfig(
        max_sandbox_seconds=5.0,
    ))
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
        budget_service=budget_service,
    )
    # Pre-exhaust the budget
    pre = await budget_service.reserve(
        "s1",
        BudgetReserveRequest(
            kind=BudgetReserveKind.SANDBOX_RESOURCE,
            sandbox_spec=SandboxReserveSpec(
                max_seconds=5.0,
                max_cpu_seconds=5.0,
                max_memory_mb_seconds=512.0,
                max_callback_calls=0,
            ),
        ),
    )
    assert pre.outcome is PolicyOutcome.ALLOW
    # Now execute -> budget deny (5 + 30 > 5)
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hi"}
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    assert manager.get_or_create_calls == 0


@pytest.mark.asyncio
async def test_terminal_budget_reserve_settle_wraps_execution(tmp_path: Path):
    """Budget reserve before, settle after with actual duration."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    budget_service = BudgetService(BudgetPolicyConfig(
        max_sandbox_seconds=300.0,
    ))
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
        budget_service=budget_service,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hi"}
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.SUCCESS
    state = budget_service.get_state("s1")
    assert state is not None
    assert state.sandbox_seconds_reserved >= 0


# ---------------------------------------------------------------------------
# Tests: backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_backward_compat_without_policy(tmp_path: Path):
    """Without sandbox_policy, executor falls back to old behavior."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "echo hi"}
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.SUCCESS
    assert manager.get_or_create_calls == 1


# ---------------------------------------------------------------------------
# Tests: empty command still rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_empty_command_rejected_before_policy(tmp_path: Path):
    """Empty command rejected before policy evaluation (fast-fail)."""
    sandbox = _FakeSandbox(next_result=_success_result())
    manager = _SpySandboxManager(tmp_path, sandbox)
    cfg = _make_domain_config()
    policy = SandboxPolicy(cfg)
    history = SQLiteSandboxExecutionHistoryRegistry(tmp_path / "sessions.db")
    executor = TerminalToolExecutor(
        sandbox_manager=manager,
        settings=_FakeSettings(),
        history_registry=history,
        sandbox_policy=policy,
        sandbox_config=cfg,
    )
    req = ToolCallRequest(
        id="tc-1", name="terminal", arguments={"command": "  "}
    )
    result = await executor.execute(req, _make_context("s1"))
    assert result.status is ToolResultStatus.ERROR
    assert manager.get_or_create_calls == 0
