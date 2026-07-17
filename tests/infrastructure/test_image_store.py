from __future__ import annotations

import httpx
import pytest

from app.infrastructure.image_store import LocalImageStore


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_persist_downloads_and_returns_serve_url(tmp_path):
    payload = b"\xff\xd8\xff\xe0fakejpegbytes"

    def handler(request):
        return httpx.Response(200, content=payload, headers={"content-type": "image/jpeg"})

    store = LocalImageStore(tmp_path / "images", "http://localhost:8201", transport=_transport(handler))
    url = await store.persist("https://oss.example.com/photo.jpg?sig=x")

    assert url is not None
    assert url.startswith("http://localhost:8201/chat/images/")
    assert url.endswith(".jpg")
    image_id = url.rsplit("/", 1)[-1]
    path = store.resolve(image_id)
    assert path is not None
    assert path.read_bytes() == payload


@pytest.mark.asyncio
async def test_persist_uses_mime_hint_extension(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"pngbytes", headers={"content-type": "text/plain"})

    store = LocalImageStore(tmp_path / "images", "http://localhost:8201", transport=_transport(handler))
    url = await store.persist("https://oss.example.com/p", mime_hint="image/png")
    assert url is not None and url.endswith(".png")


@pytest.mark.asyncio
async def test_persist_returns_none_on_http_error(tmp_path):
    def handler(request):
        return httpx.Response(403, content=b"forbidden")

    store = LocalImageStore(tmp_path / "images", "http://localhost:8201", transport=_transport(handler))
    assert await store.persist("https://oss.example.com/photo.jpg?sig=x") is None


@pytest.mark.asyncio
async def test_persist_returns_none_when_oversize_and_leaves_no_partial(tmp_path):
    big = b"x" * 2048

    def handler(request):
        return httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})

    store = LocalImageStore(
        tmp_path / "images", "http://localhost:8201", transport=_transport(handler), max_bytes=512
    )
    assert await store.persist("https://oss.example.com/photo.jpg?sig=x") is None
    assert list((tmp_path / "images").iterdir()) == []


@pytest.mark.asyncio
async def test_persist_returns_none_on_network_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    store = LocalImageStore(tmp_path / "images", "http://localhost:8201", transport=_transport(handler))
    assert await store.persist("https://oss.example.com/photo.jpg?sig=x") is None


@pytest.mark.asyncio
async def test_persist_strips_trailing_slash_from_base_url(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"})

    store = LocalImageStore(tmp_path / "images", "http://localhost:8201/", transport=_transport(handler))
    url = await store.persist("https://oss.example.com/photo.jpg?sig=x")
    assert url is not None
    assert "//chat/images" not in url


def test_resolve_rejects_unsafe_names(tmp_path):
    store = LocalImageStore(tmp_path / "images", "http://localhost:8201")
    assert store.resolve("../etc/passwd") is None
    assert store.resolve("not-a-valid-name") is None
    # valid pattern but file does not exist
    assert store.resolve("abc123.jpg") is None
    assert store.resolve("abc123.png") is None


def test_resolve_returns_path_for_stored_file(tmp_path):
    store = LocalImageStore(tmp_path / "images", "http://localhost:8201")
    image_id = "deadbeef.jpg"
    (tmp_path / "images" / image_id).write_bytes(b"img")
    assert store.resolve(image_id) == tmp_path / "images" / image_id


def test_media_type_by_extension(tmp_path):
    store = LocalImageStore(tmp_path / "images", "http://localhost:8201")
    assert store.media_type("abc123.jpg") == "image/jpeg"
    assert store.media_type("abc123.png") == "image/png"
    assert store.media_type("abc123.gif") == "image/gif"
    assert store.media_type("abc123.webp") == "image/webp"
    assert store.media_type("abc123.unknown") == "image/jpeg"
