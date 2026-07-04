"""ACP event bridge -- bridges N-Agent ChatEvent stream and history to ACP session/update notifications.

The ACP agent (T12) constructs an :class:`ACPEventBridge` per prompt, feeds it
:class:`ChatEvent` instances from ``AgentGraphRunner.stream_events()``, and the
bridge calls ``conn.session_update()`` to push updates to the VsCode client.
For ``session/load`` and ``resume_session``, the bridge replays stored history
(messages + tool_calls) to reconstruct the transcript.

Uses ACP SDK helpers (``update_user_message_text``, ``update_agent_message_text``,
``start_tool_call``, ``update_tool_call``) instead of constructing schema
models directly.

Import strategy: helpers and ``Client`` are imported from their submodules
(``acp.helpers``, ``acp.interfaces``) rather than the ``acp`` top level. The
project's ``tests/conftest.py`` pre-imports ``acp.schema`` (which caches the
real SDK's submodules) and pops ``acp`` so pytest's prepend import mode can
collect the local ``tests/interfaces/cli/commands/acp/`` test package as
``acp.test_*``. Importing ``acp`` at top level would therefore resolve to the
local test package during tests; importing from submodules avoids the
shadowing.
"""

from __future__ import annotations

from acp.helpers import (
    start_tool_call,
    update_agent_message_text,
    update_tool_call,
    update_user_message_text,
)
from acp.interfaces import Client

from app.application.events import ChatEvent, ChatEventType
from app.domain.session import ConversationMessage, ToolCall


class ACPEventBridge:
    """Bridges ChatEvent stream and history to ACP session/update notifications."""

    def __init__(self, conn: Client, session_id: str) -> None:
        self.conn = conn
        self.session_id = session_id

    async def emit_user_message(self, content: str) -> None:
        """Send a UserMessageChunk for the prompt's user input."""
        if not content:
            return
        update = update_user_message_text(content)
        await self.conn.session_update(session_id=self.session_id, update=update)

    async def emit_event(self, event: ChatEvent) -> None:
        """Convert a ChatEvent to an ACP session/update and send it."""
        if event.type is ChatEventType.MESSAGE_START:
            return  # internal state, no update
        if event.type is ChatEventType.CONTENT_DELTA:
            text = event.content or ""
            if not text:
                return
            update = update_agent_message_text(text)
            await self.conn.session_update(session_id=self.session_id, update=update)
            return
        if event.type is ChatEventType.TOOL_CALL_DELTA:
            await self._emit_tool_call_delta(event)
            return
        if event.type is ChatEventType.ERROR:
            error_text = event.error or "unknown error"
            update = update_agent_message_text(f"[error] {error_text}")
            await self.conn.session_update(session_id=self.session_id, update=update)
            return
        # MESSAGE_DONE and DONE: no update sent (completion signaled by prompt return)

    async def _emit_tool_call_delta(self, event: ChatEvent) -> None:
        tool_call = event.tool_call or {}
        tool_call_id = tool_call.get("id", "")
        tool_name = tool_call.get("name", "")
        status = tool_call.get("status", "")

        if not tool_call_id:
            return

        if status == "pending":
            update = start_tool_call(
                tool_call_id=tool_call_id,
                title=tool_name,
                status="pending",
            )
        elif status == "success":
            update = update_tool_call(
                tool_call_id=tool_call_id,
                status="completed",
            )
        elif status in ("error", "permission_denied"):
            update = update_tool_call(
                tool_call_id=tool_call_id,
                status="failed",
            )
        else:
            return  # unknown status, skip

        await self.conn.session_update(session_id=self.session_id, update=update)

    async def replay_history(
        self,
        messages: list[ConversationMessage],
        tool_calls: list[ToolCall],
    ) -> None:
        """Replay stored history as ACP session/update notifications.

        Iterates messages in order. For each:
        - user message: emit UserMessageChunk
        - assistant message with text: emit AgentMessageChunk
        - assistant message with tool_calls: emit AgentMessageChunk for text (if any),
          then ToolCallStart for each tool_call
        - tool message: find matching tool_call by tool_call_id, emit ToolCallProgress
          (completed if success, failed otherwise)
        """
        tool_call_by_id = {tc.id: tc for tc in tool_calls}

        for message in messages:
            if message.role == "user":
                await self._replay_user_message(message)
            elif message.role == "assistant":
                await self._replay_assistant_message(message)
            elif message.role == "tool":
                await self._replay_tool_message(message, tool_call_by_id)

    async def _replay_user_message(self, message: ConversationMessage) -> None:
        content = message.content
        text = content if isinstance(content, str) else str(content)
        if not text:
            return
        update = update_user_message_text(text)
        await self.conn.session_update(session_id=self.session_id, update=update)

    async def _replay_assistant_message(self, message: ConversationMessage) -> None:
        content = message.content
        if isinstance(content, dict) and "tool_calls" in content:
            text = content.get("content", "") or ""
            if text:
                update = update_agent_message_text(text)
                await self.conn.session_update(session_id=self.session_id, update=update)
            for tc in content.get("tool_calls", []):
                tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                tc_name = ""
                if isinstance(tc, dict):
                    function = tc.get("function", {})
                    if isinstance(function, dict):
                        tc_name = function.get("name", "")
                if tc_id and tc_name:
                    update = start_tool_call(
                        tool_call_id=tc_id,
                        title=tc_name,
                        status="pending",
                    )
                    await self.conn.session_update(session_id=self.session_id, update=update)
        elif isinstance(content, str) and content:
            update = update_agent_message_text(content)
            await self.conn.session_update(session_id=self.session_id, update=update)

    async def _replay_tool_message(
        self,
        message: ConversationMessage,
        tool_call_by_id: dict[str, ToolCall],
    ) -> None:
        tc_id = message.tool_call_id or ""
        if not tc_id or tc_id not in tool_call_by_id:
            return
        tc = tool_call_by_id[tc_id]
        status = "completed" if tc.status == "success" else "failed"
        update = update_tool_call(
            tool_call_id=tc_id,
            status=status,
        )
        await self.conn.session_update(session_id=self.session_id, update=update)
