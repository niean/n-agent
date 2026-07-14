"""Memory Policy -- Domain-level governance for session and external memory access.

This module is pure Domain: it imports only stdlib/typing/dataclasses/enum,
``app.domain.policy``, and ``app.domain.session``.  It does NOT import
MemoryStore, Infrastructure, or Application modules.

The Policy takes session/profile facts + operation + config values and
returns a ``MemoryAccessDecision``.  It performs NO database reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.domain.policy import ExecutionMode, PolicyOutcome


class MemoryOperation(str, Enum):
    """Operations that the Runtime may request against session/external memory."""

    CREATE_SESSION = "create_session"
    LOCK_PROFILE = "lock_profile"
    READ_SESSION = "read_session"
    READ_EXTERNAL = "read_external"
    WRITE_MESSAGE = "write_message"
    WRITE_SUMMARY = "write_summary"
    SYNC_EXTERNAL = "sync_external"
    TOOL_WRITE_EXTERNAL = "tool_write_external"


@dataclass(frozen=True)
class MemoryPolicyRequest:
    """Input to ``MemoryPolicy.evaluate``.

    Carries session/profile facts, the requested operation, agent context
    (execution_mode, agent_context), provider slot info, and config values
    (cross_session_read_enabled, unattended_write_enabled).  All fields are
    plain values -- no DB references, no store handles.
    """

    operation: MemoryOperation
    session_id: str
    target_session_id: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.REALTIME
    agent_context: str = "unattended"
    provider_slot: str | None = None
    enabled_slots: tuple[str, ...] = ()
    cross_session_read_enabled: bool = False
    unattended_write_enabled: bool = False


@dataclass(frozen=True)
class MemoryAccessDecision:
    """Output of ``MemoryPolicy.evaluate``.

    When ``verdict`` is ``ALLOW``, ``providers`` / ``scopes`` / ``sync_mode``
    / ``retention`` carry the allowed values.  When ``verdict`` is ``DENY``,
    those fields are empty/None and ``reason`` explains the denial.
    """

    verdict: PolicyOutcome
    reason: str
    providers: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    sync_mode: str | None = None
    retention: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("memory access decision reason must not be empty")


class MemoryPolicy:
    """Pure-domain policy that decides whether a memory operation is allowed.

    Decision table (first-stage defaults):

    - ``CREATE_SESSION`` / ``LOCK_PROFILE`` -> ALLOW (core behavior).
    - ``WRITE_MESSAGE`` / ``WRITE_SUMMARY`` -> ALLOW (session memory write is core).
    - ``READ_SESSION`` (current session) -> ALLOW.
    - Cross-session read (target != current) -> DENY unless
      ``cross_session_read_enabled`` (False by default).
    - ``READ_EXTERNAL`` -> ALLOW only for explicitly enabled slots; else DENY.
    - ``SYNC_EXTERNAL`` -> ALLOW only for trusted primary (agent_context=
      "primary") and not under UNATTENDED/DELEGATED (unless
      ``unattended_write_enabled``).
    - ``TOOL_WRITE_EXTERNAL`` -> DENY under UNATTENDED/DELEGATED unless
      ``unattended_write_enabled``; otherwise ALLOW if slot is enabled.
    """

    def evaluate(self, request: MemoryPolicyRequest) -> MemoryAccessDecision:
        op = request.operation

        # ---- Core operations: always ALLOW ----
        if op in (MemoryOperation.CREATE_SESSION, MemoryOperation.LOCK_PROFILE):
            return MemoryAccessDecision(
                verdict=PolicyOutcome.ALLOW,
                reason=f"{op.value} is core behavior",
            )

        if op in (MemoryOperation.WRITE_MESSAGE, MemoryOperation.WRITE_SUMMARY):
            return MemoryAccessDecision(
                verdict=PolicyOutcome.ALLOW,
                reason=f"{op.value} is core session write",
            )

        # ---- Session read ----
        if op is MemoryOperation.READ_SESSION:
            target = request.target_session_id or request.session_id
            if target != request.session_id and not request.cross_session_read_enabled:
                return MemoryAccessDecision(
                    verdict=PolicyOutcome.DENY,
                    reason="cross-session read denied (cross_session_read_enabled=False)",
                )
            return MemoryAccessDecision(
                verdict=PolicyOutcome.ALLOW,
                reason="current-session read allowed",
            )

        # ---- External read ----
        if op is MemoryOperation.READ_EXTERNAL:
            slot = request.provider_slot
            if slot is None:
                # Bulk read from all enabled providers -- allow if any are enabled
                if request.enabled_slots:
                    return MemoryAccessDecision(
                        verdict=PolicyOutcome.ALLOW,
                        reason="external read allowed for enabled providers",
                        providers=request.enabled_slots,
                        scopes=("read",),
                    )
                return MemoryAccessDecision(
                    verdict=PolicyOutcome.DENY,
                    reason="external read denied: no enabled slots",
                )
            if slot in request.enabled_slots:
                return MemoryAccessDecision(
                    verdict=PolicyOutcome.ALLOW,
                    reason=f"external read allowed for enabled slot '{slot}'",
                    providers=(slot,),
                    scopes=("read",),
                )
            return MemoryAccessDecision(
                verdict=PolicyOutcome.DENY,
                reason="external read denied: slot not explicitly enabled",
            )

        # ---- External write / sync ----
        if op in (MemoryOperation.SYNC_EXTERNAL, MemoryOperation.TOOL_WRITE_EXTERNAL):
            # Check execution mode gate first
            if request.execution_mode in (ExecutionMode.UNATTENDED, ExecutionMode.DELEGATED):
                if not request.unattended_write_enabled:
                    return MemoryAccessDecision(
                        verdict=PolicyOutcome.DENY,
                        reason=(
                            f"{op.value} denied under "
                            f"{request.execution_mode.value} mode "
                            f"(unattended_write_enabled=False)"
                        ),
                    )

            if op is MemoryOperation.SYNC_EXTERNAL:
                # Sync requires trusted primary
                if request.agent_context != "primary":
                    return MemoryAccessDecision(
                        verdict=PolicyOutcome.DENY,
                        reason="sync denied: agent_context is not primary",
                    )
                return MemoryAccessDecision(
                    verdict=PolicyOutcome.ALLOW,
                    reason="sync allowed for trusted primary",
                    sync_mode="auto",
                )

            # TOOL_WRITE_EXTERNAL: also require enabled slot
            slot = request.provider_slot
            if slot is not None and slot not in request.enabled_slots:
                return MemoryAccessDecision(
                    verdict=PolicyOutcome.DENY,
                    reason=f"external tool write denied: slot '{slot}' not enabled",
                )
            return MemoryAccessDecision(
                verdict=PolicyOutcome.ALLOW,
                reason="external tool write allowed",
                providers=(slot,) if slot else (),
                scopes=("write",),
            )

        # Unknown operation: fail-closed
        return MemoryAccessDecision(
            verdict=PolicyOutcome.DENY,
            reason=f"unknown memory operation: {op}",
        )
