"""Delegation parent adapters (T12).

Two adapters bridge the delegation service to the two parent sources:

  - ``RealtimeDelegationAdapter``: signs a per-run, non-forgeable
    delegation capability for realtime (dashboard) chat runs and cascades
    cancellation to scope delegations on disconnect.
  - ``TaskDelegationAdapter``: gates the ``delegate_agents`` tool grant on
    three conditions (global switch + task policy + runtime grant),
    heartbeats during join without introducing new task state, and cancels
    scope delegations on task cancel.

Both adapters read/write only server-injected ``trusted_metadata`` (pattern
twelve). Children/aggregators always have ``delegate_agents`` stripped
(enforced in ``ChildAgentExecutor``); the adapters never grant it to a
child.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.domain.delegation import DelegationStatus


# ---------------------------------------------------------------------------
# Non-serializable server sentinel
# ---------------------------------------------------------------------------


class _ServerSentinel:
    """Process-local marker object that does not survive JSON serialization.

    A live ``_ServerSentinel`` instance is only ever produced by
    ``RealtimeDelegationAdapter.sign_capability`` within this process. A
    deserialized copy (across any JSON boundary) becomes a plain string or
    dict, so ``DelegationCapability.is_valid`` rejects it.
    """


_SENTINEL = _ServerSentinel()


# ---------------------------------------------------------------------------
# DelegationCapability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelegationCapability:
    """A server-signed delegation capability bound to one execution.

    Carries a process-local ``_ServerSentinel`` so it is non-serializable:
    ``json.dumps`` of ``to_dict()`` raises, and a re-deserialized dict is
    rejected by ``is_valid``. This enforces "capability only valid for the
    current execution, not forgeable across a boundary".
    """

    source: str
    scope_id: str
    run_id: str
    session_id: str
    actor_id: str | None
    classification: str
    parent_allowed_tools: frozenset[str]
    system_child_allowlist: frozenset[str]
    _sentinel: _ServerSentinel = field(default=_SENTINEL, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_capability": True,
            "source": self.source,
            "scope_id": self.scope_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "actor_id": self.actor_id,
            "classification": self.classification,
            "parent_allowed_tools": self.parent_allowed_tools,
            "system_child_allowlist": self.system_child_allowlist,
            # Non-serializable marker -> json.dumps raises TypeError.
            "__server_signed__": self._sentinel,
        }

    @staticmethod
    def is_valid(d: Mapping[str, Any]) -> bool:
        """True only for a dict produced by ``to_dict()`` in this process."""
        if not isinstance(d, Mapping):
            return False
        if not d.get("has_capability"):
            return False
        marker = d.get("__server_signed__")
        return isinstance(marker, _ServerSentinel) and marker is _SENTINEL


# ---------------------------------------------------------------------------
# RealtimeDelegationAdapter
# ---------------------------------------------------------------------------


class RealtimeDelegationAdapter:
    """Signs delegation capabilities for realtime (dashboard) chat runs.

    ``ChatCompletionService.complete`` calls ``sign_capability`` for the
    current run and writes the result into
    ``trusted_metadata["delegation_capability"]``. On disconnect/cancel,
    ``on_disconnect`` cascades cancellation to all non-terminal delegations
    in the run's scope.
    """

    def sign_capability(
        self,
        *,
        run_id: str,
        session_id: str,
        scope_id: str,
        actor_id: str | None = None,
        classification: str = "internal",
        parent_allowed_tools: frozenset[str] = frozenset(),
        system_child_allowlist: frozenset[str] = frozenset(),
    ) -> DelegationCapability:
        # Same forbidden-tool strip as sign_task_capability: granted_tools
        # is a tool-exposure list, never a child-grantable list.
        from app.domain.delegation_policy import FORBIDDEN_CHILD_TOOLS

        safe_parent = frozenset(parent_allowed_tools) - FORBIDDEN_CHILD_TOOLS
        safe_allowlist = frozenset(system_child_allowlist) - FORBIDDEN_CHILD_TOOLS
        return DelegationCapability(
            source="realtime",
            scope_id=scope_id,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            classification=classification,
            parent_allowed_tools=safe_parent,
            system_child_allowlist=safe_allowlist,
        )

    async def on_disconnect(
        self,
        *,
        scope_id: str,
        registry: Any,
        run_service: Any,
        reason: str = "realtime_disconnect",
    ) -> None:
        """Cancel all non-terminal delegations in ``scope_id``.

        Uses the server-trusted scope (session id), never a client-supplied
        identifier.
        """
        delegations = await registry.list_for_trusted_scope(scope_id)
        for delegation in delegations:
            if delegation.is_terminal:
                continue
            await run_service.request_cancel(delegation.id, reason)


# ---------------------------------------------------------------------------
# TaskDelegationAdapter
# ---------------------------------------------------------------------------


_DELEGATE_TOOL_NAME = "delegate_agents"


class TaskDelegationAdapter:
    """Adapts delegation to the Task parent source.

    ``should_grant`` is a pure gate: the ``delegate_agents`` tool is added
    to a task worker's explicit tool set only when all three conditions
    hold (global switch, task policy, runtime grant). ``on_task_cancel``
    queries uncancelled delegations by the server-trusted task scope and
    cancels them.
    """

    @staticmethod
    def should_grant(
        *,
        global_enabled: bool,
        task_policy_allows: bool,
        delegate_in_grants: bool,
    ) -> bool:
        """True only when all three conditions allow delegation."""
        return bool(global_enabled and task_policy_allows and delegate_in_grants)

    @staticmethod
    def sign_task_capability(
        *,
        run_id: str,
        session_id: str,
        scope_id: str,
        actor_id: str | None = None,
        classification: str = "internal",
        parent_allowed_tools: frozenset[str] = frozenset(),
        system_child_allowlist: frozenset[str] = frozenset(),
    ) -> DelegationCapability:
        """Sign a delegation capability for a task worker run.

        ``scope_id`` is the server-trusted task id (never a client-submitted
        value). The capability is bound to the task's execution run/session.

        FORBIDDEN_CHILD_TOOLS (delegate_agents + task/approval lifecycle
        tools) are stripped from both tool sets: callers pass the worker's
        granted_tools (which legitimately contains delegate_agents so the
        parent can call it), but those tools are parent-level capabilities
        that DelegationPolicy check 3(a) forbids inside
        parent_allowed_tools and that children must never receive.
        """
        from app.domain.delegation_policy import FORBIDDEN_CHILD_TOOLS

        safe_parent = frozenset(parent_allowed_tools) - FORBIDDEN_CHILD_TOOLS
        safe_allowlist = frozenset(system_child_allowlist) - FORBIDDEN_CHILD_TOOLS
        return DelegationCapability(
            source="task",
            scope_id=scope_id,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            classification=classification,
            parent_allowed_tools=safe_parent,
            system_child_allowlist=safe_allowlist,
        )

    @staticmethod
    def grant_delegate_tool(
        granted_tools: list[str], *, allow: bool
    ) -> list[str]:
        """Add or strip ``delegate_agents`` from the granted tool list.

        Idempotent: never duplicates the entry. When ``allow`` is False the
        tool is always stripped (children/aggregators never get it).
        """
        result = [t for t in granted_tools if t != _DELEGATE_TOOL_NAME]
        if allow:
            result.append(_DELEGATE_TOOL_NAME)
        return result

    async def on_task_cancel(
        self,
        *,
        scope_id: str,
        reason: str,
        registry: Any,
        run_service: Any,
    ) -> None:
        """Cancel all non-terminal delegations in the task's trusted scope.

        ``scope_id`` is the server-side task id from the trusted task
        context, never the tool argument or a user-submitted id.
        """
        delegations = await registry.list_for_trusted_scope(scope_id)
        for delegation in delegations:
            if delegation.is_terminal:
                continue
            await run_service.request_cancel(delegation.id, reason)

    @staticmethod
    async def heartbeat_during_join(
        *,
        task_registry: Any,
        task_id: str,
        task_run_id: int,
    ) -> None:
        """Heartbeat during delegation join using the existing cadence.

        Does NOT introduce a ``WAITING_CHILDREN`` state: the task remains
        RUNNING and the heartbeat simply extends the lease so the worker is
        not reclaimed while waiting for child agents to finish.
        """
        await task_registry.heartbeat(task_id, task_run_id)
