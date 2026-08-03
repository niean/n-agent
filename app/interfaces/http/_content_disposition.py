"""Shared Content-Disposition header builder for HTTP routes.

HTTP header values are latin-1 (Starlette encodes via
``v.encode("latin-1")`` in ``Response.init_headers``), so the legacy
``filename`` parameter must stay ASCII-only. Non-ASCII names (e.g. Chinese
filenames) fall back to an ASCII placeholder (extension preserved so legacy
clients keep the file type) while the real name is carried by the RFC 5987
``filename*`` parameter that modern clients prefer.

Without this, a non-ASCII name in ``filename="..."`` raises
UnicodeEncodeError when Starlette encodes the header, surfacing as an
HTTP 500 / frontend "request_failed".

All Content-Disposition-emitting routes (artifact content/export, task
attachment download, published-artifact content) MUST build the header
through this single helper so the latin-1 safety rule lives in one place.
Duplicating the logic per-route is what let the rule drift and the same
bug recur across three files.
"""
from __future__ import annotations

from urllib.parse import quote


def build_content_disposition(
    filename: str, disposition: str = "attachment",
) -> str:
    """Build a safe Content-Disposition header with RFC 5987 UTF-8 filename.

    The legacy ``filename`` parameter is kept latin-1 encodable (ASCII-only);
    a non-ASCII name falls back to an ASCII placeholder that preserves a
    safe extension. The real (possibly non-ASCII) name is always carried by
    the RFC 5987 ``filename*`` parameter so modern clients recover it.
    """
    raw = filename or "artifact"
    # Sanitize: remove path separators, quotes, control chars.
    safe = raw.replace("/", "_").replace("\\", "_").replace("\x00", "")
    safe = safe.replace('"', "").replace("\r", "").replace("\n", "")
    if not safe:
        safe = "artifact"
    # Legacy filename is latin-1 only; fall back to an ASCII placeholder
    # (extension preserved) when the name contains non-ASCII characters.
    try:
        safe.encode("latin-1")
    except UnicodeEncodeError:
        safe = _ascii_fallback_name(raw)
    quoted = quote(raw, safe="")
    return f'{disposition}; filename="{safe}"; filename*=UTF-8\'\'{quoted}'


def _ascii_fallback_name(raw: str) -> str:
    """ASCII placeholder for a non-ASCII filename, preserving a safe
    extension so legacy clients retain the file type. The real name is
    delivered via the RFC 5987 ``filename*`` parameter."""
    ext = ""
    dot = raw.rfind(".")
    if dot > 0:
        candidate = raw[dot + 1:].lower()
        if candidate and candidate.isascii() and candidate.isalnum() and len(candidate) <= 8:
            ext = "." + candidate
    return "artifact" + ext
