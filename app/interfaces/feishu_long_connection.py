from __future__ import annotations

import json
from typing import Any, Protocol

from app.application.gateway_service import GatewayService
from app.domain.gateway import GatewaySessionKey, InteractionMessage, InteractionSourceType


class FeishuEventClient(Protocol):
    def verify_long_connection_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def send_text(self, receive_id: str, text: str) -> None: ...

    async def listen_events(self, handler) -> None: ...


class FeishuLongConnectionGateway:
    def __init__(self, gateway_service: GatewayService, feishu_client: FeishuEventClient):
        self.gateway_service = gateway_service
        self.feishu_client = feishu_client

    async def start(self) -> None:
        await self.feishu_client.listen_events(self.handle_event)

    async def handle_event(self, payload: dict[str, Any]) -> None:
        verified = self.feishu_client.verify_long_connection_event(payload)
        event = verified.get("event", {})
        message = event.get("message", {})
        chat_id = message.get("chat_id", "")
        if message.get("message_type") != "text":
            if chat_id:
                await self.feishu_client.send_text(chat_id, "不支持该消息类型")
            return
        content = _text_content(message.get("content", ""))
        if message.get("chat_type") == "group" and "</at>" not in content:
            return
        content = _strip_at(content).strip()
        sender = event.get("sender", {}).get("sender_id", {})
        response = await self.gateway_service.handle_message(
            InteractionMessage(
                id=verified.get("header", {}).get("event_id") or message.get("message_id", ""),
                session_key=GatewaySessionKey(
                    InteractionSourceType.FEISHU,
                    chat_id or sender.get("open_id", ""),
                    display_name=sender.get("open_id", ""),
                ),
                text=content,
                metadata={"message_id": message.get("message_id", "")},
            )
        )
        if response.metadata.get("duplicate"):
            return
        for outbound in response.messages:
            await self.feishu_client.send_text(chat_id, outbound.content)


def _text_content(raw: str | dict[str, Any]) -> str:
    if isinstance(raw, dict):
        return str(raw.get("text", ""))
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return str(raw)
    return str(payload.get("text", ""))


def _strip_at(text: str) -> str:
    if "</at>" not in text:
        return text
    return text.split("</at>", 1)[1]
