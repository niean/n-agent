from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest

from app.infrastructure.browser.url_safety import UrlSafetyError, UrlVerifier


# ---------------------------------------------------------------------------
# Helpers: controlled DNS mocks
# ---------------------------------------------------------------------------

def _mock_resolver_factory(ips: list[str]):
    """Return a getaddrinfo replacement that resolves any host to *ips*."""
    def _mock(host, port, *args, **kwargs):
        results = []
        for ip in ips:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
            sockaddr = (ip, 0) if family == socket.AF_INET else (ip, 0, 0, 0)
            results.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
        return results
    return _mock


# ---------------------------------------------------------------------------
# Scheme validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allows_http(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    url = await verifier.verify_url("http://example.com/path")
    assert url == "http://example.com/path"


@pytest.mark.asyncio
async def test_allows_https(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    url = await verifier.verify_url("https://example.com/path")
    assert url == "https://example.com/path"


@pytest.mark.asyncio
async def test_rejects_file_scheme():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="http"):
        await verifier.verify_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_rejects_data_scheme():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="http"):
        await verifier.verify_url("data:text/html,<script>alert(1)</script>")


@pytest.mark.asyncio
async def test_rejects_javascript_scheme():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="http"):
        await verifier.verify_url("javascript:alert(1)")


@pytest.mark.asyncio
async def test_rejects_ftp_scheme():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="http"):
        await verifier.verify_url("ftp://example.com/file")


@pytest.mark.asyncio
async def test_rejects_empty_hostname():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="hostname"):
        await verifier.verify_url("http:///path")


# ---------------------------------------------------------------------------
# Userinfo rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_userinfo_in_url(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="userinfo"):
        await verifier.verify_url("http://user:pass@example.com/path")


@pytest.mark.asyncio
async def test_rejects_username_only(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="userinfo"):
        await verifier.verify_url("http://user@example.com/path")


# ---------------------------------------------------------------------------
# IP literal rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_loopback_ipv4_literal():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal|loopback"):
        await verifier.verify_url("http://127.0.0.1/path")


@pytest.mark.asyncio
async def test_rejects_loopback_ipv6_literal():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal|loopback"):
        await verifier.verify_url("http://[::1]/path")


@pytest.mark.asyncio
async def test_rejects_rfc1918_10(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["10.0.0.1"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal"):
        await verifier.verify_url("http://internal.example.com/path")


@pytest.mark.asyncio
async def test_rejects_rfc1918_172(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["172.16.0.1"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal"):
        await verifier.verify_url("http://internal.example.com/path")


@pytest.mark.asyncio
async def test_rejects_rfc1918_192(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["192.168.1.1"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal"):
        await verifier.verify_url("http://internal.example.com/path")


@pytest.mark.asyncio
async def test_rejects_ipv6_ula(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["fc00::1"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal"):
        await verifier.verify_url("http://internal.example.com/path")


@pytest.mark.asyncio
async def test_rejects_link_local_ipv4(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["169.254.1.1"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal|link"):
        await verifier.verify_url("http://internal.example.com/path")


@pytest.mark.asyncio
async def test_rejects_link_local_ipv6(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["fe80::1"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal|link"):
        await verifier.verify_url("http://internal.example.com/path")


# ---------------------------------------------------------------------------
# Cloud metadata rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_cloud_metadata_ip_literal():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="metadata"):
        await verifier.verify_url("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_rejects_cloud_metadata_hostname(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="metadata|blocked"):
        await verifier.verify_url("http://metadata.google.internal/computeMetadata/")


@pytest.mark.asyncio
async def test_rejects_cloud_metadata_hostname_goog(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="metadata|blocked"):
        await verifier.verify_url("http://metadata.goog/computeMetadata/")


# ---------------------------------------------------------------------------
# Mixed public/private resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_mixed_resolution(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _mock_resolver_factory(["93.184.216.34", "10.0.0.1"]),
    )
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="mixed"):
        await verifier.verify_url("http://example.com/path")


# ---------------------------------------------------------------------------
# Hostname resolves to private IP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_hostname_resolving_to_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["10.0.0.5"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError, match="private|internal"):
        await verifier.verify_url("http://internal.corp/path")


@pytest.mark.asyncio
async def test_allows_hostname_resolving_to_public(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    url = await verifier.verify_url("http://example.com/path")
    assert url == "http://example.com/path"


# ---------------------------------------------------------------------------
# Custom resolver injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_custom_resolver_injection():
    def resolver(hostname: str):
        return [ipaddress.ip_address("93.184.216.34")]
    verifier = UrlVerifier(resolver=resolver)
    url = await verifier.verify_url("http://example.com/path")
    assert url == "http://example.com/path"


# ---------------------------------------------------------------------------
# Per-redirect re-validation hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_redirect_revalidates(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    url = await verifier.verify_redirect("http://example.com/redirected")
    assert url == "http://example.com/redirected"


@pytest.mark.asyncio
async def test_verify_redirect_rejects_unsafe(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError):
        await verifier.verify_redirect("http://169.254.169.254/")


# ---------------------------------------------------------------------------
# DNS rebinding defense
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_and_verify_returns_safe_ips(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    resolved = await verifier.resolve_and_verify("example.com")
    assert resolved == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_verify_peer_address_passes_on_match(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    resolved = await verifier.resolve_and_verify("example.com")
    verifier.verify_peer_address("93.184.216.34", resolved)  # should not raise


@pytest.mark.asyncio
async def test_verify_peer_address_fails_on_mismatch(monkeypatch):
    """DNS rebinding: resolved public IP, but peer connects from private."""
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    resolved = await verifier.resolve_and_verify("example.com")
    with pytest.raises(UrlSafetyError, match="rebinding|mismatch|peer"):
        verifier.verify_peer_address("10.0.0.1", resolved)


@pytest.mark.asyncio
async def test_verify_peer_address_fails_on_invalid_peer():
    verifier = UrlVerifier()
    with pytest.raises(UrlSafetyError):
        verifier.verify_peer_address("not-an-ip", ["1.2.3.4"])


# ---------------------------------------------------------------------------
# sanitize_url
# ---------------------------------------------------------------------------

def test_sanitize_url_strips_userinfo_query_fragment():
    verifier = UrlVerifier()
    sanitized = verifier.sanitize_url("http://user:pass@example.com/path?q=1&r=2#section")
    assert sanitized == "http://example.com/path"


def test_sanitize_url_keeps_port():
    verifier = UrlVerifier()
    sanitized = verifier.sanitize_url("http://user:pass@example.com:8080/path?q=1#f")
    assert sanitized == "http://example.com:8080/path"


def test_sanitize_url_keeps_https_scheme():
    verifier = UrlVerifier()
    sanitized = verifier.sanitize_url("https://user:pass@example.com/secure?t=token#top")
    assert sanitized == "https://example.com/secure"


def test_sanitize_url_no_query_or_fragment():
    verifier = UrlVerifier()
    sanitized = verifier.sanitize_url("http://example.com/path")
    assert sanitized == "http://example.com/path"


# ---------------------------------------------------------------------------
# Port handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allows_custom_port(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolver_factory(["93.184.216.34"]))
    verifier = UrlVerifier()
    url = await verifier.verify_url("http://example.com:8080/path")
    assert url == "http://example.com:8080/path"
