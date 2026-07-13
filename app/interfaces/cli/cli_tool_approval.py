from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.domain.gateway import GatewaySessionKey
from app.domain.tool import (
    ApprovalDecider,
    ApprovalDecision,
    ApprovalRequest,
)

ApprovalNotifier = Callable[[dict[str, Any]], None]
ApprovalCleanup = Callable[[str], None]
SessionGrantUpdater = Callable[[str, str, str], None]
SessionGrantChecker = Callable[[str, str, str], bool]
logger = logging.getLogger(__name__)

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
_ARGUMENTS_SUMMARY_MAX_LENGTH = 800
_DEFAULT_TIMEOUT_SECONDS = 900.0


@dataclass
class PendingCliToolApproval:
    confirmation_id: str
    request: ApprovalRequest
    session_key: GatewaySessionKey
    actor_id: str
    future: asyncio.Future[ApprovalDecision]
    created_at: float
    expires_at: float
    cleanup: ApprovalCleanup | None = None
    session_grant_updater: SessionGrantUpdater | None = None
    claimed: bool = False
    cleanup_called: bool = False


@dataclass(frozen=True)
class CliToolApprovalClaim:
    confirmation_id: str
    choice: str
    pending: PendingCliToolApproval


class CliToolApprovalError(ValueError):
    pass


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            normalized = "".join(c for c in text_key.lower() if c.isalnum())
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                redacted[text_key] = "***"
            else:
                redacted[text_key] = redact_sensitive(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


def arguments_summary(arguments: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(
            redact_sensitive(arguments),
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    except Exception:
        return "***"
    if len(rendered) <= _ARGUMENTS_SUMMARY_MAX_LENGTH:
        return rendered
    return rendered[: _ARGUMENTS_SUMMARY_MAX_LENGTH - 3] + "..."


class CliToolApprovalBridge:
    def __init__(self, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, PendingCliToolApproval] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def is_claimed(self, confirmation_id: str) -> bool:
        pending = self._pending.get(confirmation_id)
        return bool(pending is not None and pending.claimed)

    def owns_confirmation(self, confirmation_id: str) -> bool:
        return confirmation_id in self._pending

    def create_decider(
        self,
        session_key: GatewaySessionKey,
        actor_id: str,
        notifier: ApprovalNotifier,
        cleanup: ApprovalCleanup | None = None,
        session_grant_updater: SessionGrantUpdater | None = None,
        session_grant_checker: SessionGrantChecker | None = None,
    ) -> ApprovalDecider:
        async def decide(request: ApprovalRequest) -> ApprovalDecision:
            if session_grant_checker is not None:
                try:
                    if session_grant_checker(
                        request.session_id, actor_id, request.tool_name
                    ):
                        return ApprovalDecision(allowed=True, scope="session")
                except Exception:
                    logger.warning(
                        "cli tool approval grant check failed",
                        extra={"tool_name": request.tool_name},
                    )
            loop = asyncio.get_running_loop()
            confirmation_id = f"tool-confirm-{uuid4()}"
            now = loop.time()
            future: asyncio.Future[ApprovalDecision] = loop.create_future()
            pending = PendingCliToolApproval(
                confirmation_id=confirmation_id,
                request=request,
                session_key=session_key,
                actor_id=actor_id,
                future=future,
                created_at=now,
                expires_at=now + self.timeout_seconds,
                cleanup=cleanup,
                session_grant_updater=session_grant_updater,
            )
            if self._pending:
                return ApprovalDecision(
                    allowed=False, scope="deny", reason="concurrent_approval"
                )
            self._pending[confirmation_id] = pending
            try:
                try:
                    notifier(_approval_metadata(pending))
                except Exception:
                    return ApprovalDecision(
                        allowed=False, scope="deny", reason="notification_failed"
                    )
                remaining = max(0.0, pending.expires_at - loop.time())
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return ApprovalDecision(allowed=False, scope="deny", reason="timeout")
            finally:
                self._discard_pending(pending)

        return decide

    def claim(
        self,
        confirmation_id: str,
        choice: str,
        *,
        actor_id: str,
        session_key: GatewaySessionKey,
    ) -> CliToolApprovalClaim:
        if choice not in {"once", "trust_session", "cancel"}:
            raise CliToolApprovalError("确认选项无效")
        pending = self._pending.get(confirmation_id)
        if pending is None:
            raise CliToolApprovalError("确认已失效")
        now = asyncio.get_running_loop().time()
        if pending.expires_at <= now:
            if not pending.future.done():
                pending.future.set_result(
                    ApprovalDecision(allowed=False, scope="deny", reason="timeout")
                )
            self._discard_pending(pending)
            raise CliToolApprovalError("确认已失效")
        if pending.claimed:
            raise CliToolApprovalError("确认已失效")
        if pending.actor_id != actor_id or pending.session_key != session_key:
            raise CliToolApprovalError("只有发起者可以确认")
        pending.claimed = True
        return CliToolApprovalClaim(confirmation_id, choice, pending)

    def complete(self, claim: CliToolApprovalClaim) -> None:
        pending = self._pending.get(claim.confirmation_id)
        if pending is not claim.pending or not pending.claimed:
            return
        self._pending.pop(claim.confirmation_id, None)
        if claim.choice == "trust_session":
            if pending.session_grant_updater is not None:
                try:
                    pending.session_grant_updater(
                        pending.request.session_id,
                        pending.actor_id,
                        pending.request.tool_name,
                    )
                except Exception:
                    logger.warning("cli session tool grant update failed")
                    decision = ApprovalDecision(
                        allowed=True, scope="once", reason="session_grant_failed"
                    )
                else:
                    decision = ApprovalDecision(allowed=True, scope="session")
            else:
                decision = ApprovalDecision(allowed=True, scope="session")
        elif claim.choice == "once":
            decision = ApprovalDecision(allowed=True, scope="once")
        else:
            decision = ApprovalDecision(
                allowed=False, scope="deny", reason="cancelled"
            )
        if not pending.future.done():
            pending.future.set_result(decision)

    def discard(self, confirmation_id: str) -> None:
        pending = self._pending.get(confirmation_id)
        if pending is not None:
            self._discard_pending(pending)

    def discard_pending_for_actor(
        self, actor_id: str, session_key: GatewaySessionKey
    ) -> None:
        stale = [
            p
            for p in self._pending.values()
            if p.actor_id == actor_id and p.session_key == session_key
        ]
        for pending in stale:
            if not pending.future.done():
                pending.future.set_result(
                    ApprovalDecision(
                        allowed=False, scope="deny", reason="cancelled"
                    )
                )
            self._discard_pending(pending)

    def _discard_pending(self, pending: PendingCliToolApproval) -> None:
        current = self._pending.get(pending.confirmation_id)
        if current is pending:
            self._pending.pop(pending.confirmation_id, None)
        if not pending.future.done():
            pending.future.cancel()
        if pending.cleanup is not None and not pending.cleanup_called:
            pending.cleanup_called = True
            try:
                pending.cleanup(pending.confirmation_id)
            except Exception:
                logger.warning("cli tool approval cleanup failed")


def _approval_metadata(pending: PendingCliToolApproval) -> dict[str, Any]:
    request = pending.request
    return {
        "id": pending.confirmation_id,
        "kind": "tool_policy",
        "tool_name": request.tool_name,
        "description": request.description,
        "arguments_summary": arguments_summary(request.arguments),
    }
