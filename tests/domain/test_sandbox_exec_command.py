from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields as dataclass_fields

import pytest

from app.domain.sandbox import Sandbox, SandboxExecResult, SandboxStatus


def test_sandbox_exec_result_is_frozen_dataclass():
    result = SandboxExecResult(
        status=SandboxStatus.SUCCESS,
        stdout="hello",
        stderr="",
        returncode=0,
        duration_seconds=0.123,
    )
    with pytest.raises(FrozenInstanceError):
        result.stdout = "mutated"  # type: ignore[misc]


def test_sandbox_exec_result_fields():
    field_names = {f.name for f in dataclass_fields(SandboxExecResult)}
    assert field_names == {
        "status",
        "stdout",
        "stderr",
        "returncode",
        "duration_seconds",
    }


def test_sandbox_exec_result_status_types():
    result = SandboxExecResult(
        status=SandboxStatus.SUCCESS,
        stdout="",
        stderr="",
        returncode=0,
        duration_seconds=0.0,
    )
    assert isinstance(result.status, SandboxStatus)
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)
    assert isinstance(result.returncode, int)
    assert isinstance(result.duration_seconds, float)


def test_sandbox_exec_result_nonzero_returncode_with_success():
    # Shell semantics: non-zero exit is still a successful command execution,
    # just a failed command. The caller distinguishes via returncode, not status.
    result = SandboxExecResult(
        status=SandboxStatus.SUCCESS,
        stdout="",
        stderr="command not found",
        returncode=127,
        duration_seconds=0.05,
    )
    assert result.status is SandboxStatus.SUCCESS
    assert result.returncode != 0


def test_sandbox_protocol_declares_exec_command():
    assert hasattr(Sandbox, "exec_command")
    sig = inspect.signature(Sandbox.exec_command)
    params = list(sig.parameters)
    assert params == ["self", "command", "workdir", "timeout_seconds"]
    hints = inspect.get_annotations(Sandbox.exec_command, eval_str=True)
    assert hints["command"] is str
    assert hints["workdir"] is str
    assert hints["timeout_seconds"] is int
    assert hints["return"] is SandboxExecResult


def test_sandbox_exec_command_is_async():
    assert inspect.iscoroutinefunction(Sandbox.exec_command)


def test_sandbox_protocol_still_declares_execute():
    # Existing execute method must remain unchanged.
    assert hasattr(Sandbox, "execute")
    sig = inspect.signature(Sandbox.execute)
    params = list(sig.parameters)
    assert params == ["self", "request"]


class _StubSandbox:
    async def execute(self, request):
        ...

    async def exec_command(
        self, command: str, workdir: str, timeout_seconds: int
    ) -> SandboxExecResult:
        return SandboxExecResult(
            status=SandboxStatus.SUCCESS,
            stdout=command,
            stderr="",
            returncode=0,
            duration_seconds=0.001,
        )


def test_stub_sandbox_implements_exec_command():
    stub = _StubSandbox()
    assert callable(stub.exec_command)
