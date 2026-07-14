from __future__ import annotations

import pytest

from app.domain.memory_policy import (
    MemoryAccessDecision,
    MemoryOperation,
    MemoryPolicy,
    MemoryPolicyRequest,
)
from app.domain.policy import ExecutionMode, PolicyOutcome


# ---------------------------------------------------------------------------
# S1: MemoryPolicy default decision table tests
# ---------------------------------------------------------------------------


def _request(
    operation: MemoryOperation,
    *,
    session_id: str = "s1",
    target_session_id: str | None = None,
    execution_mode: ExecutionMode = ExecutionMode.REALTIME,
    agent_context: str = "primary",
    provider_slot: str | None = None,
    enabled_slots: tuple[str, ...] = (),
    cross_session_read_enabled: bool = False,
    unattended_write_enabled: bool = False,
) -> MemoryPolicyRequest:
    return MemoryPolicyRequest(
        operation=operation,
        session_id=session_id,
        target_session_id=target_session_id or session_id,
        execution_mode=execution_mode,
        agent_context=agent_context,
        provider_slot=provider_slot,
        enabled_slots=enabled_slots,
        cross_session_read_enabled=cross_session_read_enabled,
        unattended_write_enabled=unattended_write_enabled,
    )


class TestCreateSession:
    def test_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(MemoryOperation.CREATE_SESSION),
        )
        assert decision.verdict is PolicyOutcome.ALLOW


class TestLockProfile:
    def test_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(MemoryOperation.LOCK_PROFILE),
        )
        assert decision.verdict is PolicyOutcome.ALLOW


class TestReadSession:
    def test_current_session_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(MemoryOperation.READ_SESSION, session_id="s1"),
        )
        assert decision.verdict is PolicyOutcome.ALLOW

    def test_cross_session_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_SESSION,
                session_id="s1",
                target_session_id="s2",
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY

    def test_cross_session_allow_when_enabled(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_SESSION,
                session_id="s1",
                target_session_id="s2",
                cross_session_read_enabled=True,
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW


class TestReadExternal:
    def test_explicit_enabled_slot_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_EXTERNAL,
                provider_slot="builtin",
                enabled_slots=("builtin", "mem0"),
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW
        assert "builtin" in decision.providers

    def test_slot_not_in_enabled_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_EXTERNAL,
                provider_slot="mem0",
                enabled_slots=("builtin",),
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY

    def test_no_slot_with_enabled_providers_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_EXTERNAL,
                enabled_slots=("builtin", "mem0"),
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW

    def test_no_slot_no_enabled_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_EXTERNAL,
                enabled_slots=(),
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY


class TestWriteMessage:
    def test_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(MemoryOperation.WRITE_MESSAGE),
        )
        assert decision.verdict is PolicyOutcome.ALLOW


class TestWriteSummary:
    def test_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(MemoryOperation.WRITE_SUMMARY),
        )
        assert decision.verdict is PolicyOutcome.ALLOW


class TestSyncExternal:
    def test_trusted_primary_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.SYNC_EXTERNAL,
                agent_context="primary",
                execution_mode=ExecutionMode.REALTIME,
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW

    def test_non_primary_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.SYNC_EXTERNAL,
                agent_context="unattended",
                execution_mode=ExecutionMode.REALTIME,
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY

    def test_unattended_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.SYNC_EXTERNAL,
                agent_context="primary",
                execution_mode=ExecutionMode.UNATTENDED,
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY

    def test_unattended_allow_when_enabled(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.SYNC_EXTERNAL,
                agent_context="primary",
                execution_mode=ExecutionMode.UNATTENDED,
                unattended_write_enabled=True,
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW

    def test_delegated_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.SYNC_EXTERNAL,
                agent_context="primary",
                execution_mode=ExecutionMode.DELEGATED,
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY


class TestToolWriteExternal:
    def test_realtime_allow(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.TOOL_WRITE_EXTERNAL,
                agent_context="primary",
                execution_mode=ExecutionMode.REALTIME,
                provider_slot="builtin",
                enabled_slots=("builtin",),
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW

    def test_unattended_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.TOOL_WRITE_EXTERNAL,
                execution_mode=ExecutionMode.UNATTENDED,
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY

    def test_delegated_deny(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.TOOL_WRITE_EXTERNAL,
                execution_mode=ExecutionMode.DELEGATED,
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY

    def test_unattended_allow_when_enabled(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.TOOL_WRITE_EXTERNAL,
                execution_mode=ExecutionMode.UNATTENDED,
                unattended_write_enabled=True,
                provider_slot="builtin",
                enabled_slots=("builtin",),
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW


class TestDecisionShape:
    def test_deny_decision_has_reason(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_SESSION,
                target_session_id="other",
            ),
        )
        assert decision.verdict is PolicyOutcome.DENY
        assert decision.reason

    def test_allow_external_returns_providers(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.READ_EXTERNAL,
                provider_slot="mem0",
                enabled_slots=("mem0",),
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW
        assert "mem0" in decision.providers

    def test_sync_allow_returns_sync_mode(self):
        decision = MemoryPolicy().evaluate(
            _request(
                MemoryOperation.SYNC_EXTERNAL,
                agent_context="primary",
            ),
        )
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.sync_mode is not None
