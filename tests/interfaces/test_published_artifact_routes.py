"""T13: Public published-artifact route tests.

Tests the public unauthenticated routes GET /p/{publish_id} (page) and
GET /p/{publish_id}/content (content) using a fake service and TestClient.

Security coverage:
- publish_id regex validation (^[A-Za-z0-9_-]{22,64}$)
- invalid/not-found -> 404 (no enumeration leak)
- active -> 200; revoked -> 410; page and /content CONSISTENT
- all responses Cache-Control: no-store
- active page/content have CSP, X-Content-Type-Options: nosniff,
  Referrer-Policy: no-referrer
- renderer: fixed server-side page template; markdown uses safe
  convert_to_html; HTML snapshot only as escaped sandbox="" iframe srcdoc
  (NO allow-* permissions); plain text uses text nodes; binary uses
  controlled content URL + download fallback
- malicious quotes, </iframe>, <script>, event attributes CANNOT escape
  the template or obtain allow-* sandbox permissions
- tests do NOT trigger source artifact registry/store (only published-
  snapshot read path)
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.artifact import (
    ArtifactContentUnavailableError,
    ArtifactKind,
    PublishedArtifact,
    PublishedArtifactNotFoundError,
    PublishedArtifactStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# Valid publish_id: 22 URL-safe chars (base64url, no padding).
_VALID_PUBLISH_ID = "ABCDEFGHIJKLMNOPQRSTUVabcdefghij"  # 32 chars


def _make_published(
    publish_id: str = _VALID_PUBLISH_ID,
    artifact_id: str = "art-1",
    name: str = "test.md",
    kind: ArtifactKind = ArtifactKind.MARKDOWN,
    mime: str = "text/markdown",
    inline_content: str = "# Hello",
    content_ref: str | None = None,
    status: PublishedArtifactStatus = PublishedArtifactStatus.ACTIVE,
    summary: str = "",
) -> PublishedArtifact:
    data = inline_content.encode("utf-8")
    return PublishedArtifact(
        publish_id=publish_id,
        artifact_id=artifact_id,
        snapshot_name=name,
        snapshot_kind=kind,
        snapshot_mime=mime,
        snapshot_content_ref=content_ref,
        snapshot_inline_content=inline_content if inline_content else None,
        snapshot_size=len(data) if inline_content else 0,
        snapshot_checksum=_sha256(data) if inline_content else _sha256(b""),
        snapshot_summary=summary,
        published_at=datetime.now(timezone.utc),
        published_by="dashboard",
        status=status,
        revoked_at=datetime.now(timezone.utc) if status == PublishedArtifactStatus.REVOKED else None,
    )


# ---------------------------------------------------------------------------
# Fake service -- ONLY publish read methods, NO source artifact methods
# ---------------------------------------------------------------------------


class FakePublishedService:
    """Fake service that ONLY supports published-artifact read methods.

    Deliberately does NOT implement get_artifact, get_content, list_artifacts,
    etc. If the route calls any source-artifact method, the test will fail
    with AttributeError, proving no source read occurs.
    """

    def __init__(self) -> None:
        self._published: dict[str, PublishedArtifact] = {}
        self._file_content: dict[str, bytes] = {}  # publish_id -> bytes
        self.get_published_calls: list[str] = []
        self.get_published_content_calls: list[str] = []
        self.convert_markdown_calls: list[str] = []

    def add(self, published: PublishedArtifact, file_content: bytes | None = None) -> None:
        self._published[published.publish_id] = published
        if file_content is not None:
            self._file_content[published.publish_id] = file_content

    async def get_published(self, publish_id: str) -> PublishedArtifact:
        self.get_published_calls.append(publish_id)
        pub = self._published.get(publish_id)
        if pub is None:
            raise PublishedArtifactNotFoundError(
                f"published artifact not found: {publish_id}"
            )
        return pub

    async def get_published_content(
        self, publish_id: str,
    ) -> tuple[bytes, PublishedArtifact]:
        self.get_published_content_calls.append(publish_id)
        pub = self._published.get(publish_id)
        if pub is None:
            raise PublishedArtifactNotFoundError(
                f"published artifact not found: {publish_id}"
            )
        if pub.snapshot_inline_content is not None:
            return pub.snapshot_inline_content.encode("utf-8"), pub
        if pub.snapshot_content_ref is None:
            raise ArtifactContentUnavailableError(
                f"published artifact has no content: {publish_id}"
            )
        data = self._file_content.get(publish_id, b"")
        return data, pub

    def convert_markdown_to_html(self, content: str) -> str:
        self.convert_markdown_calls.append(content)
        from app.infrastructure.artifact.export_converter import convert_to_html
        return convert_to_html(content)


# ---------------------------------------------------------------------------
# Test client setup
# ---------------------------------------------------------------------------


def _make_app(service: FakePublishedService) -> FastAPI:
    from app.interfaces.http.published_artifact_routes import (
        register_published_artifact_routes,
    )
    app = FastAPI()
    register_published_artifact_routes(app, service)
    return app


def _client(service: FakePublishedService) -> TestClient:
    return TestClient(_make_app(service))


# ---------------------------------------------------------------------------
# publish_id regex validation tests
# ---------------------------------------------------------------------------


class TestPublishIdValidation:
    """publish_id must match ^[A-Za-z0-9_-]{22,64}$; invalid -> 404."""

    def test_valid_22_chars_passes_regex(self):
        service = FakePublishedService()
        pid = "a" * 22  # minimum valid length
        service.add(_make_published(publish_id=pid))
        client = _client(service)
        resp = client.get(f"/p/{pid}")
        assert resp.status_code == 200

    def test_valid_64_chars_passes_regex(self):
        service = FakePublishedService()
        pid = "a" * 64  # maximum valid length
        service.add(_make_published(publish_id=pid))
        client = _client(service)
        resp = client.get(f"/p/{pid}")
        assert resp.status_code == 200

    def test_too_short_returns_404(self):
        service = FakePublishedService()
        pid = "a" * 21  # 21 chars: below minimum
        service.add(_make_published(publish_id=pid))
        client = _client(service)
        resp = client.get(f"/p/{pid}")
        assert resp.status_code == 404

    def test_too_long_returns_404(self):
        service = FakePublishedService()
        pid = "a" * 65  # 65 chars: above maximum
        service.add(_make_published(publish_id=pid))
        client = _client(service)
        resp = client.get(f"/p/{pid}")
        assert resp.status_code == 404

    def test_slash_returns_404(self):
        service = FakePublishedService()
        pid = "a" * 20 + "/x"  # contains slash
        client = _client(service)
        resp = client.get(f"/p/{pid}")
        assert resp.status_code == 404

    def test_dot_returns_404(self):
        service = FakePublishedService()
        pid = "a" * 20 + ".xy"  # contains dot
        client = _client(service)
        resp = client.get(f"/p/{pid}")
        assert resp.status_code == 404

    def test_plus_returns_404(self):
        service = FakePublishedService()
        pid = "a" * 20 + "+xy"  # contains plus
        client = _client(service)
        resp = client.get(f"/p/{pid}")
        assert resp.status_code == 404

    def test_content_endpoint_too_short_returns_404(self):
        service = FakePublishedService()
        pid = "a" * 21
        client = _client(service)
        resp = client.get(f"/p/{pid}/content")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Status consistency tests
# ---------------------------------------------------------------------------


class TestStatusConsistency:
    """active -> 200; revoked -> 410; not-found -> 404; page and /content consistent."""

    def test_active_page_200(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.ACTIVE))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200

    def test_active_content_200(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.ACTIVE))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.status_code == 200

    def test_revoked_page_410(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.REVOKED))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 410

    def test_revoked_content_410(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.REVOKED))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.status_code == 410

    def test_not_found_page_404(self):
        service = FakePublishedService()
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 404

    def test_not_found_content_404(self):
        service = FakePublishedService()
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.status_code == 404

    def test_revoked_page_and_content_both_410(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.REVOKED))
        client = _client(service)
        page_resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        content_resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert page_resp.status_code == 410
        assert content_resp.status_code == 410

    def test_active_page_and_content_both_200(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.ACTIVE))
        client = _client(service)
        page_resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        content_resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert page_resp.status_code == 200
        assert content_resp.status_code == 200

    def test_invalid_format_same_as_not_found(self):
        """Invalid format and not-found both return 404 (no enumeration leak)."""
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.ACTIVE))
        client = _client(service)
        # Invalid format
        resp_invalid = client.get("/p/short")
        # Not found (valid format, doesn't exist)
        resp_notfound = client.get("/p/NotExist00000000000000000011")
        assert resp_invalid.status_code == 404
        assert resp_notfound.status_code == 404


# ---------------------------------------------------------------------------
# Security headers tests
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    """All responses: Cache-Control: no-store. Active: CSP, nosniff, no-referrer."""

    def test_active_page_has_no_store(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.headers.get("cache-control") == "no-store"

    def test_active_content_has_no_store(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.headers.get("cache-control") == "no-store"

    def test_active_page_has_csp(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "script-src" not in csp or "'none'" in csp

    def test_active_page_has_nosniff(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_active_page_has_no_referrer(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.headers.get("referrer-policy") == "no-referrer"

    def test_active_content_has_csp(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp

    def test_active_content_has_nosniff(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_active_content_has_no_referrer(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.headers.get("referrer-policy") == "no-referrer"

    def test_404_has_no_store(self):
        service = FakePublishedService()
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.headers.get("cache-control") == "no-store"

    def test_410_has_no_store(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.REVOKED))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.headers.get("cache-control") == "no-store"

    def test_404_content_has_no_store(self):
        service = FakePublishedService()
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.headers.get("cache-control") == "no-store"

    def test_410_content_has_no_store(self):
        service = FakePublishedService()
        service.add(_make_published(status=PublishedArtifactStatus.REVOKED))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Renderer security tests
# ---------------------------------------------------------------------------


class TestRendererMarkdown:
    """Markdown uses safe convert_to_html result."""

    def test_markdown_page_contains_safe_html(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            inline_content="# Hello World",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert "<h1>Hello World</h1>" in resp.text

    def test_markdown_uses_service_converter(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.MARKDOWN,
            inline_content="# Test",
        ))
        client = _client(service)
        client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert len(service.convert_markdown_calls) > 0

    def test_markdown_script_tag_sanitized(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.MARKDOWN,
            inline_content="<script>alert(1)</script>",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # The script tag must NOT appear as a live script in the output
        assert "<script>" not in resp.text

    def test_markdown_event_handler_sanitized(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.MARKDOWN,
            inline_content='<img src=x onerror="alert(1)">',
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # onerror must be stripped
        assert "onerror" not in resp.text


class TestRendererHtmlSnapshot:
    """HTML snapshot only as escaped sandbox='' iframe srcdoc."""

    def test_html_page_uses_iframe(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            mime="text/html",
            inline_content="<p>HTML content</p>",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert "<iframe" in resp.text

    def test_html_iframe_has_empty_sandbox(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content="<p>test</p>",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert 'sandbox=""' in resp.text

    def test_html_iframe_no_allow_permissions(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content="<p>test</p>",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # NO allow-scripts, allow-same-origin, allow-forms, allow-popups,
        # allow-top-navigation
        assert "allow-scripts" not in resp.text
        assert "allow-same-origin" not in resp.text
        assert "allow-forms" not in resp.text
        assert "allow-popups" not in resp.text
        assert "allow-top-navigation" not in resp.text

    def test_html_iframe_uses_srcdoc(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content="<p>test</p>",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert "srcdoc=" in resp.text

    def test_malicious_quotes_cannot_escape_srcdoc(self):
        malicious_html = '<p>"</p><img src=x onerror="alert(1)">'
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content=malicious_html,
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # Find the iframe srcdoc attribute value.
        iframe_match = re.search(r'<iframe[^>]*srcdoc="([^"]*)"', resp.text)
        assert iframe_match is not None, "iframe with srcdoc not found"
        srcdoc_value = iframe_match.group(1)
        # Double quotes in the content must be escaped to &quot; so they
        # cannot break out of the srcdoc attribute.
        assert "&quot;" in srcdoc_value
        # No raw unescaped " in the srcdoc value (would break the attribute
        # and allow attribute injection).
        assert '"' not in srcdoc_value

    def test_closing_iframe_tag_cannot_escape(self):
        malicious_html = "</iframe><script>alert(1)</script>"
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content=malicious_html,
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # The </iframe> in the content must not close the actual iframe
        # It should be escaped or contained within the srcdoc attribute
        # Count iframe tags: should be exactly 1 opening and 1 closing
        open_count = resp.text.count("<iframe")
        close_count = resp.text.count("</iframe>")
        assert open_count == 1, f"expected 1 <iframe, got {open_count}"
        assert close_count == 1, f"expected 1 </iframe>, got {close_count}"

    def test_script_tag_in_html_snapshot_is_inert(self):
        """Script in HTML snapshot is in srcdoc + sandbox="" (no execution)."""
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content="<script>alert('xss')</script>",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # The iframe must have sandbox="" (no allow-scripts)
        assert 'sandbox=""' in resp.text
        assert "allow-scripts" not in resp.text

    def test_event_attributes_in_html_snapshot_are_inert(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content='<div onclick="alert(1)">click</div>',
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # sandbox="" prevents script execution even if onclick is present
        assert 'sandbox=""' in resp.text

    def test_html_snapshot_appears_in_srcdoc_escaped(self):
        html_content = '<p>Hello & "World"</p>'
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content=html_content,
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # The & must be escaped to &amp; and " to &quot; in the srcdoc
        assert "&amp;" in resp.text or "&quot;" in resp.text

    def test_no_allow_permissions_even_with_malicious_content(self):
        """Malicious content cannot obtain allow-* sandbox permissions."""
        malicious = 'allow-scripts allow-same-origin" sandbox="allow-scripts"'
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.HTML,
            inline_content=malicious,
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # Extract the iframe opening tag.
        iframe_match = re.search(r'<iframe\s+([^>]*?)>', resp.text)
        assert iframe_match is not None, "iframe tag not found"
        attrs_str = iframe_match.group(1)
        # sandbox must be empty string (no allow-* permissions).
        assert 'sandbox=""' in attrs_str
        # The only real attributes should be sandbox and srcdoc.
        # Since " in content is escaped to &quot;, the malicious " cannot
        # break out of the srcdoc attribute to inject new attributes.
        # Extract attribute names: word chars followed by =" (a real
        # attribute delimiter, not &quot; inside the value).
        attr_names = re.findall(r'(\w+)="', attrs_str)
        assert set(attr_names) == {"sandbox", "srcdoc"}, \
            f"unexpected attributes on iframe: {attr_names}"


class TestRendererPlainText:
    """Plain text uses text nodes (escaped)."""

    def test_text_page_uses_pre_tag(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.TEXT,
            mime="text/plain",
            inline_content="Hello plain text",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert "<pre>" in resp.text
        assert "Hello plain text" in resp.text

    def test_text_escapes_html(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.TEXT,
            inline_content="<script>alert(1)</script>",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # Script tag must be escaped
        assert "<script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

    def test_code_escapes_html(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.CODE,
            mime="text/plain",
            inline_content='print("<img src=x>")',
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert "<img src=x>" not in resp.text
        assert "&lt;img" in resp.text

    def test_json_escapes_html(self):
        service = FakePublishedService()
        service.add(_make_published(
            kind=ArtifactKind.JSON,
            mime="application/json",
            inline_content='{"key": "<b>value</b>"}',
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert "<b>value</b>" not in resp.text


class TestRendererBinary:
    """Binary uses controlled content URL + download fallback."""

    def test_binary_page_has_content_link(self):
        service = FakePublishedService()
        binary_data = b"\x89PNG fake png data"
        pub = _make_published(
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            inline_content="",
            content_ref="item:pub-1",
        )
        service.add(pub, file_content=binary_data)
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # Must reference /content endpoint
        assert f"/p/{_VALID_PUBLISH_ID}/content" in resp.text

    def test_binary_page_does_not_embed_content(self):
        service = FakePublishedService()
        binary_data = b"\x89PNG fake png data"
        pub = _make_published(
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            inline_content="",
            content_ref="item:pub-1",
        )
        service.add(pub, file_content=binary_data)
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        # Binary content must NOT be embedded in the page HTML
        assert "fake png data" not in resp.text

    def test_pdf_page_has_content_link(self):
        service = FakePublishedService()
        pub = _make_published(
            kind=ArtifactKind.PDF,
            mime="application/pdf",
            inline_content="",
            content_ref="item:pub-pdf",
        )
        service.add(pub, file_content=b"%PDF-1.4 fake")
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert f"/p/{_VALID_PUBLISH_ID}/content" in resp.text

    def test_binary_content_endpoint_returns_bytes(self):
        service = FakePublishedService()
        binary_data = b"\x89PNG fake png data"
        pub = _make_published(
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            inline_content="",
            content_ref="item:pub-1",
        )
        service.add(pub, file_content=binary_data)
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.status_code == 200
        assert resp.content == binary_data

    def test_binary_content_has_correct_mime(self):
        service = FakePublishedService()
        binary_data = b"\x89PNG fake png data"
        pub = _make_published(
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            inline_content="",
            content_ref="item:pub-1",
        )
        service.add(pub, file_content=binary_data)
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.status_code == 200
        assert "image/png" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# No source read tests
# ---------------------------------------------------------------------------


class TestNoSourceRead:
    """Route must NOT read source artifact registry/store."""

    def test_page_does_not_call_source_methods(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        client.get(f"/p/{_VALID_PUBLISH_ID}")
        # FakePublishedService has NO get_artifact/get_content methods.
        # If the route called them, it would raise AttributeError.
        # The request succeeded, so no source methods were called.
        assert len(service.get_published_calls) > 0
        assert len(service.get_published_content_calls) >= 0  # may or may not

    def test_content_does_not_call_source_methods(self):
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert len(service.get_published_content_calls) == 1

    def test_binary_page_does_not_read_content(self):
        """Binary page should NOT read content bytes (only metadata needed)."""
        service = FakePublishedService()
        pub = _make_published(
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            inline_content="",
            content_ref="item:pub-1",
        )
        service.add(pub, file_content=b"binary data")
        client = _client(service)
        client.get(f"/p/{_VALID_PUBLISH_ID}")
        # Page for binary should NOT call get_published_content
        assert len(service.get_published_content_calls) == 0

    def test_no_host_trust(self):
        """Route must NOT use Host/X-Forwarded-Host headers for share_url."""
        service = FakePublishedService()
        service.add(_make_published())
        client = _client(service)
        # Send a malicious Host header
        resp = client.get(
            f"/p/{_VALID_PUBLISH_ID}",
            headers={"Host": "evil.com", "X-Forwarded-Host": "evil.com"},
        )
        assert resp.status_code == 200
        # The response must NOT contain the malicious host
        assert "evil.com" not in resp.text

    def test_no_internal_ref_leak_in_page(self):
        """Page response must NOT leak internal refs (artifact_id, content_ref, etc.)."""
        service = FakePublishedService()
        service.add(_make_published(
            artifact_id="secret-artifact-id-12345",
            content_ref=None,
            inline_content="# Hello",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}")
        assert resp.status_code == 200
        assert "secret-artifact-id-12345" not in resp.text
        assert "content_ref" not in resp.text
        assert "artifact_id" not in resp.text

    def test_no_internal_ref_leak_in_content(self):
        """Content response headers must NOT leak internal refs."""
        service = FakePublishedService()
        service.add(_make_published(
            artifact_id="secret-artifact-id-12345",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.status_code == 200
        # Check no internal refs in headers
        for header_value in resp.headers.values():
            assert "secret-artifact-id-12345" not in header_value


# ---------------------------------------------------------------------------
# Unicode (non-ASCII) snapshot name tests
# ---------------------------------------------------------------------------


class TestUnicodeFilename:
    """Non-ASCII snapshot names must not crash the content endpoint.

    HTTP headers are latin-1; the legacy ``filename`` parameter must stay
    ASCII-only while the real name is carried via the RFC 5987 ``filename*``
    parameter. Bug: ``_sanitize_filename`` left the non-ASCII name in
    ``filename="..."`` (and emitted no ``filename*``) -> UnicodeEncodeError
    -> HTTP 500."""

    def test_content_unicode_filename_does_not_500(self):
        service = FakePublishedService()
        service.add(_make_published(
            name="横向-邮箱归属.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            inline_content="# 横向-邮箱归属\n",
        ))
        client = _client(service)
        resp = client.get(f"/p/{_VALID_PUBLISH_ID}/content")
        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        # RFC 5987 UTF-8 form carries the real (non-ASCII) name.
        assert "filename*=UTF-8''" in cd
        assert quote("横向-邮箱归属.md", safe="") in cd
        # Legacy filename must be latin-1 encodable so the header builds.
        legacy = cd.split('filename="', 1)[1].split('"', 1)[0]
        legacy.encode("latin-1")  # must not raise
