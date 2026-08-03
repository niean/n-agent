"""T13: Public published-artifact routes.

Public unauthenticated routes for viewing published artifact snapshots.
Registers GET /p/{publish_id} (page) and GET /p/{publish_id}/content
(content) on a FastAPI app.

Security:
- publish_id validated against ^[A-Za-z0-9_-]{22,64}$; invalid -> 404
- active -> 200; revoked -> 410; not-found -> 404 (no enumeration leak)
- all responses: Cache-Control: no-store, CSP, nosniff, no-referrer
- markdown: safe convert_to_html via service boundary (no direct
  Infrastructure import)
- HTML snapshot: escaped sandbox="" iframe srcdoc (NO allow-* permissions)
- plain text: escaped text nodes in <pre>
- binary: controlled /content URL (no content read on page)
- NO Host/X-Forwarded-Host trust (share_url computed by service, not route)
- NO source Artifact read (only published-snapshot read path)
- NO path params (publish_id is the only route var)
- Page and /content status CONSISTENT (revoked -> both 410)
"""
from __future__ import annotations

import re
from html import escape

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

from app.domain.artifact import (
    ArtifactContentUnavailableError,
    ArtifactKind,
    PublishedArtifact,
    PublishedArtifactNotFoundError,
)

# publish_id: URL-safe 128-bit+ random, no padding. 22-64 chars.
_PUBLISH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")

# CSP for page responses: no scripts, allows images and frames from self
# (for srcdoc iframe and binary image references via /content).
_PAGE_CSP = (
    "default-src 'none'; "
    "img-src 'self' http: https:; "
    "frame-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

# CSP for content responses: no scripts, no rendering context.
_CONTENT_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'"

# Fixed server-side page template (non-markdown kinds).
_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
</head>
<body>
{body}
</body>
</html>
"""

# Text kinds rendered as escaped text in <pre>.
_TEXT_RENDER_KINDS = frozenset({
    ArtifactKind.CODE,
    ArtifactKind.DATA,
    ArtifactKind.CSV,
    ArtifactKind.JSON,
    ArtifactKind.TEXT,
})

# Binary kinds that reference /content (no content read on page).
_BINARY_KINDS = frozenset({
    ArtifactKind.IMAGE,
    ArtifactKind.PDF,
    ArtifactKind.OTHER,
})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_published_artifact_routes(app, service) -> None:
    """Register public /p/{publish_id} routes on the FastAPI app.

    Args:
        app: FastAPI application instance.
        service: ArtifactService (or compatible) with read-only publish
            methods: get_published, get_published_content,
            convert_markdown_to_html.
    """
    router = APIRouter()

    # ---- GET /p/{publish_id} -- page ----
    @router.get("/p/{publish_id}", response_class=HTMLResponse)
    async def published_page(publish_id: str):
        # 1. Validate publish_id format (before any service call).
        if not _PUBLISH_ID_RE.match(publish_id):
            return _not_found_response()

        # 2. Get published metadata (read-only, no source read).
        try:
            published = await service.get_published(publish_id)
        except PublishedArtifactNotFoundError:
            return _not_found_response()

        # 3. Check status: revoked -> 410.
        if not published.is_active:
            return _gone_response()

        # 4. Render based on snapshot kind.
        kind = published.snapshot_kind
        if kind in (ArtifactKind.MARKDOWN, ArtifactKind.DOCUMENT):
            return await _render_markdown_page(publish_id, published, service)
        if kind == ArtifactKind.HTML:
            return await _render_html_page(publish_id, published, service)
        if kind in _TEXT_RENDER_KINDS:
            return await _render_text_page(publish_id, published, service)
        # Binary kinds (image/pdf/other): reference /content, no content read.
        return _render_binary_page(publish_id, published)

    # ---- GET /p/{publish_id}/content -- content ----
    @router.get("/p/{publish_id}/content")
    async def published_content(publish_id: str):
        # 1. Validate publish_id format (before any service call).
        if not _PUBLISH_ID_RE.match(publish_id):
            return _not_found_response(is_content=True)

        # 2. Get published content (read-only, no source read).
        try:
            data, published = await service.get_published_content(publish_id)
        except PublishedArtifactNotFoundError:
            return _not_found_response(is_content=True)
        except ArtifactContentUnavailableError:
            # Published artifact exists but content is missing (data integrity).
            return _internal_error_response(is_content=True)

        # 3. Check status: revoked -> 410 (no content bypass).
        if not published.is_active:
            return _gone_response(is_content=True)

        # 4. Return content bytes with security headers.
        return _build_content_response(data, published)

    app.include_router(router)


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------


async def _render_markdown_page(
    publish_id: str,
    published: PublishedArtifact,
    service,
) -> Response:
    """Render markdown/document snapshot as safe standalone HTML.

    Uses the service's convert_markdown_to_html (injected converter) so
    the route never imports Infrastructure directly. The converter returns
    a full HTML document with a generic <title>Artifact</title>; we replace
    it with the published snapshot name for consistency with other renderers.
    """
    try:
        data, _ = await service.get_published_content(publish_id)
    except (PublishedArtifactNotFoundError, ArtifactContentUnavailableError):
        return _not_found_response()

    content_str = data.decode("utf-8", errors="replace")
    html_doc = service.convert_markdown_to_html(content_str)
    # Replace the converter's generic title with the artifact name.
    title = escape(published.snapshot_name or "Artifact", quote=True)
    html_doc = html_doc.replace(
        "<title>Artifact</title>",
        f"<title>{title}</title>",
    )
    return Response(
        content=html_doc,
        media_type="text/html; charset=utf-8",
        headers=_page_headers(),
    )


async def _render_html_page(
    publish_id: str,
    published: PublishedArtifact,
    service,
) -> Response:
    """Render HTML snapshot in a sandbox="" iframe via srcdoc.

    The HTML content is fully escaped (html.escape with quote=True) so
    it cannot break out of the srcdoc attribute. The iframe has
    sandbox="" with NO allow-* permissions, preventing script execution,
    same-origin access, forms, popups, and top navigation.
    """
    try:
        data, _ = await service.get_published_content(publish_id)
    except (PublishedArtifactNotFoundError, ArtifactContentUnavailableError):
        return _not_found_response()

    raw_html = data.decode("utf-8", errors="replace")
    # Full escape: & -> &amp;, < -> &lt;, > -> &gt;, " -> &quot;, ' -> &#x27;
    # The browser decodes the srcdoc attribute value back to the original
    # HTML before rendering in the sandboxed iframe.
    escaped_srcdoc = escape(raw_html, quote=True)
    title = escape(published.snapshot_name or "Artifact", quote=True)
    body = f'<iframe sandbox="" srcdoc="{escaped_srcdoc}"></iframe>'
    page = _PAGE_TEMPLATE.format(title=title, body=body)
    return Response(
        content=page,
        media_type="text/html; charset=utf-8",
        headers=_page_headers(),
    )


async def _render_text_page(
    publish_id: str,
    published: PublishedArtifact,
    service,
) -> Response:
    """Render plain text snapshot as escaped text nodes in <pre>."""
    try:
        data, _ = await service.get_published_content(publish_id)
    except (PublishedArtifactNotFoundError, ArtifactContentUnavailableError):
        return _not_found_response()

    text = data.decode("utf-8", errors="replace")
    # Escape & < > so no HTML injection is possible. Quotes are safe in
    # text content but we escape them too for defense in depth.
    escaped_text = escape(text, quote=True)
    title = escape(published.snapshot_name or "Artifact", quote=True)
    body = f"<pre>{escaped_text}</pre>"
    page = _PAGE_TEMPLATE.format(title=title, body=body)
    return Response(
        content=page,
        media_type="text/html; charset=utf-8",
        headers=_page_headers(),
    )


def _render_binary_page(
    publish_id: str,
    published: PublishedArtifact,
) -> Response:
    """Render binary snapshot page with a controlled /content URL.

    Does NOT read content bytes -- only references /p/{publish_id}/content.
    The publish_id is already regex-validated (URL-safe chars only).
    """
    name = escape(published.snapshot_name or "Artifact", quote=True)
    # publish_id contains only [A-Za-z0-9_-] (regex-validated), safe in URL.
    body = (
        f'<p>Published content: '
        f'<a href="/p/{publish_id}/content" download>{name}</a></p>'
    )
    title = name
    page = _PAGE_TEMPLATE.format(title=title, body=body)
    return Response(
        content=page,
        media_type="text/html; charset=utf-8",
        headers=_page_headers(),
    )


# ---------------------------------------------------------------------------
# Content response builder
# ---------------------------------------------------------------------------


def _build_content_response(
    data: bytes,
    published: PublishedArtifact,
) -> Response:
    """Build the /content response with security headers.

    HTML content uses Content-Disposition: attachment to prevent
    same-origin rendering. Other types use inline.
    """
    mime = published.snapshot_mime or "application/octet-stream"
    content_type = _content_type_with_charset(mime)
    base_mime = mime.lower().split(";")[0].strip()
    is_html = base_mime == "text/html"
    disposition = "attachment" if is_html else "inline"
    filename = _sanitize_filename(published.snapshot_name or "published")
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": _CONTENT_CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        },
    )


# ---------------------------------------------------------------------------
# Error responses (same security headers, no internal detail leak)
# ---------------------------------------------------------------------------


def _not_found_response(is_content: bool = False) -> Response:
    """404 response with no-cache and security headers. No enumeration leak."""
    return Response(
        status_code=404,
        headers=_security_headers(is_content),
    )


def _gone_response(is_content: bool = False) -> Response:
    """410 response for revoked publishes. No-cache so revoke takes effect."""
    return Response(
        status_code=410,
        headers=_security_headers(is_content),
    )


def _internal_error_response(is_content: bool = False) -> Response:
    """500 response for internal errors. No internal detail leak."""
    return Response(
        status_code=500,
        headers=_security_headers(is_content),
    )


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def _page_headers() -> dict[str, str]:
    """Security headers for active page (200) responses."""
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": _PAGE_CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def _security_headers(is_content: bool = False) -> dict[str, str]:
    """Security headers for all responses (including 404/410/500)."""
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": _CONTENT_CSP if is_content else _PAGE_CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def _content_type_with_charset(mime: str) -> str:
    """Add charset=utf-8 for text MIME types."""
    base = (mime or "application/octet-stream").split(";")[0].strip().lower()
    if (
        base.startswith("text/")
        or base in ("application/json", "application/xml", "application/javascript")
    ):
        return f"{base}; charset=utf-8"
    return base


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename for Content-Disposition (remove path/quote chars)."""
    safe = filename.replace("/", "_").replace("\\", "_").replace("\x00", "")
    safe = safe.replace('"', "").replace("\r", "").replace("\n", "")
    if not safe:
        safe = "published"
    return safe
