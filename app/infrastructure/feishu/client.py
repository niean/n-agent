from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
import asyncio
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


class FeishuVerificationError(Exception):
    pass


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    tenant_key: str = ""
    allowed_open_ids: list[str] = field(default_factory=list)
    allowed_chat_ids: list[str] = field(default_factory=list)


class FeishuClient:
    def __init__(self, config: FeishuConfig, http_client: httpx.AsyncClient | None = None):
        self.config = config
        self.http_client = http_client or httpx.AsyncClient(base_url="https://open.feishu.cn", timeout=10)
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0.0

    def verify_long_connection_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._verify_payload(payload)
        self._verify_allowlist(payload)
        return payload

    def verify_card_action_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._verify_payload(payload)
        event = payload.get("event", {})
        open_id = event.get("operator", {}).get("open_id", "")
        chat_id = event.get("context", {}).get("open_chat_id", "")
        self._verify_actor_allowlist(open_id, chat_id)
        return payload

    async def listen_events(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()

        def run_lark() -> None:
            # lark-oapi binds to ``asyncio.get_event_loop()`` at module import
            # time. Importing it here, inside a dedicated thread that has just
            # installed a fresh loop, keeps lark off uvicorn's running loop —
            # otherwise lark schedules its receive-loop / reconnect tasks onto
            # the server loop and locks request dispatch.
            thread_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_loop)
            try:
                import lark_oapi as lark
            except ImportError as exc:
                raise RuntimeError("lark-oapi is required for Feishu long connection") from exc

            def on_message(payload: Any) -> None:
                data = _event_to_dict(payload)
                _submit_event_handler(handler, data, loop)

            try:
                builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message)
                if hasattr(builder, "register_p2_card_action_trigger"):
                    builder = builder.register_p2_card_action_trigger(on_message)
                client = lark.ws.Client(self.config.app_id, self.config.app_secret, event_handler=builder.build())
                client.start()
            finally:
                try:
                    thread_loop.close()
                except Exception:
                    pass

        await asyncio.to_thread(run_lark)

    async def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None:
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        response.raise_for_status()

    async def send_interactive_card(self, receive_id: str, card: dict[str, Any], receive_id_type: str = "chat_id") -> None:
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        response.raise_for_status()

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.patch(
            f"/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            json={
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        response.raise_for_status()

    async def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> None:
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.post(
            f"/open-apis/im/v1/messages/{message_id}/reactions",
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            json={"reaction_type": {"emoji_type": emoji_type}},
        )
        response.raise_for_status()

    async def get_tenant_access_token(self) -> str:
        if self._tenant_access_token and time.time() < self._tenant_access_token_expires_at:
            return self._tenant_access_token
        response = await self.http_client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("tenant_access_token", ""))
        if not token:
            raise RuntimeError("failed to get feishu tenant access token")
        expire = int(payload.get("expire", 7200))
        self._tenant_access_token = token
        self._tenant_access_token_expires_at = time.time() + max(expire - 60, 0)
        return token

    def _verify_payload(self, payload: dict[str, Any]) -> None:
        header = payload.get("header", {})
        if self.config.app_id and header.get("app_id", self.config.app_id) != self.config.app_id:
            raise FeishuVerificationError("invalid app id")
        if self.config.tenant_key and header.get("tenant_key", self.config.tenant_key) != self.config.tenant_key:
            raise FeishuVerificationError("invalid tenant key")

    def _verify_allowlist(self, payload: dict[str, Any]) -> None:
        event = payload.get("event", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")
        message = event.get("message", {})
        chat_id = message.get("chat_id", "")
        self._verify_actor_allowlist(open_id, chat_id)

    def _verify_actor_allowlist(self, open_id: str, chat_id: str) -> None:
        if self.config.allowed_open_ids and open_id not in self.config.allowed_open_ids:
            raise FeishuVerificationError("open id not allowed")
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            raise FeishuVerificationError("chat id not allowed")


def _submit_event_handler(
    handler: Callable[[dict[str, Any]], Awaitable[None]], payload: dict[str, Any], loop: asyncio.AbstractEventLoop
):
    future = asyncio.run_coroutine_threadsafe(handler(payload), loop)

    def log_failure(done):
        try:
            done.result()
        except Exception:
            logger.exception("feishu event handler failed")

    future.add_done_callback(log_failure)
    return future


def _event_to_dict(payload: Any) -> dict[str, Any]:
    data = _to_plain(payload)
    if not isinstance(data, dict):
        raise TypeError("unsupported feishu event payload")
    return data


def _to_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if value is None or isinstance(value, bool | int | float):
        return value
    if hasattr(value, "raw"):
        raw = getattr(value, "raw")
        if isinstance(raw, str | dict):
            return _to_plain(raw)
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump())
    if hasattr(value, "to_dict"):
        return _to_plain(value.to_dict())
    attrs = {
        name: _to_plain(getattr(value, name))
        for name in dir(value)
        if not name.startswith("_") and not callable(getattr(value, name))
    }
    if attrs:
        return attrs
    raise TypeError("unsupported feishu event payload")
