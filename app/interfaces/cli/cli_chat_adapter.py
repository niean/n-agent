from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from app.application.events import ChatEvent
from app.domain.gateway import (
    GatewayConfirmationChoice,
    GatewaySessionKey,
    InteractionMessage,
    InteractionResponse,
)
from app.domain.session import SessionSource


class CliChatAdapter:
    def __init__(self, gateway_service) -> None:
        self._svc = gateway_service

    async def send_stream(self, text: str, conversation_id: str) -> AsyncIterator[ChatEvent]:
        event = self._build_event(text, conversation_id)
        async for evt in self._svc.handle_message_stream(event):
            yield evt

    async def send(self, text: str, conversation_id: str) -> InteractionResponse:
        event = self._build_event(text, conversation_id)
        return await self._svc.handle_message(event)

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

    def _build_event(self, text: str, conversation_id: str) -> InteractionMessage:
        return InteractionMessage(
            id=f"cli-{uuid4()}",
            session_key=GatewaySessionKey(SessionSource.CLI.value, conversation_id, display_name=conversation_id),
            text=text,
            metadata={"actor_id": f"cli:{conversation_id}"},
        )
