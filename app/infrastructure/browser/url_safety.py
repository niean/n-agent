"""Async URL and network target safety verifier for the browser domain.

Validates that URLs are safe for browser navigation:
- Only http/https schemes allowed
- Rejects userinfo, non-http(s) schemes, loopback, link-local,
  RFC1918, IPv6 ULA, and cloud metadata IPs/hostnames
- Rejects mixed public/private DNS resolution
- Per-redirect re-validation hook
- DNS rebinding defense (resolve before connect, verify post-connect peer)
- Sanitized audit URL production (strip userinfo/query/fragment)

Uses stdlib ``ipaddress`` and ``socket.getaddrinfo`` only.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from typing import Callable, Sequence

ResolverFn = Callable[[str], "list[ipaddress.IPv4Address | ipaddress.IPv6Address]"]

_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

# RFC 2544 benchmark range (198.18.0.0/15) is used by some proxy tools
# (e.g. Clash fake-ip) to resolve public domain names. Allowed (mirrors
# web_fetch's allow_benchmark) so public domains routed via such proxies are
# not false-rejected. CGNAT (100.64.0.0/10) is blocked like web_fetch.
_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

_CLOUD_METADATA_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.169.253"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
})


class UrlSafetyError(PermissionError):
    """Raised when a URL or network target fails safety verification."""


def _default_resolver(
    hostname: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        addr_info = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise UrlSafetyError(
            f"hostname could not be resolved: {hostname}"
        ) from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _, _, _, sockaddr in addr_info:
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise UrlSafetyError(
            f"hostname resolved to no addresses: {hostname}"
        )
    return addresses


def _is_cloud_metadata_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return addr in _CLOUD_METADATA_IPS


def _is_unsafe_address(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Check if an IP address is private, loopback, link-local, reserved, etc."""
    if addr in _BENCHMARK_NETWORK:
        # Proxy fake-ip for public domains (allowed, consistent with web_fetch).
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    if addr in _CGNAT_NETWORK:
        return True
    return False


class UrlVerifier:
    """Async URL and network target safety verifier.

    Call ``verify_url`` before navigating. Call ``verify_redirect`` on each
    redirect. Call ``resolve_and_verify`` + ``verify_peer_address`` to defend
    against DNS rebinding at the socket layer.
    """

    def __init__(self, *, resolver: ResolverFn | None = None) -> None:
        self._resolver: ResolverFn = resolver or _default_resolver

    # -- URL verification ---------------------------------------------------

    async def verify_url(self, url: str) -> str:
        """Verify a URL is safe for navigation.

        Returns the original URL if safe. Raises ``UrlSafetyError`` if
        the scheme, hostname, userinfo, or resolved addresses are unsafe.
        """
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")

        if scheme not in ("http", "https"):
            raise UrlSafetyError(
                f"only http and https URLs are allowed: {scheme or '(none)'}"
            )
        if not hostname:
            raise UrlSafetyError("URL hostname is required")
        if parsed.username or parsed.password:
            raise UrlSafetyError("userinfo in URL is not allowed")
        if hostname in _BLOCKED_HOSTNAMES:
            raise UrlSafetyError(
                f"URL targets a blocked metadata hostname: {hostname}"
            )

        await self._resolve_and_verify_addresses(hostname)
        return url

    async def verify_redirect(self, new_url: str) -> str:
        """Re-validate a redirect URL (per-redirect re-validation hook)."""
        return await self.verify_url(new_url)

    # -- DNS resolution + verification -------------------------------------

    async def resolve_and_verify(self, hostname: str) -> list[str]:
        """Resolve *hostname* and verify all addresses are safe.

        Returns a list of safe IP address strings (for post-connect
        peer verification). Raises ``UrlSafetyError`` if any address is
        unsafe or resolution fails.
        """
        addresses = await self._resolve_and_verify_addresses(hostname)
        return [str(addr) for addr in addresses]

    async def _resolve_and_verify_addresses(
        self, hostname: str
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        # IP literal: no DNS resolution needed.
        try:
            literal = ipaddress.ip_address(hostname)
            addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [literal]
        except ValueError:
            addresses = await asyncio.to_thread(self._resolver, hostname)

        has_public = False
        has_private = False
        for addr in addresses:
            if _is_cloud_metadata_ip(addr):
                raise UrlSafetyError(
                    f"URL targets a cloud metadata address: {addr}"
                )
            if _is_unsafe_address(addr):
                has_private = True
            else:
                has_public = True

        if has_private and has_public:
            raise UrlSafetyError(
                "mixed public/private resolution is not allowed"
            )
        if has_private:
            raise UrlSafetyError(
                "URL targets a private or internal network address"
            )
        return addresses

    # -- DNS rebinding defense ---------------------------------------------

    def verify_peer_address(
        self, peer_ip: str, resolved: Sequence[str]
    ) -> None:
        """Verify that the connected peer IP matches one of the
        pre-resolved addresses.

        Call after establishing the TCP connection to detect DNS
        rebinding. Raises ``UrlSafetyError`` on mismatch (fail-closed).
        """
        try:
            peer = ipaddress.ip_address(peer_ip)
        except ValueError as exc:
            raise UrlSafetyError(
                f"invalid peer address: {peer_ip}"
            ) from exc
        resolved_set: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for ip_str in resolved:
            try:
                resolved_set.add(ipaddress.ip_address(ip_str))
            except ValueError:
                continue
        if peer not in resolved_set:
            raise UrlSafetyError(
                f"DNS rebinding detected: peer {peer_ip} does not match "
                f"resolved addresses {list(resolved)}"
            )

    # -- Audit URL sanitization --------------------------------------------

    @staticmethod
    def sanitize_url(url: str) -> str:
        """Produce a sanitized audit URL.

        Strips userinfo, query string, and fragment. Keeps scheme,
        host, port, and path.
        """
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
        if ":" in host:  # IPv6 literal
            if port is not None:
                netloc = f"[{host}]:{port}"
            else:
                netloc = f"[{host}]"
        else:
            if host and port is not None:
                netloc = f"{host}:{port}"
            else:
                netloc = host
        return urllib.parse.urlunsplit(
            (parsed.scheme, netloc, parsed.path, "", "")
        )
