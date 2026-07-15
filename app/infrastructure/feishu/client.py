from __future__ import annotations

import json
import logging
import re
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

    async def send_interactive_card(self, receive_id: str, card: dict[str, Any], receive_id_type: str = "chat_id") -> str:
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
        payload = response.json()
        return str((payload.get("data") or {}).get("message_id") or "")

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

    async def download_image(self, message_id: str, image_key: str) -> tuple[bytes, str | None]:
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.get(
            f"/open-apis/im/v1/messages/{message_id}/resources/{image_key}",
            params={"type": "image"},
            headers={"Authorization": f"Bearer {tenant_access_token}"},
        )
        response.raise_for_status()
        data = response.content
        if len(data) > 15 * 1024 * 1024:
            raise ValueError("image too large")
        mime = response.headers.get("Content-Type")
        if mime and not mime.startswith("image/"):
            raise ValueError("non-image content type")
        return data, mime

    async def download_url(self, url: str) -> tuple[bytes, str | None]:
        """Download an arbitrary http(s) image (e.g. an OSS signed url) for upload."""
        if not _HTTP_URL_RE.match(url):
            raise ValueError("unsupported url scheme")
        response = await self.http_client.get(url, timeout=30.0)
        response.raise_for_status()
        data = response.content
        if len(data) > 15 * 1024 * 1024:
            raise ValueError("image too large")
        mime = response.headers.get("Content-Type")
        return data, mime

    async def upload_image(self, image_bytes: bytes, mime: str) -> str:
        """Upload image bytes to Feishu and return the resulting image_key."""
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.post(
            "/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            data={"image_type": "message"},
            files={"image": (_image_filename(mime), image_bytes, mime or "image/jpeg")},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu image upload failed: {payload.get('msg') or payload.get('code')}")
        image_key = str((payload.get("data") or {}).get("image_key") or "")
        if not image_key:
            raise RuntimeError("feishu image upload returned no image_key")
        return image_key

    async def send_post(self, receive_id: str, post_content: dict[str, Any], receive_id_type: str = "chat_id") -> None:
        tenant_access_token = await self.get_tenant_access_token()
        response = await self.http_client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "post",
                "content": json.dumps(post_content, ensure_ascii=False),
            },
        )
        response.raise_for_status()

    async def send_markdown_reply(self, receive_id: str, content: str, receive_id_type: str = "chat_id") -> None:
        """Render markdown content as a Feishu message.

        Plain text (no markdown image/link) is sent as a text message, matching
        prior behavior. Content carrying ``![alt](url)`` or ``[label](url)`` is
        rendered as a post rich-text message: images are downloaded from their
        url, uploaded to Feishu for an image_key, then embedded; links become
        friendly ``a`` elements so the raw url is not shown. Any failure during
        image fetch/upload degrades that image to a placeholder row; if post
        delivery itself fails, the whole content falls back to a text message.
        """
        segments = _parse_markdown_segments(content)
        if not any(segment[0] in ("image", "link") for segment in segments):
            await self.send_text(receive_id, content, receive_id_type)
            return
        image_keys: dict[str, str | None] = {}
        for segment in segments:
            if segment[0] != "image":
                continue
            url = segment[2]
            if url not in image_keys:
                image_keys[url] = await self._fetch_image_key(url)
        try:
            post_content = _build_post_content(segments, image_keys)
            await self.send_post(receive_id, post_content, receive_id_type)
        except Exception:
            await self.send_text(receive_id, content, receive_id_type)

    async def _fetch_image_key(self, url: str) -> str | None:
        try:
            data, mime = await self.download_url(url)
            return await self.upload_image(data, mime or "image/jpeg")
        except Exception:
            return None

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


_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _parse_markdown_segments(text: str) -> list[tuple]:
    """Split markdown text into ordered (text/image/link) segments.

    Image syntax ``![alt](url)`` and link syntax ``[label](url)`` are detected
    in source order; a link span that lies inside an image span (the ``[alt]
    (url)`` portion of an image) is skipped so images are not double-counted.
    """
    images = [
        (m.start(), m.end(), "image", m.group(1), m.group(2))
        for m in _MARKDOWN_IMAGE_RE.finditer(text)
    ]
    image_ranges = [(start, end) for start, end, *_ in images]
    links: list[tuple] = []
    for m in _MARKDOWN_LINK_RE.finditer(text):
        if any(start <= m.start() < end for start, end in image_ranges):
            continue
        links.append((m.start(), m.end(), "link", m.group(1), m.group(2)))
    marks = [*images, *links]
    marks.sort(key=lambda item: item[0])
    segments: list[tuple] = []
    pos = 0
    for start, end, kind, label, url in marks:
        if start > pos:
            segments.append(("text", text[pos:start]))
        segments.append((kind, label, url))
        pos = end
    if pos < len(text):
        segments.append(("text", text[pos:]))
    return segments


def _build_post_content(
    segments: list[tuple], image_keys: dict[str, str | None]
) -> dict[str, Any]:
    """Build a Feishu post rich-text body (zh_cn) from parsed segments."""
    rows: list[list[dict[str, Any]]] = []
    for segment in segments:
        kind = segment[0]
        if kind == "text":
            for line in segment[1].split("\n"):
                # collapse consecutive blank lines so paragraph breaks at
                # image/link boundaries do not stack into multiple empty rows
                if (
                    line == ""
                    and rows
                    and rows[-1] == [{"tag": "text", "text": ""}]
                ):
                    continue
                rows.append([{"tag": "text", "text": line}])
        elif kind == "link":
            rows.append([{"tag": "a", "text": segment[1], "href": segment[2]}])
        elif kind == "image":
            image_key = image_keys.get(segment[2])
            if image_key:
                rows.append([{"tag": "img", "image_key": image_key}])
            else:
                rows.append([{"tag": "text", "text": "[图片加载失败]"}])
    return {"zh_cn": {"content": rows}}


def _image_filename(mime: str | None) -> str:
    if mime and "png" in mime:
        return "image.png"
    if mime and "gif" in mime:
        return "image.gif"
    return "image.jpeg"
