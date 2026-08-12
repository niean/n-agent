"""ChildAgentExecutor -- runs a single child agent in an isolated session.

Application Layer. Wraps ``ChatCompletionService`` to execute a delegation
member (worker or aggregator) with:

  - An isolated ``delegation-`` prefixed session (never the parent session).
  - ``persist_messages=False`` so child prompts, per-turn messages and
    detailed reasoning never enter the user-visible message store.
  - ``source=delegation`` + ``ExecutionMode.UNATTENDED`` ingress facts.
  - A child prompt containing ONLY role + instruction + input + output
    protocol + deadline + budget hint -- never parent history, sibling
    context, or Task state.
  - ``granted_tools`` stripped of delegation/approval/Task tools.

The executor does NOT decide delegation success (join policy does). It
returns a ``DelegationResult`` capturing the child's outcome, usage, and
any error. Budget ledger reserve/settle is handled by
``DelegationRunService``, not here.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.domain.delegation import DelegationMember, DelegationResult, DelegationMemberStatus
from app.domain.policy import ExecutionMode

# Tools that must never be granted to a child agent, regardless of the
# member's allowed_tools. Mirrors DelegationPolicy.FORBIDDEN_CHILD_TOOLS.
_FORBIDDEN_CHILD_TOOLS: frozenset[str] = frozenset({
    "delegate_agents",
    "create_task", "list_tasks", "approve_task", "reject_task",
    "revise_task", "task_show", "task_complete", "task_heartbeat",
    "task_propose_change", "task_fail",
    "manage_schedule", "schedule_query",
    "skill_manage", "manage_plugin",
})


class _ChatServiceLike(Protocol):
    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult: ...


class _ClockLike(Protocol):
    def now_iso(self) -> str: ...


class ChildAgentExecutor:
    """Executes a single delegation member in an isolated child session."""

    def __init__(self, chat_service: _ChatServiceLike, clock: _ClockLike) -> None:
        self._chat = chat_service
        self._clock = clock

    async def execute(
        self,
        *,
        member: DelegationMember,
        model: str,
        parent_capability: Mapping[str, Any],
        deadline_at: str | None,
    ) -> DelegationResult:
        """Run ``member`` as a child agent and return its ``DelegationResult``.

        The execution session ID is ``member.execution_session_id`` (stable
        across retries). On exception, returns a FAILED result with a
        model-safe error code (never the raw exception message).
        """
        session_id = member.execution_session_id or f"delegation-{member.id}"
        granted_tools = self._strip_forbidden(member.allowed_tools)
        messages = self._build_child_prompt(member, deadline_at)
        started_at = self._clock.now_iso()

        request = ChatCompletionInput(
            model=model,
            messages=messages,
            stream=False,
            session_id=session_id,
            persist_messages=False,
            ingress_facts=self._build_ingress_facts(parent_capability),
            trusted_metadata={
                "granted_tools": list(granted_tools),
                "delegation": {
                    "delegation_id": member.delegation_id,
                    "member_id": member.id,
                    "role": member.role.value,
                    "ordinal": member.ordinal,
                    "deadline_at": deadline_at,
                    "budget_tokens": member.budget_tokens,
                },
            },
        )

        try:
            result = await self._chat.complete(request)
        except Exception:
            return DelegationResult(
                status=DelegationMemberStatus.FAILED,
                error_code="delegation_child_execution_error",
                error_message="child agent execution failed",
                started_at=started_at,
                ended_at=self._clock.now_iso(),
            )

        return self._parse_result(result, started_at)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_forbidden(tools) -> tuple[str, ...]:
        return tuple(t for t in tools if t not in _FORBIDDEN_CHILD_TOOLS)

    @staticmethod
    def _build_child_prompt(member: DelegationMember, deadline_at: str | None) -> list[dict[str, Any]]:
        """Build the child prompt: system (role + output protocol) + user
        (instruction + deadline + budget). No parent history."""
        role_label = "aggregator" if member.role.value == "aggregator" else "worker"
        system_content = (
            f"You are a delegated {role_label} agent. "
            f"Execute your assigned instruction and return a concise result. "
            f"Output protocol: respond with a clear summary of your work. "
            f"If you produce structured data, format it as JSON. "
            f"Do not invoke delegation, task management, or approval tools."
        )
        user_parts = [f"Task: {member.instruction}"]
        if member.title:
            user_parts.insert(0, f"Title: {member.title}")
        if deadline_at:
            user_parts.append(f"Deadline: {deadline_at}")
        if member.budget_tokens > 0:
            user_parts.append(f"Budget: {member.budget_tokens} tokens")
        if member.allowed_tools:
            user_parts.append(f"Allowed tools: {', '.join(member.allowed_tools)}")
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

    @staticmethod
    def _build_ingress_facts(parent_capability: Mapping[str, Any]):
        from app.application.policy_snapshot import IngressFacts
        return IngressFacts(
            run_id=parent_capability.get("run_id", ""),
            session_id=parent_capability.get("session_id", ""),
            source="delegation",
            actor_id=parent_capability.get("actor_id"),
            execution_mode=ExecutionMode.UNATTENDED,
            trusted_claims={
                "parent_source": parent_capability.get("source", ""),
                "parent_scope_id": parent_capability.get("scope_id", ""),
            },
        )

    def _parse_result(self, chat_result: ChatCompletionResult, started_at: str) -> DelegationResult:
        message = chat_result.message or {}
        content = message.get("content", "")
        summary = str(content) if content else ""
        usage = chat_result.usage or {}
        usage_summary: dict[str, int] = {}
        for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
            val = usage.get(key)
            if isinstance(val, (int, float)):
                usage_summary[key] = int(val)
        # A non-empty summary means the child produced output.
        status = DelegationMemberStatus.SUCCEEDED if summary else DelegationMemberStatus.FAILED
        if not summary:
            summary = ""
        return DelegationResult(
            status=status,
            summary=summary,
            usage_summary=usage_summary,
            started_at=started_at,
            ended_at=self._clock.now_iso(),
        )
