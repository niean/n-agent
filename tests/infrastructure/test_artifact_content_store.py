from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.domain.artifact import (
    ArtifactContentUnavailableError,
    ArtifactSource,
    ArtifactValidationError,
)
from app.infrastructure.artifact.content_store import LocalArtifactContentStore


def _make_store(tmp_path: Path, **kwargs) -> LocalArtifactContentStore:
    root = tmp_path / "artifacts"
    att_root = tmp_path / "attachments"
    ws_root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    att_root.mkdir(parents=True, exist_ok=True)
    ws_root.mkdir(parents=True, exist_ok=True)
    defaults = dict(max_bytes=1024, publish_max_bytes=512, inline_max_bytes=256)
    defaults.update(kwargs)
    return LocalArtifactContentStore(root, att_root, ws_root, **defaults)


# ---------------------------------------------------------------------------
# Owned content tests
# ---------------------------------------------------------------------------


async def test_write_uses_server_generated_disk_filename_not_client(tmp_path):
    store = _make_store(tmp_path)
    data = b"hello world"
    ref = await store.write_atomic("art-1", "../../etc/passwd", data)
    assert ref.startswith("item:art-1/")
    server_name = ref.split("/", 1)[1]
    # disk name is server-generated, not the client-supplied malicious path
    assert server_name != "../../etc/passwd"
    assert "/" not in server_name
    assert ".." not in server_name
    disk_path = store._root / "items" / "art-1" / server_name
    assert disk_path.is_file()
    assert disk_path.read_bytes() == data


async def test_write_atomic_leaves_no_temp_residue(tmp_path):
    store = _make_store(tmp_path)
    data = b"atomic payload"
    ref = await store.write_atomic("art-2", "report.txt", data)
    art_dir = store._root / "items" / "art-2"
    leftover = [
        p.name for p in art_dir.iterdir()
        if p.name.startswith(".__tmp")
    ]
    assert leftover == []
    files = [p for p in art_dir.iterdir() if not p.name.startswith(".")]
    assert len(files) == 1
    assert files[0].read_bytes() == data


async def test_write_read_roundtrip(tmp_path):
    store = _make_store(tmp_path, max_bytes=4096)
    data = b"checksum me" * 100
    ref = await store.write_atomic("art-3", "data.bin", data)
    result = await store.read(ref, max_bytes=8192)
    assert result == data


async def test_write_over_limit_aborts_with_no_temp_residue(tmp_path):
    store = _make_store(tmp_path, max_bytes=512)
    data = b"x" * 2048
    with pytest.raises(ArtifactValidationError):
        await store.write_atomic("art-4", "big.bin", data)
    art_dir = store._root / "items" / "art-4"
    if art_dir.exists():
        leftover = list(art_dir.iterdir())
        assert leftover == [], f"temp residue left behind: {leftover}"


async def test_read_enforces_max_bytes_before_returning(tmp_path):
    store = _make_store(tmp_path)
    data = b"x" * 1000
    ref = await store.write_atomic("art-5", "file.bin", data)
    with pytest.raises(ArtifactValidationError):
        await store.read(ref, max_bytes=500)
    result = await store.read(ref, max_bytes=2000)
    assert result == data


async def test_read_missing_content_raises_unavailable(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ArtifactContentUnavailableError):
        await store.read("item:art-x/nonexistent.bin", max_bytes=1024)


async def test_items_and_published_refs_are_isolated(tmp_path):
    store = _make_store(tmp_path)
    data = b"owned content"
    item_ref = await store.write_atomic("art-6", "doc.txt", data)
    pub_ref = await store.copy_to_publish_snapshot(item_ref, "pub-1")
    assert item_ref.startswith("item:")
    assert pub_ref.startswith("published:")
    assert await store.read(item_ref, max_bytes=1024) == data
    assert await store.read(pub_ref, max_bytes=1024) == data
    # deleting item does not affect published copy
    assert await store.delete_owned(item_ref) is True
    with pytest.raises(ArtifactContentUnavailableError):
        await store.read(item_ref, max_bytes=1024)
    assert await store.read(pub_ref, max_bytes=1024) == data


async def test_delete_owned_rejects_attachment_and_workspace_refs(tmp_path):
    store = _make_store(tmp_path)
    att_dir = store._attachments_root / "task-1"
    att_dir.mkdir(parents=True)
    (att_dir / "att_1.bin").write_bytes(b"attachment data")
    (store._workspace_root / "ws.txt").write_bytes(b"workspace data")
    with pytest.raises(ArtifactValidationError):
        await store.delete_owned("attachment:task-1/att_1.bin")
    with pytest.raises(ArtifactValidationError):
        await store.delete_owned("workspace:ws.txt")
    # source files must still exist (not deleted)
    assert (att_dir / "att_1.bin").is_file()
    assert (store._workspace_root / "ws.txt").is_file()


async def test_delete_owned_deletes_item_and_published(tmp_path):
    store = _make_store(tmp_path)
    data = b"to be deleted"
    item_ref = await store.write_atomic("art-7", "f.txt", data)
    pub_ref = await store.copy_to_publish_snapshot(item_ref, "pub-2")
    assert await store.delete_owned(item_ref) is True
    assert await store.delete_owned(pub_ref) is True
    # deleting again returns False (already gone)
    assert await store.delete_owned(item_ref) is False


async def test_delete_publish_snapshot_removes_dir_and_is_idempotent(tmp_path):
    store = _make_store(tmp_path)
    item_ref = await store.write_atomic("art-ps", "f.txt", b"snapshot data")
    await store.copy_to_publish_snapshot(item_ref, "pub-ps")
    pub_dir = store._root / "published" / "pub-ps"
    assert pub_dir.is_dir()
    # delete removes the snapshot file + its directory
    await store.delete_publish_snapshot("pub-ps")
    assert not pub_dir.exists()
    # idempotent: second call on the now-gone dir is a no-op (no error)
    await store.delete_publish_snapshot("pub-ps")
    # a publish_id that was never materialized (inline publish) is a no-op
    await store.delete_publish_snapshot("pub-never")


async def test_delete_owned_rejects_unknown_scheme(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ArtifactValidationError):
        await store.delete_owned("unknown:foo/bar")


async def test_delete_owned_returns_false_when_parent_dir_gone(tmp_path):
    store = _make_store(tmp_path)
    ref = await store.write_atomic("art-gone", "f.txt", b"data")
    # remove the entire artifact directory to simulate cleanup
    import shutil
    shutil.rmtree(store._root / "items" / "art-gone")
    assert await store.delete_owned(ref) is False


# ---------------------------------------------------------------------------
# Source/path security tests
# ---------------------------------------------------------------------------


async def test_attachment_resolver_rejects_client_supplied_paths(tmp_path):
    store = _make_store(tmp_path)
    bad_refs = [
        "attachment:task-1/../../etc/passwd",
        "attachment:task-1/.env",
        "attachment:/etc/passwd",
        "attachment:task-1/",
        "attachment:task-1/.",
        "attachment:task-1/..",
        "attachment:task-1/normal\\evil",
        "attachment:task-1/a/b",
        "unknownscheme:foo/bar",
        "",
    ]
    for bad in bad_refs:
        with pytest.raises(
            (ArtifactValidationError, ArtifactContentUnavailableError)
        ):
            await store.read(bad, max_bytes=1024)


async def test_workspace_only_accepts_workspace_relative_scheme(tmp_path):
    store = _make_store(tmp_path)
    bad_refs = [
        "workspace:/abs/path",
        "workspace:",
        "workspace:.",
        "workspace:..",
        "workspace:../../etc/passwd",
        "workspace:dir/../..",
        "workspace:foo\\bar",
        "workspace:dir/",
        "unknown:x",
    ]
    for bad in bad_refs:
        with pytest.raises(
            (ArtifactValidationError, ArtifactContentUnavailableError)
        ):
            await store.read(bad, max_bytes=1024)


async def test_workspace_rejects_parent_component_symlink(tmp_path):
    store = _make_store(tmp_path)
    evil_dir = tmp_path / "evil_target"
    evil_dir.mkdir()
    (evil_dir / "secret.txt").write_bytes(b"secret")
    link = store._workspace_root / "reports"
    link.symlink_to(evil_dir)
    with pytest.raises(
        (ArtifactValidationError, ArtifactContentUnavailableError)
    ):
        await store.read("workspace:reports/secret.txt", max_bytes=1024)


async def test_workspace_rejects_leaf_symlink(tmp_path):
    store = _make_store(tmp_path)
    target = tmp_path / "evil_secret.txt"
    target.write_bytes(b"secret")
    link = store._workspace_root / "file.txt"
    link.symlink_to(target)
    with pytest.raises(
        (ArtifactValidationError, ArtifactContentUnavailableError)
    ):
        await store.read("workspace:file.txt", max_bytes=1024)


async def test_attachment_rejects_symlinked_task_dir(tmp_path):
    store = _make_store(tmp_path)
    evil = tmp_path / "evil_att"
    evil.mkdir()
    (evil / "passwd").write_bytes(b"root:x:0:0")
    (store._attachments_root / "task-1").symlink_to(evil)
    with pytest.raises(
        (ArtifactValidationError, ArtifactContentUnavailableError)
    ):
        await store.read("attachment:task-1/passwd", max_bytes=1024)


async def test_attachment_rejects_leaf_symlink(tmp_path):
    store = _make_store(tmp_path)
    evil = tmp_path / "evil_leaf"
    evil.write_bytes(b"stolen")
    att_dir = store._attachments_root / "task-1"
    att_dir.mkdir(parents=True)
    (att_dir / "stored.bin").symlink_to(evil)
    with pytest.raises(
        (ArtifactValidationError, ArtifactContentUnavailableError)
    ):
        await store.read("attachment:task-1/stored.bin", max_bytes=1024)


async def test_materialize_source_does_not_change_source_file(tmp_path):
    store = _make_store(tmp_path, max_bytes=4096)
    att_dir = store._attachments_root / "task-1"
    att_dir.mkdir(parents=True)
    src_data = b"original attachment content"
    (att_dir / "src_001.bin").write_bytes(src_data)
    ref = await store.materialize_source(
        ArtifactSource.TASK_ATTACHMENT,
        "attachment:task-1/src_001.bin",
        "art-8",
    )
    assert ref.startswith("item:art-8/")
    # source file unchanged
    assert (att_dir / "src_001.bin").read_bytes() == src_data
    # owned copy has same content
    assert await store.read(ref, max_bytes=4096) == src_data


async def test_materialize_workspace_source(tmp_path):
    store = _make_store(tmp_path, max_bytes=4096)
    src_data = b"workspace output"
    sub = store._workspace_root / "outputs"
    sub.mkdir()
    (sub / "result.txt").write_bytes(src_data)
    ref = await store.materialize_source(
        ArtifactSource.TASK_ARTIFACT,
        "workspace:outputs/result.txt",
        "art-9",
    )
    assert ref.startswith("item:art-9/")
    assert await store.read(ref, max_bytes=4096) == src_data
    assert (sub / "result.txt").read_bytes() == src_data


async def test_materialize_attachment_rejects_symlink(tmp_path):
    store = _make_store(tmp_path, max_bytes=4096)
    evil = tmp_path / "evil_att_src"
    evil.write_bytes(b"bad")
    att_dir = store._attachments_root / "task-1"
    att_dir.mkdir(parents=True)
    (att_dir / "link.bin").symlink_to(evil)
    with pytest.raises(
        (ArtifactValidationError, ArtifactContentUnavailableError)
    ):
        await store.materialize_source(
            ArtifactSource.TASK_ATTACHMENT,
            "attachment:task-1/link.bin",
            "art-x",
        )


async def test_copy_to_publish_snapshot_survives_source_deletion(tmp_path):
    store = _make_store(tmp_path)
    data = b"publishable content"
    item_ref = await store.write_atomic("art-10", "doc.txt", data)
    pub_ref = await store.copy_to_publish_snapshot(item_ref, "pub-3")
    await store.delete_owned(item_ref)
    assert await store.read(pub_ref, max_bytes=1024) == data


async def test_copy_to_publish_snapshot_inline(tmp_path):
    store = _make_store(tmp_path, inline_max_bytes=256)
    text = "hello inline snapshot"
    ref = await store.copy_to_publish_snapshot("", "pub-4", inline=text)
    assert ref.startswith("published:pub-4/")
    result = await store.read(ref, max_bytes=1024)
    assert result.decode("utf-8") == text


async def test_copy_to_publish_inline_over_limit(tmp_path):
    store = _make_store(tmp_path, inline_max_bytes=10)
    text = "x" * 100
    with pytest.raises(ArtifactValidationError):
        await store.copy_to_publish_snapshot("", "pub-5", inline=text)


async def test_publish_copy_over_limit(tmp_path):
    store = _make_store(tmp_path, max_bytes=1024, publish_max_bytes=100)
    data = b"x" * 500
    item_ref = await store.write_atomic("art-11", "f.bin", data)
    with pytest.raises(ArtifactValidationError):
        await store.copy_to_publish_snapshot(item_ref, "pub-6")


async def test_malicious_display_filename_does_not_affect_disk_path(tmp_path):
    store = _make_store(tmp_path)
    malicious_names = [
        "../../etc/passwd",
        "foo/bar/baz",
        "con.txt",
        "",
    ]
    for name in malicious_names:
        ref = await store.write_atomic("art-12", name, b"data")
        server_name = ref.split("/", 1)[1]
        assert "/" not in server_name
        assert ".." not in server_name
        disk = store._root / "items" / "art-12" / server_name
        assert disk.is_file()


async def test_write_preserves_safe_extension(tmp_path):
    store = _make_store(tmp_path)
    ref = await store.write_atomic("art-13", "report.pdf", b"pdf data")
    server_name = ref.split("/", 1)[1]
    assert server_name.endswith(".pdf")


async def test_read_attachment_source(tmp_path):
    store = _make_store(tmp_path)
    att_dir = store._attachments_root / "task-1"
    att_dir.mkdir(parents=True)
    (att_dir / "data.bin").write_bytes(b"attachment bytes")
    result = await store.read("attachment:task-1/data.bin", max_bytes=1024)
    assert result == b"attachment bytes"


async def test_read_attachment_source_accepts_unicode_filename(tmp_path):
    """Non-ASCII (e.g. Chinese) stored_names must be readable.

    TaskService upload preserves the original filename in stored_name
    (``f"{uuid_hex}_{filename}"``) and accepts Unicode via its denylist
    validator. The content_ref built from such a stored_name must parse
    so the attachment can register as an artifact. Bug: the ASCII-only
    _FILENAME_RE rejected non-ASCII stored_names, silently breaking
    artifact registration for Chinese-named attachments.
    """
    store = _make_store(tmp_path)
    att_dir = store._attachments_root / "task-1"
    att_dir.mkdir(parents=True)
    stored_name = "2f361f8023564333_横向-邮箱归属.md"
    payload = "# 横向-邮箱归属\n".encode("utf-8")
    (att_dir / stored_name).write_bytes(payload)
    result = await store.read(
        f"attachment:task-1/{stored_name}", max_bytes=1024
    )
    assert result == payload


async def test_attachment_ref_still_rejects_traversal_chars(tmp_path):
    """Allowing Unicode letters must not weaken path-traversal defense:
    separators, backslash, control chars, and exact dot/dotdot stay rejected."""
    store = _make_store(tmp_path)
    bad_refs = [
        "attachment:task-1/foo/bar.md",   # nested path in filename
        "attachment:task-1/foo\\bar.md",   # backslash separator
        "attachment:task-1/\x00bad.md",    # NUL control char
        "attachment:task-1/..",            # parent-dir reference
        "attachment:task-1/.",             # current-dir reference
    ]
    for ref in bad_refs:
        with pytest.raises(ArtifactValidationError):
            await store.read(ref, max_bytes=1024)


async def test_read_workspace_source(tmp_path):
    store = _make_store(tmp_path)
    (store._workspace_root / "notes.md").write_bytes(b"workspace notes")
    result = await store.read("workspace:notes.md", max_bytes=1024)
    assert result == b"workspace notes"


async def test_read_rejects_unknown_scheme(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ArtifactValidationError):
        await store.read("ftp://evil.com/file", max_bytes=1024)
