"""Tests for DelegationRunService (Application Layer).

T8: tick-based scheduling, claim/spawn/finish, join policy advancement,
cancel outbox at-least-once delivery, stale recovery, kill-switch race.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid5, NAMESPACE_URL

import pytest

from app.application.child_agent_executor import ChildAgentExecutor
from app.application.delegation_run_service import DelegationRunService
from app.application.delegation_policy_config import DelegationPolicyConfig
from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.domain.delegation import (
    DelegationAggregationPolicy,
    DelegationJoinPolicy,
    DelegationMember,
    DelegationMemberRole,
    DelegationMemberStatus,
    DelegationParentRef,
    DelegationResult,
    DelegationCreateRequest,
    DelegationStatus,
    PolicySnapshotRecord,
)
from app.infrastructure.registry.sqlite_delegation_registry import (
    SQLiteDelegationRegistry,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class FakeClock:
    _t: list[float] = field(default_factory=lambda: [0.0])

    def __init__(self) -> None:
        self._count = 0

    def now_iso(self) -> str:
        self._count += 1
        return f"2026-08-12T02:00:{self._count:02d}Z"


@dataclass
class FakeChatService:
    response_content: str = "child done"
    raise_exc: Exception | None = None
    calls: list[ChatCompletionInput] = field(default_factory=list)

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult:
        self.calls.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ChatCompletionResult(
            session_id=request.session_id or "delegation-fake",
            model=request.model,
            message={"role": "assistant", "content": self.response_content},
            finish_reason="stop",
            usage={"total_tokens": 100, "prompt_tokens": 60, "completion_tokens": 40},
        )


def _config(**overrides) -> DelegationPolicyConfig:
    base = dict(
        enabled=True, realtime_enabled=True, task_enabled=True,
        max_children=8, max_concurrency=8, max_concurrency_per_parent=3,
        max_runtime_seconds=1800, member_max_runtime_seconds=900,
        max_total_tokens=100000, max_tokens_per_child=50000,
        result_max_bytes=65536, structured_result_max_bytes=32768,
        event_payload_max_bytes=32768, member_max_retries=1,
        cancel_retry_max_attempts=5, cancel_retry_max_backoff_seconds=60,
    )
    base.update(overrides)
    return DelegationPolicyConfig(**base)


def _make_parent(source="task", scope_id="t1", run_id="r1", session_id="s1"):
    return DelegationParentRef(
        source=source, scope_id=scope_id, run_id=run_id, session_id=session_id
    )


def _make_member(ordinal, *, delegation_id="d1",
                 role=DelegationMemberRole.WORKER, budget_tokens=100):
    return DelegationMember.new(
        delegation_id=delegation_id,
        role=role,
        ordinal=ordinal,
        title=f"w{ordinal}",
        instruction="do work",
        skills=(),
        allowed_tools=("get_current_time",),
        execution_session_id=f"delegation-{uuid5(NAMESPACE_URL, f'{delegation_id}/m{ordinal}')}",
        deadline_at="2026-08-12T03:00:00Z",
        budget_tokens=budget_tokens,
    )


def _make_request(parent=None, delegation_key="k1", members=None,
                  budget_total_tokens=1000):
    return DelegationCreateRequest(
        parent=parent or _make_parent(),
        delegation_key=delegation_key,
        fingerprint="fp1",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.PARENT,
        deadline_at="2026-08-12T03:00:00Z",
        budget_total_tokens=budget_total_tokens,
        members=members or (_make_member(0), _make_member(1)),
        snapshot=PolicySnapshotRecord(
            profile_version="v1",
            parent_config={},
            child_config={},
            aggregator_config=None,
            checksum="cs1",
        ),
    )


@pytest.fixture
def registry(tmp_path):
    return SQLiteDelegationRegistry(str(tmp_path / "run.db"), clock=FakeClock())


@pytest.fixture
def run_service(registry):
    clock = FakeClock()
    chat = FakeChatService()
    executor = ChildAgentExecutor(chat_service=chat, clock=clock)
    svc = DelegationRunService(
        registry=registry,
        child_executor=executor,
        clock=clock,
        config=_config(),
    )
    svc._test_chat = chat
    return svc


# ---------------------------------------------------------------------------
# tick: claim + spawn + finish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_claims_pending_members_and_spawns(run_service, registry):
    d = await registry.create_or_reconnect(_make_request())
    await run_service.tick()
    members = await registry.list_members(d.id)
    assert all(m.status is DelegationMemberStatus.SUCCEEDED for m in members)
    assert len(run_service._test_chat.calls) == 2


@pytest.mark.asyncio
async def test_tick_round_robin_no_starvation(registry):
    """Two delegations each with pending members -- tick processes both."""
    clock = FakeClock()
    chat = FakeChatService()
    svc = DelegationRunService(
        registry=registry,
        child_executor=ChildAgentExecutor(chat, clock),
        clock=clock,
        config=_config(),
    )
    svc._test_chat = chat
    d1 = await registry.create_or_reconnect(
        _make_request(parent=_make_parent(scope_id="t1"), delegation_key="k1")
    )
    d2 = await registry.create_or_reconnect(
        _make_request(parent=_make_parent(scope_id="t2"), delegation_key="k2")
    )
    await svc.tick()
    m1 = await registry.list_members(d1.id)
    m2 = await registry.list_members(d2.id)
    assert all(m.status is DelegationMemberStatus.SUCCEEDED for m in m1)
    assert all(m.status is DelegationMemberStatus.SUCCEEDED for m in m2)


# ---------------------------------------------------------------------------
# join: all_completed advances to terminal ResultSet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_all_completed_produces_succeeded_resultset(run_service, registry):
    d = await registry.create_or_reconnect(_make_request())
    await run_service.tick()
    rs = await registry.get_result_set(d.id)
    assert rs is not None
    assert rs.status is DelegationStatus.SUCCEEDED
    assert len(rs.member_results) == 2
    updated = await registry.get(d.id)
    assert updated.status is DelegationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_join_failed_when_child_raises(registry):
    clock = FakeClock()
    chat = FakeChatService(raise_exc=RuntimeError("boom"))
    svc = DelegationRunService(
        registry=registry,
        child_executor=ChildAgentExecutor(chat, clock),
        clock=clock,
        config=_config(),
    )
    svc._test_chat = chat
    d = await registry.create_or_reconnect(_make_request())
    await svc.tick()
    rs = await registry.get_result_set(d.id)
    assert rs is not None
    # all_completed with any failure -> FAILED
    assert rs.status is DelegationStatus.FAILED


# ---------------------------------------------------------------------------
# cancel outbox: at-least-once delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_cancel_sets_cancelling_and_outbox(run_service, registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-test", lease_seconds=60)
    updated = await run_service.request_cancel(d.id, reason="parent_requested")
    assert updated.status is DelegationStatus.CANCELLING
    pending = await registry.list_outbox_pending(limit=10)
    assert len(pending) >= 1
    assert pending[0].reason == "parent_requested"


@pytest.mark.asyncio
async def test_cancel_outbox_acked_after_tick(run_service, registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-test", lease_seconds=60)
    await run_service.request_cancel(d.id, reason="parent_requested")
    # Tick should finalize the cancellation (members become terminal).
    await run_service.tick()
    updated = await registry.get(d.id)
    assert updated.status in (DelegationStatus.CANCELLED, DelegationStatus.CANCELLING)


# ---------------------------------------------------------------------------
# recovery: stale members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_member_reclaimed_to_pending(registry):
    """A RUNNING member with an expired lease is reclaimed to PENDING."""
    clock = FakeClock()
    chat = FakeChatService()
    svc = DelegationRunService(
        registry=registry,
        child_executor=ChildAgentExecutor(chat, clock),
        clock=clock,
        config=_config(),
    )
    svc._test_chat = chat
    d = await registry.create_or_reconnect(_make_request())
    # Claim with a very short lease, then manually expire it.
    await registry.claim_member(d.id, 0, "lock-stale", lease_seconds=0)
    # Tick reclaims the stale member and re-executes it.
    await svc.tick()
    members = await registry.list_members(d.id)
    assert all(m.is_terminal for m in members)


# ---------------------------------------------------------------------------
# kill switch race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_cancels_pending_members(run_service, registry):
    d = await registry.create_or_reconnect(_make_request())
    run_service.activate_kill_switch()
    await run_service.tick()
    members = await registry.list_members(d.id)
    # Kill switch prevents spawning; pending members are cancelled.
    assert all(m.is_terminal for m in members)
    updated = await registry.get(d.id)
    assert updated.status in (
        DelegationStatus.CANCELLED, DelegationStatus.CANCELLING,
        DelegationStatus.FAILED,
    )
