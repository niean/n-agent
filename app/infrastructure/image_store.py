from __future__ import annotations

import logging
import re
from pathlib import Path
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 30.0

# photo-upload 产出 JPEG；按 content-type 兜底其它图片格式
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 落盘文件名为 uuid hex + 白名单扩展名；服务端 resolve 时按此校验，杜绝路径穿越
_SAFE_NAME_RE = re.compile(r"^[a-f0-9]+\.(jpg|jpeg|png|gif|webp)$")


class LocalImageStore:
    """Persist photo-upload images so the Dashboard Chat can render them
    permanently.

    OSS signed URLs returned by the photo-upload skill expire after ~1h.
    Feishu re-hosts the image at delivery time (downloads + uploads as a
    permanent image_key), but the Dashboard Chat stores and renders the
    original expiring URL, so images break after expiry. This store closes
    that gap: at host_terminal tool-success time it downloads the image from
    the still-fresh signed URL, saves it under ``store_dir``, and returns a
    permanent serve URL (``{base_url}/chat/images/{image_id}``) for the agent
    to embed. Any failure returns ``None`` so the executor falls back to the
    original signed URL (graceful degradation, no conversation breakage).
    """

    def __init__(
        self,
        store_dir: Path,
        base_url: str,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._store_dir = Path(store_dir)
        self._base_url = base_url.rstrip("/")
        self._max_bytes = max_bytes
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._store_dir.mkdir(parents=True, exist_ok=True)

    async def persist(self, source_url: str, mime_hint: str | None = None) -> str | None:
        """Download the image from ``source_url`` and return a permanent serve URL.

        Returns ``None`` on HTTP error, oversize, or network failure so the
        caller can fall back to the original (expiring) URL.
        """
        target: Path | None = None
        image_id: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", source_url) as response:
                    if not (200 <= response.status_code < 300):
                        return None
                    raw_mime = mime_hint or response.headers.get("content-type", "") or "image/jpeg"
                    mime = raw_mime.split(";")[0].strip().lower() or "image/jpeg"
                    ext = _EXT_BY_MIME.get(mime, ".jpg")
                    image_id = f"{uuid4().hex}{ext}"
                    target = self._store_dir / image_id
                    written = 0
                    oversize = False
                    with target.open("wb") as fh:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > self._max_bytes:
                                oversize = True
                                break
                            fh.write(chunk)
                    if oversize:
                        target.unlink(missing_ok=True)
                        return None
            return f"{self._base_url}/chat/images/{image_id}"
        except Exception:
            if target is not None:
                try:
                    target.unlink(missing_ok=True)
                except Exception:
                    pass
            logger.warning("image persist failed: %s", source_url, exc_info=True)
            return None

    def resolve(self, image_id: str) -> Path | None:
        """Return the on-disk path for a served image id, or ``None`` if the
        name is unsafe or the file does not exist."""
        if not _SAFE_NAME_RE.match(image_id):
            return None
        path = self._store_dir / image_id
        return path if path.is_file() else None

    def media_type(self, image_id: str) -> str:
        ext = Path(image_id).suffix.lower()
        return _MIME_BY_EXT.get(ext, "image/jpeg")
