from __future__ import annotations

import html
import re
from collections.abc import Iterable
from urllib.parse import quote_plus

from app.domain.sandbox import SearchResult, SearchProvider


_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = "Mozilla/5.0 (compatible; n-agent-sandbox-websearch/1.0)"
_DEFAULT_TIMEOUT = 10
_MAX_RESPONSE_BYTES = 200_000  # 200 KB hard cap — stream read, reject on exceed


class DuckDuckGoHtmlSearchProvider(SearchProvider):
    """Stream HTML scraper for DuckDuckGo (no API key required).

    Reads the response in chunks and aborts once `_MAX_RESPONSE_BYTES` is
    exceeded (rejecting rather than truncating, so a malicious/huge response
    cannot smuggle partial HTML past the parser).
    """

    name = "duckduckgo"

    def __init__(self, timeout_seconds: int = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout_seconds

    def is_available(self) -> bool:
        # No external dependency beyond stdlib + network; treat as available.
        return True

    async def search(self, query: str, top_k: int) -> list[SearchResult]:
        try:
            import httpx
        except ImportError:
            return []
        params = {"q": query, "kl": "us-en"}
        headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                async with client.stream("GET", _DDG_HTML_URL, params=params, headers=headers) as resp:
                    body = await self._read_capped(resp.iter_bytes())
        except Exception:
            return []
        if body is None:
            return []
        return self._parse(body)[:top_k]

    async def _read_capped(self, chunks: Iterable[bytes]) -> bytes | None:
        buf = bytearray()
        for chunk in chunks:
            buf.extend(chunk)
            if len(buf) > _MAX_RESPONSE_BYTES:
                return None  # reject oversized responses
        return bytes(buf)

    @staticmethod
    def _parse(html_text: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        # DuckDuckGo HTML results: <a class="result__a" href="...">title</a>
        # followed by <a class="result__snippet" ...>snippet</a>
        link_re = re.compile(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        snippet_re = re.compile(
            r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        links = link_re.findall(html_text)
        snippets = snippet_re.findall(html_text)
        for idx, (raw_url, raw_title) in enumerate(links):
            url = _extract_ddg_url(raw_url)
            if not url:
                continue
            title = _strip_tags(raw_title).strip()
            snippet = _strip_tags(snippets[idx]).strip() if idx < len(snippets) else ""
            if not title:
                continue
            results.append(SearchResult(title=title, url=url, snippet=snippet))
        return results


def _extract_ddg_url(raw: str) -> str:
    # DuckDuckGo redirects via /l/?uddg=<encoded>; extract the uddg param
    if "uddg=" in raw:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [None])[0]
        if uddg:
            return uddg
    return raw


def _strip_tags(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", "", text)
    return html.unescape(no_tags)
