"""Tests for ArtifactService Revision use cases (T5).

Covers create/read/update/list/diff/rollback, text_patch atomicity, diff
limits/cursor validation, and register_from_* initial-revision refactoring.
Uses the typed fakes from test_artifact_service.py.
"""
from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.application.artifact_service import (
    ArtifactMigrationIncompleteError,
    DiffResult,
    UpdateRevisionResult,
)
from app.domain.artifact import (
    Artifact,
    ArtifactContentUnavailableError,
    ArtifactDiffTooLargeError,
    ArtifactDiffUnsupportedError,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactRevision,
    ArtifactRevisionConflictError,
    ArtifactRevisionNotFoundError,
    ArtifactRevisionValidationError,
    ArtifactSource,
    ArtifactStatus,
    PublishedArtifactStatus,
)
from app.domain.task import TaskArtifact

# Reuse the typed fakes and helpers from the existing service test module.
from test_artifact_service import (
    FakeArtifactContentStore,
    FakeArtifactRegistry,
    FakeInformationFlowService,
    _make_config,
    _make_file_artifact,
    _make_inline_artifact,
    _make_service,
    _sha256,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_md(
    svc, content: str = "# hi", *, name: str = "d.md",
    artifact_id: str | None = None,
):
    """Create a markdown artifact and return (artifact, initial_revision)."""
    art = await svc.create_artifact(
        name=name,
        kind=ArtifactKind.MARKDOWN,
        mime="text/markdown",
        inline_content=content,
    )
    rev = await svc.get_current_revision(art.id)
    return art, rev


async def _create_binary(svc, data: bytes = b"\x89PNG data", *, name: str = "img.png"):
    """Create a binary (image) artifact and return (artifact, initial_revision)."""
    art = await svc.create_artifact(
        name=name,
        kind=ArtifactKind.IMAGE,
        mime="image/png",
        file_data=data,
        filename=name,
    )
    rev = await svc.get_current_revision(art.id)
    return art, rev


async def snapshot_graph_and_files(svc, store, artifact_id):
    """Capture revision count, current_revision_id, and content-store file set
    for atomicity assertions."""
    art = await svc.get_artifact(artifact_id)
    page = await svc.list_revisions(artifact_id, limit=100)
    return {
        "revision_count": len(page.items),
        "current_revision_id": art.current_revision_id,
        "files": frozenset(store._content.keys()),
    }


def _make_low_config(
    *,
    diff_max_bytes: int = 8,
    diff_max_lines: int = 2,
    diff_max_output_chars: int = 10,
) -> "ArtifactServiceConfig":
    from app.application.artifact_service import ArtifactServiceConfig
    return ArtifactServiceConfig(
        diff_max_bytes=diff_max_bytes,
        diff_max_lines=diff_max_lines,
        diff_max_output_chars=diff_max_output_chars,
    )


# ---------------------------------------------------------------------------
# S1: create / read / update use cases
# ---------------------------------------------------------------------------


class TestCreateAndReadRevision:
    @pytest.mark.asyncio
    async def test_create_artifact_creates_initial_revision(self):
        svc = _make_service()
        art = await svc.create_artifact(
            name="d",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            inline_content="# hi",
            source_kind=ArtifactSource.SESSION,
            source_session_id="s1",
        )
        assert art.current_revision_id is not None
        rev = await svc.get_current_revision(art.id)
        assert rev.revision_number == 1
        assert rev.inline_content == "# hi"
        assert rev.artifact_id == art.id
        assert rev.parent_revision_id is None
        assert rev.rollback_from_revision_id is None

    @pytest.mark.asyncio
    async def test_create_file_artifact_creates_initial_revision(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art = await svc.create_artifact(
            name="img.png",
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            file_data=b"\x89PNG data",
            filename="img.png",
        )
        assert art.current_revision_id is not None
        rev = await svc.get_current_revision(art.id)
        assert rev.revision_number == 1
        assert rev.content_ref is not None
        assert rev.content_ref.startswith("item:")
        assert rev.inline_content is None
        assert rev.checksum == _sha256(b"\x89PNG data")

    @pytest.mark.asyncio
    async def test_get_current_revision_artifact_not_found(self):
        svc = _make_service()
        with pytest.raises(ArtifactNotFoundError):
            await svc.get_current_revision("missing")

    @pytest.mark.asyncio
    async def test_get_current_revision_migration_incomplete(self):
        registry = FakeArtifactRegistry()
        # Seed a legacy artifact without current_revision_id (no revision).
        art = Artifact(
            id="legacy-1", name="d.md", kind=ArtifactKind.MARKDOWN,
            mime="text/markdown", content_ref=None, inline_content="legacy",
            size=6, checksum=_sha256(b"legacy"),
            source_kind=ArtifactSource.MANUAL, source_ref="legacy-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry)
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.get_current_revision("legacy-1")

    @pytest.mark.asyncio
    async def test_get_revision_cross_artifact_not_found(self):
        svc = _make_service()
        art_a, rev_a = await _create_md(svc, "a")
        # rev_a belongs to art_a; querying it under a different artifact id.
        art_b, _ = await _create_md(svc, "b")
        with pytest.raises(ArtifactRevisionNotFoundError):
            await svc.get_revision(art_b.id, rev_a.id)

    @pytest.mark.asyncio
    async def test_get_revision_not_found(self):
        svc = _make_service()
        art, _ = await _create_md(svc)
        with pytest.raises(ArtifactRevisionNotFoundError):
            await svc.get_revision(art.id, "rev-missing")

    @pytest.mark.asyncio
    async def test_get_revision_migration_incomplete(self):
        registry = FakeArtifactRegistry()
        art = Artifact(
            id="legacy-1", name="d.md", kind=ArtifactKind.MARKDOWN,
            mime="text/markdown", content_ref=None, inline_content="legacy",
            size=6, checksum=_sha256(b"legacy"),
            source_kind=ArtifactSource.MANUAL, source_ref="legacy-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry)
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.get_revision("legacy-1", "any-revision-id")

    @pytest.mark.asyncio
    async def test_diff_migration_incomplete(self):
        registry = FakeArtifactRegistry()
        art = Artifact(
            id="legacy-1", name="d.md", kind=ArtifactKind.MARKDOWN,
            mime="text/markdown", content_ref=None, inline_content="legacy",
            size=6, checksum=_sha256(b"legacy"),
            source_kind=ArtifactSource.MANUAL, source_ref="legacy-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry)
        # Diff is revision-scoped: an unmigrated artifact must signal 503
        # migration-incomplete before any revision lookup, regardless of the
        # from/to ids supplied.
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.diff_revisions(
                "legacy-1", "from-id", "to-id", context_lines=3,
            )


class TestUpdateRevision:
    @pytest.mark.asyncio
    async def test_update_creates_new_revision(self):
        svc = _make_service()
        art, rev = await _create_md(svc, "# hi")
        new, result = await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            inline_content="# hi v2", change_summary="v2",
        )
        assert new.revision_number == 2
        assert new.parent_revision_id == rev.id
        assert new.inline_content == "# hi v2"
        assert isinstance(result, UpdateRevisionResult)
        # old revision unchanged
        old = await svc.get_revision(art.id, rev.id)
        assert old.inline_content == "# hi"

    @pytest.mark.asyncio
    async def test_update_conflict_returns_409(self):
        svc = _make_service()
        art, rev = await _create_md(svc)
        await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            inline_content="v2", change_summary="v2",
        )
        with pytest.raises(ArtifactRevisionConflictError):
            await svc.update_revision(
                art.id, expected_revision_id=rev.id,
                inline_content="v3", change_summary="v3",
            )

    @pytest.mark.asyncio
    async def test_update_migration_incomplete(self):
        registry = FakeArtifactRegistry()
        art = Artifact(
            id="legacy-1", name="d.md", kind=ArtifactKind.MARKDOWN,
            mime="text/markdown", content_ref=None, inline_content="legacy",
            size=6, checksum=_sha256(b"legacy"),
            source_kind=ArtifactSource.MANUAL, source_ref="legacy-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry)
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.update_revision(
                "legacy-1", expected_revision_id="x", inline_content="v2",
            )

    @pytest.mark.asyncio
    async def test_update_archived_deny(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art = await svc.create_artifact(
            name="d.md", kind=ArtifactKind.MARKDOWN, mime="text/markdown",
            inline_content="# hi",
        )
        # Archive the artifact via direct registry manipulation.
        archived = replace(
            registry._artifacts[art.id], status=ArtifactStatus.ARCHIVED,
        )
        registry._artifacts[art.id] = archived
        rev = await svc.get_current_revision(art.id)
        with pytest.raises(Exception):
            await svc.update_revision(
                art.id, expected_revision_id=rev.id,
                inline_content="v2", change_summary="v2",
            )

    @pytest.mark.asyncio
    async def test_content_unchanged_marked_on_identical_checksum(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "same")
        r2, result = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="same", change_summary="noop",
        )
        assert result.content_unchanged is True
        assert r2.checksum == r1.checksum

    @pytest.mark.asyncio
    async def test_update_file_data_creates_content_ref_revision(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, rev = await _create_md(svc, "# text")
        new, _ = await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            file_data=b"\x89PNG new", kind=ArtifactKind.IMAGE,
            mime="image/png", change_summary="to binary",
        )
        assert new.content_ref is not None
        assert new.content_ref.startswith("item:")
        assert new.inline_content is None
        assert new.kind is ArtifactKind.IMAGE
        assert new.checksum == _sha256(b"\x89PNG new")

    @pytest.mark.asyncio
    async def test_update_requires_exactly_one_content_input(self):
        svc = _make_service()
        art, rev = await _create_md(svc, "# hi")
        with pytest.raises(ArtifactRevisionValidationError):
            await svc.update_revision(
                art.id, expected_revision_id=rev.id,
                inline_content="x", file_data=b"y", change_summary="both",
            )
        with pytest.raises(ArtifactRevisionValidationError):
            await svc.update_revision(
                art.id, expected_revision_id=rev.id, change_summary="none",
            )

    @pytest.mark.asyncio
    async def test_publish_sync_state_unpublished(self):
        svc = _make_service()
        art, rev = await _create_md(svc, "# v1")
        _, result = await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            inline_content="# v2", change_summary="v2",
        )
        assert result.publish_sync_state == "unpublished"


# ---------------------------------------------------------------------------
# S1: text_patch
# ---------------------------------------------------------------------------


class TestTextPatch:
    @pytest.mark.asyncio
    async def test_text_patch_first_mode(self):
        svc = _make_service()
        art, rev = await _create_md(svc, "aaa bbb aaa\n")
        new, _ = await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            text_patch=[{"search": "aaa", "replace": "X", "mode": "first"}],
            change_summary="p",
        )
        assert new.inline_content == "X bbb aaa\n"

    @pytest.mark.asyncio
    async def test_text_patch_all_mode(self):
        svc = _make_service()
        art, rev = await _create_md(svc, "aaa bbb aaa\n")
        new, _ = await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            text_patch=[{"search": "aaa", "replace": "X", "mode": "all"}],
            change_summary="p",
        )
        assert new.inline_content == "X bbb X\n"

    @pytest.mark.asyncio
    async def test_text_patch_multiple_ordered(self):
        svc = _make_service()
        art, rev = await _create_md(svc, "foo bar foo baz\n")
        new, _ = await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            text_patch=[
                {"search": "foo", "replace": "FOO", "mode": "all"},
                {"search": "bar", "replace": "BAR", "mode": "first"},
            ],
            change_summary="p",
        )
        assert new.inline_content == "FOO BAR FOO baz\n"

    @pytest.mark.asyncio
    async def test_text_patch_preserves_kind_and_mime(self):
        svc = _make_service()
        art, rev = await _create_md(svc, "hello world")
        new, _ = await svc.update_revision(
            art.id, expected_revision_id=rev.id,
            text_patch=[{"search": "hello", "replace": "hi", "mode": "first"}],
            change_summary="p",
        )
        assert new.kind is rev.kind
        assert new.mime == rev.mime

    @pytest.mark.asyncio
    async def test_text_patch_binary_kind_rejected(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, rev = await _create_binary(svc)
        with pytest.raises(ArtifactRevisionValidationError):
            await svc.update_revision(
                art.id, expected_revision_id=rev.id,
                text_patch=[{"search": "x", "replace": "y", "mode": "first"}],
                change_summary="p",
            )

    @pytest.mark.asyncio
    async def test_text_patch_failure_is_file_and_db_atomic(self):
        """All patch validation/match failures leave zero side effects:
        no new revision, no current_revision_id change, no new files."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, rev = await _create_md(svc, "aaa bbb aaa\n")

        # Non-UTF-8 content: create a text-kind file-backed revision with
        # invalid UTF-8 bytes, then attempt a patch (decode must fail).
        art_bin = await svc.create_artifact(
            name="bad.txt", kind=ArtifactKind.TEXT, mime="text/plain",
            file_data=b"\xff\xfe bad utf8",
        )
        rev_bin = await svc.get_current_revision(art_bin.id)

        before = await snapshot_graph_and_files(svc, store, art.id)
        before_bin = await snapshot_graph_and_files(svc, store, art_bin.id)

        failure_patches = [
            # empty array
            [],
            # unknown field
            [{"search": "aaa", "replace": "X", "mode": "first", "extra": 1}],
            # empty search
            [{"search": "", "replace": "X", "mode": "first"}],
            # invalid mode
            [{"search": "aaa", "replace": "X", "mode": "invalid"}],
            # first unmatched
            [{"search": "zzz", "replace": "X", "mode": "first"}],
            # all unmatched
            [{"search": "zzz", "replace": "X", "mode": "all"}],
            # 101 items (over limit)
            [{"search": "aaa", "replace": "X", "mode": "first"}] * 101,
        ]

        for patch in failure_patches:
            with pytest.raises(ArtifactRevisionValidationError):
                await svc.update_revision(
                    art.id, expected_revision_id=rev.id,
                    text_patch=patch, change_summary="fail",
                )

        # Non-UTF-8 decode failure on the binary-content text revision.
        with pytest.raises(ArtifactRevisionValidationError):
            await svc.update_revision(
                art_bin.id, expected_revision_id=rev_bin.id,
                text_patch=[
                    {"search": "x", "replace": "y", "mode": "first"}
                ],
                change_summary="fail",
            )

        # Post-application exceeds inline limit.
        svc_low = _make_service(
            registry=registry, content_store=store,
            config=_make_config(artifact_inline_max_bytes=2),
        )
        with pytest.raises(ArtifactRevisionValidationError):
            await svc_low.update_revision(
                art.id, expected_revision_id=rev.id,
                text_patch=[
                    {"search": "aaa", "replace": "XXXX", "mode": "first"}
                ],
                change_summary="fail",
            )

        after = await snapshot_graph_and_files(svc, store, art.id)
        after_bin = await snapshot_graph_and_files(svc, store, art_bin.id)
        assert after == before
        assert after_bin == before_bin


# ---------------------------------------------------------------------------
# S4: diff / rollback tests
# ---------------------------------------------------------------------------


class TestDiffRevisions:
    @pytest.mark.asyncio
    async def test_diff_text_revisions(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "line1\nline2\nline3\n")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="line1\nCHANGED\nline3\n", change_summary="c",
        )
        d = await svc.diff_revisions(art.id, r1.id, r2.id, context_lines=3)
        assert isinstance(d, DiffResult)
        assert "revision-1" in d.diff_text
        assert "revision-2" in d.diff_text
        assert "CHANGED" in d.diff_text
        assert d.binary_changed is False

    @pytest.mark.asyncio
    async def test_diff_identical_revisions_empty(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "same\n")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="same\n", change_summary="noop",
        )
        d = await svc.diff_revisions(art.id, r1.id, r2.id)
        # No diff lines (content identical); headers may still appear but no
        # changed lines.
        assert d.binary_changed is False

    @pytest.mark.asyncio
    async def test_diff_unsupported_for_cross_kind(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, r1 = await _create_md(svc, "# text")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            file_data=b"\x89PNG data", kind=ArtifactKind.IMAGE,
            mime="image/png", change_summary="to binary",
        )
        with pytest.raises(ArtifactDiffUnsupportedError):
            await svc.diff_revisions(art.id, r1.id, r2.id, context_lines=3)

    @pytest.mark.asyncio
    async def test_diff_binary_pair_returns_binary_changed(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, r1 = await _create_binary(svc, b"\x89PNG data v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            file_data=b"\x89PNG data v2", kind=ArtifactKind.IMAGE,
            mime="image/png", change_summary="v2",
        )
        d = await svc.diff_revisions(art.id, r1.id, r2.id, context_lines=3)
        assert d.diff_text == ""
        assert d.binary_changed is True

    @pytest.mark.asyncio
    async def test_diff_binary_unchanged(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, r1 = await _create_binary(svc, b"\x89PNG same")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            file_data=b"\x89PNG same", kind=ArtifactKind.IMAGE,
            mime="image/png", change_summary="noop",
        )
        d = await svc.diff_revisions(art.id, r1.id, r2.id)
        assert d.binary_changed is False

    @pytest.mark.asyncio
    async def test_diff_revision_not_found(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "# hi")
        with pytest.raises(ArtifactRevisionNotFoundError):
            await svc.diff_revisions(art.id, r1.id, "rev-missing")

    @pytest.mark.asyncio
    async def test_diff_context_lines_out_of_range(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "# hi")
        for bad in (-1, 21):
            with pytest.raises(ArtifactRevisionValidationError):
                await svc.diff_revisions(
                    art.id, r1.id, r1.id, context_lines=bad,
                )

    @pytest.mark.asyncio
    async def test_diff_max_bytes_limit(self):
        svc = _make_service(config=_make_low_config(diff_max_bytes=8))
        art, r1 = await _create_md(svc, "this is more than eight bytes")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="also more than eight bytes\n", change_summary="c",
        )
        with pytest.raises(ArtifactDiffTooLargeError):
            await svc.diff_revisions(art.id, r1.id, r2.id)

    @pytest.mark.asyncio
    async def test_diff_max_lines_limit(self):
        svc = _make_service(config=_make_low_config(diff_max_lines=2))
        art, r1 = await _create_md(svc, "a\nb\nc\nd\n")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="a\nb\nc\nd\ne\n", change_summary="c",
        )
        with pytest.raises(ArtifactDiffTooLargeError):
            await svc.diff_revisions(art.id, r1.id, r2.id)

    @pytest.mark.asyncio
    async def test_diff_max_output_chars_limit(self):
        svc = _make_service(config=_make_low_config(diff_max_output_chars=10))
        art, r1 = await _create_md(svc, "short")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="completely different content here\n",
            change_summary="c",
        )
        with pytest.raises(ArtifactDiffTooLargeError):
            await svc.diff_revisions(art.id, r1.id, r2.id)

    @pytest.mark.asyncio
    async def test_diff_non_utf8_unsupported(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        # Create a text-kind artifact with non-UTF-8 file content.
        art = await svc.create_artifact(
            name="bad.txt", kind=ArtifactKind.TEXT, mime="text/plain",
            file_data=b"\xff\xfe bad",
        )
        r1 = await svc.get_current_revision(art.id)
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            file_data=b"\xff\xfe also bad", kind=ArtifactKind.TEXT,
            mime="text/plain", change_summary="c",
        )
        with pytest.raises(ArtifactDiffUnsupportedError):
            await svc.diff_revisions(art.id, r1.id, r2.id)


class TestListRevisions:
    @pytest.mark.asyncio
    async def test_list_revisions_returns_newest_first(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="v2", change_summary="v2",
        )
        r3, _ = await svc.update_revision(
            art.id, expected_revision_id=r2.id,
            inline_content="v3", change_summary="v3",
        )
        page = await svc.list_revisions(art.id, limit=10)
        assert len(page.items) == 3
        assert page.items[0].revision_number == 3
        assert page.items[2].revision_number == 1

    @pytest.mark.asyncio
    async def test_list_revisions_pagination(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "v1")
        prev = r1
        for i in range(2, 6):
            r, _ = await svc.update_revision(
                art.id, expected_revision_id=prev.id,
                inline_content=f"v{i}", change_summary=f"v{i}",
            )
            prev = r
        page1 = await svc.list_revisions(art.id, limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor is not None
        page2 = await svc.list_revisions(
            art.id, limit=2, cursor=page1.next_cursor,
        )
        assert len(page2.items) == 2

    @pytest.mark.asyncio
    async def test_list_revisions_limit_clamped(self):
        svc = _make_service()
        art, _ = await _create_md(svc)
        page = await svc.list_revisions(art.id, limit=0)
        assert len(page.items) == 1  # clamped to 1

    @pytest.mark.asyncio
    async def test_list_revisions_artifact_not_found(self):
        svc = _make_service()
        with pytest.raises(ArtifactNotFoundError):
            await svc.list_revisions("missing")

    @pytest.mark.asyncio
    async def test_list_revisions_migration_incomplete_returns_empty(self):
        registry = FakeArtifactRegistry()
        art = Artifact(
            id="legacy-1", name="d.md", kind=ArtifactKind.MARKDOWN,
            mime="text/markdown", content_ref=None, inline_content="legacy",
            size=6, checksum=_sha256(b"legacy"),
            source_kind=ArtifactSource.MANUAL, source_ref="legacy-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry)
        page = await svc.list_revisions("legacy-1")
        assert page.items == ()
        assert page.next_cursor is None

    @pytest.mark.asyncio
    async def test_list_revisions_cross_artifact_cursor_rejected(self):
        svc = _make_service()
        art_a, _ = await _create_md(svc, "a")
        art_b, _ = await _create_md(svc, "b")
        # Get a cursor from art_a's revisions.
        page_a = await svc.list_revisions(art_a.id, limit=1)
        cursor = page_a.next_cursor
        # No next_cursor with only 1 revision; create more.
        prev = (await svc.get_current_revision(art_a.id))
        for i in range(2, 4):
            prev, _ = await svc.update_revision(
                art_a.id, expected_revision_id=prev.id,
                inline_content=f"a{i}", change_summary=f"a{i}",
            )
        page_a = await svc.list_revisions(art_a.id, limit=1)
        assert page_a.next_cursor is not None
        # Use art_a's cursor on art_b -> cross-artifact rejection.
        with pytest.raises(ArtifactRevisionValidationError):
            await svc.list_revisions(art_b.id, cursor=page_a.next_cursor)


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_creates_new_revision_sharing_content(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="v2", change_summary="v2",
        )
        r3, result = await svc.rollback(
            art.id, target_revision_id=r1.id,
            expected_revision_id=r2.id, change_summary="back",
        )
        assert r3.revision_number == 3
        assert r3.rollback_from_revision_id == r1.id
        assert r3.parent_revision_id == r2.id
        assert r3.inline_content == "v1"
        assert r3.checksum == r1.checksum
        # history preserved
        r2_check = await svc.get_revision(art.id, r2.id)
        assert r2_check.inline_content == "v2"
        assert isinstance(result, UpdateRevisionResult)

    @pytest.mark.asyncio
    async def test_rollback_conflict(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="v2", change_summary="v2",
        )
        with pytest.raises(ArtifactRevisionConflictError):
            await svc.rollback(
                art.id, target_revision_id=r1.id,
                expected_revision_id=r1.id, change_summary="back",
            )

    @pytest.mark.asyncio
    async def test_rollback_target_not_found(self):
        svc = _make_service()
        art, r1 = await _create_md(svc, "v1")
        with pytest.raises(ArtifactRevisionNotFoundError):
            await svc.rollback(
                art.id, target_revision_id="rev-missing",
                expected_revision_id=r1.id, change_summary="back",
            )

    @pytest.mark.asyncio
    async def test_rollback_migration_incomplete(self):
        registry = FakeArtifactRegistry()
        art = Artifact(
            id="legacy-1", name="d.md", kind=ArtifactKind.MARKDOWN,
            mime="text/markdown", content_ref=None, inline_content="legacy",
            size=6, checksum=_sha256(b"legacy"),
            source_kind=ArtifactSource.MANUAL, source_ref="legacy-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry)
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.rollback(
                "legacy-1", target_revision_id="x",
                expected_revision_id="y", change_summary="back",
            )

    @pytest.mark.asyncio
    async def test_rollback_file_backed_materializes_new_item(self):
        """Rollback to a file-backed target creates a NEW item: path (the
        target's item file is not reused in-place)."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, r1 = await _create_binary(svc, b"\x89PNG v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            file_data=b"\x89PNG v2", kind=ArtifactKind.IMAGE,
            mime="image/png", change_summary="v2",
        )
        r3, _ = await svc.rollback(
            art.id, target_revision_id=r1.id,
            expected_revision_id=r2.id, change_summary="back",
        )
        assert r3.content_ref is not None
        assert r3.content_ref != r1.content_ref
        assert r3.checksum == r1.checksum
        # both item refs exist in store (target's and rollback's)
        assert store.has(r1.content_ref)
        assert store.has(r3.content_ref)

    @pytest.mark.asyncio
    async def test_rollback_archived_deny(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, r1 = await _create_md(svc, "v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="v2", change_summary="v2",
        )
        archived = replace(
            registry._artifacts[art.id], status=ArtifactStatus.ARCHIVED,
        )
        registry._artifacts[art.id] = archived
        with pytest.raises(Exception):
            await svc.rollback(
                art.id, target_revision_id=r1.id,
                expected_revision_id=r2.id, change_summary="back",
            )


# ---------------------------------------------------------------------------
# S4c: register_from_* creates initial revision
# ---------------------------------------------------------------------------


class TestRegisterFromInitialRevision:
    @pytest.mark.asyncio
    async def test_register_from_attachment_creates_initial_revision(self):
        from app.domain.artifact import ArtifactAttachmentSource

        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        data = b"attachment content"
        store.seed(attachment_ref, data)
        svc = _make_service(registry=registry, content_store=store)
        src = ArtifactAttachmentSource(
            attachment_id="att-1", task_id="task-1", stored_name="stored-1",
            filename="report.md", content_type="text/markdown",
            size=len(data), checksum=_sha256(data), uploaded_by="user-1",
        )
        art = await svc.register_from_attachment(src)
        assert art is not None
        assert art.current_revision_id is not None
        rev = await svc.get_current_revision(art.id)
        assert rev.revision_number == 1
        assert rev.content_ref is not None
        assert rev.content_ref.startswith("item:")
        assert rev.content_ref != attachment_ref
        # content is materialized -- deleting the source does not affect it
        data_before = store._content[rev.content_ref]
        store._content.pop(attachment_ref, None)
        assert store._content[rev.content_ref] == data_before
        # re-read revision content unchanged
        rev2 = await svc.get_current_revision(art.id)
        assert rev2.content_ref == rev.content_ref

    @pytest.mark.asyncio
    async def test_register_from_task_artifact_creates_initial_revision(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ws_ref = "workspace:reports/output.md"
        data = b"# Task output"
        store.seed(ws_ref, data)
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="file", name="output.md", mime="text/markdown",
            size=len(data), storage_ref=ws_ref, source_task_id="task-1",
            summary="task result", checksum=_sha256(data),
        )
        art = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert art is not None
        assert art.current_revision_id is not None
        rev = await svc.get_current_revision(art.id)
        assert rev.revision_number == 1
        assert rev.content_ref is not None
        assert rev.content_ref.startswith("item:")
        assert rev.content_ref != ws_ref
        # content survives source deletion
        data_before = store._content[rev.content_ref]
        store._content.pop(ws_ref, None)
        assert store._content[rev.content_ref] == data_before

    @pytest.mark.asyncio
    async def test_register_from_task_artifact_inline_creates_initial_revision(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        body = "# Report\n\nlong content here"
        ta = TaskArtifact(
            type="text", name="report.md", mime="text/markdown",
            size=0, storage_ref="", source_task_id="task-1",
            summary="short abstract", checksum="", content=body,
        )
        art = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert art is not None
        assert art.current_revision_id is not None
        rev = await svc.get_current_revision(art.id)
        assert rev.revision_number == 1
        assert rev.inline_content == body
        assert rev.content_ref is None

    @pytest.mark.asyncio
    async def test_register_from_signature_unchanged(self):
        svc = _make_service()
        sig_att = inspect.signature(svc.register_from_attachment)
        assert tuple(sig_att.parameters) == ("attachment",)
        sig_ta = inspect.signature(svc.register_from_task_artifact)
        assert tuple(sig_ta.parameters) == (
            "task_artifact", "task_id", "run_id", "ordinal",
        )

    @pytest.mark.asyncio
    async def test_register_from_attachment_db_failure_compensates_file(self):
        from app.domain.artifact import ArtifactAttachmentSource

        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"attachment data")
        registry.fail_on_create = True
        svc = _make_service(registry=registry, content_store=store)
        src = ArtifactAttachmentSource(
            attachment_id="att-1", task_id="task-1", stored_name="stored-1",
            filename="report.md", content_type="text/markdown",
            size=15, checksum=_sha256(b"attachment data"), uploaded_by="user-1",
        )
        result = await svc.register_from_attachment(src)
        assert result is None
        # materialized item: file was compensated (deleted)
        item_refs = [r for r in store._content if r.startswith("item:")]
        assert item_refs == []

    @pytest.mark.asyncio
    async def test_register_from_idempotent_returns_existing(self):
        from app.domain.artifact import ArtifactAttachmentSource

        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"attachment data")
        svc = _make_service(registry=registry, content_store=store)
        src = ArtifactAttachmentSource(
            attachment_id="att-1", task_id="task-1", stored_name="stored-1",
            filename="report.md", content_type="text/markdown",
            size=15, checksum=_sha256(b"attachment data"), uploaded_by="user-1",
        )
        a1 = await svc.register_from_attachment(src)
        a2 = await svc.register_from_attachment(src)
        assert a1 is not None and a2 is not None
        assert a1.id == a2.id
        assert len(registry.create_calls) == 1


# ---------------------------------------------------------------------------
# T6: publish_revision + export delegation
# ---------------------------------------------------------------------------


class TestPublishRevision:
    """S1-S2: publish_revision binds Revision, CAS, reuse/switch."""

    @pytest.mark.asyncio
    async def test_new_revision_does_not_revoke_active_publish(self):
        """Publish r1, then update to r2: active publish still points at
        r1, sync_state == outdated."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# v1")
        # publish r1
        pr1 = await svc.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        assert not pr1.reused
        active = await registry.get_active_publish(art.id)
        assert active is not None
        assert active.published_revision_id == r1.id
        # update to r2 (does NOT revoke active publish)
        r2, result = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2", change_summary="v2",
        )
        assert result.publish_sync_state == "outdated"
        # active publish still points at r1
        active = await registry.get_active_publish(art.id)
        assert active is not None
        assert active.published_revision_id == r1.id
        assert active.status is PublishedArtifactStatus.ACTIVE
        # T12 S1: the published snapshot is immutable -- its checksum stays
        # r1's (copied at publish time), NOT r2's. The old public URL would
        # serve r1's bytes/checksum unchanged even though current is now r2.
        assert active.snapshot_checksum == r1.checksum
        assert active.snapshot_checksum != r2.checksum

    @pytest.mark.asyncio
    async def test_republish_switches_to_current(self):
        """Re-publish r2: active.published_revision_id == r2.id."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# v1")
        await svc.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2", change_summary="v2",
        )
        # re-publish r2 (current)
        pr2 = await svc.publish_revision(
            art.id, revision_id=r2.id, expected_current_revision_id=r2.id,
        )
        assert not pr2.reused
        active = await registry.get_active_publish(art.id)
        assert active is not None
        assert active.published_revision_id == r2.id
        # old publish revoked
        old = await registry.get_published(pr2.published.publish_id)
        assert old.status is PublishedArtifactStatus.ACTIVE
        old_pr1 = await registry.list_published(art.id)
        revoked = [p for p in old_pr1 if p.status is PublishedArtifactStatus.REVOKED]
        assert len(revoked) >= 1

    @pytest.mark.asyncio
    async def test_publish_wrong_revision_returns_conflict(self):
        """expected_current_revision_id mismatch -> ConflictError."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2", change_summary="v2",
        )
        # current is r2, but expected says r1 -> conflict
        with pytest.raises(ArtifactRevisionConflictError):
            await svc.publish_revision(
                art.id, revision_id=r2.id,
                expected_current_revision_id=r1.id,
            )

    @pytest.mark.asyncio
    async def test_concurrent_publish_only_one_active(self):
        """Two concurrent publishes of the same revision: at most one new
        active (reuse or one conflict)."""
        import asyncio
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2", change_summary="v2",
        )
        results = await asyncio.gather(
            svc.publish_revision(
                art.id, revision_id=r2.id,
                expected_current_revision_id=r2.id,
            ),
            svc.publish_revision(
                art.id, revision_id=r2.id,
                expected_current_revision_id=r2.id,
            ),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(successes) >= 1
        # at most one new active publish
        active = await registry.get_active_publish(art.id)
        assert active is not None
        # count active publishes in _published
        actives = [
            p for p in registry._published.values()
            if p.artifact_id == art.id
            and p.status is PublishedArtifactStatus.ACTIVE
        ]
        assert len(actives) == 1

    @pytest.mark.asyncio
    async def test_republish_failures_preserve_old_active_and_snapshot(self):
        """register_revision_publish DB failure on re-publish: old share URL
        bytes/checksum/status stay active, failed staging file compensated.

        Spec-valid flow: publish the CURRENT revision r1, update to r2 (active
        publish stays on r1 -> outdated), then re-publish the CURRENT revision
        r2. r2's released content differs from r1's so no early reuse; the new
        staging snapshot is written then register_revision_publish fails, leaving
        the old active publish on r1 intact.
        """
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc_create = _make_service(
            registry=registry, content_store=store, flow=flow,
        )
        art, r1 = await _create_md(svc_create, "# v1 content")
        # Publish the current revision r1.
        pr1 = await svc_create.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        old_checksum = pr1.published.snapshot_checksum
        old_publish_id = pr1.published.publish_id
        # Update to r2 -- current moves to r2, active publish stays on r1.
        r2, _ = await svc_create.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2 content", change_summary="v2",
        )
        # Force file-backed publish snapshots so staging compensation is
        # observable via delete_calls.
        config = _make_config(artifact_inline_max_bytes=2)
        svc = _make_service(
            registry=registry, content_store=store, flow=flow, config=config,
        )
        # --- DB failure on re-publish current r2 (content differs from
        # active r1 -> not reuse -> new staging snapshot -> register fails) ---
        registry.fail_on_register = True
        with pytest.raises(RuntimeError):
            await svc.publish_revision(
                art.id, revision_id=r2.id,
                expected_current_revision_id=r2.id,
            )
        registry.fail_on_register = False
        # old active (r1) preserved
        active = await registry.get_active_publish(art.id)
        assert active is not None
        assert active.publish_id == old_publish_id
        assert active.snapshot_checksum == old_checksum
        assert active.status is PublishedArtifactStatus.ACTIVE
        # failed staging file compensated (deleted)
        assert len(store.delete_calls) >= 1

    @pytest.mark.asyncio
    async def test_publish_snapshot_write_failure_preserves_old_active(self):
        """Staging snapshot write failure (copy_to_publish_snapshot raises)
        before any DB change: old active publish stays active and no new
        publish row is registered."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc_create = _make_service(
            registry=registry, content_store=store, flow=flow,
        )
        art, r1 = await _create_md(svc_create, "# v1 content")
        await svc_create.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        r2, _ = await svc_create.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2 content", change_summary="v2",
        )
        # Force file-backed snapshot so copy_to_publish_snapshot is exercised.
        config = _make_config(artifact_inline_max_bytes=2)
        svc = _make_service(
            registry=registry, content_store=store, flow=flow, config=config,
        )
        old_active = await registry.get_active_publish(art.id)
        assert old_active is not None
        old_register_count = len(registry.register_calls)
        store.fail_on_copy = True
        with pytest.raises(RuntimeError):
            await svc.publish_revision(
                art.id, revision_id=r2.id,
                expected_current_revision_id=r2.id,
            )
        store.fail_on_copy = False
        # No new publish registered (failure happened before DB write).
        assert len(registry.register_calls) == old_register_count
        # Old active publish unchanged.
        active = await registry.get_active_publish(art.id)
        assert active is not None
        assert active.publish_id == old_active.publish_id
        assert active.snapshot_checksum == old_active.snapshot_checksum
        assert active.status is PublishedArtifactStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_publish_reuse_uses_released_checksum(self):
        """Same revision published twice: second time reuses based on
        final public checksum (after policy + release)."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# reusable")
        pr1 = await svc.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        assert not pr1.reused
        pr2 = await svc.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        assert pr2.reused
        assert pr1.published.publish_id == pr2.published.publish_id

    @pytest.mark.asyncio
    async def test_publish_unknown_revision_returns_conflict(self):
        # Spec 54/200: revision_id must equal the in-transaction current
        # revision; an unknown id is not current -> conflict (not not-found),
        # because publish is excluded from the cross-artifact not-found list
        # (spec 262) and uses the CAS conflict contract instead.
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# hi")
        with pytest.raises(ArtifactRevisionConflictError):
            await svc.publish_revision(
                art.id, revision_id="rev-missing",
                expected_current_revision_id=r1.id,
            )

    @pytest.mark.asyncio
    async def test_publish_stale_revision_returns_conflict(self):
        # Publishing a historical (non-current) revision is forbidden even
        # when expected_current_revision_id is correct: revision_id must be
        # the current revision (spec 54).
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# v1")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2", change_summary="v2",
        )
        with pytest.raises(ArtifactRevisionConflictError):
            await svc.publish_revision(
                art.id, revision_id=r1.id,
                expected_current_revision_id=r2.id,
            )

    @pytest.mark.asyncio
    async def test_publish_cross_artifact_returns_conflict(self):
        # Cross-artifact revision_id is not the current revision of the path
        # artifact -> conflict (publish is excluded from the cross-artifact
        # not-found list, spec 262).
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art_a, rev_a = await _create_md(svc, "a")
        art_b, rev_b = await _create_md(svc, "b")
        with pytest.raises(ArtifactRevisionConflictError):
            await svc.publish_revision(
                art_b.id, revision_id=rev_a.id,
                expected_current_revision_id=rev_b.id,
            )

    @pytest.mark.asyncio
    async def test_get_publish_sync_state_current(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# v1")
        await svc.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        assert await svc.get_publish_sync_state(art.id) == "current"

    @pytest.mark.asyncio
    async def test_get_publish_sync_state_outdated(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        art, r1 = await _create_md(svc, "# v1")
        await svc.publish_revision(
            art.id, revision_id=r1.id, expected_current_revision_id=r1.id,
        )
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2", change_summary="v2",
        )
        assert await svc.get_publish_sync_state(art.id) == "outdated"

    @pytest.mark.asyncio
    async def test_get_publish_sync_state_unpublished(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        art, _ = await _create_md(svc, "# v1")
        assert await svc.get_publish_sync_state(art.id) == "unpublished"

    @pytest.mark.asyncio
    async def test_publish_migration_incomplete(self):
        """publish() on artifact without revision -> MigrationIncomplete."""
        registry = FakeArtifactRegistry()
        art = Artifact(
            id="legacy-1", name="d.md", kind=ArtifactKind.MARKDOWN,
            mime="text/markdown", content_ref=None, inline_content="legacy",
            size=6, checksum=_sha256(b"legacy"),
            source_kind=ArtifactSource.MANUAL, source_ref="legacy-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry)
        from app.application.artifact_service import (
            ArtifactMigrationIncompleteError,
        )
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.publish("legacy-1")


class TestExportDelegation:
    """S3-S4: export delegates to ArtifactExporter."""

    @pytest.mark.asyncio
    async def test_export_delegates_to_exporter(self):
        """Markdown artifact exports as docx: mime correct, filename .docx."""
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        svc = _make_service(exporter=exporter)
        art, _ = await _create_md(svc, "# Title\n\nContent here")
        data, mime, filename = await svc.export(art.id, format="docx")
        assert mime == (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        )
        assert filename.endswith(".docx")
        assert len(data) > 0

    @pytest.mark.asyncio
    async def test_export_capabilities_endpoint_logic(self):
        """Caps for markdown revision include docx."""
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        svc = _make_service(exporter=exporter)
        art, _ = await _create_md(svc, "# Title\n\nContent")
        caps = await svc.export_capabilities(art.id)
        assert "docx" in caps
        assert "original" in caps

    @pytest.mark.asyncio
    async def test_export_unsupported_format_raises(self):
        """Text artifact xlsx -> UnsupportedError; binary docx -> Unsupported."""
        from app.application.artifact_service import (
            ArtifactExportUnsupportedError,
        )
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        svc = _make_service(exporter=exporter)
        # text (markdown) artifact: xlsx not supported
        art_md, _ = await _create_md(svc, "# text content")
        with pytest.raises(ArtifactExportUnsupportedError):
            await svc.export(art_md.id, format="xlsx")
        # binary (image) artifact: docx not supported
        art_bin, _ = await _create_binary(svc, b"\x89PNG data")
        with pytest.raises(ArtifactExportUnsupportedError):
            await svc.export(art_bin.id, format="docx")

    @pytest.mark.asyncio
    async def test_historical_export_and_capabilities_are_same_revision_and_read_only(
        self,
    ):
        """For a historical revision_id, capabilities + each format export
        leave snapshot_graph_and_files unchanged."""
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(
            registry=registry, content_store=store, exporter=exporter,
        )
        art, r1 = await _create_md(svc, "# v1\n\nparagraph")
        r2, _ = await svc.update_revision(
            art.id, expected_revision_id=r1.id,
            inline_content="# v2\n\nnew content", change_summary="v2",
        )
        before = await snapshot_graph_and_files(svc, store, art.id)

        # capabilities for historical r1
        caps = await svc.export_capabilities(art.id, revision_id=r1.id)
        assert "original" in caps
        # export each format for historical r1
        for fmt in caps:
            data, mime, filename = await svc.export(
                art.id, format=fmt, revision_id=r1.id,
            )
            assert len(data) > 0
            assert mime
            assert filename

        after = await snapshot_graph_and_files(svc, store, art.id)
        assert after == before

    @pytest.mark.asyncio
    async def test_export_format_case_insensitive(self):
        """Format is lowercased before capability check."""
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        svc = _make_service(exporter=exporter)
        art, _ = await _create_md(svc, "# Title")
        data, mime, filename = await svc.export(art.id, format="DOCX")
        assert filename.endswith(".docx")

    @pytest.mark.asyncio
    async def test_export_original_preserves_filename(self):
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        svc = _make_service(exporter=exporter)
        art, _ = await _create_md(svc, "# content", name="report.md")
        data, mime, filename = await svc.export(art.id, format="original")
        assert mime == "text/markdown"
        assert "report" in filename

    @pytest.mark.asyncio
    async def test_export_legacy_when_no_exporter(self):
        """When exporter is None, legacy path (original/html) works."""
        svc = _make_service()  # no exporter
        art, _ = await _create_md(svc, "# Title\n\nContent")
        data, mime, filename = await svc.export(art.id, format="html")
        assert mime == "text/html"
        assert b"<html" in data.lower()

    @pytest.mark.asyncio
    async def test_export_capabilities_legacy_when_no_exporter(self):
        svc = _make_service()
        art, _ = await _create_md(svc, "# md")
        caps = await svc.export_capabilities(art.id)
        assert "html" in caps
        assert "original" in caps

    @pytest.mark.asyncio
    async def test_export_revision_not_found(self):
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        svc = _make_service(exporter=exporter)
        art, _ = await _create_md(svc, "# hi")
        with pytest.raises(ArtifactRevisionNotFoundError):
            await svc.export(art.id, format="original", revision_id="rev-missing")

    @pytest.mark.asyncio
    async def test_export_cross_artifact_revision(self):
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        exporter = OfficeArtifactExporter()
        svc = _make_service(exporter=exporter)
        art_a, rev_a = await _create_md(svc, "a")
        art_b, _ = await _create_md(svc, "b")
        with pytest.raises(ArtifactRevisionNotFoundError):
            await svc.export(
                art_b.id, format="original", revision_id=rev_a.id,
            )


# ---------------------------------------------------------------------------
# T7: get_content Revision-aware + migration backfill + health
# ---------------------------------------------------------------------------


class TestGetContentRevisionAware:
    @pytest.mark.asyncio
    async def test_get_content_reads_current_revision_after_update(self):
        """get_content must read the current Revision after update_revision,
        not the stale legacy content (spec 109: update_revision does not
        update legacy columns)."""
        svc = _make_service()
        art, rev1 = await _create_md(svc, "v1")
        rev2, _ = await svc.update_revision(
            art.id,
            expected_revision_id=rev1.id,
            inline_content="v2",
        )
        data, returned_art = await svc.get_content(art.id)
        assert data == b"v2"

    @pytest.mark.asyncio
    async def test_get_content_legacy_for_unmigrated(self):
        """get_content on an unmigrated artifact reads legacy content."""
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# legacy"))
        svc = _make_service(registry=registry)
        data, _ = await svc.get_content("art-1")
        assert data == b"# legacy"

    @pytest.mark.asyncio
    async def test_get_content_missing_current_revision(self):
        """get_content on a migrated artifact whose revision row is missing
        raises ArtifactContentUnavailableError (completeness)."""
        registry = FakeArtifactRegistry()
        art = _make_inline_artifact(inline_content="# hi")
        # Simulate migrated (current_revision_id set) but revision row missing
        registry.seed(replace(art, current_revision_id="rev-missing"))
        svc = _make_service(registry=registry)
        with pytest.raises(ArtifactContentUnavailableError):
            await svc.get_content("art-1")


class TestUnmigratedArtifactWriteBlocked:
    @pytest.mark.asyncio
    async def test_unmigrated_artifact_write_returns_503(self):
        """update_revision on an unmigrated artifact raises
        ArtifactMigrationIncompleteError."""
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Hi"))
        svc = _make_service(registry=registry)
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.update_revision(
                "art-1",
                expected_revision_id="fake",
                inline_content="new",
            )


class TestMigrationBackfill:
    @pytest.mark.asyncio
    async def test_migration_backfill_result(self):
        """migrate_revisions backfills multiple unmigrated artifacts (inline,
        item, attachment, workspace sources) with correct checksums and
        idempotency."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()

        # 1. Inline artifact
        inline_data = b"# Inline doc"
        inline_art = _make_inline_artifact(
            artifact_id="art-inline",
            inline_content=inline_data.decode("utf-8"),
        )
        registry.seed(inline_art)

        # 2. Item-backed artifact (binary)
        item_data = b"\x89PNG item data"
        store.seed("item:art-item/file", item_data)
        item_art = _make_file_artifact(
            artifact_id="art-item",
            content_ref="item:art-item/file",
            size=len(item_data),
            checksum=_sha256(item_data),
        )
        registry.seed(item_art)

        # 3. Attachment-backed artifact (text)
        att_data = b"attachment content"
        store.seed("attachment:task-1/stored-1.txt", att_data)
        att_art = _make_file_artifact(
            artifact_id="art-att",
            name="file.txt",
            kind=ArtifactKind.TEXT,
            mime="text/plain",
            content_ref="attachment:task-1/stored-1.txt",
            size=len(att_data),
            checksum=_sha256(att_data),
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-1",
        )
        registry.seed(att_art)

        # 4. Workspace-backed artifact (json)
        ws_data = b'{"key": "value"}'
        store.seed("workspace:run-1/output.json", ws_data)
        ws_art = _make_file_artifact(
            artifact_id="art-ws",
            name="output.json",
            kind=ArtifactKind.JSON,
            mime="application/json",
            content_ref="workspace:run-1/output.json",
            size=len(ws_data),
            checksum=_sha256(ws_data),
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t1:run:1:artifact:0",
        )
        registry.seed(ws_art)

        svc = _make_service(registry=registry, content_store=store)
        stats = await svc.migrate_revisions(batch_size=100)
        assert stats["failed"] == 0
        assert stats["migrated"] == 4
        assert stats["processed"] == 4

        # Verify current_revision_id is set, revision_number==1
        for aid in ("art-inline", "art-item", "art-att", "art-ws"):
            art = await registry.get_artifact(aid)
            assert art.current_revision_id is not None
            rev = await registry.get_revision(aid, art.current_revision_id)
            assert rev is not None
            assert rev.revision_number == 1
            assert rev.change_summary == "migration backfill"
            assert rev.created_by == "system"

        # Inline: content is inline, no content_ref
        rev_inline = await registry.get_revision(
            "art-inline",
            (await registry.get_artifact("art-inline")).current_revision_id,
        )
        assert rev_inline.inline_content == inline_data.decode("utf-8")
        assert rev_inline.content_ref is None
        assert rev_inline.checksum == _sha256(inline_data)

        # File-backed: content_ref is a new item: path (not the source)
        for aid, src_ref, data in [
            ("art-item", "item:art-item/file", item_data),
            ("art-att", "attachment:task-1/stored-1.txt", att_data),
            ("art-ws", "workspace:run-1/output.json", ws_data),
        ]:
            rev = await registry.get_revision(
                aid, (await registry.get_artifact(aid)).current_revision_id,
            )
            assert rev.content_ref is not None
            assert rev.content_ref.startswith("item:")
            assert rev.content_ref != src_ref  # new path, not source
            assert rev.checksum == _sha256(data)
            # The new item: content exists in the store
            assert store.has(rev.content_ref)

        # Idempotency: re-run, no new revisions or items
        file_count_before = len(store._content)
        stats2 = await svc.migrate_revisions(batch_size=100)
        assert stats2["migrated"] == 0
        assert stats2["failed"] == 0
        assert stats2["processed"] == 0  # no candidates (all migrated)
        assert len(store._content) == file_count_before

        # Revision count unchanged
        for aid in ("art-inline", "art-item", "art-att", "art-ws"):
            page = await svc.list_revisions(aid, limit=100)
            assert len(page.items) == 1

    @pytest.mark.asyncio
    async def test_migration_backfill_failures(self):
        """migrate_revisions counts failures for unreadable source,
        checksum mismatch, and size exceeds limit.  Failed artifacts remain
        readable via legacy, and update/rollback/publish return
        MigrationIncomplete.  health failed_count is accurate."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()

        # 1. Unreadable source: content_ref not seeded in store
        unreadable_art = _make_file_artifact(
            artifact_id="art-unreadable",
            content_ref="item:art-unreadable/missing",
            size=10,
            checksum=_sha256(b"x" * 10),
        )
        registry.seed(unreadable_art)

        # 2. Checksum mismatch: store has different data than declared
        mismatch_data = b"actual content"
        store.seed("item:art-mismatch/file", mismatch_data)
        mismatch_art = _make_file_artifact(
            artifact_id="art-mismatch",
            content_ref="item:art-mismatch/file",
            size=999,
            checksum=_sha256(b"declared but different"),
        )
        registry.seed(mismatch_art)

        # 3. Size exceeds limit: data larger than artifact_max_bytes
        big_data = b"x" * 200
        store.seed("item:art-big/file", big_data)
        big_art = _make_file_artifact(
            artifact_id="art-big",
            content_ref="item:art-big/file",
            size=len(big_data),
            checksum=_sha256(big_data),
        )
        registry.seed(big_art)

        # Use a config with small artifact_max_bytes
        config = _make_config(artifact_max_bytes=100)
        svc = _make_service(
            registry=registry, content_store=store, config=config,
        )
        stats = await svc.migrate_revisions(batch_size=100)
        assert stats["failed"] == 3
        assert stats["migrated"] == 0
        assert stats["processed"] == 3

        # Failed artifacts remain unmigrated (current_revision_id is None)
        for aid in ("art-unreadable", "art-mismatch", "art-big"):
            art = await registry.get_artifact(aid)
            assert art.current_revision_id is None

        # Legacy content still readable for mismatch and big
        # (unreadable source was never seeded)
        data_mismatch, _ = await svc.get_content("art-mismatch")
        assert data_mismatch == mismatch_data

        # update/rollback/publish return MigrationIncomplete
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.update_revision(
                "art-mismatch",
                expected_revision_id="fake",
                inline_content="new",
            )
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.rollback(
                "art-mismatch",
                target_revision_id="fake",
                expected_revision_id="fake",
            )
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.publish_revision(
                "art-mismatch",
                revision_id="fake",
                expected_current_revision_id="fake",
            )

        # health failed_count is accurate
        status = svc.migration_status()
        assert status["state"] == "degraded"
        assert status["failed_count"] == 3
        # last_error is sanitized (no paths/refs/class names)
        assert status["last_error"] is not None
        assert "item:" not in status["last_error"]
        assert "art-" not in status["last_error"]
        assert "Error" not in status["last_error"]

    @pytest.mark.asyncio
    async def test_migration_status_for_health(self):
        """migration_status returns state in (ok, degraded), failed_count,
        and sanitized last_error."""
        # Default: migration not run yet
        svc = _make_service()
        status = svc.migration_status()
        assert status["state"] == "ok"
        assert status["failed_count"] == 0
        assert status["last_error"] is None

        # After successful migration
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# hi"))
        svc = _make_service(registry=registry)
        await svc.migrate_revisions()
        status = svc.migration_status()
        assert status["state"] == "ok"
        assert status["failed_count"] == 0
        assert status["last_error"] is None

        # After failed migration
        registry2 = FakeArtifactRegistry()
        store2 = FakeArtifactContentStore()
        # Unreadable source
        registry2.seed(_make_file_artifact(
            artifact_id="art-fail",
            content_ref="item:art-fail/missing",
            size=10,
            checksum=_sha256(b"x" * 10),
        ))
        svc2 = _make_service(registry=registry2, content_store=store2)
        await svc2.migrate_revisions()
        status = svc2.migration_status()
        assert status["state"] == "degraded"
        assert status["failed_count"] == 1
        assert status["last_error"] is not None
        # Sanitized: no paths, refs, or class names
        assert "/" not in status["last_error"]
        assert "item:" not in status["last_error"]
        assert "Error" not in status["last_error"]


class TestUnmigratedLegacyOperationMatrix:
    @pytest.mark.asyncio
    async def test_unmigrated_legacy_operation_matrix(self):
        """Unmigrated artifact: legacy ops available, revision ops blocked.

        - get_artifact / list_artifacts: available
        - get_content (legacy): available
        - export(original|html): available (legacy)
        - update_revision / rollback / publish_revision / diff_revisions /
          get_current_revision / get_revision: MigrationIncomplete
        - list_revisions: empty page
        - export(docx/pptx/xlsx): MigrationIncomplete
        - update_artifact (metadata only): available, no content write
        """
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        content = b"# Markdown content"
        store.seed("item:art-1/file", content)
        art = Artifact(
            id="art-1",
            name="doc.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            content_ref="item:art-1/file",
            inline_content=None,
            size=len(content),
            checksum=_sha256(content),
            source_kind=ArtifactSource.MANUAL,
            source_ref="art-1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            created_by="dashboard",
        )
        registry.seed(art)
        svc = _make_service(registry=registry, content_store=store)

        # Available legacy ops
        assert (await svc.get_artifact("art-1")).id == "art-1"
        page = await svc.list_artifacts(limit=50)
        assert len(page.items) == 1
        data, _ = await svc.get_content("art-1")
        assert data == content

        # export(original|html) available
        exp_data, exp_mime, _ = await svc.export("art-1", format="original")
        assert exp_data == content
        html_data, html_mime, _ = await svc.export("art-1", format="html")
        assert html_mime == "text/html"
        assert b"<html" in html_data.lower()

        # export_capabilities returns legacy caps
        caps = await svc.export_capabilities("art-1")
        assert "original" in caps
        assert "html" in caps

        # Revision ops blocked
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.update_revision(
                "art-1", expected_revision_id="fake", inline_content="new",
            )
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.rollback(
                "art-1", target_revision_id="fake",
                expected_revision_id="fake",
            )
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.publish_revision(
                "art-1", revision_id="fake",
                expected_current_revision_id="fake",
            )
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.diff_revisions("art-1", "a", "b")
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.get_current_revision("art-1")
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.get_revision("art-1", "fake")

        # list_revisions returns empty page
        rev_page = await svc.list_revisions("art-1", limit=50)
        assert len(rev_page.items) == 0
        assert rev_page.next_cursor is None

        # export(docx) -> MigrationIncomplete
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.export("art-1", format="docx")

        # update_artifact (metadata only) available, no content write
        updated = await svc.update_artifact("art-1", name="renamed.md")
        assert updated.name == "renamed.md"
        # Content columns unchanged
        assert updated.content_ref == "item:art-1/file"
        assert updated.inline_content is None
        assert updated.checksum == _sha256(content)
        assert updated.size == len(content)


class TestUnmigratedExportWithExporter:
    """Unmigrated artifacts with an exporter injected still get legacy
    original/html fallback (spec 157)."""

    @pytest.mark.asyncio
    async def test_unmigrated_export_original_with_exporter(self):
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        content = b"# Hi"
        store.seed("item:art-1/file", content)
        art = Artifact(
            id="art-1",
            name="doc.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            content_ref="item:art-1/file",
            inline_content=None,
            size=len(content),
            checksum=_sha256(content),
            source_kind=ArtifactSource.MANUAL,
            source_ref="art-1",
        )
        registry.seed(art)
        svc = _make_service(
            registry=registry, content_store=store,
            exporter=OfficeArtifactExporter(),
        )
        data, mime, _ = await svc.export("art-1", format="original")
        assert data == content
        assert mime == "text/markdown"

    @pytest.mark.asyncio
    async def test_unmigrated_export_html_with_exporter(self):
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Title"))
        svc = _make_service(
            registry=registry, exporter=OfficeArtifactExporter(),
        )
        data, mime, _ = await svc.export("art-1", format="html")
        assert mime == "text/html"
        assert b"<html" in data.lower()

    @pytest.mark.asyncio
    async def test_unmigrated_export_docx_with_exporter_blocked(self):
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Title"))
        svc = _make_service(
            registry=registry, exporter=OfficeArtifactExporter(),
        )
        with pytest.raises(ArtifactMigrationIncompleteError):
            await svc.export("art-1", format="docx")

    @pytest.mark.asyncio
    async def test_unmigrated_export_caps_with_exporter(self):
        from app.infrastructure.artifact.exporters import OfficeArtifactExporter
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Title"))
        svc = _make_service(
            registry=registry, exporter=OfficeArtifactExporter(),
        )
        caps = await svc.export_capabilities("art-1")
        # Legacy caps: markdown gets html + original
        assert "original" in caps
        assert "html" in caps
