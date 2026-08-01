from __future__ import annotations

import httpx
import pytest

from app.infrastructure.browser.novnc_proxy import BrowserNoVncProxy


@pytest.mark.asyncio
async def test_fetch_proxies_only_the_requested_novnc_asset_and_safe_query():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://browser:6080/vnc.html?autoconnect=true&resize=scale"
        )
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "cache-control": "no-cache",
                "set-cookie": "must-not-leak=1",
            },
            content=b"<html>noVNC</html>",
        )

    proxy = BrowserNoVncProxy(
        "http://browser:6080",
        http_transport=httpx.MockTransport(handler),
    )

    status, headers, body = await proxy.fetch(
        "vnc.html", "cap=secret&autoconnect=true&resize=scale"
    )

    assert status == 200
    assert headers == {
        "content-type": "text/html",
        "cache-control": "no-cache",
    }
    assert body == b"<html>noVNC</html>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset_path",
    ["", "/vnc.html", "../secret", "app/../secret", "app\\ui.js", "x\x00y"],
)
async def test_fetch_rejects_unsafe_asset_paths(asset_path):
    proxy = BrowserNoVncProxy("http://browser:6080")

    with pytest.raises(ValueError, match="novnc_proxy_path_invalid"):
        await proxy.fetch(asset_path, "")


@pytest.mark.asyncio
async def test_fetch_rejects_oversized_upstream_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 17)

    proxy = BrowserNoVncProxy(
        "http://browser:6080",
        max_http_response_bytes=16,
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="novnc_proxy_response_too_large"):
        await proxy.fetch("vnc.html", "")
