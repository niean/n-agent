from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
import asyncio
from typing import Any, Awaitable, Callable

import httpx


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

    async def listen_events(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError("lark-oapi is required for Feishu long connection") from exc

        loop = asyncio.get_running_loop()

        def on_message(payload: Any) -> None:
            data = _event_to_dict(payload)
            asyncio.run_coroutine_threadsafe(handler(data), loop)

        client = lark.ws.Client(self.config.app_id, self.config.app_secret, event_handler=lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build())
        await asyncio.to_thread(client.start)

    async def send_text(self, receive_id: str, text: str) -> None:
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
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
        if self.config.allowed_open_ids and open_id not in self.config.allowed_open_ids:
            raise FeishuVerificationError("open id not allowed")
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            raise FeishuVerificationError("chat id not allowed")


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
