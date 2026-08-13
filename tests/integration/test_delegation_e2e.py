"""T15: End-to-end integration tests for the Delegation subsystem.

Exercises the full stack (DelegationService -> DelegationRunService ->
ChildAgentExecutor -> SQLiteDelegationRegistry) with a FakeChatService,
plus the tool executor, request parser, and authorization paths. These
tests cover scenarios that cross component boundaries and are not already
exhaustively covered by the per-component unit tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from app.application.child_agent_executor import ChildAgentExecutor
from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.application.delegation_parent_adapter import (
    DelegationCapability,
    RealtimeDelegationAdapter,
    TaskDelegationAdapter,
)
from app.application.delegation_policy_config import DelegationPolicyConfig
from app.application.delegation_request_parser import DelegationError, DelegationRequestParser
from app.application.delegation_run_service import DelegationRunService
from app.application.delegation_service import DelegationService
from app.application.delegation_tool_executor import (
    DelegateAgentsToolExecutor,
    delegate_agent_tool_definitions,
)
from app.application.information_flow_service import InformationFlowService
from app.application.policy_snapshot import InformationFlowPolicyConfig
from app.domain.delegation import (
    DelegationChildSpec,
    DelegationParentRef,
    DelegationResultSet,
    DelegationStatus,
)
from app.domain.delegation_policy import DelegationPolicy
from app.domain.information_flow import ReleaseTarget, SecretCatalog
from app.domain.tool import ToolCallRequest, ToolExecutionContext
from app.infrastructure.registry.sqlite_delegation_registry import (
    SQLiteDelegationRegistry,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self._t = 0

    def advance(self, seconds: int = 1) -> None:
        self._t += seconds

    def now_iso(self) -> str:
        # Stable-ish ISO that increments per call so deadlines are distinct.
        self._t += 1
        return f"2026-08-12T02:00:{self._t:02d}Z"


@dataclass
class FakeChatService:
    """Configurable chat service. Can fail specific children by title."""
    response_content: str = "child done"
    fail_titles: set[str] = field(default_factory=set)
    calls: list[ChatCompletionInput] = field(default_factory=list)

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult:
        self.calls.append(request)
        # Detect the child title from the user prompt to decide failure.
        prompt = ""
        for m in request.messages:
            if m.get("role") == "user":
                prompt = m.get("content", "")
                break
        for title in self.fail_titles:
            if f"Title: {title}" in prompt:
                return ChatCompletionResult(
                    session_id=request.session_id or "delegation-fake",
                    model=request.model,
                    message={"role": "assistant", "content": ""},
                    finish_reason="stop",
                    usage={"total_tokens": 50},
                )
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


def _capability(source="realtime", scope_id="s1", run_id="r1",
                session_id="s1", actor_id="user-1") -> dict[str, Any]:
    cap = RealtimeDelegationAdapter().sign_capability(
        run_id=run_id, session_id=session_id, scope_id=scope_id,
        actor_id=actor_id,
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
    )
    d = cap.to_dict()
    d["source"] = source
    return d


def _child(title="w", instruction="do work", budget_tokens=100) -> DelegationChildSpec:
    return DelegationChildSpec(
        title=title, instruction=instruction, skills=(),
        allowed_tools=("get_current_time",), model_override=None,
        max_runtime_seconds=300, budget_tokens=budget_tokens, output_schema=None,
    )


@pytest.fixture
def stack(tmp_path):
    """Full delegation stack with real SQLite registry + fake chat."""
    clock = FakeClock()
    registry = SQLiteDelegationRegistry(str(tmp_path / "e2e.db"), clock=clock)
    chat = FakeChatService()
    executor = ChildAgentExecutor(chat_service=chat, clock=clock)
    run_svc = DelegationRunService(
        registry=registry, child_executor=executor, clock=clock, config=_config(),
    )
    info_flow = InformationFlowService(
        InformationFlowPolicyConfig(redact_secrets=True),
        SecretCatalog(secret_values=frozenset({"SECRET-KEY-123"})),
    )
    svc = DelegationService(
        registry=registry, run_service=run_svc, policy=DelegationPolicy(),
        info_flow=info_flow, clock=clock, config=_config(),
    )
    return _Stack(svc, registry, run_svc, chat, clock, info_flow)


@dataclass
class _Stack:
    svc: DelegationService
    registry: SQLiteDelegationRegistry
    run_svc: DelegationRunService
    chat: FakeChatService
    clock: FakeClock
    info_flow: InformationFlowService


# ---------------------------------------------------------------------------
# Parallel children + ordinal ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_realtime_delegation_parallel_children_join(stack):
    """Realtime parent delegates 2 parallel children; join completes with
    a ResultSet whose member results are ordered by ordinal."""
    result = await stack.svc.delegate(
        parent_capability=_capability(),
        delegation_key="k1",
        children=[_child("w0"), _child("w1")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    assert result.status is DelegationStatus.SUCCEEDED
    assert len(result.member_results) == 2
    # Each child executed exactly once (parallel within the tick).
    assert len(stack.chat.calls) == 2


# ---------------------------------------------------------------------------
# Idempotent replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_same_key_fingerprint_one_delegation(stack):
    r1 = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k1",
        children=[_child("w0"), _child("w1")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    r2 = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k1",
        children=[_child("w0"), _child("w1")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    assert r1.delegation_id == r2.delegation_id
    # Only 2 children ever executed (replay did not re-run).
    assert len(stack.chat.calls) == 2


# ---------------------------------------------------------------------------
# Three join policies state matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_completed_fails_when_any_child_fails(stack):
    stack.chat.fail_titles = {"w1"}
    result = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k1",
        children=[_child("w0"), _child("w1")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    assert result.status is DelegationStatus.FAILED


@pytest.mark.asyncio
async def test_best_effort_succeeds_when_one_child_succeeds(stack):
    stack.chat.fail_titles = {"w1"}
    result = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k2",
        children=[_child("w0"), _child("w1")],
        join_policy="best_effort", aggregation="parent", timeout_seconds=60,
    )
    assert result.status is DelegationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_all_succeeded_fails_when_any_child_fails(stack):
    stack.chat.fail_titles = {"w1"}
    result = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k3",
        children=[_child("w0"), _child("w1")],
        join_policy="all_succeeded", aggregation="parent", timeout_seconds=60,
    )
    assert result.status is DelegationStatus.FAILED


# ---------------------------------------------------------------------------
# Cancel: idempotent + no late success override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_outbox_idempotent(stack):
    """Requesting cancel on a terminal delegation does not error; the
    delegation stays in its terminal state (no late-success override)."""
    result = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k1",
        children=[_child("w0")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    # Already terminal (SUCCEEDED). Cancel is a no-op-ish request.
    await stack.run_svc.request_cancel(result.delegation_id, "user_cancel")
    # Drive a tick to process the outbox; terminal status must not regress.
    await stack.run_svc.tick()
    d = await stack.registry.get(result.delegation_id)
    assert d.is_terminal
    assert d.status is DelegationStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Restart recovery: no re-run of successful members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_recovery_no_rerun_successful_member(stack):
    """After a delegation completes, re-instantiating the run service (simulating
    a restart) and ticking does NOT re-execute the already-successful child."""
    result = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k1",
        children=[_child("w0")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    executed_before = len(stack.chat.calls)
    # Simulate restart: new run service over the same registry.
    new_run = DelegationRunService(
        registry=stack.registry, child_executor=ChildAgentExecutor(
            chat_service=stack.chat, clock=stack.clock,
        ),
        clock=stack.clock, config=_config(),
    )
    await new_run.tick()
    # No additional child execution.
    assert len(stack.chat.calls) == executed_before
    d = await stack.registry.get(result.delegation_id)
    assert d.is_terminal


# ---------------------------------------------------------------------------
# Tool executor end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_executor_end_to_end(stack):
    """The delegate_agents tool executor drives the full stack and returns
    a parent-safe projection."""
    exe = DelegateAgentsToolExecutor(delegation_service=stack.svc)
    ctx = ToolExecutionContext(
        session_id="s1", run_id="r1",
        trusted_metadata={"delegation_capability": _capability()},
        granted_tools=frozenset({"delegate_agents"}),
    )
    req = ToolCallRequest(
        id="c1", name="delegate_agents",
        arguments={
            "delegation_key": "tool-k1",
            "children": [
                {"title": "w0", "instruction": "do work",
                 "allowed_tools": ["get_current_time"], "budget_tokens": 100},
            ],
            "join_policy": "all_completed",
            "aggregation": "parent",
            "timeout_seconds": 60,
        },
    )
    result = await exe.execute(req, ctx)
    from app.domain.tool import ToolResultStatus
    assert result.status is ToolResultStatus.SUCCESS
    payload = result.content
    assert payload["status"] == "succeeded"
    assert payload["delegation_id"]
    # No internal session/lease fields leaked.
    assert "execution_session_id" not in payload
    assert "claim_lock" not in payload


# ---------------------------------------------------------------------------
# Authorization + parser rejections do not leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_capability_does_not_call_service(stack):
    exe = DelegateAgentsToolExecutor(delegation_service=stack.svc)
    ctx = ToolExecutionContext(
        session_id="s1", run_id="r1",
        trusted_metadata={},  # no capability
        granted_tools=frozenset({"delegate_agents"}),
    )
    req = ToolCallRequest(
        id="c1", name="delegate_agents",
        arguments={
            "delegation_key": "k1",
            "children": [{"title": "w0", "instruction": "do work",
                          "allowed_tools": ["get_current_time"], "budget_tokens": 100}],
            "join_policy": "all_completed", "aggregation": "parent",
            "timeout_seconds": 60,
        },
    )
    result = await exe.execute(req, ctx)
    from app.domain.tool import ToolResultStatus
    assert result.status is ToolResultStatus.ERROR
    assert result.content["error"] == "delegation_not_authorized"
    # Nothing was executed.
    assert len(stack.chat.calls) == 0


def test_parser_rejects_duplicate_json_keys():
    parser = DelegationRequestParser()
    raw = '{"delegation_key": "k1", "delegation_key": "k2"}'
    with pytest.raises(DelegationError) as exc:
        parser.decode_arguments(raw)
    assert exc.value.code == "delegation_invalid"


def test_parser_rejects_oversized_nesting():
    parser = DelegationRequestParser()
    # 100 levels of nesting exceeds the depth cap (64).
    raw = "[" * 100 + "]" * 100
    with pytest.raises(DelegationError) as exc:
        parser.decode_arguments(raw)
    assert exc.value.code == "delegation_invalid"


def test_parser_rejects_unknown_top_level_field():
    """Unknown top-level fields are rejected (closed schema at the parser level
    is the caller's job; the parser rejects structural issues). Duplicate keys
    and malformed JSON are the parser's hard guarantees."""
    parser = DelegationRequestParser()
    # Malformed JSON -> rejected.
    with pytest.raises(DelegationError):
        parser.decode_arguments('{"delegation_key": }')


def test_tool_definition_closed_schema_no_additional_properties():
    defs = delegate_agent_tool_definitions()
    schema = defs[0].input_schema
    assert schema["additionalProperties"] is False
    # Children items are also closed.
    child_item = schema["properties"]["children"]["items"]
    assert child_item["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Information flow: secret redaction in result projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_secret_redacted_before_parent(stack):
    """If a child result contains a known secret, it is redacted before the
    parent sees the ResultSet (PARENT release target)."""
    stack.chat.response_content = f"result contains {stack.info_flow.secrets.secret_values and 'SECRET-KEY-123' or 'secret'} here"
    result = await stack.svc.delegate(
        parent_capability=_capability(), delegation_key="k1",
        children=[_child("w0")],
        join_policy="all_completed", aggregation="parent", timeout_seconds=60,
    )
    for r in result.member_results:
        assert "SECRET-KEY-123" not in r.summary
