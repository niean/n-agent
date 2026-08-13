"""Tests for DelegationService (Application Layer).

T6+T9: delegate orchestration, idempotent replay, policy deny, budget
reserve/release on failure, information-flow filtering on result set.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid5, NAMESPACE_URL

import pytest

from app.application.delegation_service import (
    DelegationService,
    DelegationRequestParser,
    DelegationError,
)
from app.application.delegation_policy_config import DelegationPolicyConfig
from app.application.delegation_run_service import DelegationRunService
from app.application.child_agent_executor import ChildAgentExecutor
from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.application.information_flow_service import InformationFlowService
from app.application.policy_snapshot import InformationFlowPolicyConfig
from app.domain.delegation import (
    DelegationAggregationPolicy,
    DelegationJoinPolicy,
    DelegationMember,
    DelegationMemberRole,
    DelegationResultSet,
    DelegationStatus,
)
from app.domain.delegation_policy import DelegationPolicy
from app.domain.information_flow import ReleaseTarget, SecretCatalog
from app.domain.policy import ExecutionMode
from app.infrastructure.registry.sqlite_delegation_registry import (
    SQLiteDelegationRegistry,
)


class FakeClock:
    def __init__(self) -> None:
        self._n = 0

    def now_iso(self) -> str:
        self._n += 1
        return f"2026-08-12T02:00:{self._n:02d}Z"


@dataclass
class FakeChatService:
    response_content: str = "child done"
    calls: list[ChatCompletionInput] = field(default_factory=list)

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult:
        self.calls.append(request)
        return ChatCompletionResult(
            session_id=request.session_id or "delegation-fake",
            model=request.model,
            message={"role": "assistant", "content": self.response_content},
            finish_reason="stop",
            usage={"total_tokens": 100},
        )


def _config(**ov) -> DelegationPolicyConfig:
    base = dict(
        enabled=True, realtime_enabled=True, task_enabled=True,
        max_children=8, max_concurrency=8, max_concurrency_per_parent=3,
        max_runtime_seconds=1800, member_max_runtime_seconds=900,
        max_total_tokens=100000, max_tokens_per_child=50000,
        result_max_bytes=65536, structured_result_max_bytes=32768,
        event_payload_max_bytes=32768, member_max_retries=1,
        cancel_retry_max_attempts=5, cancel_retry_max_backoff_seconds=60,
    )
    base.update(ov)
    return DelegationPolicyConfig(**base)


def _parent_capability(source="task", scope_id="t1", run_id="r1",
                       session_id="s1", actor_id="user-1"):
    return {
        "source": source, "scope_id": scope_id, "run_id": run_id,
        "session_id": session_id, "actor_id": actor_id,
        "has_capability": True,
        "classification": "internal",
        "parent_allowed_tools": frozenset({"get_current_time"}),
        "system_child_allowlist": frozenset({"get_current_time"}),
    }


def _child_spec(title="w", instruction="do work", budget_tokens=100):
    from app.domain.delegation import DelegationChildSpec
    return DelegationChildSpec(
        title=title, instruction=instruction, skills=(), allowed_tools=("get_current_time",),
        model_override=None, max_runtime_seconds=300, budget_tokens=budget_tokens,
        output_schema=None,
    )


@pytest.fixture
def setup(tmp_path):
    clock = FakeClock()
    registry = SQLiteDelegationRegistry(str(tmp_path / "svc.db"), clock=clock)
    chat = FakeChatService()
    executor = ChildAgentExecutor(chat_service=chat, clock=clock)
    run_svc = DelegationRunService(
        registry=registry, child_executor=executor, clock=clock, config=_config(),
    )
    info_flow = InformationFlowService(InformationFlowPolicyConfig(), SecretCatalog())
    svc = DelegationService(
        registry=registry, run_service=run_svc,
        policy=DelegationPolicy(), info_flow=info_flow,
        clock=clock, config=_config(),
    )
    return svc, registry, run_svc, chat, clock


# ---------------------------------------------------------------------------
# delegate: create + run + return result set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_creates_and_returns_resultset(setup):
    svc, registry, run_svc, chat, clock = setup
    result = await svc.delegate(
        parent_capability=_parent_capability(),
        delegation_key="k1",
        children=[_child_spec("w0"), _child_spec("w1")],
        join_policy="all_completed",
        aggregation="parent",
        timeout_seconds=60,
    )
    assert isinstance(result, DelegationResultSet)
    assert result.status is DelegationStatus.SUCCEEDED
    assert len(result.member_results) == 2


@pytest.mark.asyncio
async def test_delegate_idempotent_replay_returns_same_delegation(setup):
    svc, registry, run_svc, chat, clock = setup
    r1 = await svc.delegate(
        parent_capability=_parent_capability(),
        delegation_key="k1",
        children=[_child_spec("w0"), _child_spec("w1")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    r2 = await svc.delegate(
        parent_capability=_parent_capability(),
        delegation_key="k1",
        children=[_child_spec("w0"), _child_spec("w1")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    # Same fingerprint -> same delegation, same outcome.
    assert r1.delegation_id == r2.delegation_id


@pytest.mark.asyncio
async def test_delegate_denied_when_not_authorized(setup):
    svc, registry, run_svc, chat, clock = setup
    cap = _parent_capability()
    cap["has_capability"] = False  # no capability
    with pytest.raises(DelegationError) as exc:
        await svc.delegate(
            parent_capability=cap, delegation_key="k1",
            children=[_child_spec()], join_policy="all_completed",
            aggregation="parent", timeout_seconds=60,
        )
    assert exc.value.code == "delegation_not_authorized"


@pytest.mark.asyncio
async def test_delegate_denied_when_feature_disabled(setup):
    svc, registry, run_svc, chat, clock = setup
    # Override config to disable task delegation.
    svc._config = _config(task_enabled=False)
    with pytest.raises(DelegationError):
        await svc.delegate(
            parent_capability=_parent_capability(source="task"),
            delegation_key="k1", children=[_child_spec()],
            join_policy="all_completed", aggregation="parent", timeout_seconds=60,
        )


@pytest.mark.asyncio
async def test_delegate_releases_budget_on_policy_deny(setup):
    """If the policy denies, no delegation is created."""
    svc, registry, run_svc, chat, clock = setup
    # Too many children -> policy deny.
    with pytest.raises(DelegationError):
        await svc.delegate(
            parent_capability=_parent_capability(),
            delegation_key="k1",
            children=[_child_spec(f"w{i}") for i in range(20)],  # exceeds max_children
            join_policy="all_completed", aggregation="parent", timeout_seconds=60,
        )
    # No delegation persisted.
    delegations = await registry.list_for_trusted_scope("t1")
    assert len(delegations) == 0


# ---------------------------------------------------------------------------
# request parser: normalization + fingerprint
# ---------------------------------------------------------------------------


def test_parser_normalizes_delegation_key():
    parser = DelegationRequestParser()
    assert parser.normalize_key("  K1 ") == "k1"


def test_parser_fingerprint_stable_for_same_input():
    parser = DelegationRequestParser()
    children = [_child_spec("w0"), _child_spec("w1")]
    fp1 = parser.fingerprint("k1", children, "all_completed", "parent", 60)
    fp2 = parser.fingerprint("k1", children, "all_completed", "parent", 60)
    assert fp1 == fp2


def test_parser_fingerprint_differs_for_different_input():
    parser = DelegationRequestParser()
    children = [_child_spec("w0"), _child_spec("w1")]
    fp1 = parser.fingerprint("k1", children, "all_completed", "parent", 60)
    fp2 = parser.fingerprint("k2", children, "all_completed", "parent", 60)
    assert fp1 != fp2


def test_parser_rejects_duplicate_child_titles():
    parser = DelegationRequestParser()
    children = [_child_spec("dup"), _child_spec("dup")]
    with pytest.raises(DelegationError):
        parser.normalize_children(children)


def test_parser_rejects_empty_instruction():
    parser = DelegationRequestParser()
    from app.domain.delegation import DelegationChildSpec
    children = [DelegationChildSpec(
        title="w", instruction="  ", skills=(), allowed_tools=(),
        model_override=None, max_runtime_seconds=300, budget_tokens=100,
        output_schema=None,
    )]
    with pytest.raises(DelegationError):
        parser.normalize_children(children)
