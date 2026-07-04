"""Tests for ACP event bridge (T9).

Bridges N-Agent ChatEvent stream and stored history to ACP session/update
notifications. Uses ``getattr(update, "session_update", None)`` for type
discrimination per S 1 (the SDK models expose ``session_update`` as the
discriminator field; ``.type`` does not exist on ACP update models).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.application.events import ChatEvent, ChatEventType
from app.domain.session import ConversationMessage, ToolCall
from app.interfaces.cli.commands.acp.event_bridge import ACPEventBridge


class FakeConn:
    """Records all session_update calls for assertions.

    Stands in for ``acp.Client``; only the ``session_update`` coroutine
    method is invoked by :class:`ACPEventBridge`.
    """

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


def _update_type(update: Any) -> str | None:
    """Return the ACP session_update discriminator string.

    ACP update models carry ``session_update`` as the discriminator field
    (alias ``sessionUpdate``). Return ``None`` for objects that lack it.
    """
    return getattr(update, "session_update", None)


def _is_tool_call_update(update: Any) -> bool:
    """True for both ToolCallStart and ToolCallProgress updates."""
    return _update_type(update) in ("tool_call", "tool_call_update")


@pytest.mark.asyncio
async def test_emit_user_message_sends_user_message_chunk():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_user_message("hello world")

    assert len(conn.updates) == 1
    session_id, update = conn.updates[0]
    assert session_id == "s1"
    assert _update_type(update) == "user_message_chunk"


@pytest.mark.asyncio
async def test_emit_user_message_skips_empty_content():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_user_message("")

    assert conn.updates == []


@pytest.mark.asyncio
async def test_emit_event_content_delta_sends_agent_message_chunk():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(ChatEvent(ChatEventType.CONTENT_DELTA, content="hello"))

    assert len(conn.updates) == 1
    assert _update_type(conn.updates[0][1]) == "agent_message_chunk"


@pytest.mark.asyncio
async def test_emit_event_content_delta_skips_empty_text():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(ChatEvent(ChatEventType.CONTENT_DELTA, content=""))

    assert conn.updates == []


@pytest.mark.asyncio
async def test_emit_event_tool_call_pending_sends_tool_call_start():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(
        ChatEvent(
            ChatEventType.TOOL_CALL_DELTA,
            tool_call={
                "id": "tc-1",
                "name": "calculator",
                "arguments": {},
                "status": "pending",
            },
        )
    )

    assert len(conn.updates) == 1
    update = conn.updates[0][1]
    assert _update_type(update) == "tool_call"
    assert update.tool_call_id == "tc-1"
    assert update.status == "pending"
    assert update.title == "calculator"


@pytest.mark.asyncio
async def test_emit_event_tool_call_success_sends_tool_call_progress_completed():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(
        ChatEvent(
            ChatEventType.TOOL_CALL_DELTA,
            tool_call={
                "id": "tc-1",
                "name": "calculator",
                "arguments": {},
                "status": "success",
            },
        )
    )

    assert len(conn.updates) == 1
    update = conn.updates[0][1]
    assert _update_type(update) == "tool_call_update"
    assert update.tool_call_id == "tc-1"
    assert update.status == "completed"


@pytest.mark.asyncio
async def test_emit_event_tool_call_error_sends_tool_call_progress_failed():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(
        ChatEvent(
            ChatEventType.TOOL_CALL_DELTA,
            tool_call={
                "id": "tc-1",
                "name": "calculator",
                "arguments": {},
                "status": "error",
            },
        )
    )

    update = conn.updates[0][1]
    assert _update_type(update) == "tool_call_update"
    assert update.status == "failed"


@pytest.mark.asyncio
async def test_emit_event_tool_call_permission_denied_sends_failed():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(
        ChatEvent(
            ChatEventType.TOOL_CALL_DELTA,
            tool_call={
                "id": "tc-1",
                "name": "manage_schedule",
                "arguments": {},
                "status": "permission_denied",
            },
        )
    )

    update = conn.updates[0][1]
    assert _update_type(update) == "tool_call_update"
    assert update.status == "failed"


@pytest.mark.asyncio
async def test_emit_event_tool_call_unknown_status_skipped():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(
        ChatEvent(
            ChatEventType.TOOL_CALL_DELTA,
            tool_call={"id": "tc-1", "name": "calc", "arguments": {}, "status": "weird"},
        )
    )

    assert conn.updates == []


@pytest.mark.asyncio
async def test_emit_event_error_sends_agent_message():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(
        ChatEvent(ChatEventType.ERROR, error="something broke", finish_reason="error")
    )

    assert len(conn.updates) == 1
    assert _update_type(conn.updates[0][1]) == "agent_message_chunk"


@pytest.mark.asyncio
async def test_emit_event_message_start_and_done_send_no_update():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    await bridge.emit_event(ChatEvent(ChatEventType.MESSAGE_START))
    await bridge.emit_event(ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop"))
    await bridge.emit_event(ChatEvent(ChatEventType.DONE))

    assert conn.updates == []


@pytest.mark.asyncio
async def test_replay_history_user_assistant_text():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    messages = [
        ConversationMessage(role="user", content="what is 1+1"),
        ConversationMessage(role="assistant", content="the answer is 2"),
    ]

    await bridge.replay_history(messages, [])

    assert len(conn.updates) == 2
    assert _update_type(conn.updates[0][1]) == "user_message_chunk"
    assert _update_type(conn.updates[1][1]) == "agent_message_chunk"


@pytest.mark.asyncio
async def test_replay_history_assistant_with_tool_calls_and_tool_message():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    messages = [
        ConversationMessage(role="user", content="calc 1+1"),
        ConversationMessage(
            role="assistant",
            content={
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression":"1+1"}'},
                    }
                ],
            },
        ),
        ConversationMessage(
            role="tool",
            content=json.dumps(
                {"tool_call_id": "call-1", "status": "success", "content": {"result": 2}}
            ),
            tool_call_id="call-1",
            name="calculator",
        ),
        ConversationMessage(role="assistant", content="the answer is 2"),
    ]
    tool_calls = [
        ToolCall(
            id="call-1",
            session_id="s1",
            tool_name="calculator",
            arguments={"expression": "1+1"},
            result={"result": 2},
            status="success",
        ),
    ]

    await bridge.replay_history(messages, tool_calls)

    types = [_update_type(u[1]) for u in conn.updates]
    assert "user_message_chunk" in types
    assert "tool_call" in types
    assert "tool_call_update" in types
    assert "agent_message_chunk" in types

    tool_updates = [u[1] for u in conn.updates if _is_tool_call_update(u[1])]
    assert len(tool_updates) == 2  # start + progress
    assert tool_updates[0].tool_call_id == "call-1"
    assert tool_updates[0].status == "pending"  # ToolCallStart
    assert tool_updates[1].tool_call_id == "call-1"
    assert tool_updates[1].status == "completed"  # ToolCallProgress


@pytest.mark.asyncio
async def test_replay_history_tool_message_without_matching_tool_call_skipped():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    messages = [
        ConversationMessage(
            role="tool",
            content=json.dumps({"result": 2}),
            tool_call_id="orphan-call",
            name="calculator",
        ),
    ]

    await bridge.replay_history(messages, [])

    assert conn.updates == []


@pytest.mark.asyncio
async def test_replay_history_failed_tool_call_sends_failed_progress():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    messages = [
        ConversationMessage(
            role="assistant",
            content={
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": "{}"},
                    }
                ],
            },
        ),
        ConversationMessage(
            role="tool",
            content=json.dumps({"tool_call_id": "call-2", "status": "error"}),
            tool_call_id="call-2",
            name="calculator",
        ),
    ]
    tool_calls = [
        ToolCall(
            id="call-2",
            session_id="s1",
            tool_name="calculator",
            arguments={},
            result=None,
            status="error",
        ),
    ]

    await bridge.replay_history(messages, tool_calls)

    tool_updates = [u[1] for u in conn.updates if _is_tool_call_update(u[1])]
    assert len(tool_updates) == 2
    assert tool_updates[0].status == "pending"
    assert tool_updates[1].status == "failed"


@pytest.mark.asyncio
async def test_replay_history_skips_empty_content():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    messages = [
        ConversationMessage(role="user", content=""),
        ConversationMessage(role="assistant", content=""),
    ]

    await bridge.replay_history(messages, [])

    assert conn.updates == []


@pytest.mark.asyncio
async def test_replay_history_assistant_with_text_and_tool_calls_emits_both():
    conn = FakeConn()
    bridge = ACPEventBridge(conn, "s1")

    messages = [
        ConversationMessage(
            role="assistant",
            content={
                "content": "Let me calculate that.",
                "tool_calls": [
                    {
                        "id": "call-3",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": "{}"},
                    }
                ],
            },
        ),
    ]

    await bridge.replay_history(messages, [])

    types = [_update_type(u[1]) for u in conn.updates]
    assert "agent_message_chunk" in types
    assert "tool_call" in types
