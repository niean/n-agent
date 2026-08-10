"""Tests for the Artifact domain model, value objects, ports, and errors.

Pure domain tests -- no IO, no FastAPI, no SQLite. Validates the aggregate
root invariants (text/binary XOR, UTF-8 size, checksum format, manual
source_ref), PublishedArtifact immutability, public-view leakage guard,
value-object instantiation, and Protocol fake completeness.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from app.domain.artifact import (
    Artifact,
    ArtifactAttachmentSource,
    ArtifactConflictError,
    ArtifactContentStore,
    ArtifactContentUnavailableError,
    ArtifactDeleteGraph,
    ArtifactError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactListPage,
    ArtifactNotFoundError,
    ArtifactRegistry,
    ArtifactRevision,
    ArtifactSource,
    ArtifactStatus,
    ArtifactValidationError,
    PublishedArtifact,
    PublishedArtifactNotFoundError,
    PublishedArtifactStatus,
    RevisionListCursor,
    RevisionListPage,
)


_VALID_CHECKSUM = "sha256:" + "a" * 64
_VALID_PUB_CHECKSUM = "sha256:" + "b" * 64

_TEXT_KINDS = (
    ArtifactKind.DOCUMENT,
    ArtifactKind.MARKDOWN,
    ArtifactKind.CODE,
    ArtifactKind.HTML,
    ArtifactKind.DATA,
    ArtifactKind.CSV,
    ArtifactKind.JSON,
    ArtifactKind.TEXT,
)
_BINARY_KINDS = (ArtifactKind.IMAGE, ArtifactKind.PDF, ArtifactKind.OTHER)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_artifact(**overrides) -> Artifact:
    """Build a valid inline text artifact (manual source, source_ref=id)."""
    fields: dict = dict(
        id="art-1",
        name="doc.txt",
        kind=ArtifactKind.TEXT,
        mime="text/plain",
        content_ref=None,
        inline_content="hello",
        size=5,
        checksum=_VALID_CHECKSUM,
        source_kind=ArtifactSource.MANUAL,
        source_context_ref=None,
        summary="a text doc",
        classification=None,
        labels=None,
        status=ArtifactStatus.DRAFT,
        created_at=None,
        updated_at=None,
        created_by="user-1",
    )
    fields.update(overrides)
    # Default source_ref to id when not explicitly provided (manual convention).
    if "source_ref" not in overrides:
        fields["source_ref"] = fields["id"]
    return Artifact(**fields)


def _ref_text_artifact(**overrides) -> Artifact:
    """Build a valid ref-only text artifact."""
    return _text_artifact(
        inline_content=None,
        content_ref="store://bucket/doc.md",
        size=1024,
        **overrides,
    )


def _binary_artifact(**overrides) -> Artifact:
    """Build a valid binary (image) artifact with content_ref."""
    fields: dict = dict(
        id="art-img-1",
        name="logo.png",
        kind=ArtifactKind.IMAGE,
        mime="image/png",
        content_ref="store://bucket/logo.png",
        inline_content=None,
        size=4096,
        checksum=_VALID_CHECKSUM,
        source_kind=ArtifactSource.MANUAL,
        source_context_ref=None,
        summary="a logo",
        classification=None,
        labels=None,
        status=ArtifactStatus.DRAFT,
        created_at=None,
        updated_at=None,
        created_by="user-1",
    )
    fields.update(overrides)
    if "source_ref" not in overrides:
        fields["source_ref"] = fields["id"]
    return Artifact(**fields)


def _published(**overrides) -> PublishedArtifact:
    fields: dict = dict(
        publish_id="pub-1",
        artifact_id="art-1",
        snapshot_name="doc.txt",
        snapshot_kind=ArtifactKind.TEXT,
        snapshot_mime="text/plain",
        snapshot_content_ref="store://pub/doc.txt",
        snapshot_inline_content=None,
        snapshot_size=5,
        snapshot_checksum=_VALID_PUB_CHECKSUM,
        snapshot_summary="published doc",
        published_at=None,
        published_by="user-1",
        status=PublishedArtifactStatus.ACTIVE,
        revoked_at=None,
    )
    fields.update(overrides)
    return PublishedArtifact(**fields)


# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


def test_artifact_kind_enum_values():
    expected = {
        "document", "markdown", "code", "html", "data", "csv", "json",
        "image", "pdf", "text", "other",
    }
    assert {k.value for k in ArtifactKind} == expected


def test_artifact_source_enum_values():
    expected = {"task_attachment", "task_artifact", "session", "manual"}
    assert {k.value for k in ArtifactSource} == expected


def test_artifact_status_enum_values():
    expected = {"draft", "published", "archived"}
    assert {k.value for k in ArtifactStatus} == expected


def test_published_artifact_status_enum_values():
    expected = {"active", "revoked"}
    assert {k.value for k in PublishedArtifactStatus} == expected


# ---------------------------------------------------------------------------
# Text kind: inline_content XOR content_ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", _TEXT_KINDS)
def test_text_kind_with_inline_content_ok(kind):
    art = _text_artifact(kind=kind, inline_content="data", size=4)
    assert art.inline_content == "data"
    assert art.content_ref is None


@pytest.mark.parametrize("kind", _TEXT_KINDS)
def test_text_kind_with_content_ref_ok(kind):
    art = _ref_text_artifact(kind=kind)
    assert art.content_ref is not None
    assert art.inline_content is None


@pytest.mark.parametrize("kind", _TEXT_KINDS)
def test_text_kind_both_set_raises(kind):
    with pytest.raises(ArtifactValidationError):
        _text_artifact(
            kind=kind,
            inline_content="data",
            content_ref="store://ref",
            size=4,
        )


@pytest.mark.parametrize("kind", _TEXT_KINDS)
def test_text_kind_neither_set_raises(kind):
    with pytest.raises(ArtifactValidationError):
        _text_artifact(
            kind=kind,
            inline_content=None,
            content_ref=None,
            size=0,
        )


# ---------------------------------------------------------------------------
# Binary kind: content_ref required, inline_content forbidden
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", _BINARY_KINDS)
def test_binary_kind_with_content_ref_ok(kind):
    art = _binary_artifact(kind=kind)
    assert art.inline_content is None
    assert art.content_ref is not None


@pytest.mark.parametrize("kind", _BINARY_KINDS)
def test_binary_kind_with_inline_content_raises(kind):
    with pytest.raises(ArtifactValidationError):
        _binary_artifact(kind=kind, inline_content="not-bytes")


@pytest.mark.parametrize("kind", _BINARY_KINDS)
def test_binary_kind_without_content_ref_raises(kind):
    with pytest.raises(ArtifactValidationError):
        _binary_artifact(kind=kind, content_ref=None)


@pytest.mark.parametrize("kind", _BINARY_KINDS)
def test_binary_kind_empty_content_ref_raises(kind):
    with pytest.raises(ArtifactValidationError):
        _binary_artifact(kind=kind, content_ref="")


# ---------------------------------------------------------------------------
# UTF-8 byte size
# ---------------------------------------------------------------------------


def test_inline_size_matches_utf8_bytes():
    art = _text_artifact(inline_content="hello", size=5)
    assert art.size == 5


def test_inline_size_multibyte_utf8():
    # "héllo" -> h(1) + é(2) + l(1) + l(1) + o(1) = 6 bytes
    art = _text_artifact(inline_content="héllo", size=6)
    assert art.size == 6


def test_inline_size_chinese_utf8():
    # "你好" -> 3 + 3 = 6 bytes
    art = _text_artifact(inline_content="你好", size=6)
    assert art.size == 6


def test_inline_size_mismatch_raises():
    with pytest.raises(ArtifactValidationError):
        _text_artifact(inline_content="hello", size=99)


def test_inline_empty_string_size_zero():
    art = _text_artifact(inline_content="", size=0)
    assert art.size == 0


# ---------------------------------------------------------------------------
# Checksum format: sha256:<64 lowercase hex>
# ---------------------------------------------------------------------------


def test_valid_checksum_accepted():
    art = _text_artifact(checksum="sha256:" + "f" * 64)
    assert art.checksum.startswith("sha256:")


def test_checksum_missing_prefix_raises():
    with pytest.raises(ArtifactValidationError):
        _text_artifact(checksum="a" * 64)


def test_checksum_wrong_algorithm_raises():
    with pytest.raises(ArtifactValidationError):
        _text_artifact(checksum="md5:" + "a" * 32)


def test_checksum_short_hex_raises():
    with pytest.raises(ArtifactValidationError):
        _text_artifact(checksum="sha256:" + "a" * 63)


def test_checksum_uppercase_hex_raises():
    with pytest.raises(ArtifactValidationError):
        _text_artifact(checksum="sha256:" + "A" * 64)


def test_checksum_non_hex_raises():
    with pytest.raises(ArtifactValidationError):
        _text_artifact(checksum="sha256:" + "z" * 64)


# ---------------------------------------------------------------------------
# Status and time fields
# ---------------------------------------------------------------------------


def test_status_default_is_draft():
    art = _text_artifact()
    assert art.status is ArtifactStatus.DRAFT


def test_status_and_time_fields():
    now = datetime.now(timezone.utc)
    art = _text_artifact(
        status=ArtifactStatus.PUBLISHED,
        created_at=now,
        updated_at=now,
    )
    assert art.status is ArtifactStatus.PUBLISHED
    assert art.created_at == now
    assert art.updated_at == now


def test_all_status_values_accepted():
    for status in ArtifactStatus:
        art = _text_artifact(status=status)
        assert art.status is status


# ---------------------------------------------------------------------------
# PublishedArtifact: immutability, is_active, nullable artifact_id
# ---------------------------------------------------------------------------


def test_published_snapshot_immutable():
    pub = _published()
    with pytest.raises(FrozenInstanceError):
        pub.snapshot_name = "tampered"


def test_published_status_immutable():
    pub = _published()
    with pytest.raises(FrozenInstanceError):
        pub.status = PublishedArtifactStatus.REVOKED


def test_published_is_active_when_active():
    pub = _published(status=PublishedArtifactStatus.ACTIVE)
    assert pub.is_active is True


def test_published_is_not_active_when_revoked():
    pub = _published(status=PublishedArtifactStatus.REVOKED)
    assert pub.is_active is False


def test_published_artifact_id_nullable():
    pub = _published(artifact_id=None)
    assert pub.artifact_id is None


def test_published_snapshot_checksum_validated():
    with pytest.raises(ArtifactValidationError):
        _published(snapshot_checksum="bad")


# ---------------------------------------------------------------------------
# Source ref conventions: manual source_ref=id, task_artifact key format
# ---------------------------------------------------------------------------


def test_manual_source_ref_equals_id():
    art = _text_artifact(id="art-x", source_kind=ArtifactSource.MANUAL)
    assert art.source_ref == "art-x"
    assert art.source_ref == art.id


def test_manual_source_ref_mismatch_raises():
    with pytest.raises(ArtifactValidationError):
        _text_artifact(
            id="art-x",
            source_kind=ArtifactSource.MANUAL,
            source_ref="not-art-x",
        )


def test_task_artifact_source_ref_format():
    ref = Artifact.task_artifact_source_ref("task-42", 3, 1)
    assert ref == "task:task-42:run:3:artifact:1"


def test_task_artifact_source_ref_no_manual_constraint():
    # task_artifact source_ref is NOT constrained to equal id; only manual is.
    ref = Artifact.task_artifact_source_ref("task-9", 1, 0)
    art = _ref_text_artifact(
        id="art-9",
        source_kind=ArtifactSource.TASK_ARTIFACT,
        source_ref=ref,
    )
    assert art.source_ref == ref
    assert art.source_ref != art.id


# ---------------------------------------------------------------------------
# to_public_view: no leakage of internal details
# ---------------------------------------------------------------------------


_LEAKED_KEYS = frozenset({
    "content_ref", "inline_content", "source_ref", "snapshot_content_ref",
})


def test_to_public_view_excludes_sensitive_keys():
    art = _text_artifact()
    view = art.to_public_view()
    for key in _LEAKED_KEYS:
        assert key not in view, f"to_public_view leaked key: {key}"


def test_to_public_view_excludes_for_binary():
    art = _binary_artifact()
    view = art.to_public_view()
    assert "content_ref" not in view
    assert "source_ref" not in view
    assert "inline_content" not in view


def test_to_public_view_includes_safe_fields():
    art = _text_artifact(
        source_context_ref="session:abc",
        source_session_id="task-sess-1",
        classification="internal",
        labels=("doc", "v1"),
    )
    view = art.to_public_view()
    expected_safe = {
        "id", "name", "kind", "mime", "size", "checksum",
        "source_kind", "source_context_ref", "source_session_id", "summary",
        "classification", "labels", "status",
        "created_at", "updated_at", "created_by",
    }
    assert set(view.keys()) == expected_safe
    assert view["id"] == "art-1"
    assert view["source_kind"] == ArtifactSource.MANUAL
    assert view["source_context_ref"] == "session:abc"
    assert view["source_session_id"] == "task-sess-1"
    assert view["labels"] == ("doc", "v1")


def test_to_public_view_no_absolute_paths():
    art = _text_artifact(source_context_ref="session:abc")
    view = art.to_public_view()
    for key, val in view.items():
        if isinstance(val, str):
            assert not val.startswith("/"), f"absolute path in {key}: {val}"


# ---------------------------------------------------------------------------
# Value objects: cursor, page, attachment source
# ---------------------------------------------------------------------------


def test_artifact_list_cursor():
    now = datetime.now(timezone.utc)
    c = ArtifactListCursor(updated_at=now, artifact_id="art-1")
    assert c.updated_at == now
    assert c.artifact_id == "art-1"
    c2 = ArtifactListCursor(updated_at=None, artifact_id="art-2")
    assert c2.updated_at is None


def test_artifact_list_cursor_frozen():
    c = ArtifactListCursor(updated_at=None, artifact_id="art-1")
    with pytest.raises(FrozenInstanceError):
        c.artifact_id = "x"


def test_artifact_list_page():
    art = _text_artifact()
    page = ArtifactListPage(items=(art,), next_cursor=None)
    assert page.items == (art,)
    assert page.next_cursor is None


def test_artifact_list_page_with_cursor():
    art = _text_artifact()
    cursor = ArtifactListCursor(updated_at=None, artifact_id="art-1")
    page = ArtifactListPage(items=(art,), next_cursor=cursor)
    assert page.next_cursor == cursor


def test_artifact_list_page_frozen():
    page = ArtifactListPage(items=(), next_cursor=None)
    with pytest.raises(FrozenInstanceError):
        page.items = ()


def test_artifact_attachment_source():
    now = datetime.now(timezone.utc)
    src = ArtifactAttachmentSource(
        attachment_id="att-1",
        task_id="task-1",
        stored_name="uploads/abc.bin",
        filename="report.pdf",
        content_type="application/pdf",
        size=2048,
        checksum=_VALID_CHECKSUM,
        uploaded_by="user-1",
        created_at=now,
    )
    assert src.attachment_id == "att-1"
    assert src.task_id == "task-1"
    assert src.created_at == now


def test_artifact_attachment_source_frozen():
    src = ArtifactAttachmentSource(
        attachment_id="att-1",
        task_id="task-1",
        stored_name="uploads/abc.bin",
        filename="report.pdf",
        content_type="application/pdf",
        size=2048,
        checksum=_VALID_CHECKSUM,
        uploaded_by="user-1",
    )
    with pytest.raises(FrozenInstanceError):
        src.filename = "x"


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_error_hierarchy():
    assert issubclass(ArtifactNotFoundError, ArtifactError)
    assert issubclass(ArtifactValidationError, ArtifactError)
    assert issubclass(ArtifactContentUnavailableError, ArtifactError)
    assert issubclass(PublishedArtifactNotFoundError, ArtifactError)
    assert issubclass(ArtifactConflictError, ArtifactError)


def test_errors_are_catchable_via_base():
    for exc_cls in (
        ArtifactNotFoundError,
        ArtifactValidationError,
        ArtifactContentUnavailableError,
        PublishedArtifactNotFoundError,
        ArtifactConflictError,
    ):
        try:
            raise exc_cls("test")
        except ArtifactError:
            pass
        else:
            pytest.fail(f"{exc_cls.__name__} not catchable via ArtifactError")


# ---------------------------------------------------------------------------
# Protocol fakes: prevent method omission
# ---------------------------------------------------------------------------


class FakeArtifactRegistry:
    """Minimal in-memory fake satisfying the ArtifactRegistry Protocol."""

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._published: dict[str, PublishedArtifact] = {}
        self._published_by_artifact: dict[str, list[PublishedArtifact]] = {}
        self._attachments: list[ArtifactAttachmentSource] = []

    async def create_artifact(self, artifact: Artifact) -> Artifact:
        self._artifacts[artifact.id] = artifact
        return artifact

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self._artifacts.get(artifact_id)

    async def list_artifacts(
        self,
        *,
        source_kind: ArtifactSource | None = None,
        source_context_ref: str | None = None,
        kind: ArtifactKind | None = None,
        status: ArtifactStatus | None = None,
        q: str | None = None,
        cursor: ArtifactListCursor | None = None,
        limit: int = 50,
    ) -> ArtifactListPage:
        items: list[Artifact] = list(self._artifacts.values())
        if source_kind is not None:
            items = [a for a in items if a.source_kind == source_kind]
        if source_context_ref is not None:
            items = [a for a in items if a.source_context_ref == source_context_ref]
        if kind is not None:
            items = [a for a in items if a.kind == kind]
        if status is not None:
            items = [a for a in items if a.status == status]
        if q is not None:
            items = [a for a in items if q.lower() in a.name.lower()]
        return ArtifactListPage(items=tuple(items[:limit]), next_cursor=None)

    async def update_artifact(self, artifact: Artifact) -> Artifact:
        self._artifacts[artifact.id] = artifact
        return artifact

    async def delete_artifact(self, artifact_id: str) -> bool:
        return self._artifacts.pop(artifact_id, None) is not None

    async def get_by_source(
        self, source_kind: ArtifactSource, source_ref: str,
    ) -> Artifact | None:
        for art in self._artifacts.values():
            if art.source_kind == source_kind and art.source_ref == source_ref:
                return art
        return None

    async def register_published(
        self,
        published: PublishedArtifact,
        *,
        revoke_artifact_id: str | None = None,
    ) -> PublishedArtifact:
        self._published[published.publish_id] = published
        if published.artifact_id is not None:
            self._published_by_artifact.setdefault(
                published.artifact_id, [],
            ).append(published)
        return published

    async def get_published(self, publish_id: str) -> PublishedArtifact | None:
        return self._published.get(publish_id)

    async def get_active_publish(
        self, artifact_id: str,
    ) -> PublishedArtifact | None:
        for pub in self._published_by_artifact.get(artifact_id, []):
            if pub.is_active:
                return pub
        return None

    async def list_published(
        self, artifact_id: str | None = None,
    ) -> tuple[PublishedArtifact, ...]:
        if artifact_id is None:
            return tuple(self._published.values())
        return tuple(self._published_by_artifact.get(artifact_id, []))

    async def revoke_published(
        self, artifact_id: str,
    ) -> PublishedArtifact | None:
        pub = await self.get_active_publish(artifact_id)
        if pub is None:
            return None
        revoked = replace(
            pub,
            status=PublishedArtifactStatus.REVOKED,
            revoked_at=datetime.now(timezone.utc),
        )
        self._published[revoked.publish_id] = revoked
        lst = self._published_by_artifact.get(artifact_id, [])
        for i, p in enumerate(lst):
            if p.publish_id == revoked.publish_id:
                lst[i] = revoked
        return revoked

    async def count_artifacts(self) -> int:
        return len(self._artifacts)

    async def list_attachment_sources(
        self,
        *,
        after_attachment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ArtifactAttachmentSource, ...]:
        return tuple(self._attachments[:limit])


class FakeArtifactContentStore:
    """Minimal in-memory fake satisfying the ArtifactContentStore Protocol."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def read(self, content_ref: str, *, max_bytes: int) -> bytes:
        data = self._blobs.get(content_ref, b"")
        return data[:max_bytes]

    async def write_atomic(
        self, artifact_id: str, filename: str, data: bytes,
    ) -> str:
        ref = f"store://{artifact_id}/{filename}"
        self._blobs[ref] = data
        return ref

    async def delete_owned(self, content_ref: str) -> bool:
        return self._blobs.pop(content_ref, None) is not None

    async def materialize_source(
        self,
        source_kind: ArtifactSource,
        source_ref: str,
        artifact_id: str,
    ) -> str:
        ref = f"materialized://{artifact_id}/{source_ref}"
        self._blobs[ref] = b"\x00"
        return ref

    async def copy_to_publish_snapshot(
        self, src_ref: str, publish_id: str, *, inline: str | None = None,
    ) -> str:
        snap_ref = f"snapshot://{publish_id}"
        self._blobs[snap_ref] = self._blobs.get(src_ref, b"")
        if inline is not None:
            self._blobs[snap_ref] = inline.encode("utf-8")
        return snap_ref


async def test_fake_registry_all_methods_callable():
    reg = FakeArtifactRegistry()
    art = _text_artifact()
    await reg.create_artifact(art)
    assert await reg.get_artifact("art-1") == art
    page = await reg.list_artifacts(limit=10)
    assert page.items == (art,)
    assert await reg.count_artifacts() == 1
    assert await reg.get_by_source(ArtifactSource.MANUAL, "art-1") == art
    await reg.update_artifact(art)
    assert await reg.delete_artifact("art-1") is True
    assert await reg.get_artifact("art-1") is None

    pub = _published()
    await reg.register_published(pub)
    assert await reg.get_published("pub-1") == pub
    assert await reg.get_active_publish("art-1") == pub
    assert await reg.list_published() == (pub,)
    assert await reg.list_published("art-1") == (pub,)
    revoked = await reg.revoke_published("art-1")
    assert revoked is not None
    assert revoked.status is PublishedArtifactStatus.REVOKED
    assert await reg.list_attachment_sources() == ()


async def test_fake_registry_source_context_ref_filters():
    """The Protocol test fake accepts and applies the source_context_ref
    keyword, filtering with == (not (x or '') ==)."""
    reg = FakeArtifactRegistry()
    await reg.create_artifact(_text_artifact(
        id="ctx-a", source_kind=ArtifactSource.SESSION,
        source_ref="session-a:1", source_context_ref="session-a",
    ))
    await reg.create_artifact(_text_artifact(
        id="ctx-b", source_kind=ArtifactSource.SESSION,
        source_ref="session-b:2", source_context_ref="session-b",
    ))
    await reg.create_artifact(_text_artifact(
        id="ctx-null", source_context_ref=None,
    ))
    await reg.create_artifact(_text_artifact(
        id="ctx-empty", source_context_ref="",
    ))

    # Specific value -> only matching record
    page = await reg.list_artifacts(source_context_ref="session-a")
    assert {a.id for a in page.items} == {"ctx-a"}

    # Empty string -> only empty-string record, NOT NULL
    page = await reg.list_artifacts(source_context_ref="")
    assert {a.id for a in page.items} == {"ctx-empty"}

    # Omit -> all records
    page = await reg.list_artifacts()
    assert len(page.items) == 4


async def test_fake_content_store_all_methods_callable():
    store = FakeArtifactContentStore()
    ref = await store.write_atomic("art-1", "doc.txt", b"hello")
    assert isinstance(ref, str)
    data = await store.read(ref, max_bytes=1024)
    assert data == b"hello"
    snap = await store.copy_to_publish_snapshot(ref, "pub-1")
    assert isinstance(snap, str)
    mat = await store.materialize_source(ArtifactSource.MANUAL, "art-1", "art-1")
    assert isinstance(mat, str)
    assert await store.delete_owned(ref) is True
    assert await store.delete_owned(ref) is False


def test_fake_registry_has_all_protocol_methods():
    reg = FakeArtifactRegistry()
    expected = [
        "create_artifact", "get_artifact", "list_artifacts",
        "update_artifact", "delete_artifact", "get_by_source",
        "register_published", "get_published", "get_active_publish",
        "list_published", "revoke_published", "count_artifacts",
        "list_attachment_sources",
    ]
    for name in expected:
        assert hasattr(reg, name), f"FakeArtifactRegistry missing: {name}"
        assert callable(getattr(reg, name)), f"{name} not callable"


def test_fake_content_store_has_all_protocol_methods():
    store = FakeArtifactContentStore()
    expected = [
        "read", "write_atomic", "delete_owned",
        "materialize_source", "copy_to_publish_snapshot",
    ]
    for name in expected:
        assert hasattr(store, name), f"FakeArtifactContentStore missing: {name}"
        assert callable(getattr(store, name)), f"{name} not callable"


# ---------------------------------------------------------------------------
# ArtifactRevision: immutability, content XOR, provenance, parent/rollback
# ---------------------------------------------------------------------------


_REVISION_CHECKSUM = "sha256:" + "0" * 64


def _valid_revision_kwargs() -> dict:
    """Build a valid base kwargs dict for an initial ArtifactRevision (inline content)."""
    return dict(
        id="rev1",
        artifact_id="art1",
        revision_number=1,
        parent_revision_id=None,
        rollback_from_revision_id=None,
        content_ref=None,
        inline_content="# hi",
        size=4,
        checksum=_REVISION_CHECKSUM,
        mime="text/markdown",
        kind=ArtifactKind.MARKDOWN,
        change_summary="init",
        created_by="chat",
        source_session_id="s1",
        source_run_id=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _initial_kwargs() -> dict:
    """Alias: an initial revision has no parent and no rollback."""
    return _valid_revision_kwargs()


def test_artifact_revision_immutable_and_invariants():
    rev = ArtifactRevision(
        id="rev1",
        artifact_id="art1",
        revision_number=1,
        parent_revision_id=None,
        rollback_from_revision_id=None,
        content_ref=None,
        inline_content="# hi",
        size=4,
        checksum="sha256:" + "0" * 64,
        mime="text/markdown",
        kind=ArtifactKind.MARKDOWN,
        change_summary="init",
        created_by="chat",
        source_session_id="s1",
        source_run_id=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert rev.is_initial  # parent_revision_id is None and rollback_from is None
    with pytest.raises(FrozenInstanceError):
        rev.change_summary = "x"


def test_artifact_revision_content_xor():
    # content_ref 与 inline_content 必须恰好一项不为 null；inline 空串合法；二进制只能 content_ref
    with pytest.raises(ArtifactValidationError):
        ArtifactRevision(
            **(_valid_revision_kwargs() | {"content_ref": None, "inline_content": None})
        )
    with pytest.raises(ArtifactValidationError):
        ArtifactRevision(
            **(_valid_revision_kwargs() | {"content_ref": "", "inline_content": None})
        )  # 空 content_ref
    with pytest.raises(ArtifactValidationError):
        ArtifactRevision(
            **(_valid_revision_kwargs() | {
                "content_ref": None, "inline_content": "x",
                "kind": ArtifactKind.IMAGE, "size": 1,
            })
        )  # 二进制用 inline
    # 文本空内容合法
    rev = ArtifactRevision(
        **(_valid_revision_kwargs() | {"inline_content": "", "size": 0})
    )
    assert rev.inline_content == ""
    assert rev.size == 0


def test_artifact_revision_validates_provenance_and_materialized_content():
    # checksum 格式、size/int、mime、UTC aware created_at、持久 ref scheme 均需校验
    for bad in (
        dict(checksum="bad"),
        dict(size=-1),
        dict(mime=""),
        dict(revision_number=0),
        dict(created_at=datetime(2026, 1, 1)),
        dict(created_at=None),
        dict(content_ref="workspace:x", inline_content=None),
        dict(content_ref="attachment:t/f", inline_content=None),
        dict(content_ref="published:p/f", inline_content=None),
        dict(content_ref="item:", inline_content=None),
    ):
        with pytest.raises(ArtifactValidationError):
            ArtifactRevision(**(_valid_revision_kwargs() | bad))


def test_artifact_revision_content_ref_item_scheme_ok():
    """A revision with content_ref using item: scheme and a binary kind is valid."""
    rev = ArtifactRevision(
        **(_valid_revision_kwargs() | {
            "content_ref": "item:art1/f.bin",
            "inline_content": None,
            "kind": ArtifactKind.OTHER,
            "mime": "application/octet-stream",
            "size": 1024,
        })
    )
    assert rev.content_ref == "item:art1/f.bin"
    assert rev.inline_content is None
    assert rev.is_initial


def test_artifact_revision_rollback_valid():
    """A rollback revision with both parent and rollback_from set is valid."""
    rev = ArtifactRevision(
        **(_valid_revision_kwargs() | {
            "revision_number": 3,
            "parent_revision_id": "rev2",
            "rollback_from_revision_id": "rev2",
        })
    )
    assert not rev.is_initial
    assert rev.parent_revision_id == "rev2"
    assert rev.rollback_from_revision_id == "rev2"


def test_artifact_revision_parent_and_rollback_shape():
    assert ArtifactRevision(**_initial_kwargs()).is_initial
    with pytest.raises(ArtifactValidationError):
        ArtifactRevision(
            **(_initial_kwargs() | {"rollback_from_revision_id": "r0"})
        )


def test_artifact_current_revision_id_nullable():
    art = _text_artifact()
    assert art.current_revision_id is None


def test_published_artifact_published_revision_id_nullable():
    pub = _published()
    assert pub.published_revision_id is None


# ---------------------------------------------------------------------------
# ArtifactRegistry Protocol: revision method contracts
# ---------------------------------------------------------------------------


def test_registry_has_revision_methods():
    for name in ("create_artifact_with_initial_revision", "append_revision",
                 "get_revision", "list_revisions", "delete_artifact_graph",
                 "list_revision_migration_candidates", "commit_initial_revision_backfill",
                 "register_revision_publish", "count_artifacts_without_revision"):
        assert hasattr(ArtifactRegistry, name), name


# ---------------------------------------------------------------------------
# Revision pagination & delete-graph value objects
# ---------------------------------------------------------------------------


def test_revision_list_cursor():
    c = RevisionListCursor(artifact_id="art-1", revision_number=3, id="rev-3")
    assert c.artifact_id == "art-1"
    assert c.revision_number == 3
    assert c.id == "rev-3"


def test_revision_list_cursor_frozen():
    c = RevisionListCursor(artifact_id="art-1", revision_number=3, id="rev-3")
    with pytest.raises(FrozenInstanceError):
        c.id = "x"


def test_revision_list_page():
    rev = ArtifactRevision(**_valid_revision_kwargs())
    page = RevisionListPage(items=(rev,), next_cursor=None)
    assert page.items == (rev,)
    assert page.next_cursor is None


def test_revision_list_page_with_cursor():
    rev = ArtifactRevision(**_valid_revision_kwargs())
    cursor = RevisionListCursor(artifact_id="art1", revision_number=1, id="rev1")
    page = RevisionListPage(items=(rev,), next_cursor=cursor)
    assert page.next_cursor == cursor


def test_revision_list_page_frozen():
    page = RevisionListPage(items=(), next_cursor=None)
    with pytest.raises(FrozenInstanceError):
        page.items = ()


def test_artifact_delete_graph():
    graph = ArtifactDeleteGraph(
        revision_content_refs=("item:art1/a.bin", "item:art1/b.bin"),
        legacy_artifact_content_ref="store://bucket/legacy.bin",
        publish_snapshot_ids=("pub-1", "pub-2"),
    )
    assert graph.revision_content_refs == ("item:art1/a.bin", "item:art1/b.bin")
    assert graph.legacy_artifact_content_ref == "store://bucket/legacy.bin"
    assert graph.publish_snapshot_ids == ("pub-1", "pub-2")


def test_artifact_delete_graph_defaults():
    graph = ArtifactDeleteGraph()
    assert graph.revision_content_refs == ()
    assert graph.legacy_artifact_content_ref is None
    assert graph.publish_snapshot_ids == ()


def test_artifact_delete_graph_frozen():
    graph = ArtifactDeleteGraph(
        revision_content_refs=("item:art1/a.bin",),
        legacy_artifact_content_ref=None,
        publish_snapshot_ids=("pub-1",),
    )
    with pytest.raises(FrozenInstanceError):
        graph.revision_content_refs = ("x",)
