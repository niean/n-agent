from __future__ import annotations

from app.domain.tool import ConfirmToolGrant


class GatewayToolApprovalService:
    """Owns process-local Gateway session grants for confirm tools."""

    def __init__(self) -> None:
        self._session_grants: set[tuple[str, str, str]] = set()

    def grant_session(self, session_id: str, actor_id: str, tool_name: str) -> None:
        if session_id and actor_id and tool_name:
            self._session_grants.add((session_id, actor_id, tool_name))

    def grants_for(self, session_id: str, actor_id: str) -> dict[str, ConfirmToolGrant]:
        return {
            tool_name: "session"
            for grant_session_id, grant_actor_id, tool_name in self._session_grants
            if grant_session_id == session_id and grant_actor_id == actor_id
        }

    def is_granted(self, session_id: str, actor_id: str, tool_name: str) -> bool:
        return (session_id, actor_id, tool_name) in self._session_grants
