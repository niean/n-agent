from __future__ import annotations

from app.domain.session import ConversationSession


def test_conversation_session_default_acp_metadata_is_none():
    session = ConversationSession(id="s1")

    assert session.acp_metadata is None


def test_conversation_session_acp_metadata_roundtrip():
    session = ConversationSession(
        id="s1",
        acp_metadata={"cwd": "/ws", "mode": "default"},
    )

    assert session.acp_metadata == {"cwd": "/ws", "mode": "default"}
