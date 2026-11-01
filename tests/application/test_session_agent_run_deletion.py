import pytest

from app.application.session_service import SessionBusyError, SessionService
from app.domain.session import ConversationSession
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_agent_run_registry import SQLiteAgentRunRegistry


@pytest.mark.asyncio
async def test_active_run_rejects_delete_before_any_business_cleanup(tmp_path):
    path = tmp_path / "agent.db"
    memory = SQLiteMemoryStore(path)
    await memory.create_session(ConversationSession(id="session"))
    registry = SQLiteAgentRunRegistry(path)
    registry.register(run_id="run", session_id="session", source="api", execution_kind="chat",
                      process_id="process", owner_token="token", lease_seconds=60)
    cleaned = []
    service = SessionService(memory, on_session_deleting_handlers=[cleaned.append])
    with pytest.raises(SessionBusyError):
        await service.delete_session("session")
    assert cleaned == []
    assert await memory.get_session("session") is not None
