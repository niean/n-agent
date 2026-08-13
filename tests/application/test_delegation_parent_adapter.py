"""Tests for delegation parent adapters (T12).

RealtimeDelegationAdapter signs a per-run, non-forgeable capability;
TaskDelegationAdapter gates delegate_agents grant on three conditions and
cancels scope delegations on task cancel.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.delegation_parent_adapter import (
    RealtimeDelegationAdapter,
    TaskDelegationAdapter,
    DelegationCapability,
)
from app.domain.delegation import Delegation, DelegationParentRef, DelegationStatus


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeRegistry:
    """In-memory registry fake for scope-list + cancel."""
    delegations: list[Delegation] = field(default_factory=list)
    cancel_calls: list[tuple[str, str]] = field(default_factory=list)

    async def list_for_trusted_scope(self, scope_id: str, limit: int = 100):
        return tuple(d for d in self.delegations if d.parent.scope_id == scope_id)

    async def request_cancel(self, delegation_id: str, reason: str):
        self.cancel_calls.append((delegation_id, reason))


@dataclass
class FakeRunService:
    cancel_calls: list[tuple[str, str]] = field(default_factory=list)

    async def request_cancel(self, delegation_id: str, reason: str):
        self.cancel_calls.append((delegation_id, reason))


def _delegation(did: str, scope_id: str, status: DelegationStatus) -> Delegation:
    return Delegation(
        id=did,
        parent=DelegationParentRef(
            source="task", scope_id=scope_id, run_id="r1", session_id="s1"
        ),
        delegation_key="k1",
        fingerprint="fp1",
        join_policy="all_completed",
        aggregation="parent",
        status=status,
    )


# ---------------------------------------------------------------------------
# RealtimeDelegationAdapter: capability signing
# ---------------------------------------------------------------------------


def test_realtime_capability_bound_to_run():
    adapter = RealtimeDelegationAdapter()
    cap = adapter.sign_capability(
        run_id="r1", session_id="s1", scope_id="s1",
        actor_id="user-1",
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
    )
    d = cap.to_dict()
    assert d["has_capability"] is True
    assert d["run_id"] == "r1"
    assert d["session_id"] == "s1"
    assert d["scope_id"] == "s1"
    assert d["source"] == "realtime"


def test_realtime_capability_non_serializable():
    """The capability must not survive JSON round-trip (non-forgeable)."""
    adapter = RealtimeDelegationAdapter()
    cap = adapter.sign_capability(
        run_id="r1", session_id="s1", scope_id="s1",
        parent_allowed_tools=frozenset(), system_child_allowlist=frozenset(),
    )
    d = cap.to_dict()
    # The server marker is a non-serializable object -> json.dumps fails.
    with pytest.raises((TypeError, ValueError)):
        json.dumps(d)


def test_realtime_capability_rejects_forged_dict():
    """A dict without the server marker is not a valid capability."""
    forged = {
        "has_capability": True, "run_id": "r1", "session_id": "s1",
        "source": "realtime", "scope_id": "s1",
    }
    assert not DelegationCapability.is_valid(forged)


def test_realtime_capability_valid_only_within_process():
    adapter = RealtimeDelegationAdapter()
    cap = adapter.sign_capability(
        run_id="r1", session_id="s1", scope_id="s1",
        parent_allowed_tools=frozenset(), system_child_allowlist=frozenset(),
    )
    assert DelegationCapability.is_valid(cap.to_dict())
    # A re-deserialized dict (marker becomes a plain string) is invalid.
    deserialized = {
        **cap.to_dict(),
        "__server_signed__": "forged-string",
    }
    assert not DelegationCapability.is_valid(deserialized)


# ---------------------------------------------------------------------------
# RealtimeDelegationAdapter: disconnect cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_realtime_disconnect_cascades_cancel():
    registry = FakeRegistry(delegations=[
        _delegation("d1", "s1", DelegationStatus.RUNNING),
        _delegation("d2", "s1", DelegationStatus.JOINING),
        _delegation("d3", "s1", DelegationStatus.SUCCEEDED),  # terminal, skip
    ])
    run_svc = FakeRunService()
    adapter = RealtimeDelegationAdapter()
    await adapter.on_disconnect(scope_id="s1", registry=registry, run_service=run_svc)
    # Non-terminal delegations cancelled; terminal one skipped.
    cancelled_ids = {did for did, _ in run_svc.cancel_calls}
    assert "d1" in cancelled_ids
    assert "d2" in cancelled_ids
    assert "d3" not in cancelled_ids


# ---------------------------------------------------------------------------
# TaskDelegationAdapter: should_grant truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "global_enabled,task_allows,in_grants,expected",
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (False, False, False, False),
    ],
)
def test_task_should_grant_truth_table(global_enabled, task_allows, in_grants, expected):
    result = TaskDelegationAdapter.should_grant(
        global_enabled=global_enabled,
        task_policy_allows=task_allows,
        delegate_in_grants=in_grants,
    )
    assert result is expected


def test_task_grant_adds_delegate_agents():
    adapter = TaskDelegationAdapter()
    granted = ["execute_code", "get_current_time"]
    result = adapter.grant_delegate_tool(granted, allow=True)
    assert "delegate_agents" in result
    # Idempotent.
    result2 = adapter.grant_delegate_tool(result, allow=True)
    assert result2.count("delegate_agents") == 1


def test_task_grant_strips_delegate_when_not_allowed():
    adapter = TaskDelegationAdapter()
    granted = ["execute_code", "delegate_agents"]
    result = adapter.grant_delegate_tool(granted, allow=False)
    assert "delegate_agents" not in result


# ---------------------------------------------------------------------------
# TaskDelegationAdapter: on_task_cancel cancels scope delegations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_cancel_cancels_scope_delegations():
    registry = FakeRegistry(delegations=[
        _delegation("d1", "task-1", DelegationStatus.RUNNING),
        _delegation("d2", "task-1", DelegationStatus.CANCELLING),
        _delegation("d3", "task-1", DelegationStatus.FAILED),  # terminal, skip
        _delegation("d4", "task-2", DelegationStatus.RUNNING),  # other scope, skip
    ])
    run_svc = FakeRunService()
    adapter = TaskDelegationAdapter()
    await adapter.on_task_cancel(
        scope_id="task-1", reason="user_cancel",
        registry=registry, run_service=run_svc,
    )
    cancelled_ids = {did for did, _ in run_svc.cancel_calls}
    assert cancelled_ids == {"d1", "d2"}


# ---------------------------------------------------------------------------
# TaskDelegationAdapter: heartbeat during join (no new state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_during_join_no_new_state():
    """Heartbeat during delegation join uses existing cadence and does not
    introduce a WAITING_CHILDREN state."""
    @dataclass
    class FakeTaskRegistry:
        heartbeats: list[int] = field(default_factory=list)
        async def heartbeat(self, task_id, task_run_id):
            self.heartbeats.append(task_run_id)

    task_registry = FakeTaskRegistry()
    adapter = TaskDelegationAdapter()
    await adapter.heartbeat_during_join(
        task_registry=task_registry, task_id="task-1", task_run_id=42,
    )
    assert task_registry.heartbeats == [42]
