from __future__ import annotations

import json
from typing import Any, Protocol

from app.application.gateway_service import GatewayService
from app.domain.gateway import GatewayConfirmationChoice, GatewaySessionKey, InteractionMessage, InteractionResponse, InteractionSourceType


class FeishuEventClient(Protocol):
    def verify_long_connection_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def verify_card_action_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None: ...

    async def send_interactive_card(self, receive_id: str, card: dict[str, Any], receive_id_type: str = "chat_id") -> None: ...

    async def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> None: ...

    async def listen_events(self, handler) -> None: ...


class FeishuLongConnectionGateway:
    def __init__(self, gateway_service: GatewayService, feishu_client: FeishuEventClient):
        self.gateway_service = gateway_service
        self.feishu_client = feishu_client

    async def start(self) -> None:
        await self.feishu_client.listen_events(self.handle_event)

    async def handle_event(self, payload: dict[str, Any]) -> None:
        if _is_card_action(payload):
            await self._handle_card_action(payload)
            return
        await self._handle_message(payload)

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        verified = self.feishu_client.verify_long_connection_event(payload)
        event = verified.get("event", {})
        message = event.get("message", {})
        chat_id = _clean_text(message.get("chat_id"))
        if message.get("message_type") != "text":
            if chat_id:
                await self.feishu_client.send_text(chat_id, "不支持该消息类型")
            return
        content = _text_content(message.get("content", ""))
        if message.get("chat_type") == "group" and "</at>" not in content:
            return
        content = _strip_at(content).strip()
        sender = event.get("sender", {}).get("sender_id", {})
        open_id = _clean_text(sender.get("open_id"))
        thread_id = _clean_text(message.get("thread_id"))
        message_id = _clean_text(message.get("message_id"))
        if message_id:
            try:
                await self.feishu_client.add_reaction(message_id)
            except Exception:
                pass
        receive_id = chat_id or open_id
        receive_id_type = "chat_id" if chat_id else "open_id"
        source_id = receive_id
        response = await self.gateway_service.handle_message(
            InteractionMessage(
                id=_clean_text(verified.get("header", {}).get("event_id")) or message_id,
                session_key=GatewaySessionKey(
                    InteractionSourceType.FEISHU,
                    source_id,
                    thread_id=thread_id,
                    display_name=open_id,
                ),
                text=content,
                metadata={
                    "source_type": "feishu",
                    "source_id": source_id,
                    "conversation_id": chat_id,
                    "message_id": message_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                    "thread_id": thread_id,
                    "display_name": open_id,
                    "actor_id": open_id,
                    "capabilities": ["active_text_delivery"],
                },
            )
        )
        if response.metadata.get("duplicate"):
            return
        await self._send_response(response, receive_id, receive_id_type, source_id, thread_id)

    async def _handle_card_action(self, payload: dict[str, Any]) -> None:
        verified = self.feishu_client.verify_card_action_event(payload)
        event = verified.get("event", {})
        operator = event.get("operator", {})
        context = event.get("context", {})
        value = event.get("action", {}).get("value", {})
        actor_id = _clean_text(operator.get("open_id"))
        chat_id = _clean_text(context.get("open_chat_id"))
        source_id = _clean_text(value.get("source_id")) or chat_id
        thread_id = _clean_text(value.get("thread_id"))
        response = await self.gateway_service.handle_confirmation(
            GatewaySessionKey(InteractionSourceType.FEISHU, source_id, thread_id=thread_id, display_name=actor_id),
            actor_id,
            _clean_text(value.get("confirmation_id")),
            GatewayConfirmationChoice(_clean_text(value.get("choice"))),
        )
        await self._send_response(response, chat_id, "chat_id", source_id, thread_id)

    async def _send_response(
        self,
        response: InteractionResponse,
        receive_id: str,
        receive_id_type: str,
        source_id: str,
        thread_id: str,
    ) -> None:
        for outbound in response.messages:
            confirmation = outbound.metadata.get("confirmation")
            if confirmation is not None:
                try:
                    await self.feishu_client.send_interactive_card(
                        receive_id,
                        _confirmation_card(confirmation, source_id, thread_id),
                        receive_id_type,
                    )
                except Exception:
                    self.gateway_service.discard_confirmation(_clean_text(confirmation.get("id")))
                    await self.feishu_client.send_text(receive_id, "确认卡片发送失败，请稍后重试", receive_id_type)
                continue
            await self.feishu_client.send_text(receive_id, outbound.content, receive_id_type)


def _is_card_action(payload: dict[str, Any]) -> bool:
    value = payload.get("event", {}).get("action", {}).get("value", {})
    return bool(value.get("confirmation_id"))


def _confirmation_card(confirmation: dict[str, Any], source_id: str, thread_id: str) -> dict[str, Any]:
    command = _clean_text(confirmation.get("command"))
    confirmation_id = _clean_text(confirmation.get("id"))
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"请确认执行：{command}"}},
            {
                "tag": "action",
                "actions": [
                    _confirmation_button("执行一次", confirmation_id, "once", source_id, thread_id),
                    _confirmation_button("本会话信任", confirmation_id, "trust_session", source_id, thread_id),
                    _confirmation_button("取消", confirmation_id, "cancel", source_id, thread_id),
                ],
            },
        ],
    }


def _confirmation_button(label: str, confirmation_id: str, choice: str, source_id: str, thread_id: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if choice != "cancel" else "default",
        "value": {
            "confirmation_id": confirmation_id,
            "choice": choice,
            "source_id": source_id,
            "thread_id": thread_id,
        },
    }


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value)


def _text_content(raw: str | dict[str, Any]) -> str:
    if isinstance(raw, dict):
        return _clean_text(raw.get("text"))
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return str(raw)
    return str(payload.get("text", ""))


def _strip_at(text: str) -> str:
    if "</at>" not in text:
        return text
    return text.split("</at>", 1)[1]
