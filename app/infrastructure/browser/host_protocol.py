"""Shared, dependency-free constants for the Browser Host protocol."""
from __future__ import annotations


PROTOCOL_VERSION = "1"
AUTH_HEADER = "X-N-Agent-Browser-Token"
MAX_JSON_METADATA_BYTES = 262_144
HOST_CDP_MAX_SCREENSHOT_BYTES = 1_048_576
# Backward-compatible alias for callers predating the backend-specific name.
MAX_SCREENSHOT_BYTES = HOST_CDP_MAX_SCREENSHOT_BYTES


def base64_encoded_size(raw_bytes: int) -> int:
    """Return the exact RFC 4648 encoded size for ``raw_bytes``."""
    if type(raw_bytes) is not int or raw_bytes < 0:
        raise ValueError("host_bridge_response_limit_invalid")
    return 4 * ((raw_bytes + 2) // 3)


def max_json_response_bytes(
    max_screenshot_bytes: int = HOST_CDP_MAX_SCREENSHOT_BYTES,
) -> int:
    """Bound one response: fixed metadata allowance plus complete base64."""
    return MAX_JSON_METADATA_BYTES + base64_encoded_size(
        max_screenshot_bytes
    )


MAX_JSON_RESPONSE_BYTES = max_json_response_bytes()
MAX_RESPONSE_BYTES = MAX_JSON_RESPONSE_BYTES


__all__ = [
    "AUTH_HEADER",
    "HOST_CDP_MAX_SCREENSHOT_BYTES",
    "MAX_JSON_METADATA_BYTES",
    "MAX_JSON_RESPONSE_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_SCREENSHOT_BYTES",
    "PROTOCOL_VERSION",
    "base64_encoded_size",
    "max_json_response_bytes",
]
