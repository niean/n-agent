from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.domain.gateway import GatewaySessionKey
from app.domain.tool import (
    ApprovalDecider,
    ApprovalDecision,
    ApprovalRequest,
)


ApprovalSender = Callable[[dict[str, Any]], Awaitable[str]]
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


@dataclass
class PendingFeishuToolApproval:
    confirmation_id: str
    request: ApprovalRequest
    session_key: GatewaySessionKey
    actor_id: str
    receive_id: str
    receive_id_type: str
    future: asyncio.Future[ApprovalDecision]
    created_at: float
    expires_at: float
    cleanup: ApprovalCleanup | None = None
    session_grant_updater: SessionGrantUpdater | None = None
    card_message_id: str = ""
    claimed: bool = False


@dataclass(frozen=True)
class FeishuToolApprovalClaim:
    confirmation_id: str
    choice: str
    pending: PendingFeishuToolApproval


class FeishuToolApprovalError(ValueError):
    pass


class FeishuToolApprovalBridge:
    def __init__(self, timeout_seconds: float = 900.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, PendingFeishuToolApproval] = {}

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
        receive_id: str,
        receive_id_type: str,
        sender: ApprovalSender,
        cleanup: ApprovalCleanup | None = None,
        session_grant_updater: SessionGrantUpdater | None = None,
        session_grant_checker: SessionGrantChecker | None = None,
    ) -> ApprovalDecider:
        async def decide(request: ApprovalRequest) -> ApprovalDecision:
            if session_grant_checker is not None and session_grant_checker(
                request.session_id,
                actor_id,
                request.tool_name,
            ):
                return ApprovalDecision(allowed=True, scope="session")
            loop = asyncio.get_running_loop()
            confirmation_id = f"tool-confirm-{uuid4()}"
            now = loop.time()
            future: asyncio.Future[ApprovalDecision] = loop.create_future()
            pending = PendingFeishuToolApproval(
                confirmation_id=confirmation_id,
                request=request,
                session_key=session_key,
                actor_id=actor_id,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                future=future,
                created_at=now,
                expires_at=now + self.timeout_seconds,
                cleanup=cleanup,
                session_grant_updater=session_grant_updater,
            )
            self._pending[confirmation_id] = pending
            try:
                try:
                    card_message_id = await sender(_confirmation_metadata(pending))
                    if not card_message_id:
                        raise RuntimeError("missing card message id")
                    pending.card_message_id = card_message_id
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return ApprovalDecision(
                        allowed=False,
                        scope="deny",
                        reason="card_send_failed",
                    )

                try:
                    remaining = max(0.0, pending.expires_at - loop.time())
                    return await asyncio.wait_for(
                        asyncio.shield(future),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return ApprovalDecision(
                        allowed=False,
                        scope="deny",
                        reason="timeout",
                    )
            finally:
                self._discard_pending(pending)

        return decide

    def claim(
        self,
        confirmation_id: str,
        choice: str,
        *,
        verified_chat_id: str,
        verified_card_message_id: str,
        actor_id: str,
    ) -> FeishuToolApprovalClaim:
        if choice not in {"once", "trust_session", "cancel"}:
            raise FeishuToolApprovalError("确认选项无效")
        pending = self._pending.get(confirmation_id)
        if pending is None:
            raise FeishuToolApprovalError("确认已失效")

        now = asyncio.get_running_loop().time()
        if pending.expires_at <= now:
            if not pending.future.done():
                pending.future.set_result(
                    ApprovalDecision(False, "deny", "timeout")
                )
            self._discard_pending(pending)
            raise FeishuToolApprovalError("确认已失效")
        if pending.claimed:
            raise FeishuToolApprovalError("确认已失效")
        if pending.actor_id != actor_id:
            raise FeishuToolApprovalError("只有发起者可以确认")
        if (
            not verified_card_message_id
            or verified_card_message_id != pending.card_message_id
        ):
            raise FeishuToolApprovalError("确认已失效")
        if pending.receive_id_type == "chat_id":
            if (
                not verified_chat_id
                or verified_chat_id != pending.receive_id
                or verified_chat_id != pending.session_key.platform_session_id
            ):
                raise FeishuToolApprovalError("确认已失效")
        elif pending.receive_id_type == "open_id":
            if (
                not verified_chat_id
                or actor_id != pending.receive_id
                or actor_id != pending.session_key.platform_session_id
            ):
                raise FeishuToolApprovalError("确认已失效")
        else:
            raise FeishuToolApprovalError("确认已失效")

        pending.claimed = True
        return FeishuToolApprovalClaim(confirmation_id, choice, pending)

    def complete(self, claim: FeishuToolApprovalClaim) -> None:
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
                    logger.warning("Feishu session tool grant update failed")
                    decision = ApprovalDecision(
                        allowed=True,
                        scope="once",
                        reason="session_grant_failed",
                    )
                else:
                    decision = ApprovalDecision(allowed=True, scope="session")
            else:
                decision = ApprovalDecision(allowed=True, scope="session")
        elif claim.choice == "once":
            decision = ApprovalDecision(allowed=True, scope="once")
        else:
            decision = ApprovalDecision(
                allowed=False,
                scope="deny",
                reason="cancelled",
            )
        if not pending.future.done():
            pending.future.set_result(decision)

    def _discard_pending(self, pending: PendingFeishuToolApproval) -> None:
        current = self._pending.get(pending.confirmation_id)
        if current is pending:
            self._pending.pop(pending.confirmation_id, None)
        if not pending.future.done():
            pending.future.cancel()
        if pending.cleanup is not None:
            pending.cleanup(pending.confirmation_id)


def _confirmation_metadata(pending: PendingFeishuToolApproval) -> dict[str, Any]:
    request = pending.request
    return {
        "id": pending.confirmation_id,
        "kind": "tool_policy",
        "tool_name": request.tool_name,
        "description": request.description,
        "arguments_summary": _arguments_summary(request.arguments),
    }


def _arguments_summary(arguments: dict[str, Any]) -> str:
    rendered = json.dumps(
        _redact(arguments),
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    if len(rendered) <= _ARGUMENTS_SUMMARY_MAX_LENGTH:
        return rendered
    return rendered[: _ARGUMENTS_SUMMARY_MAX_LENGTH - 3] + "..."


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            normalized = "".join(character for character in text_key.lower() if character.isalnum())
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                redacted[text_key] = "***"
            else:
                redacted[text_key] = _redact(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value
