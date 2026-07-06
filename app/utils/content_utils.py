from __future__ import annotations

import base64
import re
from typing import Any

MAX_IMAGE_DATA_URL_CHARS = 20 * 1024 * 1024
ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_DATA_URL_RE = re.compile(r"^data:(image/[a-z+]+);base64,(.+)$", re.IGNORECASE)

_ERROR_CODES = frozenset({"unsupported_content_type", "invalid_image_url", "image_too_large"})


def validate_image_url(url: str, max_chars: int = MAX_IMAGE_DATA_URL_CHARS) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("invalid_image_url")
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("data:"):
        if len(url) > max_chars:
            raise ValueError("image_too_large")
        m = _DATA_URL_RE.match(url)
        if not m:
            raise ValueError("invalid_image_url")
        mime = m.group(1).lower()
        if mime not in ALLOWED_IMAGE_MIME:
            raise ValueError("invalid_image_url")
        try:
            base64.b64decode(m.group(2), validate=True)
        except Exception:
            raise ValueError("invalid_image_url")
        return url
    raise ValueError("invalid_image_url")


def parse_data_url(url: str) -> tuple[str | None, str | None]:
    """Split a data URL into (media_type, base64_data). Returns (None, None) if not a valid image data URL."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, None
    m = _DATA_URL_RE.match(url)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


def normalize_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("unsupported_content_type")
    result: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("unsupported_content_type")
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError("unsupported_content_type")
            result.append({"type": "text", "text": text})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else None
            result.append({"type": "image_url", "image_url": {"url": validate_image_url(url)}})
        elif ptype == "input_image":
            url = part.get("image_url") or part.get("url")
            result.append({"type": "image_url", "image_url": {"url": validate_image_url(url)}})
        else:
            raise ValueError("unsupported_content_type")
    return result


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str):
                    texts.append(t)
        return "".join(texts)
    return ""


def has_image_part(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(p, dict) and p.get("type") in ("image_url", "input_image")
        for p in content
    )


def prepend_text_part(content: str | list[dict[str, Any]], prefix: str) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return f"{prefix}{content}" if content else prefix
    if isinstance(content, list):
        return [{"type": "text", "text": prefix}] + content
    return content
