from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any, Protocol

from app.application.gateway_service import GatewayService
from app.domain.gateway import GatewayConfirmationChoice, GatewaySessionKey, InteractionMessage, InteractionResponse
from app.domain.platform import Platform
from app.interfaces.feishu_tool_approval import (
    FeishuToolApprovalBridge,
    FeishuToolApprovalError,
)


class FeishuEventClient(Protocol):
    def verify_long_connection_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def verify_card_action_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None: ...

    async def send_interactive_card(self, receive_id: str, card: dict[str, Any], receive_id_type: str = "chat_id") -> str: ...

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None: ...

    async def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> None: ...

    async def listen_events(self, handler) -> None: ...

    async def download_image(self, message_id: str, image_key: str) -> tuple[bytes, str | None]: ...


class FeishuImAdapter:
    def __init__(self, gateway_service: GatewayService, feishu_client: FeishuEventClient):
        self.gateway_service = gateway_service
        self.feishu_client = feishu_client
        self._connected = False
        self._fatal: tuple[str, str] | None = None
        # confirmation_id -> confirmation dict last rendered to a card, so we
        # can rebuild the card with disabled buttons after a click.
        self._last_confirmations: dict[str, dict[str, Any]] = {}
        self._tool_approval_bridge = FeishuToolApprovalBridge()

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
        self._cleanup_confirmation_cache()
        if _is_card_action(payload):
            await self._handle_card_action(payload)
            return
        await self._handle_message(payload)

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        verified = self.feishu_client.verify_long_connection_event(payload)
        event = verified.get("event", {})
        message = event.get("message", {})
        chat_id = _clean_text(message.get("chat_id"))
        message_type = message.get("message_type")
        if message_type == "image":
            if message.get("chat_type") == "group":
                return
            await self._handle_image_message(verified, event, message, chat_id)
            return
        if message_type != "text":
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
        session_key = GatewaySessionKey(
            Platform.FEISHU,
            platform_session_id,
            thread_id=thread_id,
            display_name=open_id,
        )
        response = await self.gateway_service.handle_message(
            InteractionMessage(
                id=_clean_text(verified.get("header", {}).get("event_id")) or message_id,
                session_key=session_key,
                text=content,
                metadata={
                    "platform": Platform.FEISHU.value,
                    "platform_session_id": platform_session_id,
                    "conversation_id": chat_id,
                    "message_id": message_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                    "thread_id": thread_id,
                    "display_name": open_id,
                    "actor_id": open_id,
                },
            ),
            approval_decider=self._tool_approval_decider(
                session_key,
                open_id,
                receive_id,
                receive_id_type,
                thread_id,
            ),
        )
        if response.metadata.get("duplicate"):
            return
        await self._send_response(response, receive_id, receive_id_type, platform_session_id, thread_id)

    async def _handle_image_message(
        self,
        verified: dict[str, Any],
        event: dict[str, Any],
        message: dict[str, Any],
        chat_id: str,
    ) -> None:
        message_id = _clean_text(message.get("message_id"))
        image_key = _image_key(message.get("content", ""))
        if not image_key:
            if chat_id:
                await self.feishu_client.send_text(chat_id, "图片格式无效或过大")
            return
        try:
            data, mime = await self.feishu_client.download_image(message_id, image_key)
        except ValueError:
            if chat_id:
                await self.feishu_client.send_text(chat_id, "图片格式无效或过大")
            return
        except Exception:
            if chat_id:
                await self.feishu_client.send_text(chat_id, "图片下载失败，请重试")
            return
        media_type = mime or "image/png"
        if not media_type.startswith("image/"):
            if chat_id:
                await self.feishu_client.send_text(chat_id, "图片格式无效或过大")
            return
        try:
            encoded = base64.b64encode(data).decode("ascii")
        except (binascii.Error, ValueError):
            if chat_id:
                await self.feishu_client.send_text(chat_id, "图片格式无效或过大")
            return
        data_url = f"data:{media_type};base64,{encoded}"
        sender = event.get("sender", {}).get("sender_id", {})
        open_id = _clean_text(sender.get("open_id"))
        thread_id = _clean_text(message.get("thread_id"))
        receive_id = chat_id or open_id
        receive_id_type = "chat_id" if chat_id else "open_id"
        platform_session_id = receive_id
        session_key = GatewaySessionKey(
            Platform.FEISHU,
            platform_session_id,
            thread_id=thread_id,
            display_name=open_id,
        )
        response = await self.gateway_service.handle_message(
            InteractionMessage(
                id=_clean_text(verified.get("header", {}).get("event_id")) or message_id,
                session_key=session_key,
                text="",
                images=[data_url],
                metadata={
                    "platform": Platform.FEISHU.value,
                    "platform_session_id": platform_session_id,
                    "conversation_id": chat_id,
                    "message_id": message_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                    "thread_id": thread_id,
                    "display_name": open_id,
                    "actor_id": open_id,
                },
            ),
            approval_decider=self._tool_approval_decider(
                session_key,
                open_id,
                receive_id,
                receive_id_type,
                thread_id,
            ),
        )
        if response.metadata.get("duplicate"):
            return
        await self._send_response(response, receive_id, receive_id_type, platform_session_id, thread_id)

    def _tool_approval_decider(
        self,
        session_key: GatewaySessionKey,
        actor_id: str,
        receive_id: str,
        receive_id_type: str,
        thread_id: str,
    ):
        async def send(confirmation: dict[str, Any]) -> str:
            confirmation_id = _clean_text(confirmation.get("id"))
            if confirmation_id:
                self._last_confirmations[confirmation_id] = dict(confirmation)
            try:
                card_message_id = await self.feishu_client.send_interactive_card(
                    receive_id,
                    _confirmation_card(
                        confirmation,
                        session_key.platform_session_id,
                        thread_id,
                    ),
                    receive_id_type,
                )
                if not card_message_id:
                    raise RuntimeError("missing card message id")
                return card_message_id
            except Exception:
                self._last_confirmations.pop(confirmation_id, None)
                await self.feishu_client.send_text(
                    receive_id,
                    "确认卡片发送失败，请稍后重试",
                    receive_id_type,
                )
                raise

        return self._tool_approval_bridge.create_decider(
            session_key,
            actor_id,
            receive_id,
            receive_id_type,
            send,
            cleanup=lambda confirmation_id: self._last_confirmations.pop(
                confirmation_id,
                None,
            ),
            session_grant_updater=self.gateway_service.grant_tool_for_session,
            session_grant_checker=self.gateway_service.is_tool_granted,
        )

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
        confirmation_kind = _clean_text(value.get("confirmation_kind"))
        tool_owned = self._tool_approval_bridge.owns_confirmation(confirmation_id)
        slash_owned = self.gateway_service.owns_confirmation(confirmation_id)
        if tool_owned:
            if confirmation_kind != "tool_policy":
                await self._send_confirmation_error(chat_id, actor_id, "确认类型无效")
                return
            try:
                claim = self._tool_approval_bridge.claim(
                    confirmation_id,
                    choice,
                    verified_chat_id=chat_id,
                    verified_card_message_id=card_message_id,
                    actor_id=actor_id,
                )
            except FeishuToolApprovalError as exc:
                await self._send_confirmation_error(chat_id, actor_id, str(exc))
                return

            stored = self._last_confirmations.get(confirmation_id)
            self._tool_approval_bridge.complete(claim)
            await self._disable_confirmation_card(
                card_message_id,
                stored,
                claim.pending.session_key.platform_session_id,
                claim.pending.session_key.thread_id,
                choice,
            )
            self._last_confirmations.pop(confirmation_id, None)
            return
        if not slash_owned or confirmation_kind not in {"", "slash_command"}:
            await self._send_confirmation_error(chat_id, actor_id, "确认已失效")
            return
        if choice not in {"once", "trust_session", "cancel"}:
            await self._send_confirmation_error(chat_id, actor_id, "确认选项无效")
            return

        stored = self._last_confirmations.get(confirmation_id)

        async def disable_card() -> None:
            await self._disable_confirmation_card(
                card_message_id,
                stored,
                platform_session_id,
                thread_id,
                choice,
            )

        response = await self.gateway_service.handle_confirmation(
            GatewaySessionKey(Platform.FEISHU, platform_session_id, thread_id=thread_id, display_name=actor_id),
            actor_id,
            confirmation_id,
            GatewayConfirmationChoice(choice),
            on_consumed=disable_card,
        )
        if not self.gateway_service.owns_confirmation(confirmation_id):
            self._last_confirmations.pop(confirmation_id, None)
        await self._send_response(response, chat_id, "chat_id", platform_session_id, thread_id)

    async def _disable_confirmation_card(
        self,
        card_message_id: str,
        confirmation: dict[str, Any] | None,
        platform_session_id: str,
        thread_id: str,
        choice: str,
    ) -> None:
        if not card_message_id or confirmation is None:
            return
        try:
            await self.feishu_client.update_card(
                card_message_id,
                _confirmation_card(
                    confirmation,
                    platform_session_id,
                    thread_id,
                    disabled=True,
                    status_text=_status_text_for_choice(choice),
                ),
            )
        except Exception:
            pass

    async def _send_confirmation_error(
        self,
        chat_id: str,
        actor_id: str,
        message: str,
    ) -> None:
        receive_id = chat_id or actor_id
        if receive_id:
            await self.feishu_client.send_text(
                receive_id,
                message,
                "chat_id" if chat_id else "open_id",
            )

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
                    self._last_confirmations.pop(confirmation_id, None)
                    await self.feishu_client.send_text(receive_id, "确认卡片发送失败，请稍后重试", receive_id_type)
                continue
            await self.feishu_client.send_text(receive_id, outbound.content, receive_id_type)

    def _cleanup_confirmation_cache(self) -> None:
        now = datetime.now(timezone.utc)
        expired: list[str] = []
        for confirmation_id, confirmation in self._last_confirmations.items():
            raw_expires_at = _clean_text(confirmation.get("expires_at"))
            if not raw_expires_at:
                continue
            try:
                expires_at = datetime.fromisoformat(raw_expires_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                expired.append(confirmation_id)
        for confirmation_id in expired:
            self._last_confirmations.pop(confirmation_id, None)


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
    confirmation_kind = _clean_text(confirmation.get("kind")) or "slash_command"
    command = _clean_text(confirmation.get("command"))
    confirmation_id = _clean_text(confirmation.get("id"))
    elements: list[dict[str, Any]] = []
    if confirmation_kind == "tool_policy":
        tool_name = _clean_text(confirmation.get("tool_name"))
        description = _clean_text(confirmation.get("description"))
        arguments_summary = _clean_text(confirmation.get("arguments_summary"))
        content = f"请确认调用工具：{tool_name}"
        if description:
            content += f"\n描述：{description}"
        if arguments_summary:
            content += f"\n参数：`{arguments_summary}`"
    else:
        content = f"请确认执行：{command}"
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})
    if status_text:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": status_text}})
    elements.append({
        "tag": "action",
        "actions": [
            _confirmation_button("执行一次", confirmation_id, "once", platform_session_id, thread_id, confirmation_kind, disabled),
            _confirmation_button("本会话信任", confirmation_id, "trust_session", platform_session_id, thread_id, confirmation_kind, disabled),
            _confirmation_button("取消", confirmation_id, "cancel", platform_session_id, thread_id, confirmation_kind, disabled),
        ],
    })
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def _confirmation_button(
    label: str,
    confirmation_id: str,
    choice: str,
    platform_session_id: str,
    thread_id: str,
    confirmation_kind: str,
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
            "confirmation_kind": confirmation_kind,
        },
    }
    if disabled:
        button["disabled"] = True
    return button


def _status_text_for_choice(choice: str) -> str:
    if choice == "once":
        return "**已点击「执行一次」**"
    if choice == "trust_session":
        return "**已点击「本会话信任」**"
    if choice == "cancel":
        return "**已点击「取消」**"
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


def _image_key(raw: str | dict[str, Any]) -> str:
    if isinstance(raw, dict):
        return _clean_text(raw.get("image_key"))
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ""
    return _clean_text(payload.get("image_key"))


def _strip_at(text: str) -> str:
    if "</at>" not in text:
        return text
    return text.split("</at>", 1)[1]
