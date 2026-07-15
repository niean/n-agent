"""Exact, shared scope predicate for the photo signed-URL capability."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def is_photo_capability_request(arguments: Any) -> bool:
    """Match only the administrator-designed photo Skill invocation shape."""
    return (
        isinstance(arguments, dict)
        and set(arguments).issubset(
            {"target_type", "skill", "script", "args", "timeout"}
        )
        and {"target_type", "skill", "script", "args"}.issubset(arguments)
        and arguments.get("target_type") == "skill_script"
        and arguments.get("skill") == "photo-and-upload"
        and arguments.get("script") == "scripts/photo-upload.py"
        and arguments.get("args") == []
        and (
            "timeout" not in arguments
            or (
                isinstance(arguments["timeout"], int)
                and not isinstance(arguments["timeout"], bool)
                and arguments["timeout"] >= 1
            )
        )
    )


def is_photo_capability_result(content: Any) -> bool:
    """Validate the already parsed, minimal success result shape."""
    if not isinstance(content, dict) or set(content) != {
        "capture_size",
        "upload_http",
        "signed_url",
    }:
        return False
    capture_size = content.get("capture_size")
    upload_http = content.get("upload_http")
    signed_url = content.get("signed_url")
    if (
        not isinstance(capture_size, int)
        or isinstance(capture_size, bool)
        or capture_size <= 0
        or not isinstance(upload_http, int)
        or isinstance(upload_http, bool)
        or not 200 <= upload_http <= 299
        or not isinstance(signed_url, str)
    ):
        return False
    parsed = urlsplit(signed_url)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


__all__ = ["is_photo_capability_request", "is_photo_capability_result"]
