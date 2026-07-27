from __future__ import annotations

import pytest

from app.application.gateway_tool_approval_service import GatewayToolApprovalService
from app.domain.session import ConversationSession


class _MemoryStore:
    def __init__(self) -> None:
        self.session = ConversationSession(id="s1", source="dashboard")

    async def get_session(self, session_id: str):
        return self.session if session_id == self.session.id else None

    async def update_session_acp_metadata(self, session_id: str, metadata: dict) -> None:
        assert session_id == self.session.id
        self.session = ConversationSession(
            id=self.session.id,
            source=self.session.source,
            acp_metadata=metadata,
        )


@pytest.mark.asyncio
async def test_session_grants_survive_service_recreation() -> None:
    store = _MemoryStore()
    original = GatewayToolApprovalService(store)
    assert original.grant_session("s1", "dashboard", "browser_click")
    await original.persist_session_grants("s1", "dashboard")

    recreated = GatewayToolApprovalService(store)
    await recreated.restore_session_grants("s1", "dashboard")

    assert recreated.is_granted("s1", "dashboard", "browser_click")
