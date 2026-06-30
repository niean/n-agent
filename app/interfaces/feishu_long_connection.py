from __future__ import annotations

import json
from typing import Any, Protocol

from app.application.gateway_service import GatewayService
from app.domain.gateway import GatewayConfirmationChoice, GatewaySessionKey, InteractionMessage, InteractionResponse
from app.domain.platform import Platform


class FeishuEventClient(Protocol):
    def verify_long_connection_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def verify_card_action_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None: ...

    async def send_interactive_card(self, receive_id: str, card: dict[str, Any], receive_id_type: str = "chat_id") -> None: ...

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None: ...

    async def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> None: ...

    async def listen_events(self, handler) -> None: ...


class FeishuLongConnectionGateway:
    def __init__(self, gateway_service: GatewayService, feishu_client: FeishuEventClient):
        self.gateway_service = gateway_service
        self.feishu_client = feishu_client
        self._connected = False
        self._fatal: tuple[str, str] | None = None
        # confirmation_id -> confirmation dict last rendered to a card, so we
        # can rebuild the card with disabled buttons after a click.
        self._last_confirmations: dict[str, dict[str, Any]] = {}

    def is_connected(self) -> bool:
        return self._connected

    def fatal_error(self) -> tuple[str, str] | None:
        return self._fatal

    def _mark_connected(self) -> None:
        self._connected = True
        self._fatal = None

    def _mark_disconnected(self) -> None:
        self._connected = False

    def _set_fatal_error(self, code: str, message: str) -> None:
        self._connected = False
        self._fatal = (code, message)

    async def start(self) -> None:
        self._mark_connected()
        try:
            await self.feishu_client.listen_events(self.handle_event)
            self._mark_disconnected()
        except Exception as exc:
            self._set_fatal_error("feishu_listen_error", str(exc))
            raise

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
        platform_session_id = receive_id
        response = await self.gateway_service.handle_message(
            InteractionMessage(
                id=_clean_text(verified.get("header", {}).get("event_id")) or message_id,
                session_key=GatewaySessionKey(
                    Platform.FEISHU,
                    platform_session_id,
                    thread_id=thread_id,
                    display_name=open_id,
                ),
                text=content,
                metadata={
                    "platform": "feishu",
                    "platform_session_id": platform_session_id,
                    "conversation_id": chat_id,
                    "message_id": message_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                    "thread_id": thread_id,
                    "display_name": open_id,
                    "actor_id": open_id,
                },
            )
        )
        if response.metadata.get("duplicate"):
            return
        await self._send_response(response, receive_id, receive_id_type, platform_session_id, thread_id)

    async def _handle_card_action(self, payload: dict[str, Any]) -> None:
        verified = self.feishu_client.verify_card_action_event(payload)
        event = verified.get("event", {})
        operator = event.get("operator", {})
        context = event.get("context", {})
        value = event.get("action", {}).get("value", {})
        actor_id = _clean_text(operator.get("open_id"))
        chat_id = _clean_text(context.get("open_chat_id"))
        card_message_id = _clean_text(context.get("open_message_id"))
        platform_session_id = _clean_text(value.get("platform_session_id")) or chat_id
        thread_id = _clean_text(value.get("thread_id"))
        confirmation_id = _clean_text(value.get("confirmation_id"))
        choice = _clean_text(value.get("choice"))
        # Disable buttons on the original card immediately to prevent the user
        # from double-clicking while the destructive command is in flight.
        if card_message_id and confirmation_id:
            stored = self._last_confirmations.get(confirmation_id)
            if stored is not None:
                try:
                    await self.feishu_client.update_card(
                        card_message_id,
                        _confirmation_card(
                            stored,
                            platform_session_id,
                            thread_id,
                            disabled=True,
                            status_text=_status_text_for_choice(choice),
                        ),
                    )
                except Exception:
                    # Best-effort: gateway-side pending_confirmations pop is the
                    # ultimate guard against double execution.
                    pass
        response = await self.gateway_service.handle_confirmation(
            GatewaySessionKey(Platform.FEISHU, platform_session_id, thread_id=thread_id, display_name=actor_id),
            actor_id,
            confirmation_id,
            GatewayConfirmationChoice(choice),
        )
        await self._send_response(response, chat_id, "chat_id", platform_session_id, thread_id)

    async def _send_response(
        self,
        response: InteractionResponse,
        receive_id: str,
        receive_id_type: str,
        platform_session_id: str,
        thread_id: str,
    ) -> None:
        for outbound in response.messages:
            confirmation = outbound.metadata.get("confirmation")
            if confirmation is not None:
                confirmation_id = _clean_text(confirmation.get("id"))
                if confirmation_id:
                    self._last_confirmations[confirmation_id] = dict(confirmation)
                try:
                    await self.feishu_client.send_interactive_card(
                        receive_id,
                        _confirmation_card(confirmation, platform_session_id, thread_id),
                        receive_id_type,
                    )
                except Exception:
                    self.gateway_service.discard_confirmation(confirmation_id)
                    await self.feishu_client.send_text(receive_id, "确认卡片发送失败，请稍后重试", receive_id_type)
                continue
            await self.feishu_client.send_text(receive_id, outbound.content, receive_id_type)


def _is_card_action(payload: dict[str, Any]) -> bool:
    value = payload.get("event", {}).get("action", {}).get("value", {})
    return bool(value.get("confirmation_id"))


def _confirmation_card(
    confirmation: dict[str, Any],
    platform_session_id: str,
    thread_id: str,
    *,
    disabled: bool = False,
    status_text: str = "",
) -> dict[str, Any]:
    command = _clean_text(confirmation.get("command"))
    confirmation_id = _clean_text(confirmation.get("id"))
    action = _clean_text(confirmation.get("action"))
    elements: list[dict[str, Any]] = []
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"请确认执行：{command}"}})
    if status_text:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": status_text}})
    elements.append({
        "tag": "action",
        "actions": [
            _confirmation_button("执行一次", confirmation_id, "once", platform_session_id, thread_id, disabled),
            _confirmation_button("本会话信任", confirmation_id, "trust_session", platform_session_id, thread_id, disabled),
            _confirmation_button("取消", confirmation_id, "cancel", platform_session_id, thread_id, disabled),
        ],
    })
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def _confirmation_button(
    label: str,
    confirmation_id: str,
    choice: str,
    platform_session_id: str,
    thread_id: str,
    disabled: bool = False,
) -> dict[str, Any]:
    button: dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if choice != "cancel" else "default",
        "value": {
            "confirmation_id": confirmation_id,
            "choice": choice,
            "platform_session_id": platform_session_id,
            "thread_id": thread_id,
        },
    }
    if disabled:
        button["disabled"] = True
    return button


def _status_text_for_choice(choice: str) -> str:
    if choice == "once":
        return "**已点击「执行一次」，正在处理...**"
    if choice == "trust_session":
        return "**已点击「本会话信任」，正在处理...**"
    if choice == "cancel":
        return "**已点击「取消」，正在处理...**"
    return ""


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
