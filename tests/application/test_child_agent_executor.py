"""Tests for ChildAgentExecutor (Application Layer).

T7: isolated child session, delegation tool stripping, no parent history,
member retry reuses same execution_session_id.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid5, NAMESPACE_URL

import pytest

from app.application.child_agent_executor import ChildAgentExecutor
from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.application.policy_snapshot import IngressFacts
from app.domain.delegation import (
    DelegationMember,
    DelegationMemberRole,
    DelegationMemberStatus,
    DelegationResult,
)
from app.domain.policy import ExecutionMode


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeChatService:
    """Captures the ChatCompletionInput and returns a canned result."""
    captured: ChatCompletionInput | None = None
    response_message: dict[str, Any] = field(default_factory=lambda: {
        "role": "assistant", "content": "child result summary"
    })
    response_usage: dict[str, Any] = field(default_factory=lambda: {
        "total_tokens": 120, "prompt_tokens": 80, "completion_tokens": 40
    })
    response_finish_reason: str = "stop"
    raise_exc: Exception | None = None

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult:
        self.captured = request
        if self.raise_exc is not None:
            raise self.raise_exc
        return ChatCompletionResult(
            session_id=request.session_id or "delegation-fake",
            model=request.model,
            message=self.response_message,
            finish_reason=self.response_finish_reason,
            usage=self.response_usage,
        )


class FakeClock:
    def now_iso(self) -> str:
        return "2026-08-12T02:00:00Z"


def _make_member(*, ordinal=0, allowed_tools=("get_current_time", "search_web"),
                 execution_session_id=""):
    sid = execution_session_id or f"delegation-{uuid5(NAMESPACE_URL, f'd1/m{ordinal}')}"
    return DelegationMember.new(
        delegation_id="d1",
        role=DelegationMemberRole.WORKER,
        ordinal=ordinal,
        title=f"worker-{ordinal}",
        instruction="Research the topic and return a summary.",
        skills=("research",),
        allowed_tools=allowed_tools,
        execution_session_id=sid,
        deadline_at="2026-08-12T03:00:00Z",
        budget_tokens=500,
    )


def _parent_capability():
    return {
        "source": "task",
        "run_id": "r1",
        "session_id": "parent-sess-1",
        "scope_id": "t1",
        "actor_id": "user-1",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_uses_isolated_delegation_session():
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    member = _make_member()
    result = await exe.execute(
        member=member,
        model="test-model",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    assert fake.captured is not None
    assert fake.captured.session_id == member.execution_session_id
    assert fake.captured.session_id.startswith("delegation-")
    assert isinstance(result, DelegationResult)


@pytest.mark.asyncio
async def test_child_persist_messages_false():
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    await exe.execute(
        member=_make_member(),
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    assert fake.captured.persist_messages is False


@pytest.mark.asyncio
async def test_child_uses_member_title_for_isolated_session():
    """Persist-disabled child sessions still appear in session storage for
    policy bootstrap, so they need the member's meaningful title."""
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    member = _make_member()
    await exe.execute(
        member=member,
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    assert fake.captured.session_title == member.title


@pytest.mark.asyncio
async def test_child_source_is_delegation_and_unattended():
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    await exe.execute(
        member=_make_member(),
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    facts = fake.captured.ingress_facts
    assert facts is not None
    assert facts.source == "delegation"
    assert facts.execution_mode is ExecutionMode.UNATTENDED


@pytest.mark.asyncio
async def test_child_ingress_session_matches_isolated_execution_session():
    """Policy snapshot admission requires ingress/session identity equality.

    Regression: the child request used the parent's session in IngressFacts,
    so ChatCompletionService rejected every real child before the LLM call.
    """
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    member = _make_member()
    await exe.execute(
        member=member,
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    facts = fake.captured.ingress_facts
    assert facts is not None
    assert facts.session_id == member.execution_session_id
    assert facts.session_id != _parent_capability()["session_id"]


@pytest.mark.asyncio
async def test_child_strips_delegation_and_approval_tools():
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    member = _make_member(
        allowed_tools=("get_current_time", "delegate_agents", "create_task")
    )
    await exe.execute(
        member=member,
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    granted = fake.captured.trusted_metadata.get("granted_tools", [])
    assert "delegate_agents" not in granted
    assert "create_task" not in granted
    assert "get_current_time" in granted


@pytest.mark.asyncio
async def test_child_does_not_inject_parent_history():
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    member = _make_member()
    await exe.execute(
        member=member,
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    messages = fake.captured.messages
    # Only system + user messages, no parent conversation history.
    roles = [m["role"] for m in messages]
    assert "system" in roles or "user" in roles
    # No parent session messages leaked.
    all_content = str(messages)
    assert "parent-sess-1" not in all_content
    # Instruction is present.
    assert any(member.instruction in str(m.get("content", "")) for m in messages)


@pytest.mark.asyncio
async def test_member_retry_reuses_same_execution_session_id():
    fake = FakeChatService()
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    member = _make_member(ordinal=0)
    await exe.execute(
        member=member, model="m", parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    first_session = fake.captured.session_id
    # Retry: same member, same session ID.
    await exe.execute(
        member=member, model="m", parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    assert fake.captured.session_id == first_session


@pytest.mark.asyncio
async def test_exception_produces_failed_result():
    fake = FakeChatService(raise_exc=RuntimeError("LLM exploded"))
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    result = await exe.execute(
        member=_make_member(),
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    assert result.status is DelegationMemberStatus.FAILED
    assert result.error_code is not None
    assert "LLM exploded" not in (result.error_message or "")  # model-safe


@pytest.mark.asyncio
async def test_successful_result_extracts_summary_and_usage():
    fake = FakeChatService(response_message={"role": "assistant", "content": "done"})
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    result = await exe.execute(
        member=_make_member(),
        model="m",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    assert result.status is DelegationMemberStatus.SUCCEEDED
    assert result.summary == "done"
    assert result.usage_summary.get("total_tokens") == 120


@pytest.mark.asyncio
async def test_json_result_credential_values_are_redacted_before_persistence():
    fake = FakeChatService(response_message={
        "role": "assistant",
        "content": '{"secret":"e2e-secret-123","credential":"e2e-token-456"}',
    })
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())

    result = await exe.execute(
        member=_make_member(),
        model="test-model",
        parent_capability=_parent_capability(),
        deadline_at=None,
    )

    assert result.summary == '{"secret":"[REDACTED]","credential":"[REDACTED]"}'


@pytest.mark.asyncio
async def test_chat_error_result_is_not_reported_as_child_success():
    """ChatCompletionService returns provider failures as an error result;
    the error text must not be mistaken for a successful child summary."""
    fake = FakeChatService(
        response_message={"role": "assistant", "content": "provider failed"},
        response_finish_reason="error",
    )
    exe = ChildAgentExecutor(chat_service=fake, clock=FakeClock())
    result = await exe.execute(
        member=_make_member(),
        model="N-Agent",
        parent_capability=_parent_capability(),
        deadline_at="2026-08-12T03:00:00Z",
    )
    assert result.status is DelegationMemberStatus.FAILED
    assert result.error_code == "delegation_child_execution_error"
    assert result.summary == ""
