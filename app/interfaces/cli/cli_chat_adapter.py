from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from app.application.events import ChatEvent, ChatEventType
from app.application.telemetry_details import StreamDetailsAccumulator, project_details
from app.application.telemetry_semantics import business_operation
from app.domain.gateway import (
    GatewayConfirmationChoice,
    GatewaySessionKey,
    InteractionMessage,
    InteractionResponse,
)
from app.domain.session import SessionSource
from app.domain.tool import ApprovalDecider


class CliChatAdapter:
    def __init__(self, gateway_service: Any) -> None:
        self._svc = gateway_service

    async def send_stream(
        self,
        text: str,
        conversation_id: str,
        *,
        approval_decider: ApprovalDecider | None = None,
    ) -> AsyncIterator[ChatEvent]:
        event = self._build_event(text, conversation_id)
        with business_operation("cli.request", source="cli") as facts:
            facts["details"] = project_details(input_messages=[{"role": "user", "content": text}])
            output = StreamDetailsAccumulator()
            saw_delta = False
            finished = False
            capture_failed = False
            stream = self._svc.handle_message_stream(event, approval_decider=approval_decider)
            try:
                async for evt in stream:
                    try:
                        if evt.type is ChatEventType.CONTENT_DELTA:
                            saw_delta = True
                            output.append_text(evt.content)
                        elif evt.type is ChatEventType.MESSAGE_DONE:
                            if not saw_delta:
                                output.append_text(evt.content)
                            saw_delta = False
                        elif evt.type is ChatEventType.ERROR:
                            facts["outcome"] = "cancelled" if evt.finish_reason == "cancelled" else "failed"
                            facts["details"].update(project_details(error=evt.error))
                    except Exception:
                        capture_failed = True
                    yield evt
                finished = True
            finally:
                try:
                    await stream.aclose()
                finally:
                    try:
                        facts["details"].update(output.finish(
                            incomplete=capture_failed or not finished or facts.get("outcome") in {"failed", "cancelled"},
                        ))
                    except Exception:
                        pass  # Observation failure must preserve close/cancellation.

    async def send(self, text: str, conversation_id: str) -> InteractionResponse:
        event = self._build_event(text, conversation_id)
        with business_operation("cli.request", source="cli") as facts:
            facts["details"] = project_details(input_messages=[{"role": "user", "content": text}])
            response = await self._svc.handle_message(event)
            facts["details"].update(project_details(output_messages=[{"content": m.content} for m in response.messages]))
            return response

    async def confirm(
        self,
        confirmation_id: str,
        choice: str,
        conversation_id: str,
    ) -> InteractionResponse:
        choice_enum = GatewayConfirmationChoice(choice)
        session_key = GatewaySessionKey(SessionSource.CLI.value, conversation_id, display_name=conversation_id)
        actor_id = f"cli:{conversation_id}"
        return await self._svc.handle_confirmation(session_key, actor_id, confirmation_id, choice_enum)

    def grant_tool_for_session(self, session_id: str, actor_id: str, tool_name: str) -> None:
        self._svc.grant_tool_for_session(session_id, actor_id, tool_name)

    def is_tool_granted(self, session_id: str, actor_id: str, tool_name: str) -> bool:
        return self._svc.is_tool_granted(session_id, actor_id, tool_name)

    def _build_event(self, text: str, conversation_id: str) -> InteractionMessage:
        return InteractionMessage(
            id=f"cli-{uuid4()}",
            session_key=GatewaySessionKey(SessionSource.CLI.value, conversation_id, display_name=conversation_id),
            text=text,
            metadata={"actor_id": f"cli:{conversation_id}"},
        )
