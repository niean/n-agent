"""ACP permission bridge -- implements ApprovalDecider over an ACP Client connection.

When an LLM tool call has ``RiskLevel.CONFIRM`` and is not already session-approved
in ``ToolExecutionContext.allowed_confirm_tools``, the agent graph runner (T6)
calls the decider. :class:`ACPPermissionBridge` builds an ACP ``ToolCallUpdate``
payload (via ``acp.helpers.update_tool_call`` -- NOT a ``ToolCallStart``), sends
``request_permission`` to the VsCode ACP client, and maps the response back to an
:class:`ApprovalDecision`.

Option mapping: the ACP SDK has no ``allow_session`` kind, so ``allow_session`` is
mapped to ACP ``kind="allow_always"`` (closest semantic match). The bridge keeps
its own stable ``option_id`` strings ("allow_once", "allow_session", "reject_once")
to interpret the response -- the ACP client returns the ``option_id`` it selected.

On ``allow_session``, the bridge invokes an injected ``metadata_updater`` callback
so T12 can persist ``allowed_confirm_tools[tool_name] = "session"`` -- subsequent
calls for the same tool skip the bridge entirely.

Any timeout or exception fails closed: ``ApprovalDecision(allowed=False, scope="deny")``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from acp.helpers import update_tool_call
from acp.interfaces import Client
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
)

from app.domain.tool import ApprovalDecision, ApprovalRequest


MetadataUpdater = Callable[[str, str, str], Awaitable[None] | None]


class ACPPermissionBridge:
    """Approval decider that delegates to the VsCode ACP client's request_permission."""

    def __init__(
        self,
        conn: Client,
        timeout_seconds: float = 30.0,
        metadata_updater: MetadataUpdater | None = None,
    ) -> None:
        self.conn = conn
        self.timeout_seconds = timeout_seconds
        self.metadata_updater = metadata_updater

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        tool_call = update_tool_call(
            req.tool_call_id,
            title=req.tool_name,
            status="in_progress",
        )
        options = [
            PermissionOption(
                kind="allow_once",
                name="Allow once",
                option_id="allow_once",
            ),
            PermissionOption(
                kind="allow_always",
                name="Allow for this session",
                option_id="allow_session",
            ),
            PermissionOption(
                kind="reject_once",
                name="Reject once",
                option_id="reject_once",
            ),
        ]
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response: RequestPermissionResponse = await self.conn.request_permission(
                    options=options,
                    session_id=req.session_id,
                    tool_call=tool_call,
                )
        except asyncio.TimeoutError:
            return ApprovalDecision(allowed=False, scope="deny", reason="timeout")
        except Exception as exc:
            return ApprovalDecision(allowed=False, scope="deny", reason=str(exc))

        outcome = response.outcome
        if isinstance(outcome, DeniedOutcome):
            return ApprovalDecision(allowed=False, scope="deny", reason="cancelled")

        if isinstance(outcome, AllowedOutcome):
            selected = outcome.option_id
            if selected == "allow_once":
                return ApprovalDecision(allowed=True, scope="once")
            if selected == "allow_session":
                await self._persist_session(req)
                return ApprovalDecision(allowed=True, scope="session")
            if selected == "reject_once":
                return ApprovalDecision(allowed=False, scope="deny", reason="rejected")

        return ApprovalDecision(allowed=False, scope="deny", reason="unknown option")

    async def _persist_session(self, req: ApprovalRequest) -> None:
        # Persistence is best-effort: the user already granted permission via the
        # ACP client, so a storage failure must not revoke that grant. The graph
        # still gets scope="session"; subsequent calls will re-prompt the user.
        if self.metadata_updater is None:
            return
        try:
            result = self.metadata_updater(req.session_id, req.tool_name, "session")
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass
