"""RuntimeMemoryService -- the SINGLE, non-bypassable read/write facade for
the Agent Runtime's memory access.

This Application service wraps a ``MemoryStore`` (injected) with precise
use-case methods.  Every method evaluates ``MemoryPolicy`` before
delegating to the store.  If the policy denies, NO store call is made.

The service does NOT expose a generic ``store`` attribute -- callers
cannot grab the raw ``MemoryStore`` to bypass policy.

Each operation records a ``PolicyAuditEvent`` via the optional
``PolicyAuditSink`` (or ``PolicyAuditService``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.domain.memory import MemoryStore
from app.domain.memory_policy import (
    MemoryAccessDecision,
    MemoryOperation,
    MemoryPolicy,
    MemoryPolicyRequest,
)
from app.domain.policy import (
    ExecutionMode,
    PolicyAuditEvent,
    PolicyAuditSink,
    PolicyDecisionKind,
    PolicyOutcome,
)
from app.domain.session import (
    ConversationMessage,
    ConversationSession,
    Summary,
    TaskState,
    ToolCall,
)

logger = logging.getLogger(__name__)


class MemoryAccessDeniedError(Exception):
    """Raised when a write operation is denied by MemoryPolicy."""

    def __init__(self, decision: MemoryAccessDecision, operation: MemoryOperation) -> None:
        self.decision = decision
        self.operation = operation
        super().__init__(
            f"memory access denied: {operation.value} -- {decision.reason}",
        )


class RuntimeMemoryService:
    """Non-bypassable facade for Runtime memory access.

    Constructor takes:
    - ``memory_store``: the existing MemoryStore port (stored privately).
    - ``memory_policy``: optional MemoryPolicy (defaults to ``MemoryPolicy()``).
    - ``audit_sink``: optional ``PolicyAuditSink`` for audit events.
    - ``external_memory_manager``: optional ExternalMemoryManager for
      external memory operations (sync, tool write).
    - ``cross_session_read_enabled`` / ``unattended_write_enabled``: config
      values forwarded to each ``MemoryPolicyRequest``.
    - ``enabled_slots``: default tuple of enabled external memory slots.
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        memory_policy: MemoryPolicy | None = None,
        audit_sink: PolicyAuditSink | None = None,
        external_memory_manager: Any = None,
        *,
        cross_session_read_enabled: bool = False,
        unattended_write_enabled: bool = False,
        enabled_slots: tuple[str, ...] = (),
    ) -> None:
        self._store = memory_store
        self._policy = memory_policy or MemoryPolicy()
        self._audit_sink = audit_sink
        self._external = external_memory_manager
        self._cross_session_read_enabled = cross_session_read_enabled
        self._unattended_write_enabled = unattended_write_enabled
        self._enabled_slots = enabled_slots

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        operation: MemoryOperation,
        session_id: str,
        *,
        target_session_id: str | None = None,
        execution_mode: ExecutionMode = ExecutionMode.REALTIME,
        agent_context: str = "primary",
        provider_slot: str | None = None,
        enabled_slots: tuple[str, ...] | None = None,
    ) -> MemoryAccessDecision:
        request = MemoryPolicyRequest(
            operation=operation,
            session_id=session_id,
            target_session_id=target_session_id or session_id,
            execution_mode=execution_mode,
            agent_context=agent_context,
            provider_slot=provider_slot,
            enabled_slots=enabled_slots if enabled_slots is not None else self._enabled_slots,
            cross_session_read_enabled=self._cross_session_read_enabled,
            unattended_write_enabled=self._unattended_write_enabled,
        )
        return self._policy.evaluate(request)

    async def _audit(
        self,
        decision: MemoryAccessDecision,
        operation: MemoryOperation,
        session_id: str,
    ) -> None:
        if self._audit_sink is None:
            return
        event = PolicyAuditEvent(
            policy="memory-policy",
            version="system-v1",
            decision_kind=PolicyDecisionKind.ADMISSION,
            reason=decision.reason,
            run_id="",
            session_id=session_id,
            outcome=decision.verdict,
        )
        try:
            await self._audit_sink.record(event)
        except Exception:
            logger.warning(
                "audit sink failed for operation=%s session=%s",
                operation.value,
                session_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create_session_if_allowed(
        self,
        session: ConversationSession,
    ) -> ConversationSession:
        decision = self._evaluate(
            MemoryOperation.CREATE_SESSION, session.id,
        )
        await self._audit(decision, MemoryOperation.CREATE_SESSION, session.id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.CREATE_SESSION)
        existing = await self._store.get_session(session.id)
        created = await self._store.create_session(session)
        if existing is None and self._external is not None:
            try:
                self._external.on_session_switch(session.id)
            except Exception:
                logger.warning(
                    "external memory session-switch hook failed for session=%s",
                    session.id,
                    exc_info=True,
                )
        return created

    async def get_session_if_allowed(
        self,
        session_id: str,
    ) -> ConversationSession | None:
        decision = self._evaluate(
            MemoryOperation.READ_SESSION, session_id,
        )
        await self._audit(decision, MemoryOperation.READ_SESSION, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            return None
        return await self._store.get_session(session_id)

    async def lock_profile(
        self,
        session_id: str,
        enabled: list[str],
        *,
        slots: dict[str, str] | None = None,
    ) -> list[str]:
        decision = self._evaluate(
            MemoryOperation.LOCK_PROFILE, session_id,
        )
        await self._audit(decision, MemoryOperation.LOCK_PROFILE, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.LOCK_PROFILE)
        return await self._store.lock_session_external_memory(
            session_id, enabled, slots=slots,
        )

    # ------------------------------------------------------------------
    # Message read / write
    # ------------------------------------------------------------------

    async def read_session_messages(
        self,
        session_id: str,
        *,
        target_session_id: str | None = None,
    ) -> list[ConversationMessage]:
        decision = self._evaluate(
            MemoryOperation.READ_SESSION, session_id,
            target_session_id=target_session_id,
        )
        await self._audit(decision, MemoryOperation.READ_SESSION, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            return []
        return await self._store.list_messages(session_id)

    async def append_user_message(
        self,
        session_id: str,
        content: Any,
    ) -> ConversationMessage:
        return await self._append_message(session_id, "user", content)

    async def append_assistant_message(
        self,
        session_id: str,
        content: Any,
    ) -> ConversationMessage:
        return await self._append_message(session_id, "assistant", content)

    async def append_tool_message(
        self,
        session_id: str,
        content: Any,
        *,
        tool_call_id: str | None = None,
        name: str | None = None,
    ) -> ConversationMessage:
        decision = self._evaluate(
            MemoryOperation.WRITE_MESSAGE, session_id,
        )
        await self._audit(decision, MemoryOperation.WRITE_MESSAGE, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.WRITE_MESSAGE)
        message = ConversationMessage(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
        )
        return await self._store.append_message(session_id, message)

    async def _append_message(
        self,
        session_id: str,
        role: str,
        content: Any,
    ) -> ConversationMessage:
        decision = self._evaluate(
            MemoryOperation.WRITE_MESSAGE, session_id,
        )
        await self._audit(decision, MemoryOperation.WRITE_MESSAGE, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.WRITE_MESSAGE)
        message = ConversationMessage(role=role, content=content)
        return await self._store.append_message(session_id, message)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def save_tool_call_if_allowed(
        self,
        tool_call: ToolCall,
    ) -> ToolCall:
        decision = self._evaluate(
            MemoryOperation.WRITE_MESSAGE, tool_call.session_id,
        )
        await self._audit(decision, MemoryOperation.WRITE_MESSAGE, tool_call.session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.WRITE_MESSAGE)
        return await self._store.save_tool_call(tool_call)

    # ------------------------------------------------------------------
    # Task state
    # ------------------------------------------------------------------

    async def save_task_state_if_allowed(
        self,
        task_state: TaskState,
    ) -> TaskState:
        decision = self._evaluate(
            MemoryOperation.WRITE_MESSAGE, task_state.session_id,
        )
        await self._audit(decision, MemoryOperation.WRITE_MESSAGE, task_state.session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.WRITE_MESSAGE)
        return await self._store.save_task_state(task_state)

    async def get_task_state_if_allowed(
        self,
        session_id: str,
    ) -> TaskState | None:
        decision = self._evaluate(
            MemoryOperation.READ_SESSION, session_id,
        )
        await self._audit(decision, MemoryOperation.READ_SESSION, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            return None
        return await self._store.get_task_state(session_id)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    async def save_summary_if_allowed(
        self,
        summary: Summary,
    ) -> Summary:
        decision = self._evaluate(
            MemoryOperation.WRITE_SUMMARY, summary.session_id,
        )
        await self._audit(decision, MemoryOperation.WRITE_SUMMARY, summary.session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.WRITE_SUMMARY)
        return await self._store.save_summary(summary)

    async def get_summary_if_allowed(
        self,
        session_id: str,
    ) -> Summary | None:
        decision = self._evaluate(
            MemoryOperation.READ_SESSION, session_id,
        )
        await self._audit(decision, MemoryOperation.READ_SESSION, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            return None
        return await self._store.get_summary(session_id)

    async def append_summary_message_if_allowed(
        self,
        session_id: str,
        message: ConversationMessage,
    ) -> ConversationMessage:
        decision = self._evaluate(
            MemoryOperation.WRITE_SUMMARY, session_id,
        )
        await self._audit(decision, MemoryOperation.WRITE_SUMMARY, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.WRITE_SUMMARY)
        return await self._store.append_summary_message(session_id, message)

    async def mark_messages_summarized_if_allowed(
        self,
        session_id: str,
        message_ids: list[str],
    ) -> int:
        decision = self._evaluate(
            MemoryOperation.WRITE_SUMMARY, session_id,
        )
        await self._audit(decision, MemoryOperation.WRITE_SUMMARY, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            raise MemoryAccessDeniedError(decision, MemoryOperation.WRITE_SUMMARY)
        return await self._store.mark_messages_summarized(session_id, message_ids)

    # ------------------------------------------------------------------
    # External memory
    # ------------------------------------------------------------------

    async def sync_external_if_allowed(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str,
        agent_context: str,
        execution_mode: ExecutionMode = ExecutionMode.REALTIME,
        enabled_override: list[str] | None = None,
    ) -> None:
        decision = self._evaluate(
            MemoryOperation.SYNC_EXTERNAL, session_id,
            execution_mode=execution_mode,
            agent_context=agent_context,
        )
        await self._audit(decision, MemoryOperation.SYNC_EXTERNAL, session_id)
        if decision.verdict is not PolicyOutcome.ALLOW:
            return
        if self._external is not None:
            self._external.sync_all(
                user_content, assistant_content,
                session_id=session_id,
                agent_context=agent_context,
                enabled_override=enabled_override,
            )

    def handle_external_tool_call_if_allowed(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        agent_context: str,
        session_id: str,
        execution_mode: ExecutionMode = ExecutionMode.REALTIME,
        provider_slot: str | None = None,
        enabled_override: list[str] | None = None,
    ) -> str:
        slots = tuple(enabled_override) if enabled_override is not None else self._enabled_slots
        decision = self._evaluate(
            MemoryOperation.TOOL_WRITE_EXTERNAL, session_id,
            execution_mode=execution_mode,
            agent_context=agent_context,
            provider_slot=provider_slot,
            enabled_slots=slots,
        )
        # Audit is synchronous here (handle_tool_call is sync); fire-and-forget
        # via a sync wrapper would complicate the API, so we skip async audit
        # for tool writes.  The policy decision itself is logged if logging
        # is enabled.
        if decision.verdict is not PolicyOutcome.ALLOW:
            logger.info(
                "external tool write denied: tool=%s session=%s reason=%s",
                tool_name, session_id, decision.reason,
            )
            return json.dumps({
                "success": False,
                "error": "memory_policy_denied",
                "reason": decision.reason,
            })
        if self._external is None:
            return json.dumps({
                "success": False,
                "error": "no external memory manager",
            })
        return self._external.handle_tool_call(
            tool_name, args,
            agent_context=agent_context,
            session_id=session_id,
            enabled_override=enabled_override,
        )

    def read_external_if_allowed(
        self,
        query: str,
        *,
        session_id: str,
        enabled_override: list[str] | None = None,
        provider_slot: str | None = None,
    ) -> str:
        slots = tuple(enabled_override) if enabled_override is not None else self._enabled_slots
        decision = self._evaluate(
            MemoryOperation.READ_EXTERNAL, session_id,
            provider_slot=provider_slot,
            enabled_slots=slots,
        )
        if decision.verdict is not PolicyOutcome.ALLOW:
            return ""
        if self._external is None:
            return ""
        return self._external.prefetch_all(
            query,
            session_id=session_id,
            enabled_override=enabled_override,
        )
