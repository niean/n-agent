from __future__ import annotations

from typing import Any

from app.domain.memory import MemoryStore
from app.domain.tool import ConfirmToolGrant


_DASHBOARD_GRANTS_KEY = "dashboard_tool_session_grants_v1"


class GatewayToolApprovalService:
    """Owns confirm-tool grants and restores Dashboard grants after a restart."""

    def __init__(self, memory_store: MemoryStore | None = None) -> None:
        self._session_grants: set[tuple[str, str, str]] = set()
        self._memory_store = memory_store

    def grant_session(
        self, session_id: str, actor_id: str, tool_name: str
    ) -> bool:
        """Grant a session-scoped approval for the given tool.

        Returns True when the grant was stored, False when any of the three
        keys is empty (truthiness guard preserved from the original impl).
        """
        if session_id and actor_id and tool_name:
            self._session_grants.add((session_id, actor_id, tool_name))
            return True
        return False

    def revoke_session(
        self, session_id: str, actor_id: str, tool_name: str
    ) -> None:
        """Idempotently discard a matching session grant if present."""
        self._session_grants.discard((session_id, actor_id, tool_name))

    def grants_for(self, session_id: str, actor_id: str) -> dict[str, ConfirmToolGrant]:
        return {
            tool_name: "session"
            for grant_session_id, grant_actor_id, tool_name in self._session_grants
            if grant_session_id == session_id and grant_actor_id == actor_id
        }

    def is_granted(self, session_id: str, actor_id: str, tool_name: str) -> bool:
        return (session_id, actor_id, tool_name) in self._session_grants

    async def restore_session_grants(self, session_id: str, actor_id: str) -> None:
        """Load a Dashboard session's trusted tools into the fast-path cache.

        The approval bridge must stay synchronous while resolving a pending tool
        call, so durable grants are hydrated at the HTTP boundary before the
        decider is constructed.  Invalid or unavailable state never grants
        permission.
        """
        if not self._memory_store or not session_id or not actor_id:
            return
        try:
            session = await self._memory_store.get_session(session_id)
        except Exception:
            return
        if session is None or session.source != "dashboard":
            return
        metadata = session.acp_metadata
        if not isinstance(metadata, dict):
            return
        grants = metadata.get(_DASHBOARD_GRANTS_KEY)
        if not isinstance(grants, dict):
            return
        tool_names = grants.get(actor_id)
        if not isinstance(tool_names, list):
            return
        for tool_name in tool_names:
            if isinstance(tool_name, str) and tool_name:
                self.grant_session(session_id, actor_id, tool_name)

    async def persist_session_grants(self, session_id: str, actor_id: str) -> None:
        """Persist Dashboard trust grants without overwriting ACP metadata."""
        if not self._memory_store or not session_id or not actor_id:
            return
        try:
            session = await self._memory_store.get_session(session_id)
        except Exception:
            return
        if session is None or session.source != "dashboard":
            return
        metadata: dict[str, Any] = dict(session.acp_metadata or {})
        raw_grants = metadata.get(_DASHBOARD_GRANTS_KEY)
        grants = dict(raw_grants) if isinstance(raw_grants, dict) else {}
        grants[actor_id] = sorted(self.grants_for(session_id, actor_id))
        metadata[_DASHBOARD_GRANTS_KEY] = grants
        try:
            await self._memory_store.update_session_acp_metadata(session_id, metadata)
        except Exception:
            # The current in-memory grant remains valid for this request; a
            # later restart will fail closed and ask again rather than trusting
            # an unpersisted authorization.
            return
