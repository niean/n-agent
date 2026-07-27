from __future__ import annotations

import io
import os
import secrets

import pytest
from PIL import Image

from app.infrastructure.browser.screenshot_store import SqliteBrowserScreenshotStore


def _make_png(width: int = 10, height: int = 10) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg(width: int = 10, height: int = 10) -> bytes:
    img = Image.new("RGB", (width, height), color=(0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# persist + read round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_png_and_read_back(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    data = _make_png(20, 20)
    ref = await store.persist("sess-1", data, "image/png")

    assert ref is not None
    assert "/" not in ref
    read_back = await store.read(ref)
    assert read_back == data


@pytest.mark.asyncio
async def test_persist_jpeg_and_read_back(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    data = _make_jpeg(15, 15)
    ref = await store.persist("sess-1", data, "image/jpeg")
    assert await store.read(ref) == data


# ---------------------------------------------------------------------------
# magic bytes validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_invalid_png_magic(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    with pytest.raises(ValueError, match="magic"):
        await store.persist("sess-1", b"not-a-png-file-at-all", "image/png")


@pytest.mark.asyncio
async def test_reject_invalid_jpeg_magic(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    with pytest.raises(ValueError, match="magic"):
        await store.persist("sess-1", b"not-a-jpeg", "image/jpeg")


@pytest.mark.asyncio
async def test_reject_unsupported_content_type(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    with pytest.raises(ValueError, match="content type"):
        await store.persist("sess-1", b"gif89a", "image/gif")


# ---------------------------------------------------------------------------
# Pillow decode validation (pixel bombs / decode failures)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_pixel_bomb(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots", max_pixels=10_000)
    # 200x200 = 40_000 pixels > 10_000 limit
    data = _make_png(200, 200)
    with pytest.raises(ValueError, match="pixel"):
        await store.persist("sess-1", data, "image/png")


@pytest.mark.asyncio
async def test_reject_corrupt_image_data(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    # Valid PNG magic but truncated/corrupt data
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    with pytest.raises(ValueError, match="decode"):
        await store.persist("sess-1", corrupt, "image/png")


@pytest.mark.asyncio
async def test_accept_image_at_pixel_limit(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots", max_pixels=10_000)
    # 100x100 = 10_000 pixels == limit -> accepted
    data = _make_png(100, 100)
    ref = await store.persist("sess-1", data, "image/png")
    assert await store.read(ref) == data


# ---------------------------------------------------------------------------
# Unpredictable ref
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refs_are_unpredictable(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    data = _make_png(5, 5)
    refs = {await store.persist("sess-1", data, "image/png") for _ in range(10)}
    assert len(refs) == 10  # all unique


@pytest.mark.asyncio
async def test_ref_is_url_safe(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    ref = await store.persist("sess-1", _make_png(), "image/png")
    # secrets.token_urlsafe produces only [-_A-Za-z0-9]
    assert all(c.isalnum() or c in "-_" for c in ref)


# ---------------------------------------------------------------------------
# Path traversal / symlink rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_rejects_path_traversal(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    assert await store.read("../../../etc/passwd") is None
    assert await store.read("..%2f..%2fetc") is None
    assert await store.read("") is None


@pytest.mark.asyncio
async def test_read_rejects_symlink(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    data = _make_png()
    ref = await store.persist("sess-1", data, "image/png")
    # Create a symlink that points to the real file
    screenshots_dir = tmp_path / "shots" / "screenshots"
    link_path = screenshots_dir / "evil-link"
    os.symlink(screenshots_dir / ref, link_path)
    # Reading via symlink name should be refused
    assert await store.read("evil-link") is None


@pytest.mark.asyncio
async def test_read_returns_none_for_missing(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    assert await store.read(secrets.token_urlsafe(16)) is None


# ---------------------------------------------------------------------------
# Per-session quota
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_per_session_quota_evicts_oldest(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots", max_per_session=3)
    data = _make_png()
    refs = []
    for i in range(4):
        ref = await store.persist("sess-1", data, "image/png")
        refs.append(ref)
    # Only 3 should remain; the first (oldest) should be evicted
    assert await store.read(refs[0]) is None
    assert await store.read(refs[1]) is not None
    assert await store.read(refs[2]) is not None
    assert await store.read(refs[3]) is not None


@pytest.mark.asyncio
async def test_quota_is_per_session(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots", max_per_session=2)
    data = _make_png()
    r1 = await store.persist("sess-1", data, "image/png")
    r2 = await store.persist("sess-2", data, "image/png")
    r3 = await store.persist("sess-1", data, "image/png")
    r4 = await store.persist("sess-2", data, "image/png")
    # Both sessions have 2 screenshots; no eviction yet
    assert await store.read(r1) is not None
    assert await store.read(r2) is not None


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_expires_screenshots(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots", ttl_seconds=0)
    data = _make_png()
    ref = await store.persist("sess-1", data, "image/png")
    # With ttl=0, the screenshot is immediately expired
    expired = await store.cleanup_expired()
    assert expired >= 1
    assert await store.read(ref) is None


@pytest.mark.asyncio
async def test_ttl_none_keeps_screenshots(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots", ttl_seconds=None)
    data = _make_png()
    ref = await store.persist("sess-1", data, "image/png")
    await store.cleanup_expired()
    assert await store.read(ref) is not None


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_session_removes_all_screenshots(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    data = _make_png()
    r1 = await store.persist("sess-1", data, "image/png")
    r2 = await store.persist("sess-1", data, "image/png")
    await store.persist("sess-2", data, "image/png")

    await store.delete_session("sess-1")
    assert await store.read(r1) is None
    assert await store.read(r2) is None


@pytest.mark.asyncio
async def test_delete_session_idempotent(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    await store.delete_session("nope")  # no error


# ---------------------------------------------------------------------------
# Metadata persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metadata_records_session_and_content_type(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    data = _make_png()
    ref = await store.persist("sess-42", data, "image/png")
    meta = await store.get_metadata(ref)
    assert meta is not None
    assert meta["session_id"] == "sess-42"
    assert meta["content_type"] == "image/png"
    assert meta["size_bytes"] == len(data)


@pytest.mark.asyncio
async def test_metadata_returns_none_for_missing(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    assert await store.get_metadata("nonexistent") is None


@pytest.mark.asyncio
async def test_list_session_refs(tmp_path):
    store = SqliteBrowserScreenshotStore(tmp_path / "shots")
    data = _make_png()
    r1 = await store.persist("sess-1", data, "image/png")
    r2 = await store.persist("sess-1", data, "image/png")
    r3 = await store.persist("sess-2", data, "image/png")
    refs = await store.list_session_refs("sess-1")
    assert set(refs) == {r1, r2}
