"""Dashboard tool approval bridge -- process-local approval coordinator.

Reuses Feishu/CLI approval domain semantics but does NOT depend on IM cards
and does NOT copy the Feishu/CLI "global single pending" limit. Multiple pending
items may coexist on the same bridge; different sessions or different tool calls
do not block each other.

Security-critical invariants enforced here:
- The sender callback receives ONLY the 5-field approval metadata whitelist,
  never the pending object, the raw ApprovalRequest, session ID, actor, or risk
  policy internals.
- ``arguments_summary`` is generated server-side via recursive sensitive-key
  redaction with bounded depth/collection/string/total length. On any error,
  cycle, unknown type, or over-limit it falls back to a fixed safe placeholder;
  it never uses ``str()``, JSON fallback, or exception text.
- ``claim`` is synchronous, atomic (no ``await``), and returns a structured
  ``ClaimResult`` -- 404/409 never raise exceptions.
- Cross-session callers never learn confirmation ID ownership (always 404).
- Tombstones are bounded, TTL-cleaned, store no arguments or grant info, and
  expire on process restart.
- Every error path (missing decider, unknown/cross-session confirmation,
  duplicate/expired, cancel, SSE disconnect, wait timeout, sender failure,
  grant read/write failure) is fail-closed -- the tool never executes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from app.domain.tool import (
    ApprovalDecider,
    ApprovalDecision,
    ApprovalRequest,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 900.0

_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "cookie",
    "privatekey",
)
_SUMMARY_MAX_DEPTH = 5
_SUMMARY_MAX_COLLECTION_LEN = 20
_SUMMARY_MAX_STRING_LEN = 200
_SUMMARY_MAX_SERIALIZED_LEN = 800
_SUMMARY_SAFE_PLACEHOLDER = "***"

_VALID_CHOICES = frozenset({"once", "trust_session", "cancel"})

SessionGrantUpdater = Callable[[str, str, str], bool]
SessionGrantChecker = Callable[[str, str, str], bool]
SessionGrantRevoker = Callable[[str, str, str], None]
ApprovalSender = Callable[[dict[str, Any]], Awaitable[None]]
Clock = Callable[[], float]


@dataclass
class _PendingApproval:
    confirmation_id: str
    session_id: str
    actor_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    description: str
    future: asyncio.Future[ApprovalDecision]
    created_at: float
    expires_at: float
    expires_at_utc: str
    claimed: bool = False
    session_grant_updater: SessionGrantUpdater | None = None
    session_grant_checker: SessionGrantChecker | None = None
    session_grant_revoker: SessionGrantRevoker | None = None


@dataclass
class _Tombstone:
    session_id: str
    expires_at: float


@dataclass(frozen=True)
class ClaimResult:
    """Structured result of ``claim`` -- never raises for 404/409.

    ``status`` is one of ``"ok"``, ``"not_found"``, ``"conflict"``.
    ``decision`` is set when ``status == "ok"``, otherwise ``None``.
    """

    status: Literal["ok", "not_found", "conflict"]
    decision: ApprovalDecision | None = None


class DashboardToolApprovalBridge:
    """Process-local, multi-pending approval bridge for Dashboard chat."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        tombstone_ttl_seconds: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.tombstone_ttl_seconds = (
            tombstone_ttl_seconds
            if tombstone_ttl_seconds is not None
            else timeout_seconds
        )
        self._clock: Clock | None = clock
        self._pending: dict[str, _PendingApproval] = {}
        self._tombstones: dict[str, _Tombstone] = {}

    # -- public introspection (used by tests / router) --

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def tombstone_count(self) -> int:
        return len(self._tombstones)

    # -- decider factory --

    def create_decider(
        self,
        session_id: str,
        actor_id: str,
        sender: ApprovalSender,
        session_grant_updater: SessionGrantUpdater | None = None,
        session_grant_checker: SessionGrantChecker | None = None,
        session_grant_revoker: SessionGrantRevoker | None = None,
    ) -> ApprovalDecider:
        """Return an async ``ApprovalDecider`` bound to ``session_id``/``actor_id``.

        ``sender`` receives ONLY the 5-field approval metadata dict. The decider
        rejects any ``ApprovalRequest`` whose ``session_id`` does not match the
        closure's ``session_id``.
        """

        async def decide(request: ApprovalRequest) -> ApprovalDecision:
            if request.session_id != session_id:
                return ApprovalDecision(
                    allowed=False, scope="deny", reason="session_mismatch"
                )

            # Existing-grant fast path: checker truthy -> session allow.
            # Checker raising or returning falsy continues to interactive
            # approval -- never default-allow.
            if session_grant_checker is not None:
                try:
                    if session_grant_checker(
                        session_id, actor_id, request.tool_name
                    ):
                        return ApprovalDecision(allowed=True, scope="session")
                except Exception:
                    logger.warning(
                        "dashboard tool approval grant check failed",
                        extra={"tool_name": request.tool_name},
                    )

            confirmation_id = f"tool-confirm-{uuid4()}"
            loop = asyncio.get_running_loop()
            future: asyncio.Future[ApprovalDecision] = loop.create_future()
            now = self._now()
            expires_at = now + self.timeout_seconds
            expires_at_utc = _compute_expires_at_utc(self.timeout_seconds)

            pending = _PendingApproval(
                confirmation_id=confirmation_id,
                session_id=session_id,
                actor_id=actor_id,
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                arguments=request.arguments,
                description=request.description,
                future=future,
                created_at=now,
                expires_at=expires_at,
                expires_at_utc=expires_at_utc,
                claimed=False,
                session_grant_updater=session_grant_updater,
                session_grant_checker=session_grant_checker,
                session_grant_revoker=session_grant_revoker,
            )

            # Register pending BEFORE calling sender so cleanup paths can
            # always find it by identity.
            self._pending[confirmation_id] = pending
            try:
                try:
                    await sender(_approval_metadata(pending))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return ApprovalDecision(
                        allowed=False, scope="deny", reason="sender_failed"
                    )

                remaining = max(0.0, pending.expires_at - self._now())
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(future),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return ApprovalDecision(
                        allowed=False, scope="deny", reason="timeout"
                    )
            finally:
                self._cleanup_pending(pending, reason="aborted")

        return decide

    # -- claim (synchronous, atomic, no await) --

    def claim(
        self,
        confirmation_id: str,
        session_id: str,
        choice: str,
    ) -> ClaimResult:
        """Atomically claim a pending approval.

        Returns a ``ClaimResult`` -- never raises for 404/409.
        ``ValueError`` is raised only for an invalid ``choice`` (router maps
        to 422).
        """
        if choice not in _VALID_CHOICES:
            raise ValueError("invalid choice")

        # Opportunistic TTL cleanup
        self._cleanup_expired_tombstones()

        pending = self._pending.get(confirmation_id)
        if pending is None:
            return self._claim_via_tombstone(confirmation_id, session_id)

        # Pending exists -- session mismatch is 404 (must not leak ownership)
        if pending.session_id != session_id:
            return ClaimResult(status="not_found")

        # Session matches
        if pending.claimed:
            return ClaimResult(status="conflict")

        if pending.expires_at <= self._now():
            # Expired -- complete Future with deny, cleanup, tombstone, 409
            if not pending.future.done():
                pending.future.set_result(
                    ApprovalDecision(
                        allowed=False, scope="deny", reason="timeout"
                    )
                )
            self._remove_pending_by_identity(pending)
            self._write_tombstone(pending.session_id, confirmation_id)
            return ClaimResult(status="conflict")

        # -- atomic claim (no await in this section) --
        pending.claimed = True
        self._pending.pop(confirmation_id, None)

        if choice == "once":
            decision = ApprovalDecision(allowed=True, scope="once")
        elif choice == "cancel":
            decision = ApprovalDecision(
                allowed=False, scope="deny", reason="cancelled"
            )
        else:  # trust_session
            decision = self._apply_session_grant(
                pending,
                pending.session_grant_updater,
                pending.session_grant_checker,
                pending.session_grant_revoker,
            )

        if not pending.future.done():
            pending.future.set_result(decision)

        self._write_tombstone(pending.session_id, confirmation_id)
        return ClaimResult(status="ok", decision=decision)

    # -- internal helpers --

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return time.monotonic()

    def _claim_via_tombstone(
        self, confirmation_id: str, session_id: str
    ) -> ClaimResult:
        tombstone = self._tombstones.get(confirmation_id)
        if tombstone is None:
            return ClaimResult(status="not_found")
        if tombstone.expires_at <= self._now():
            self._tombstones.pop(confirmation_id, None)
            return ClaimResult(status="not_found")
        if tombstone.session_id == session_id:
            return ClaimResult(status="conflict")
        # Cross-session -- must not leak that the ID exists
        return ClaimResult(status="not_found")

    def _apply_session_grant(
        self,
        pending: _PendingApproval,
        updater: SessionGrantUpdater | None,
        checker: SessionGrantChecker | None,
        revoker: SessionGrantRevoker | None,
    ) -> ApprovalDecision:
        if updater is None:
            return ApprovalDecision(
                allowed=False, scope="deny", reason="session_grant_failed"
            )
        try:
            success = updater(
                pending.session_id, pending.actor_id, pending.tool_name
            )
        except Exception:
            return ApprovalDecision(
                allowed=False, scope="deny", reason="session_grant_failed"
            )
        if not success:
            return ApprovalDecision(
                allowed=False, scope="deny", reason="session_grant_failed"
            )

        # Readback verification: re-check is_granted. On mismatch, revoke
        # (if a revoker is supplied) and deny.
        if checker is not None:
            try:
                granted = checker(
                    pending.session_id,
                    pending.actor_id,
                    pending.tool_name,
                )
            except Exception:
                granted = False
            if not granted:
                if revoker is not None:
                    try:
                        revoker(
                            pending.session_id,
                            pending.actor_id,
                            pending.tool_name,
                        )
                    except Exception:
                        logger.warning(
                            "dashboard session grant revoke failed",
                            extra={"tool_name": pending.tool_name},
                        )
                return ApprovalDecision(
                    allowed=False, scope="deny", reason="session_grant_failed"
                )
        return ApprovalDecision(allowed=True, scope="session")

    def _cleanup_pending(
        self, pending: _PendingApproval, *, reason: str
    ) -> None:
        """Remove pending by identity and complete Future if not done.

        Writes a tombstone for the same session so duplicate claims return 409
        instead of 404 after cleanup.
        """
        self._remove_pending_by_identity(pending)
        if not pending.future.done():
            pending.future.set_result(
                ApprovalDecision(allowed=False, scope="deny", reason=reason)
            )
        self._write_tombstone(pending.session_id, pending.confirmation_id)

    def _remove_pending_by_identity(
        self, pending: _PendingApproval
    ) -> None:
        """Remove the pending from the map only if it is the exact same object.

        This prevents a later record that reused the same confirmation ID from
        being cancelled or completed by a stale cleanup.
        """
        current = self._pending.get(pending.confirmation_id)
        if current is pending:
            self._pending.pop(pending.confirmation_id, None)

    def _write_tombstone(
        self, session_id: str, confirmation_id: str
    ) -> None:
        self._tombstones[confirmation_id] = _Tombstone(
            session_id=session_id,
            expires_at=self._now() + self.tombstone_ttl_seconds,
        )

    def _cleanup_expired_tombstones(self) -> None:
        now = self._now()
        expired = [
            cid
            for cid, tomb in self._tombstones.items()
            if tomb.expires_at <= now
        ]
        for cid in expired:
            self._tombstones.pop(cid, None)


# ---------------------------------------------------------------------------
# Approval metadata (the ONLY transport payload to sender)
# ---------------------------------------------------------------------------

def _approval_metadata(pending: _PendingApproval) -> dict[str, Any]:
    """Build the 5-field approval metadata whitelist.

    Exactly these fields, all JSON scalar strings:
    ``confirmation_id``, ``tool_name``, ``description``,
    ``arguments_summary``, ``expires_at`` (UTC RFC 3339 with trailing ``Z``).
    """
    return {
        "confirmation_id": pending.confirmation_id,
        "tool_name": pending.tool_name,
        "description": pending.description,
        "arguments_summary": _arguments_summary(pending.arguments),
        "expires_at": pending.expires_at_utc,
    }


def _compute_expires_at_utc(timeout_seconds: float) -> str:
    """Compute a UTC RFC 3339 display string (NOT used for timeout judgment)."""
    expires = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    return expires.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# arguments_summary -- recursive sensitive-key redaction (defense in depth)
# ---------------------------------------------------------------------------

def _arguments_summary(arguments: dict[str, Any]) -> str:
    """Render a bounded, redacted JSON summary of tool arguments.

    On ANY error, cycle, unknown type, or over-limit: returns a fixed safe
    placeholder. Never uses ``str()`` fallback, JSON default, or exception
    text. Never logs the original value.
    """
    if not isinstance(arguments, dict):
        return _SUMMARY_SAFE_PLACEHOLDER
    try:
        redacted = _redact_arguments(arguments, depth=0, visited=set())
    except Exception:
        return _SUMMARY_SAFE_PLACEHOLDER
    try:
        rendered = json.dumps(
            redacted,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except Exception:
        return _SUMMARY_SAFE_PLACEHOLDER
    if len(rendered) <= _SUMMARY_MAX_SERIALIZED_LEN:
        return rendered
    return _SUMMARY_SAFE_PLACEHOLDER


def _redact_arguments(value: Any, depth: int, visited: set[int]) -> Any:
    """Recursively redact sensitive keys and bound depth/length/size.

    - Sensitive keys (case-insensitive, non-alphanumeric stripped) -> ``"***"``
    - Bounded depth, per-level collection length, string length.
    - Cycle detection via ``id()`` set.
    - Unknown types -> ``"***"`` (fail-closed).
    """
    if depth > _SUMMARY_MAX_DEPTH:
        return _SUMMARY_SAFE_PLACEHOLDER

    if isinstance(value, dict):
        obj_id = id(value)
        if obj_id in visited:
            return _SUMMARY_SAFE_PLACEHOLDER
        visited.add(obj_id)
        try:
            result: dict[str, Any] = {}
            items = list(value.items())[:_SUMMARY_MAX_COLLECTION_LEN]
            for key, item in items:
                text_key = str(key)
                normalized = "".join(
                    c for c in text_key.lower() if c.isalnum()
                )
                if any(part in normalized for part in _SECRET_KEY_PARTS):
                    result[text_key] = _SUMMARY_SAFE_PLACEHOLDER
                else:
                    result[text_key] = _redact_arguments(
                        item, depth + 1, visited
                    )
            return result
        finally:
            visited.discard(obj_id)

    if isinstance(value, (list, tuple)):
        obj_id = id(value)
        if obj_id in visited:
            return _SUMMARY_SAFE_PLACEHOLDER
        visited.add(obj_id)
        try:
            return [
                _redact_arguments(item, depth + 1, visited)
                for item in list(value)[:_SUMMARY_MAX_COLLECTION_LEN]
            ]
        finally:
            visited.discard(obj_id)

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _SUMMARY_MAX_STRING_LEN:
            return value[:_SUMMARY_MAX_STRING_LEN]
        return value
    if value is None:
        return None

    # Unknown type -- fail-closed, never str()/repr()
    return _SUMMARY_SAFE_PLACEHOLDER
