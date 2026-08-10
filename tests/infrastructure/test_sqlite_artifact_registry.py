"""Tests for SQLiteArtifactRegistry (Infrastructure Layer).

Covers: idempotent schema, all spec columns, manual source_ref, source
unique key, CRUD, corrupted labels JSON, combined filters, cursor
pagination with mid-page insert, publish/backfill lifecycle, ON DELETE
SET NULL, partial unique index, replacement rollback, concurrent conflict
reread, different-artifact same-checksum no-reuse, and bounded backfill
cursor by attachment id.

Revision tests: artifact_revisions table DDL + constraints, revision CRUD,
CAS optimistic locking, delete_artifact_graph dedup/cascade, migration
idempotency, commit_initial_revision_backfill three-state semantics,
register_revision_publish with CAS, list/count migration candidates.

Uses tmp_path (no Docker, no service startup).
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest

from app.domain.artifact import (
    Artifact,
    ArtifactAttachmentSource,
    ArtifactConflictError,
    ArtifactDeleteGraph,
    ArtifactError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactListPage,
    ArtifactNotFoundError,
    ArtifactRevision,
    ArtifactRevisionConflictError,
    ArtifactRevisionValidationError,
    ArtifactSource,
    ArtifactStatus,
    PublishedArtifact,
    PublishedArtifactStatus,
    RevisionListCursor,
    RevisionListPage,
)
from app.infrastructure.registry.sqlite_artifact_registry import SQLiteArtifactRegistry
from app.infrastructure.registry.sqlite_task_registry import SCHEMA_SQL as TASK_SCHEMA_SQL

_VALID_CHECKSUM = "sha256:" + "a" * 64
_VALID_PUB_CHECKSUM = "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_artifact(**overrides) -> Artifact:
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
    if "source_ref" not in overrides:
        fields["source_ref"] = fields["id"]
    return Artifact(**fields)


def _binary_artifact(**overrides) -> Artifact:
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


def _dt(year, month, day, hour=0, minute=0, second=0, microsecond=0) -> datetime:
    return datetime(
        year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


async def test_schema_idempotent(tmp_path):
    """Creating the registry twice on the same DB must not error."""
    db = tmp_path / "sessions.db"
    SQLiteArtifactRegistry(str(db))
    SQLiteArtifactRegistry(str(db))


async def test_artifacts_table_columns(tmp_path):
    db = tmp_path / "sessions.db"
    SQLiteArtifactRegistry(str(db))
    with sqlite3.connect(str(db)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(artifacts)")}
    expected = {
        "id", "name", "kind", "mime", "content_ref", "inline_content",
        "size", "checksum", "source_kind", "source_ref", "source_context_ref",
        "source_session_id",
        "summary", "classification", "labels_json", "status", "created_by",
        "created_at", "updated_at",
    }
    assert expected <= cols


async def test_published_artifacts_table_columns(tmp_path):
    db = tmp_path / "sessions.db"
    SQLiteArtifactRegistry(str(db))
    with sqlite3.connect(str(db)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(published_artifacts)")}
    expected = {
        "publish_id", "artifact_id", "snapshot_name", "snapshot_kind",
        "snapshot_mime", "snapshot_content_ref", "snapshot_inline_content",
        "snapshot_size", "snapshot_checksum", "snapshot_summary",
        "published_by", "published_at", "status", "revoked_at",
    }
    assert expected <= cols


async def test_manual_source_ref_non_empty(tmp_path):
    """Manual source_ref must equal the artifact id (non-empty)."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-manual-1")
    assert art.source_ref == "art-manual-1"
    assert art.source_ref != ""
    created = await registry.create_artifact(art)
    assert created.source_ref == "art-manual-1"
    fetched = await registry.get_artifact("art-manual-1")
    assert fetched is not None
    assert fetched.source_ref == "art-manual-1"


async def test_source_unique_key_violation(tmp_path):
    """Duplicate (source_kind, source_ref) must raise ArtifactConflictError."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-dup")
    await registry.create_artifact(art)
    # Same source_kind=MANUAL, source_ref=art-dup (manual source_ref == id)
    art2 = _text_artifact(id="art-dup", checksum="sha256:" + "c" * 64)
    with pytest.raises(ArtifactConflictError):
        await registry.create_artifact(art2)


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


async def test_create_and_get_artifact(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(
        id="art-crud-1",
        labels=("alpha", "beta"),
        classification="public",
        source_context_ref="ctx://session/abc",
    )
    created = await registry.create_artifact(art)
    assert created.id == "art-crud-1"
    assert created.created_at is not None
    assert created.updated_at is not None
    assert created.labels == ("alpha", "beta")

    fetched = await registry.get_artifact("art-crud-1")
    assert fetched is not None
    assert fetched.name == "doc.txt"
    assert fetched.kind is ArtifactKind.TEXT
    assert fetched.inline_content == "hello"
    assert fetched.labels == ("alpha", "beta")
    assert fetched.classification == "public"
    assert fetched.source_context_ref == "ctx://session/abc"
    assert fetched.status is ArtifactStatus.DRAFT


async def test_create_binary_artifact(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _binary_artifact(id="art-bin-1")
    created = await registry.create_artifact(art)
    fetched = await registry.get_artifact("art-bin-1")
    assert fetched is not None
    assert fetched.kind is ArtifactKind.IMAGE
    assert fetched.inline_content is None
    assert fetched.content_ref == "store://bucket/logo.png"


async def test_get_artifact_not_found(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    assert await registry.get_artifact("nope") is None


async def test_update_artifact(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-upd-1")
    await registry.create_artifact(art)
    original = await registry.get_artifact("art-upd-1")
    assert original is not None
    updated = await registry.update_artifact(
        _text_artifact(
            id="art-upd-1",
            name="updated.txt",
            summary="new summary",
            status=ArtifactStatus.PUBLISHED,
            labels=("x", "y"),
            created_at=original.created_at,
        )
    )
    assert updated.name == "updated.txt"
    assert updated.summary == "new summary"
    assert updated.status is ArtifactStatus.PUBLISHED
    assert updated.labels == ("x", "y")
    assert updated.created_at == original.created_at


async def test_update_artifact_not_found(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    with pytest.raises(ArtifactNotFoundError):
        await registry.update_artifact(_text_artifact(id="ghost"))


async def test_delete_artifact(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-del-1")
    await registry.create_artifact(art)
    assert await registry.delete_artifact("art-del-1") is True
    assert await registry.get_artifact("art-del-1") is None


async def test_delete_artifact_returns_false_for_missing(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    assert await registry.delete_artifact("ghost") is False


async def test_get_by_source(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-src-1")
    await registry.create_artifact(art)
    found = await registry.get_by_source(ArtifactSource.MANUAL, "art-src-1")
    assert found is not None
    assert found.id == "art-src-1"
    assert await registry.get_by_source(ArtifactSource.MANUAL, "nope") is None


async def test_count_artifacts(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    assert await registry.count_artifacts() == 0
    await registry.create_artifact(_text_artifact(id="c1"))
    await registry.create_artifact(_text_artifact(id="c2", checksum="sha256:" + "d" * 64))
    assert await registry.count_artifacts() == 2
    await registry.delete_artifact("c1")
    assert await registry.count_artifacts() == 1


# ---------------------------------------------------------------------------
# Labels JSON corruption
# ---------------------------------------------------------------------------


async def test_corrupted_labels_json_raises(tmp_path):
    """Corrupted labels_json must raise a stable registry error, not silent default."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-bad-labels", labels=("ok",))
    await registry.create_artifact(art)
    # Corrupt labels_json directly in the DB
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE artifacts SET labels_json='{invalid json' WHERE id='art-bad-labels'"
        )
        conn.commit()
    with pytest.raises(ArtifactError):
        await registry.get_artifact("art-bad-labels")


# ---------------------------------------------------------------------------
# Combined filters
# ---------------------------------------------------------------------------


async def test_combined_filters(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(
        id="f1", name="alpha report", summary="quarterly data",
        kind=ArtifactKind.TEXT, status=ArtifactStatus.DRAFT,
        checksum="sha256:" + "01" * 32,
    ))
    await registry.create_artifact(_text_artifact(
        id="f2", name="beta report", summary="monthly summary",
        kind=ArtifactKind.MARKDOWN, status=ArtifactStatus.PUBLISHED,
        checksum="sha256:" + "02" * 32,
    ))
    await registry.create_artifact(_binary_artifact(
        id="f3", name="gamma image", summary="quarterly chart",
        kind=ArtifactKind.IMAGE, status=ArtifactStatus.DRAFT,
        checksum="sha256:" + "03" * 32,
    ))

    # Filter by source_kind only
    page = await registry.list_artifacts(source_kind=ArtifactSource.MANUAL)
    assert len(page.items) == 3

    # Filter by kind
    page = await registry.list_artifacts(kind=ArtifactKind.TEXT)
    assert {a.id for a in page.items} == {"f1"}

    # Filter by status
    page = await registry.list_artifacts(status=ArtifactStatus.PUBLISHED)
    assert {a.id for a in page.items} == {"f2"}

    # Filter by q (text search)
    page = await registry.list_artifacts(q="quarterly")
    assert {a.id for a in page.items} == {"f1", "f3"}

    # Combined: source_kind + kind + status + q
    page = await registry.list_artifacts(
        source_kind=ArtifactSource.MANUAL,
        kind=ArtifactKind.TEXT,
        status=ArtifactStatus.DRAFT,
        q="report",
    )
    assert {a.id for a in page.items} == {"f1"}


# ---------------------------------------------------------------------------
# source_session_id: persistence, migration, filter, backfill query
# ---------------------------------------------------------------------------


async def test_source_session_id_persisted(tmp_path):
    """source_session_id is written on create and read back on get."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(
        id="art-sess-1",
        source_kind=ArtifactSource.TASK_ARTIFACT,
        source_ref="task:t1:run:1:artifact:0",
        source_context_ref="t1",
        source_session_id="task-sess-1",
        checksum="sha256:" + "11" * 32,
    )
    await registry.create_artifact(art)
    fetched = await registry.get_artifact("art-sess-1")
    assert fetched is not None
    assert fetched.source_session_id == "task-sess-1"
    assert fetched.source_context_ref == "t1"


async def test_source_session_id_defaults_null(tmp_path):
    """Omitting source_session_id stores NULL, read back as None."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-null", checksum="sha256:" + "12" * 32))
    fetched = await registry.get_artifact("art-null")
    assert fetched is not None
    assert fetched.source_session_id is None


async def test_source_session_id_migration(tmp_path):
    """A DB created before the column exists is migrated idempotently."""
    db = tmp_path / "sessions.db"
    # Create the artifacts table WITHOUT source_session_id, then construct the
    # registry which must add the column via the idempotent migration.
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                mime TEXT NOT NULL,
                content_ref TEXT,
                inline_content TEXT,
                size INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_context_ref TEXT,
                summary TEXT NOT NULL DEFAULT '',
                classification TEXT,
                labels_json TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    registry = SQLiteArtifactRegistry(str(db))
    with sqlite3.connect(str(db)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(artifacts)")}
    assert "source_session_id" in cols
    # Registry is usable after migration.
    await registry.create_artifact(_text_artifact(
        id="art-mig", source_session_id="sess-mig",
        checksum="sha256:" + "13" * 32,
    ))
    fetched = await registry.get_artifact("art-mig")
    assert fetched is not None
    assert fetched.source_session_id == "sess-mig"


async def test_update_artifact_persists_source_session_id(tmp_path):
    """update_artifact writes source_session_id (used by session backfill)."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(
        id="art-upd",
        source_kind=ArtifactSource.TASK_ARTIFACT,
        source_context_ref="t9",
        checksum="sha256:" + "14" * 32,
    )
    await registry.create_artifact(art)
    assert art.source_session_id is None
    from dataclasses import replace as dc_replace
    updated = await registry.update_artifact(
        dc_replace(art, source_session_id="task-sess-9")
    )
    assert updated.source_session_id == "task-sess-9"
    fetched = await registry.get_artifact("art-upd")
    assert fetched is not None
    assert fetched.source_session_id == "task-sess-9"


async def test_list_filter_by_source_session_id(tmp_path):
    """list_artifacts filters by source_session_id (the panel query)."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(
        id="a1", source_kind=ArtifactSource.TASK_ARTIFACT,
        source_context_ref="t1", source_session_id="sess-A",
        checksum="sha256:" + "21" * 32,
    ))
    await registry.create_artifact(_binary_artifact(
        id="a2", source_kind=ArtifactSource.TASK_ATTACHMENT,
        source_context_ref="t1", source_session_id="sess-A",
        checksum="sha256:" + "22" * 32,
    ))
    await registry.create_artifact(_text_artifact(
        id="a3", source_kind=ArtifactSource.TASK_ARTIFACT,
        source_context_ref="t2", source_session_id="sess-B",
        checksum="sha256:" + "23" * 32,
    ))

    page = await registry.list_artifacts(source_session_id="sess-A")
    assert {a.id for a in page.items} == {"a1", "a2"}
    # Both task source kinds are found (no source_kind filter) -- this is the
    # behavior the conversation panel relies on.
    assert {a.source_kind for a in page.items} == {
        ArtifactSource.TASK_ARTIFACT, ArtifactSource.TASK_ATTACHMENT,
    }

    page = await registry.list_artifacts(source_session_id="sess-B")
    assert {a.id for a in page.items} == {"a3"}

    page = await registry.list_artifacts(source_session_id="sess-missing")
    assert page.items == ()


async def test_list_task_artifacts_missing_session(tmp_path):
    """Backfill query returns only task-source artifacts with NULL session."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    # task_artifact, no session -> should appear
    await registry.create_artifact(_text_artifact(
        id="miss1", source_kind=ArtifactSource.TASK_ARTIFACT,
        source_context_ref="t1", checksum="sha256:" + "31" * 32,
    ))
    # task_attachment, no session -> should appear
    await registry.create_artifact(_binary_artifact(
        id="miss2", source_kind=ArtifactSource.TASK_ATTACHMENT,
        source_context_ref="t2", checksum="sha256:" + "32" * 32,
    ))
    # task_artifact WITH session -> should NOT appear
    await registry.create_artifact(_text_artifact(
        id="has1", source_kind=ArtifactSource.TASK_ARTIFACT,
        source_context_ref="t3", source_session_id="sess-3",
        checksum="sha256:" + "33" * 32,
    ))
    # manual, no session -> should NOT appear (not a task source)
    await registry.create_artifact(_text_artifact(
        id="man1", source_kind=ArtifactSource.MANUAL,
        checksum="sha256:" + "34" * 32,
    ))

    missing = await registry.list_task_artifacts_missing_session(limit=200)
    ids = {a.id for a in missing}
    assert ids == {"miss1", "miss2"}

    # After backfilling miss1, it drops out of the next query.
    from dataclasses import replace as dc_replace
    miss1 = next(a for a in missing if a.id == "miss1")
    await registry.update_artifact(dc_replace(miss1, source_session_id="sess-1"))
    missing2 = await registry.list_task_artifacts_missing_session(limit=200)
    assert {a.id for a in missing2} == {"miss2"}


async def test_list_artifacts_with_empty_mime(tmp_path):
    """Kind backfill query returns only artifacts with empty mime."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    # empty mime (OTHER) -> should appear
    await registry.create_artifact(_binary_artifact(
        id="empty1", name="task-output-b.md",
        kind=ArtifactKind.OTHER, mime="",
        source_kind=ArtifactSource.TASK_ARTIFACT,
        source_context_ref="t1", checksum="sha256:" + "41" * 32,
    ))
    # empty mime, unknown extension -> should appear (backfill skips it)
    await registry.create_artifact(_binary_artifact(
        id="empty2", name="blob.dat",
        kind=ArtifactKind.OTHER, mime="",
        checksum="sha256:" + "42" * 32,
    ))
    # non-empty mime -> should NOT appear
    await registry.create_artifact(_text_artifact(
        id="has1", name="doc.md",
        kind=ArtifactKind.MARKDOWN, mime="text/markdown",
        checksum="sha256:" + "43" * 32,
    ))

    empties = await registry.list_artifacts_with_empty_mime(limit=200)
    assert {a.id for a in empties} == {"empty1", "empty2"}

    # After backfilling empty1 (mime populated), it drops out of the next query.
    from dataclasses import replace as dc_replace
    empty1 = next(a for a in empties if a.id == "empty1")
    await registry.update_artifact(dc_replace(
        empty1, mime="text/markdown", kind=ArtifactKind.MARKDOWN,
    ))
    empties2 = await registry.list_artifacts_with_empty_mime(limit=200)
    assert {a.id for a in empties2} == {"empty2"}


# ---------------------------------------------------------------------------
# Cursor pagination -- insert between pages
# ---------------------------------------------------------------------------


async def test_cursor_pagination_no_repeat(tmp_path):
    """Inserting a record between two pages must not cause already-consumed
    items to repeat on the next page."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    t1 = _dt(2024, 1, 1, 0, 0, 0)
    t2 = _dt(2024, 1, 1, 0, 0, 1)
    t3 = _dt(2024, 1, 1, 0, 0, 2)
    await registry.create_artifact(_text_artifact(
        id="pg-a", updated_at=t1, checksum="sha256:" + "0a" * 32,
    ))
    await registry.create_artifact(_text_artifact(
        id="pg-b", updated_at=t2, checksum="sha256:" + "0b" * 32,
    ))
    await registry.create_artifact(_text_artifact(
        id="pg-c", updated_at=t3, checksum="sha256:" + "0c" * 32,
    ))

    # Page 1: limit=2 -> [pg-c (t3), pg-b (t2)] (DESC)
    page1 = await registry.list_artifacts(limit=2)
    assert [a.id for a in page1.items] == ["pg-c", "pg-b"]
    assert page1.next_cursor is not None

    # Insert a record between pg-b and pg-c (t2.5)
    t2_5 = _dt(2024, 1, 1, 0, 0, 1, 500000)
    await registry.create_artifact(_text_artifact(
        id="pg-d", updated_at=t2_5, checksum="sha256:" + "0d" * 32,
    ))

    # Page 2: cursor at pg-b -> only items strictly after pg-b in DESC order
    page2 = await registry.list_artifacts(
        cursor=page1.next_cursor, limit=2,
    )
    page2_ids = {a.id for a in page2.items}
    # pg-c was already consumed in page1, must NOT repeat
    assert "pg-c" not in page2_ids
    # pg-b was already consumed, must NOT repeat
    assert "pg-b" not in page2_ids
    # pg-a is the only remaining item below the cursor
    assert "pg-a" in page2_ids
    assert page2.next_cursor is None


# ---------------------------------------------------------------------------
# Publish lifecycle
# ---------------------------------------------------------------------------


async def test_register_and_get_published(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-pub-1")
    await registry.create_artifact(art)
    pub = _published(publish_id="pub-001", artifact_id="art-pub-1")
    result = await registry.register_published(pub)
    assert result.publish_id == "pub-001"
    assert result.artifact_id == "art-pub-1"
    assert result.status is PublishedArtifactStatus.ACTIVE
    assert result.published_at is not None

    fetched = await registry.get_published("pub-001")
    assert fetched is not None
    assert fetched.publish_id == "pub-001"
    assert fetched.snapshot_name == "doc.txt"


async def test_register_published_syncs_artifact_status(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    art = _text_artifact(id="art-sync-1", status=ArtifactStatus.DRAFT)
    await registry.create_artifact(art)
    pub = _published(publish_id="pub-sync-1", artifact_id="art-sync-1")
    await registry.register_published(pub)
    updated = await registry.get_artifact("art-sync-1")
    assert updated is not None
    assert updated.status is ArtifactStatus.PUBLISHED


async def test_get_active_publish(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-act-1"))
    assert await registry.get_active_publish("art-act-1") is None
    await registry.register_published(
        _published(publish_id="pub-act-1", artifact_id="art-act-1")
    )
    active = await registry.get_active_publish("art-act-1")
    assert active is not None
    assert active.publish_id == "pub-act-1"


async def test_list_published(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-lp-1"))
    await registry.create_artifact(
        _text_artifact(id="art-lp-2", checksum="sha256:" + "e" * 64)
    )
    await registry.register_published(
        _published(publish_id="pub-lp-1", artifact_id="art-lp-1")
    )
    await registry.register_published(
        _published(
            publish_id="pub-lp-2", artifact_id="art-lp-2",
            snapshot_checksum="sha256:" + "ee" * 32,
        )
    )
    # List all
    all_pubs = await registry.list_published()
    assert len(all_pubs) == 2
    # List by artifact
    pubs1 = await registry.list_published("art-lp-1")
    assert len(pubs1) == 1
    assert pubs1[0].publish_id == "pub-lp-1"


async def test_revoke_published(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-rev-1"))
    await registry.register_published(
        _published(publish_id="pub-rev-1", artifact_id="art-rev-1")
    )
    revoked = await registry.revoke_published("art-rev-1")
    assert revoked is not None
    assert revoked.status is PublishedArtifactStatus.REVOKED
    assert revoked.revoked_at is not None
    # No more active publish
    assert await registry.get_active_publish("art-rev-1") is None


async def test_revoke_published_idempotent(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-rev-id-1"))
    await registry.register_published(
        _published(publish_id="pub-rev-id-1", artifact_id="art-rev-id-1")
    )
    first = await registry.revoke_published("art-rev-id-1")
    assert first is not None
    assert first.status is PublishedArtifactStatus.REVOKED
    second = await registry.revoke_published("art-rev-id-1")
    assert second is not None
    assert second.publish_id == first.publish_id
    assert second.status is PublishedArtifactStatus.REVOKED


async def test_revoke_published_no_publish_returns_none(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    assert await registry.revoke_published("ghost-artifact") is None


# ---------------------------------------------------------------------------
# ON DELETE SET NULL
# ---------------------------------------------------------------------------


async def test_on_delete_set_null(tmp_path):
    """After deleting the source artifact, the published snapshot row still
    exists with artifact_id NULL."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-del-pub-1"))
    await registry.register_published(
        _published(publish_id="pub-del-1", artifact_id="art-del-pub-1")
    )
    # Delete the source artifact
    assert await registry.delete_artifact("art-del-pub-1") is True
    # Published row still exists, artifact_id is NULL
    pub = await registry.get_published("pub-del-1")
    assert pub is not None
    assert pub.artifact_id is None
    assert pub.status is PublishedArtifactStatus.ACTIVE


# ---------------------------------------------------------------------------
# Partial unique index -- at most one active per artifact_id
# ---------------------------------------------------------------------------


async def test_partial_unique_index_rereads_existing(tmp_path):
    """Registering a second active publish for the same artifact_id must
    not create a duplicate; the existing active is reread and returned."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-pui-1"))
    pub1 = _published(publish_id="pub-pui-1", artifact_id="art-pui-1")
    await registry.register_published(pub1)
    # Try to register a second active for the same artifact (no revoke)
    pub2 = _published(
        publish_id="pub-pui-2", artifact_id="art-pui-1",
        snapshot_checksum="sha256:" + "ff" * 32,
    )
    result = await registry.register_published(pub2)
    # Must return the existing active publish, not pub2
    assert result.publish_id == "pub-pui-1"
    # Only one published row exists
    all_pubs = await registry.list_published("art-pui-1")
    assert len(all_pubs) == 1


# ---------------------------------------------------------------------------
# Duplicate publish_id PK -- must raise ArtifactConflictError, not silent return
# ---------------------------------------------------------------------------


async def test_duplicate_publish_id_raises_conflict(tmp_path):
    """A duplicate publish_id (PK violation) for an artifact with no active
    publish must raise ArtifactConflictError, not silently return an
    existing publish from a different artifact."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-dup-pk-1"))
    await registry.create_artifact(
        _text_artifact(id="art-dup-pk-2", checksum="sha256:" + "9" * 64)
    )
    # Register pub-dup-1 for artifact 1 (succeeds)
    await registry.register_published(
        _published(publish_id="pub-dup-1", artifact_id="art-dup-pk-1")
    )
    # Try to register the SAME publish_id for artifact 2 (no active publish).
    # This is a PK violation, NOT a concurrent active-publish collision.
    dup_pub = _published(
        publish_id="pub-dup-1", artifact_id="art-dup-pk-2",
        snapshot_checksum="sha256:" + "33" * 32,
    )
    with pytest.raises(ArtifactConflictError):
        await registry.register_published(dup_pub)
    # Artifact 2 must NOT have any publish
    assert await registry.get_active_publish("art-dup-pk-2") is None


async def test_duplicate_publish_id_same_artifact_after_revoke(tmp_path):
    """A duplicate publish_id for the same artifact (after revocation, no
    active publish) must raise ArtifactConflictError."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-dup-rev-1"))
    await registry.register_published(
        _published(publish_id="pub-dup-rev-1", artifact_id="art-dup-rev-1")
    )
    # Revoke -> no active publish for this artifact
    await registry.revoke_published("art-dup-rev-1")
    # Try to register the SAME publish_id again (PK violation, no active)
    dup_pub = _published(
        publish_id="pub-dup-rev-1", artifact_id="art-dup-rev-1",
        snapshot_checksum="sha256:" + "44" * 32,
    )
    with pytest.raises(ArtifactConflictError):
        await registry.register_published(dup_pub)


# ---------------------------------------------------------------------------
# Replacement transaction rollback -- old active preserved on failure
# ---------------------------------------------------------------------------


async def test_replacement_rollback_preserves_old_active(tmp_path):
    """If register_published fails mid-transaction (FK violation), the old
    active publish must remain active (full rollback)."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-rb-1"))
    await registry.register_published(
        _published(publish_id="pub-rb-old", artifact_id="art-rb-1")
    )
    # Attempt replacement with a publish referencing a non-existent artifact
    # (FK violation on published_artifacts.artifact_id)
    bad_pub = _published(
        publish_id="pub-rb-new",
        artifact_id="nonexistent-artifact",
        snapshot_checksum="sha256:" + "11" * 32,
    )
    with pytest.raises(ArtifactConflictError):
        await registry.register_published(bad_pub, revoke_artifact_id="art-rb-1")
    # Old active publish must still be active
    active = await registry.get_active_publish("art-rb-1")
    assert active is not None
    assert active.publish_id == "pub-rb-old"
    assert active.status is PublishedArtifactStatus.ACTIVE


# ---------------------------------------------------------------------------
# Same-checksum concurrent conflict rereads existing
# ---------------------------------------------------------------------------


async def test_same_checksum_concurrent_rereads_existing(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-sc-1"))
    checksum = "sha256:" + "ab" * 32
    pub1 = _published(
        publish_id="pub-sc-1", artifact_id="art-sc-1",
        snapshot_checksum=checksum,
    )
    await registry.register_published(pub1)
    # Concurrent publish with same checksum and same artifact_id
    pub2 = _published(
        publish_id="pub-sc-2", artifact_id="art-sc-1",
        snapshot_checksum=checksum,
    )
    result = await registry.register_published(pub2)
    # Must reread the existing active, not insert pub2
    assert result.publish_id == "pub-sc-1"
    assert result.snapshot_checksum == checksum


# ---------------------------------------------------------------------------
# Different artifact with same checksum does NOT reuse
# ---------------------------------------------------------------------------


async def test_different_artifact_same_checksum_no_reuse(tmp_path):
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-da-1"))
    await registry.create_artifact(
        _text_artifact(id="art-da-2", checksum="sha256:" + "9" * 64)
    )
    checksum = "sha256:" + "cd" * 32
    pub1 = _published(
        publish_id="pub-da-1", artifact_id="art-da-1",
        snapshot_checksum=checksum,
    )
    pub2 = _published(
        publish_id="pub-da-2", artifact_id="art-da-2",
        snapshot_checksum=checksum,
    )
    r1 = await registry.register_published(pub1)
    r2 = await registry.register_published(pub2)
    # Both succeed independently (different artifact_id)
    assert r1.publish_id == "pub-da-1"
    assert r2.publish_id == "pub-da-2"
    assert r1.artifact_id == "art-da-1"
    assert r2.artifact_id == "art-da-2"
    # Both are active
    assert await registry.get_active_publish("art-da-1") is not None
    assert await registry.get_active_publish("art-da-2") is not None


# ---------------------------------------------------------------------------
# Replacement via revoke_artifact_id (happy path)
# ---------------------------------------------------------------------------


async def test_register_published_replacement(tmp_path):
    """Replacing an active publish via revoke_artifact_id in one transaction."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await registry.create_artifact(_text_artifact(id="art-repl-1"))
    await registry.register_published(
        _published(publish_id="pub-repl-old", artifact_id="art-repl-1")
    )
    new_pub = _published(
        publish_id="pub-repl-new", artifact_id="art-repl-1",
        snapshot_checksum="sha256:" + "22" * 32,
        snapshot_name="doc-v2.txt",
    )
    result = await registry.register_published(
        new_pub, revoke_artifact_id="art-repl-1",
    )
    assert result.publish_id == "pub-repl-new"
    assert result.status is PublishedArtifactStatus.ACTIVE
    # Old publish is revoked
    old = await registry.get_published("pub-repl-old")
    assert old is not None
    assert old.status is PublishedArtifactStatus.REVOKED
    # New publish is active
    active = await registry.get_active_publish("art-repl-1")
    assert active is not None
    assert active.publish_id == "pub-repl-new"


# ---------------------------------------------------------------------------
# Backfill -- list_attachment_sources with stable cursor by attachment id
# ---------------------------------------------------------------------------


def _setup_task_attachments(db_path) -> None:
    """Create the task schema + insert fixture attachment rows."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(TASK_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tasks (id, title, created_at, updated_at) "
            "VALUES ('task-1', 'test task', "
            "'2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')"
        )
        for i in range(5):
            conn.execute(
                "INSERT INTO task_attachments "
                "(id, task_id, filename, stored_name, content_type, size, "
                "checksum, uploaded_by, created_at) "
                "VALUES (?, 'task-1', ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"ta_{i:03d}",
                    f"file{i}.txt",
                    f"stored{i}",
                    "text/plain",
                    100 + i,
                    f"sha256:{i:064x}",
                    "user-1",
                    "2024-01-01T00:00:00+00:00",
                ),
            )
        conn.commit()


async def test_list_attachment_sources_pagination(tmp_path):
    """list_attachment_sources returns bounded batches with a stable cursor
    by attachment id."""
    db = tmp_path / "sessions.db"
    _setup_task_attachments(db)
    registry = SQLiteArtifactRegistry(str(db))

    # Page 1: limit=2
    batch1 = await registry.list_attachment_sources(limit=2)
    assert len(batch1) == 2
    assert all(isinstance(s, ArtifactAttachmentSource) for s in batch1)
    assert batch1[0].attachment_id == "ta_000"
    assert batch1[1].attachment_id == "ta_001"
    assert batch1[0].filename == "file0.txt"
    assert batch1[0].stored_name == "stored0"
    assert batch1[0].content_type == "text/plain"
    assert batch1[0].size == 100
    assert batch1[0].checksum == "sha256:" + "0" * 64
    assert batch1[0].uploaded_by == "user-1"
    assert batch1[0].task_id == "task-1"

    # Page 2: after ta_001, limit=2
    batch2 = await registry.list_attachment_sources(
        after_attachment_id="ta_001", limit=2,
    )
    assert len(batch2) == 2
    assert batch2[0].attachment_id == "ta_002"
    assert batch2[1].attachment_id == "ta_003"

    # Page 3: after ta_003, limit=2 -- only 1 remaining
    batch3 = await registry.list_attachment_sources(
        after_attachment_id="ta_003", limit=2,
    )
    assert len(batch3) == 1
    assert batch3[0].attachment_id == "ta_004"

    # Page 4: after ta_004 -- exhausted
    batch4 = await registry.list_attachment_sources(
        after_attachment_id="ta_004", limit=2,
    )
    assert len(batch4) == 0

    # Full scan from start
    all_sources = await registry.list_attachment_sources(limit=100)
    assert len(all_sources) == 5
    assert [s.attachment_id for s in all_sources] == [
        "ta_000", "ta_001", "ta_002", "ta_003", "ta_004",
    ]


# ---------------------------------------------------------------------------
# source_context_ref precise filter
# ---------------------------------------------------------------------------


async def _seed_source_context_ref_data(registry: SQLiteArtifactRegistry) -> None:
    """Seed artifacts with varied source_kind / source_context_ref for filter tests."""
    # Session A: two artifacts
    await registry.create_artifact(_text_artifact(
        id="sc-session-a-1", source_kind=ArtifactSource.SESSION,
        source_ref="session-a:art-1", source_context_ref="session-a",
        updated_at=_dt(2024, 1, 1, 0, 0, 1),
        checksum="sha256:" + "a1" * 32,
    ))
    await registry.create_artifact(_text_artifact(
        id="sc-session-a-2", source_kind=ArtifactSource.SESSION,
        source_ref="session-a:art-2", source_context_ref="session-a",
        updated_at=_dt(2024, 1, 1, 0, 0, 2),
        checksum="sha256:" + "a2" * 32,
    ))
    # Session B: one artifact
    await registry.create_artifact(_text_artifact(
        id="sc-session-b-1", source_kind=ArtifactSource.SESSION,
        source_ref="session-b:art-1", source_context_ref="session-b",
        updated_at=_dt(2024, 1, 1, 0, 0, 3),
        checksum="sha256:" + "b1" * 32,
    ))
    # Manual with NULL source_context_ref
    await registry.create_artifact(_text_artifact(
        id="sc-manual-null", source_context_ref=None,
        updated_at=_dt(2024, 1, 1, 0, 0, 4),
        checksum="sha256:" + "c1" * 32,
    ))
    # Manual with empty-string source_context_ref
    await registry.create_artifact(_text_artifact(
        id="sc-manual-empty", source_context_ref="",
        updated_at=_dt(2024, 1, 1, 0, 0, 5),
        checksum="sha256:" + "c2" * 32,
    ))
    # Task attachment with source_context_ref="task-1"
    await registry.create_artifact(_text_artifact(
        id="sc-task-att-1", source_kind=ArtifactSource.TASK_ATTACHMENT,
        source_ref="att-1", source_context_ref="task-1",
        updated_at=_dt(2024, 1, 1, 0, 0, 6),
        checksum="sha256:" + "d1" * 32,
    ))


async def test_source_context_ref_excludes_other_sessions_kinds_and_null(tmp_path):
    """source_kind=session + source_context_ref=session-a excludes session B,
    other source kinds, and NULL context."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await _seed_source_context_ref_data(registry)

    page = await registry.list_artifacts(
        source_kind=ArtifactSource.SESSION,
        source_context_ref="session-a",
    )
    ids = {a.id for a in page.items}
    assert ids == {"sc-session-a-1", "sc-session-a-2"}


async def test_source_context_ref_omit_empty_specific_distinguishable(tmp_path):
    """Omitting, empty string, and a specific value are distinguishable.
    Empty string only matches empty string, not NULL."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await _seed_source_context_ref_data(registry)

    all_ids = {
        "sc-session-a-1", "sc-session-a-2", "sc-session-b-1",
        "sc-manual-null", "sc-manual-empty", "sc-task-att-1",
    }

    # Omit -> returns all (no source_context_ref filter)
    page = await registry.list_artifacts()
    assert {a.id for a in page.items} == all_ids

    # Empty string -> only the artifact whose source_context_ref == ""
    page = await registry.list_artifacts(source_context_ref="")
    assert {a.id for a in page.items} == {"sc-manual-empty"}

    # Specific value -> only session-a artifacts
    page = await registry.list_artifacts(source_context_ref="session-a")
    assert {a.id for a in page.items} == {"sc-session-a-1", "sc-session-a-2"}


async def test_source_context_ref_combined_with_kind_status_q_is_and(tmp_path):
    """source_context_ref AND-combines with kind, status, and q filters."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await _seed_source_context_ref_data(registry)

    # Add a published session-a artifact with a distinctive name
    await registry.create_artifact(_text_artifact(
        id="sc-session-a-pub", name="published report",
        source_kind=ArtifactSource.SESSION,
        source_ref="session-a:art-pub", source_context_ref="session-a",
        status=ArtifactStatus.PUBLISHED,
        updated_at=_dt(2024, 1, 1, 0, 0, 7),
        checksum="sha256:" + "a7" * 32,
    ))

    # context + kind
    page = await registry.list_artifacts(
        source_context_ref="session-a", kind=ArtifactKind.TEXT,
    )
    assert {a.id for a in page.items} == {
        "sc-session-a-1", "sc-session-a-2", "sc-session-a-pub",
    }

    # context + status
    page = await registry.list_artifacts(
        source_context_ref="session-a", status=ArtifactStatus.PUBLISHED,
    )
    assert {a.id for a in page.items} == {"sc-session-a-pub"}

    # context + q
    page = await registry.list_artifacts(
        source_context_ref="session-a", q="report",
    )
    assert {a.id for a in page.items} == {"sc-session-a-pub"}

    # context + kind + status + q (all AND)
    page = await registry.list_artifacts(
        source_context_ref="session-a",
        kind=ArtifactKind.TEXT,
        status=ArtifactStatus.PUBLISHED,
        q="report",
    )
    assert {a.id for a in page.items} == {"sc-session-a-pub"}


async def test_source_context_ref_with_cursor_and_limit(tmp_path):
    """source_context_ref + cursor + limit preserves updated_at DESC, id DESC
    ordering and the next_cursor contract."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    # Create 3 session-a artifacts with distinct updated_at
    for i in range(3):
        await registry.create_artifact(_text_artifact(
            id=f"sc-page-{i}",
            source_kind=ArtifactSource.SESSION,
            source_ref=f"session-a:page-{i}",
            source_context_ref="session-a",
            updated_at=_dt(2024, 1, 1, 0, 0, i),
            checksum=f"sha256:{i:064x}",
        ))
    # Create a session-b artifact that must NOT appear
    await registry.create_artifact(_text_artifact(
        id="sc-page-b",
        source_kind=ArtifactSource.SESSION,
        source_ref="session-b:page-b",
        source_context_ref="session-b",
        updated_at=_dt(2024, 1, 1, 0, 0, 10),
        checksum="sha256:" + "bb" * 32,
    ))

    # Page 1: limit=2 -> newest two session-a artifacts (DESC)
    page1 = await registry.list_artifacts(
        source_context_ref="session-a", limit=2,
    )
    assert [a.id for a in page1.items] == ["sc-page-2", "sc-page-1"]
    assert page1.next_cursor is not None

    # Page 2: cursor from page1 -> remaining one session-a artifact
    page2 = await registry.list_artifacts(
        source_context_ref="session-a", limit=2, cursor=page1.next_cursor,
    )
    assert [a.id for a in page2.items] == ["sc-page-0"]
    assert page2.next_cursor is None


async def test_source_context_ref_omit_matches_existing_behavior(tmp_path):
    """Omitting source_context_ref yields the same results as before the
    filter was added (no WHERE clause for source_context_ref)."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))
    await _seed_source_context_ref_data(registry)

    # Omit source_context_ref, filter only by source_kind
    page = await registry.list_artifacts(source_kind=ArtifactSource.SESSION)
    assert {a.id for a in page.items} == {
        "sc-session-a-1", "sc-session-a-2", "sc-session-b-1",
    }

    # Omit source_context_ref, no other filters
    page = await registry.list_artifacts()
    assert len(page.items) == 6


async def test_source_context_ref_special_chars_precise_match(tmp_path):
    """Values containing single quotes, double quotes, %, _, backslash, and
    non-ASCII characters are matched with = (exact). Similar-but-unequal
    records prove no LIKE expansion or SQL injection."""
    db = tmp_path / "sessions.db"
    registry = SQLiteArtifactRegistry(str(db))

    # Exact-value records
    exact = [
        ("sc-quote", "O'Brien"),
        ("sc-dquote", 'say "hello"'),
        ("sc-percent", "100%"),
        ("sc-underscore", "session_a"),
        ("sc-backslash", "path\\to"),
        ("sc-unicode", "会话-α"),
    ]
    for idx, (aid, ctx) in enumerate(exact):
        await registry.create_artifact(_text_artifact(
            id=aid, source_kind=ArtifactSource.SESSION,
            source_ref=f"session:{aid}", source_context_ref=ctx,
            checksum=f"sha256:{idx:064x}",
        ))

    # Similar-but-unequal records that WOULD match under LIKE
    similar = [
        ("sc-percent-like", "100Xpercent"),    # LIKE '100%' matches
        ("sc-underscore-like", "sessionXa"),   # LIKE 'session_a' matches
    ]
    for idx, (aid, ctx) in enumerate(similar):
        await registry.create_artifact(_text_artifact(
            id=aid, source_kind=ArtifactSource.SESSION,
            source_ref=f"session:{aid}", source_context_ref=ctx,
            checksum=f"sha256:{(idx + 10):064x}",
        ))

    # Each exact value matches only its own record
    for aid, ctx in exact:
        page = await registry.list_artifacts(source_context_ref=ctx)
        ids = {a.id for a in page.items}
        assert ids == {aid}, f"filter by {ctx!r} returned {ids}, expected {{{aid}}}"

    # Percent does NOT match the similar record (no LIKE expansion)
    page = await registry.list_artifacts(source_context_ref="100%")
    assert {a.id for a in page.items} == {"sc-percent"}

    # Underscore does NOT match the similar record (no LIKE expansion)
    page = await registry.list_artifacts(source_context_ref="session_a")
    assert {a.id for a in page.items} == {"sc-underscore"}


# ===========================================================================
# Revision tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Revision helpers
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _revision(
    artifact_id: str,
    *,
    id: str = "rev-1",
    revision_number: int = 1,
    parent_revision_id: str | None = None,
    rollback_from_revision_id: str | None = None,
    inline_content: str | None = "hello",
    content_ref: str | None = None,
    checksum: str | None = None,
    mime: str = "text/plain",
    kind: ArtifactKind = ArtifactKind.TEXT,
    change_summary: str = "",
    created_by: str = "user-1",
    source_session_id: str | None = None,
    source_run_id: str | None = None,
    created_at: datetime | None = None,
) -> ArtifactRevision:
    """Build a valid ArtifactRevision (inline text by default)."""
    if inline_content is not None:
        size = len(inline_content.encode("utf-8"))
        if checksum is None:
            checksum = _sha(inline_content)
    elif content_ref is not None:
        size = 100
        if checksum is None:
            checksum = _sha(content_ref)
    else:
        raise ValueError("must provide inline_content or content_ref")
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    return ArtifactRevision(
        id=id,
        artifact_id=artifact_id,
        revision_number=revision_number,
        parent_revision_id=parent_revision_id,
        rollback_from_revision_id=rollback_from_revision_id,
        content_ref=content_ref,
        inline_content=inline_content,
        size=size,
        checksum=checksum,
        mime=mime,
        kind=kind,
        created_at=created_at,
        change_summary=change_summary,
        created_by=created_by,
        source_session_id=source_session_id,
        source_run_id=source_run_id,
    )


def _binary_revision(
    artifact_id: str,
    *,
    id: str = "rev-1",
    revision_number: int = 1,
    parent_revision_id: str | None = None,
    rollback_from_revision_id: str | None = None,
    content_ref: str | None = None,
    checksum: str | None = None,
    mime: str = "image/png",
    kind: ArtifactKind = ArtifactKind.IMAGE,
    **kwargs,
) -> ArtifactRevision:
    if content_ref is None:
        content_ref = f"item:{artifact_id}/file.bin"
    return _revision(
        artifact_id,
        id=id,
        revision_number=revision_number,
        parent_revision_id=parent_revision_id,
        rollback_from_revision_id=rollback_from_revision_id,
        inline_content=None,
        content_ref=content_ref,
        checksum=checksum,
        mime=mime,
        kind=kind,
        **kwargs,
    )


def _create_legacy_db_before_registry_init(
    db_path,
    *,
    inline: str = "hello",
    artifact_id: str = "art-legacy",
    content_ref: str | None = None,
) -> str:
    """Create a DB with the old schema (no revisions, no current_revision_id,
    no published_revision_id) and insert one artifact row.  Returns the
    artifact_id."""
    size = len(inline.encode("utf-8")) if inline else 0
    if content_ref is not None:
        size = 100
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                mime TEXT NOT NULL,
                content_ref TEXT,
                inline_content TEXT,
                size INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_context_ref TEXT,
                source_session_id TEXT,
                summary TEXT NOT NULL DEFAULT '',
                classification TEXT,
                labels_json TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE published_artifacts (
                publish_id TEXT PRIMARY KEY,
                artifact_id TEXT,
                snapshot_name TEXT NOT NULL,
                snapshot_kind TEXT NOT NULL,
                snapshot_mime TEXT NOT NULL,
                snapshot_content_ref TEXT,
                snapshot_inline_content TEXT,
                snapshot_size INTEGER NOT NULL,
                snapshot_checksum TEXT NOT NULL,
                snapshot_summary TEXT NOT NULL DEFAULT '',
                published_by TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                revoked_at TEXT,
                FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
            )
        """)
        conn.execute(
            "INSERT INTO artifacts (id, name, kind, mime, content_ref, "
            "inline_content, size, checksum, source_kind, source_ref, "
            "source_context_ref, source_session_id, summary, classification, "
            "labels_json, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id, "legacy.txt", "text", "text/plain",
                content_ref, inline if content_ref is None else None,
                size, _VALID_CHECKSUM, "manual", artifact_id,
                None, None, "", None, None, "draft", "user-1",
                "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    return artifact_id


# ---------------------------------------------------------------------------
# Schema -- artifact_revisions table, constraints, foreign keys
# ---------------------------------------------------------------------------


async def test_revisions_table_created_with_constraints(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    with reg._connect() as conn:
        info = {r["name"] for r in conn.execute("PRAGMA table_info(artifact_revisions)")}
    assert {
        "id", "artifact_id", "revision_number", "parent_revision_id",
        "rollback_from_revision_id", "content_ref", "inline_content",
        "size", "checksum", "mime", "kind", "change_summary",
        "created_by", "source_session_id", "source_run_id", "created_at",
    } <= info

    # UNIQUE(artifact_id, revision_number) via partial unique index or table constraint
    with reg._connect() as conn:
        idx_list = conn.execute(
            "PRAGMA index_list(artifact_revisions)"
        ).fetchall()
    has_unique = any(
        idx["origin"] == "u" or idx["name"] == "sqlite_autoindex_artifact_revisions_1"
        for idx in idx_list
    )
    assert has_unique, f"expected UNIQUE constraint on (artifact_id, revision_number), got {idx_list}"


async def test_revisions_check_constraints_enforced(tmp_path):
    """CHECK constraints reject invalid data at the DB level."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    # Create an artifact first so FK is satisfied
    art = _text_artifact(id="art-cc")
    await reg.create_artifact(art)
    now = datetime.now(timezone.utc).isoformat()
    base = (
        "INSERT INTO artifact_revisions (id, artifact_id, revision_number, "
        "parent_revision_id, rollback_from_revision_id, content_ref, "
        "inline_content, size, checksum, mime, kind, change_summary, "
        "created_by, source_session_id, source_run_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with reg._connect() as conn:
        # revision_number < 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (
                "r0", "art-cc", 0, None, None, None, "x", 1,
                _VALID_CHECKSUM, "text/plain", "text", "", "u", None, None, now,
            ))
        # size < 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (
                "r-neg", "art-cc", 1, None, None, None, "x", -1,
                _VALID_CHECKSUM, "text/plain", "text", "", "u", None, None, now,
            ))
        # both content_ref and inline_content set
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (
                "r-both", "art-cc", 1, None, None, "item:art-cc/f", "x", 1,
                _VALID_CHECKSUM, "text/plain", "text", "", "u", None, None, now,
            ))
        # neither content_ref nor inline_content
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (
                "r-none", "art-cc", 1, None, None, None, None, 0,
                _VALID_CHECKSUM, "text/plain", "text", "", "u", None, None, now,
            ))
        # empty-string content_ref
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (
                "r-empty", "art-cc", 1, None, None, "", None, 0,
                _VALID_CHECKSUM, "text/plain", "text", "", "u", None, None, now,
            ))


async def test_revisions_unique_artifact_revision_number(tmp_path):
    """UNIQUE(artifact_id, revision_number) rejects duplicates."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-uq"), _revision("art-uq", id="r1"),
    )
    # Try to insert a second revision with the same (artifact_id, revision_number=1)
    now = datetime.now(timezone.utc).isoformat()
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(str(reg.path)) as conn:
            conn.execute(
                "INSERT INTO artifact_revisions (id, artifact_id, revision_number, "
                "parent_revision_id, rollback_from_revision_id, content_ref, "
                "inline_content, size, checksum, mime, kind, change_summary, "
                "created_by, source_session_id, source_run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("r-dup", "art-uq", 1, None, None, None, "dup", 3,
                 _VALID_CHECKSUM, "text/plain", "text", "", "u", None, None, now),
            )


async def test_foreign_key_list_has_four_keys(tmp_path):
    """PRAGMA foreign_key_list on artifact_revisions shows artifact, parent,
    rollback FKs; published_artifacts shows published_revision_id FK."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    with reg._connect() as conn:
        rev_fks = conn.execute(
            "PRAGMA foreign_key_list(artifact_revisions)"
        ).fetchall()
        pub_fks = conn.execute(
            "PRAGMA foreign_key_list(published_artifacts)"
        ).fetchall()
    # artifact_revisions should have 3 FKs: artifact_id, parent, rollback
    rev_fk_tables = {fk["table"] for fk in rev_fks}
    assert "artifacts" in rev_fk_tables
    assert "artifact_revisions" in rev_fk_tables
    # published_artifacts should have FK to artifacts and artifact_revisions
    pub_fk_tables = {fk["table"] for fk in pub_fks}
    assert "artifacts" in pub_fk_tables
    assert "artifact_revisions" in pub_fk_tables


async def test_foreign_keys_on_every_connection(tmp_path):
    """Every connection opened by the registry has PRAGMA foreign_keys=ON."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    with reg._connect() as conn:
        val = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert val == 1


async def test_migration_adds_revision_columns_to_legacy_db(tmp_path):
    """A legacy DB (no current_revision_id / published_revision_id) gets
    migrated idempotently."""
    db = tmp_path / "a.db"
    _create_legacy_db_before_registry_init(db)
    # First construction migrates
    reg = SQLiteArtifactRegistry(str(db))
    with sqlite3.connect(str(db)) as conn:
        art_cols = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
        pub_cols = {r[1] for r in conn.execute("PRAGMA table_info(published_artifacts)")}
        rev_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='artifact_revisions'"
        ).fetchone()
    assert "current_revision_id" in art_cols
    assert "published_revision_id" in pub_cols
    assert rev_exists is not None
    # Second construction is idempotent
    SQLiteArtifactRegistry(str(db))


# ---------------------------------------------------------------------------
# create_artifact_with_initial_revision + append_revision
# ---------------------------------------------------------------------------


async def test_create_artifact_with_initial_revision(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art = _text_artifact(id="art-ci")
    rev = _revision("art-ci", id="rev-1", inline_content="v1")
    created_art, created_rev = await reg.create_artifact_with_initial_revision(art, rev)
    assert created_art.current_revision_id == "rev-1"
    assert created_rev.id == "rev-1"
    assert created_rev.revision_number == 1
    # Fetch and verify
    fetched = await reg.get_artifact("art-ci")
    assert fetched is not None
    assert fetched.current_revision_id == "rev-1"
    revs = await reg.list_revisions("art-ci")
    assert len(revs.items) == 1
    assert revs.items[0].id == "rev-1"


async def test_create_with_initial_revision_validates_artifact_id(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art = _text_artifact(id="art-mismatch")
    rev = _revision("art-other", id="rev-1")
    with pytest.raises(ArtifactRevisionValidationError):
        await reg.create_artifact_with_initial_revision(art, rev)


async def test_create_with_initial_revision_validates_initial_shape(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art = _text_artifact(id="art-bad-init")
    # revision_number=2 with parent -- not an initial revision
    rev = _revision("art-bad-init", id="rev-1", revision_number=2,
                    parent_revision_id="rev-0")
    with pytest.raises(ArtifactRevisionValidationError):
        await reg.create_artifact_with_initial_revision(art, rev)


async def test_create_with_initial_revision_rejects_revision_number_2_no_parent(tmp_path):
    """revision_number=2 with parent=None must be rejected: initial revision
    must have revision_number==1."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art = _text_artifact(id="art-bad-num")
    rev = _revision("art-bad-num", id="rev-1", revision_number=2,
                    parent_revision_id=None)
    with pytest.raises(ArtifactRevisionValidationError):
        await reg.create_artifact_with_initial_revision(art, rev)


async def test_append_revision_happy_path(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-app"), _revision("art-app", id="r1", inline_content="v1"),
    )
    r2 = await reg.append_revision(
        "art-app",
        _revision("art-app", id="r2", revision_number=2, parent_revision_id="r1",
                  inline_content="v2"),
        expected_revision_id="r1",
    )
    assert r2.id == "r2"
    assert r2.revision_number == 2
    fetched = await reg.get_artifact("art-app")
    assert fetched is not None
    assert fetched.current_revision_id == "r2"
    revs = await reg.list_revisions("art-app")
    assert [r.revision_number for r in revs.items] == [2, 1]


async def test_append_revision_cas_conflict(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-cas"), _revision("art-cas", id="r1", inline_content="v1"),
    )
    r2 = await reg.append_revision(
        "art-cas",
        _revision("art-cas", id="r2", revision_number=2, parent_revision_id="r1",
                  inline_content="v2"),
        expected_revision_id="r1",
    )
    # CAS with stale expected -> conflict
    with pytest.raises(ArtifactRevisionConflictError):
        await reg.append_revision(
            "art-cas",
            _revision("art-cas", id="r3", revision_number=3, parent_revision_id="r1",
                      inline_content="v3"),
            expected_revision_id="r1",
        )


async def test_append_revision_wrong_parent_rejected(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-wp"), _revision("art-wp", id="r1", inline_content="v1"),
    )
    r2 = await reg.append_revision(
        "art-wp",
        _revision("art-wp", id="r2", revision_number=2, parent_revision_id="r1",
                  inline_content="v2"),
        expected_revision_id="r1",
    )
    # parent=r1 but current is r2 -> validation error (parent != current)
    with pytest.raises(ArtifactRevisionValidationError):
        await reg.append_revision(
            "art-wp",
            _revision("art-wp", id="r3", revision_number=3, parent_revision_id="r1",
                      inline_content="v3"),
            expected_revision_id="r2",
        )


async def test_append_revision_wrong_number_rejected(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-wn"), _revision("art-wn", id="r1", inline_content="v1"),
    )
    # revision_number=5 but expected next is 2
    with pytest.raises(ArtifactRevisionValidationError):
        await reg.append_revision(
            "art-wn",
            _revision("art-wn", id="r2", revision_number=5, parent_revision_id="r1",
                      inline_content="v2"),
            expected_revision_id="r1",
        )


async def test_append_revision_artifact_not_found(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    with pytest.raises(ArtifactNotFoundError):
        await reg.append_revision(
            "ghost",
            _revision("ghost", id="r1"),
            expected_revision_id="none",
        )


# ---------------------------------------------------------------------------
# get_revision
# ---------------------------------------------------------------------------


async def test_get_revision(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-gr"), _revision("art-gr", id="r1", inline_content="v1"),
    )
    fetched = await reg.get_revision("art-gr", "r1")
    assert fetched is not None
    assert fetched.id == "r1"
    assert fetched.inline_content == "v1"


async def test_get_revision_cross_artifact_returns_none(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-ca1"), _revision("art-ca1", id="r1", inline_content="v1"),
    )
    art2, r2 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-ca2", checksum=_sha("x")),
        _revision("art-ca2", id="r2", inline_content="v2"),
    )
    # Revision r2 belongs to art-ca2, not art-ca1
    assert await reg.get_revision("art-ca1", "r2") is None
    assert await reg.get_revision("art-ca2", "r1") is None


async def test_get_revision_not_found(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-gn"), _revision("art-gn", id="r1", inline_content="v1"),
    )
    assert await reg.get_revision("art-gn", "ghost") is None


# ---------------------------------------------------------------------------
# list_revisions with cursor pagination
# ---------------------------------------------------------------------------


async def test_list_revisions_descending(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-lr"), _revision("art-lr", id="r1", inline_content="1"),
    )
    r2 = await reg.append_revision(
        "art-lr", _revision("art-lr", id="r2", revision_number=2,
                            parent_revision_id="r1", inline_content="2"),
        expected_revision_id="r1",
    )
    r3 = await reg.append_revision(
        "art-lr", _revision("art-lr", id="r3", revision_number=3,
                            parent_revision_id="r2", inline_content="3"),
        expected_revision_id="r2",
    )
    page = await reg.list_revisions("art-lr")
    assert [r.revision_number for r in page.items] == [3, 2, 1]
    assert page.next_cursor is None


async def test_list_revisions_cursor_pagination(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-pg"), _revision("art-pg", id="r1", inline_content="1"),
    )
    prev = r1
    for i in range(2, 6):
        r = await reg.append_revision(
            "art-pg", _revision("art-pg", id=f"r{i}", revision_number=i,
                                parent_revision_id=prev.id, inline_content=str(i)),
            expected_revision_id=prev.id,
        )
        prev = r
    # Page 1: limit=2 -> [r5, r4]
    page1 = await reg.list_revisions("art-pg", limit=2)
    assert [r.id for r in page1.items] == ["r5", "r4"]
    assert page1.next_cursor is not None
    # Page 2: cursor -> [r3, r2]
    page2 = await reg.list_revisions("art-pg", cursor=page1.next_cursor, limit=2)
    assert [r.id for r in page2.items] == ["r3", "r2"]
    assert page2.next_cursor is not None
    # Page 3: cursor -> [r1]
    page3 = await reg.list_revisions("art-pg", cursor=page2.next_cursor, limit=2)
    assert [r.id for r in page3.items] == ["r1"]
    assert page3.next_cursor is None


async def test_list_revisions_cursor_wrong_artifact_rejected(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-cw"), _revision("art-cw", id="r1", inline_content="1"),
    )
    bad_cursor = RevisionListCursor(artifact_id="other", revision_number=1, id="r1")
    with pytest.raises(ArtifactRevisionValidationError):
        await reg.list_revisions("art-cw", cursor=bad_cursor)


async def test_list_revisions_empty(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art = _text_artifact(id="art-empty")
    await reg.create_artifact(art)
    page = await reg.list_revisions("art-empty")
    assert page.items == ()
    assert page.next_cursor is None


# ---------------------------------------------------------------------------
# delete_artifact_graph
# ---------------------------------------------------------------------------


async def test_delete_artifact_graph_returns_refs_and_cascades(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-dg", content_ref="item:art-dg/legacy", inline_content=None,
                       kind=ArtifactKind.OTHER, mime="application/octet-stream",
                       size=100, checksum=_sha("legacy")),
        _binary_revision("art-dg", id="r1", content_ref="item:art-dg/r1.bin"),
    )
    r2 = await reg.append_revision(
        "art-dg", _binary_revision("art-dg", id="r2", revision_number=2,
                                   parent_revision_id="r1", content_ref="item:art-dg/r2.bin"),
        expected_revision_id="r1",
    )
    # Publish a snapshot
    pub = _published(publish_id="pub-dg", artifact_id="art-dg")
    await reg.register_published(pub)
    graph = await reg.delete_artifact_graph("art-dg")
    assert isinstance(graph, ArtifactDeleteGraph)
    assert "item:art-dg/r1.bin" in graph.revision_content_refs
    assert "item:art-dg/r2.bin" in graph.revision_content_refs
    assert graph.legacy_artifact_content_ref == "item:art-dg/legacy"
    assert "pub-dg" in graph.publish_snapshot_ids
    # Artifact is gone
    assert await reg.get_artifact("art-dg") is None
    # Revisions are gone (cascaded)
    revs = await reg.list_revisions("art-dg")
    assert revs.items == ()


async def test_delete_graph_dedups_shared_refs(tmp_path):
    """A rollback revision sharing the same content_ref as an earlier revision
    should have that ref deduplicated in the returned graph."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    shared_ref = "item:art-dd/shared.bin"
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-dd", checksum=_sha("base")),
        _binary_revision("art-dd", id="r1", content_ref=shared_ref),
    )
    r2 = await reg.append_revision(
        "art-dd", _binary_revision("art-dd", id="r2", revision_number=2,
                                   parent_revision_id="r1", content_ref="item:art-dd/v2.bin"),
        expected_revision_id="r1",
    )
    # Rollback: r3 shares r1's content_ref
    r3 = await reg.append_revision(
        "art-dd", _binary_revision("art-dd", id="r3", revision_number=3,
                                   parent_revision_id="r2", rollback_from_revision_id="r2",
                                   content_ref=shared_ref),
        expected_revision_id="r2",
    )
    graph = await reg.delete_artifact_graph("art-dd")
    # shared_ref appears only once
    assert graph.revision_content_refs.count(shared_ref) == 1


async def test_delete_graph_missing_artifact(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    graph = await reg.delete_artifact_graph("ghost")
    assert graph.revision_content_refs == ()
    assert graph.legacy_artifact_content_ref is None
    assert graph.publish_snapshot_ids == ()


# ---------------------------------------------------------------------------
# Metadata update preserves current_revision_id
# ---------------------------------------------------------------------------


async def test_metadata_update_preserves_current_revision_id(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-mu"), _revision("art-mu", id="r1", inline_content="v1"),
    )
    r2 = await reg.append_revision(
        "art-mu", _revision("art-mu", id="r2", revision_number=2,
                            parent_revision_id="r1", inline_content="v2"),
        expected_revision_id="r1",
    )
    # Concurrent metadata updates must not reset current_revision_id
    await asyncio.gather(
        *[reg.update_artifact(_text_artifact(
            id="art-mu", name=f"n{i}", created_at=art.created_at, updated_at=art.updated_at,
            checksum=_sha("v1"), current_revision_id=r2.id,
        )) for i in range(5)]
    )
    cur = await reg.get_artifact("art-mu")
    assert cur is not None
    assert cur.current_revision_id == r2.id


# ---------------------------------------------------------------------------
# CAS concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_append_only_one_wins(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, rev1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-cc"), _revision("art-cc", id="r1", inline_content="v1"),
    )
    rev_a = _revision("art-cc", id="ra", revision_number=2, parent_revision_id="r1",
                      inline_content="a")
    rev_b = _revision("art-cc", id="rb", revision_number=2, parent_revision_id="r1",
                      inline_content="b")
    r = await asyncio.gather(
        reg.append_revision("art-cc", rev_a, expected_revision_id="r1"),
        reg.append_revision("art-cc", rev_b, expected_revision_id="r1"),
        return_exceptions=True,
    )
    successes = [x for x in r if not isinstance(x, Exception)]
    conflicts = [x for x in r if isinstance(x, ArtifactRevisionConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    revs = await reg.list_revisions("art-cc")
    assert len(revs.items) == 2  # r1 + one of ra/rb


# ---------------------------------------------------------------------------
# Migration candidates + count
# ---------------------------------------------------------------------------


async def test_list_revision_migration_candidates(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    # art-with-rev has a revision
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-wr"), _revision("art-wr", id="r1", inline_content="v1"),
    )
    # art-no-rev has no revision (legacy create)
    await reg.create_artifact(_text_artifact(id="art-nr", checksum=_sha("nr")))
    page = await reg.list_revision_migration_candidates(cursor=None, limit=50)
    ids = {a.id for a in page.items}
    assert "art-nr" in ids
    assert "art-wr" not in ids


async def test_count_artifacts_without_revision(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    assert await reg.count_artifacts_without_revision() == 0
    await reg.create_artifact(_text_artifact(id="a1", checksum=_sha("a1")))
    await reg.create_artifact(_text_artifact(id="a2", checksum=_sha("a2")))
    assert await reg.count_artifacts_without_revision() == 2
    # Create one WITH revision
    await reg.create_artifact_with_initial_revision(
        _text_artifact(id="a3", checksum=_sha("a3")),
        _revision("a3", id="r3", inline_content="v"),
    )
    assert await reg.count_artifacts_without_revision() == 2


# ---------------------------------------------------------------------------
# commit_initial_revision_backfill -- three states + idempotency + rejection
# ---------------------------------------------------------------------------


async def test_backfill_state_no_revisions_inserts_and_backfills(tmp_path):
    """State 3: no revisions, current_revision_id is NULL -> insert revision +
    backfill current."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    await reg.create_artifact(_text_artifact(id="art-b3", checksum=_sha("base")))
    rev = _revision("art-b3", id="rev-b3", inline_content="backfilled")
    result = await reg.commit_initial_revision_backfill("art-b3", rev)
    assert result.id == "rev-b3"
    art = await reg.get_artifact("art-b3")
    assert art.current_revision_id == "rev-b3"
    revs = await reg.list_revisions("art-b3")
    assert len(revs.items) == 1
    assert revs.items[0].revision_number == 1


async def test_backfill_state_existing_rev1_backfills_current_only(tmp_path):
    """State 2: revision_number=1 exists but current_revision_id is NULL ->
    backfill current only (no insert)."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    # Create artifact with initial revision (sets current_revision_id)
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-b2", checksum=_sha("base")),
        _revision("art-b2", id="rev-b2", inline_content="v1"),
    )
    # Simulate legacy: clear current_revision_id via raw SQL
    with sqlite3.connect(str(reg.path)) as conn:
        conn.execute("UPDATE artifacts SET current_revision_id = NULL WHERE id = 'art-b2'")
        conn.commit()
    # Backfill should find rev-b2 (revision_number=1) and set current only
    dummy = _revision("art-b2", id="rev-dummy", inline_content="dummy")
    result = await reg.commit_initial_revision_backfill("art-b2", dummy)
    assert result.id == "rev-b2"
    art = await reg.get_artifact("art-b2")
    assert art.current_revision_id == "rev-b2"
    # No dummy revision was inserted
    revs = await reg.list_revisions("art-b2")
    assert len(revs.items) == 1
    assert revs.items[0].id == "rev-b2"


async def test_backfill_state_valid_current_skips(tmp_path):
    """State 1: current_revision_id already points to a valid revision ->
    skip (idempotent)."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-b1", checksum=_sha("base")),
        _revision("art-b1", id="rev-b1", inline_content="v1"),
    )
    dummy = _revision("art-b1", id="rev-dummy", inline_content="dummy")
    result = await reg.commit_initial_revision_backfill("art-b1", dummy)
    assert result.id == "rev-b1"
    art = await reg.get_artifact("art-b1")
    assert art.current_revision_id == "rev-b1"
    # No dummy inserted
    revs = await reg.list_revisions("art-b1")
    assert len(revs.items) == 1
    assert revs.items[0].id == "rev-b1"


async def test_backfill_idempotent_run_twice(tmp_path):
    """Running backfill twice is safe: second call is a no-op (state 1)."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    await reg.create_artifact(_text_artifact(id="art-id", checksum=_sha("base")))
    rev = _revision("art-id", id="rev-id", inline_content="v1")
    r1 = await reg.commit_initial_revision_backfill("art-id", rev)
    r2 = await reg.commit_initial_revision_backfill("art-id", rev)
    assert r1.id == r2.id == "rev-id"
    revs = await reg.list_revisions("art-id")
    assert len(revs.items) == 1


async def test_backfill_rejects_cross_artifact_pointer(tmp_path):
    """current_revision_id pointing to another artifact's revision -> fail,
    no auto-fix."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art_a, r_a = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-ca-a", checksum=_sha("a")),
        _revision("art-ca-a", id="rev-a", inline_content="a"),
    )
    art_b, r_b = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-ca-b", checksum=_sha("b")),
        _revision("art-ca-b", id="rev-b", inline_content="b"),
    )
    # Point art-b's current_revision_id to art-a's revision
    with sqlite3.connect(str(reg.path)) as conn:
        conn.execute(
            "UPDATE artifacts SET current_revision_id = 'rev-a' WHERE id = 'art-ca-b'"
        )
        conn.commit()
    dummy = _revision("art-ca-b", id="rev-dummy", inline_content="dummy")
    with pytest.raises((ArtifactRevisionValidationError, ArtifactError)):
        await reg.commit_initial_revision_backfill("art-ca-b", dummy)
    # No pseudo revision created
    revs = await reg.list_revisions("art-ca-b")
    ids = {r.id for r in revs.items}
    assert "rev-dummy" not in ids


async def test_backfill_rejects_nonexistent_pointer(tmp_path):
    """current_revision_id pointing to a non-existent revision -> fail."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    await reg.create_artifact(_text_artifact(id="art-ne", checksum=_sha("base")))
    with sqlite3.connect(str(reg.path)) as conn:
        conn.execute(
            "UPDATE artifacts SET current_revision_id = 'ghost-rev' WHERE id = 'art-ne'"
        )
        conn.commit()
    dummy = _revision("art-ne", id="rev-dummy", inline_content="dummy")
    with pytest.raises((ArtifactRevisionValidationError, ArtifactError)):
        await reg.commit_initial_revision_backfill("art-ne", dummy)


async def test_backfill_artifact_not_found(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    with pytest.raises(ArtifactNotFoundError):
        await reg.commit_initial_revision_backfill("ghost", _revision("ghost", id="r"))


async def test_backfill_db_failure_no_residue(tmp_path):
    """If the insert fails (e.g. duplicate revision id), the transaction
    rolls back -- no revision row and current_revision_id stays NULL."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    await reg.create_artifact(_text_artifact(id="art-rb", checksum=_sha("base")))
    # Pre-insert a revision with id "rev-dup" but revision_number=2 (not 1)
    # so backfill goes to state 3 (insert) and hits PK violation on id.
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(reg.path)) as conn:
        conn.execute(
            "INSERT INTO artifact_revisions (id, artifact_id, revision_number, "
            "parent_revision_id, rollback_from_revision_id, content_ref, "
            "inline_content, size, checksum, mime, kind, change_summary, "
            "created_by, source_session_id, source_run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("rev-dup", "art-rb", 2, None, None, None, "pre", 3,
             _sha("pre"), "text/plain", "text", "", "u", None, None, now),
        )
        conn.commit()
    # Now backfill with same id -> PK violation -> rollback
    rev = _revision("art-rb", id="rev-dup", inline_content="backfill")
    with pytest.raises((ArtifactConflictError, ArtifactError)):
        await reg.commit_initial_revision_backfill("art-rb", rev)
    art = await reg.get_artifact("art-rb")
    assert art.current_revision_id is None


# ---------------------------------------------------------------------------
# register_revision_publish
# ---------------------------------------------------------------------------


async def test_register_revision_publish_happy_path(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-rp"), _revision("art-rp", id="r1", inline_content="v1"),
    )
    pub = _published(publish_id="pub-rp", artifact_id="art-rp",
                     snapshot_checksum=_sha("v1"))
    result = await reg.register_revision_publish(
        pub, artifact_id="art-rp", revision_id="r1",
        expected_current_revision_id="r1",
    )
    assert result.publish_id == "pub-rp"
    assert result.published_revision_id == "r1"
    assert result.status is PublishedArtifactStatus.ACTIVE
    art2 = await reg.get_artifact("art-rp")
    assert art2.status is ArtifactStatus.PUBLISHED


async def test_register_revision_publish_cas_conflict(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-rpc"), _revision("art-rpc", id="r1", inline_content="v1"),
    )
    r2 = await reg.append_revision(
        "art-rpc", _revision("art-rpc", id="r2", revision_number=2,
                             parent_revision_id="r1", inline_content="v2"),
        expected_revision_id="r1",
    )
    pub = _published(publish_id="pub-rpc", artifact_id="art-rpc",
                     snapshot_checksum=_sha("v2"))
    with pytest.raises(ArtifactRevisionConflictError):
        await reg.register_revision_publish(
            pub, artifact_id="art-rpc", revision_id="r2",
            expected_current_revision_id="r1",  # stale
        )


async def test_register_revision_publish_reuses_same_checksum(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-rr"), _revision("art-rr", id="r1", inline_content="v1"),
    )
    checksum = _sha("v1")
    pub1 = _published(publish_id="pub-rr-1", artifact_id="art-rr",
                      snapshot_checksum=checksum)
    await reg.register_revision_publish(
        pub1, artifact_id="art-rr", revision_id="r1",
        expected_current_revision_id="r1",
    )
    # Second publish with same checksum -> reuse
    pub2 = _published(publish_id="pub-rr-2", artifact_id="art-rr",
                      snapshot_checksum=checksum)
    result = await reg.register_revision_publish(
        pub2, artifact_id="art-rr", revision_id="r1",
        expected_current_revision_id="r1",
    )
    assert result.publish_id == "pub-rr-1"  # reused, not pub-rr-2


async def test_register_revision_publish_replaces_old_active(tmp_path):
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-rs"), _revision("art-rs", id="r1", inline_content="v1"),
    )
    r2 = await reg.append_revision(
        "art-rs", _revision("art-rs", id="r2", revision_number=2,
                            parent_revision_id="r1", inline_content="v2"),
        expected_revision_id="r1",
    )
    pub1 = _published(publish_id="pub-rs-1", artifact_id="art-rs",
                      snapshot_checksum=_sha("v1"))
    await reg.register_revision_publish(
        pub1, artifact_id="art-rs", revision_id="r1",
        expected_current_revision_id="r2",
    )
    # Publish r2 with different checksum -> revoke old, insert new
    pub2 = _published(publish_id="pub-rs-2", artifact_id="art-rs",
                      snapshot_checksum=_sha("v2"))
    result = await reg.register_revision_publish(
        pub2, artifact_id="art-rs", revision_id="r2",
        expected_current_revision_id="r2",
    )
    assert result.publish_id == "pub-rs-2"
    assert result.published_revision_id == "r2"
    old = await reg.get_published("pub-rs-1")
    assert old.status is PublishedArtifactStatus.REVOKED


async def test_register_revision_publish_duplicate_id_raises_not_returns_old(tmp_path):
    """A duplicate publish_id (PK violation) with a different checksum must
    raise ArtifactConflictError, NOT silently return the old active publish
    (which has a different checksum)."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-dp"), _revision("art-dp", id="r1", inline_content="v1"),
    )
    # First publish with checksum A
    pub1 = _published(publish_id="pub-dup", artifact_id="art-dp",
                      snapshot_checksum=_sha("v1"))
    await reg.register_revision_publish(
        pub1, artifact_id="art-dp", revision_id="r1",
        expected_current_revision_id="r1",
    )
    # Second publish with SAME publish_id (PK violation) but DIFFERENT checksum
    pub2 = _published(publish_id="pub-dup", artifact_id="art-dp",
                      snapshot_checksum=_sha("different"))
    with pytest.raises(ArtifactConflictError):
        await reg.register_revision_publish(
            pub2, artifact_id="art-dp", revision_id="r1",
            expected_current_revision_id="r1",
        )
    # Old active must still be the original (checksum A), not replaced
    active = await reg.get_active_publish("art-dp")
    assert active is not None
    assert active.publish_id == "pub-dup"
    assert active.snapshot_checksum == _sha("v1")


# ---------------------------------------------------------------------------
# Migration from legacy DB + backfill integration (Registry-only, no service)
# ---------------------------------------------------------------------------


async def test_migration_then_backfill_legacy_artifact(tmp_path):
    """Legacy DB with an artifact (no revisions, no current_revision_id) ->
    construct registry (migration) -> commit_initial_revision_backfill ->
    verify current_revision_id set; run again -> idempotent."""
    db = tmp_path / "a.db"
    artifact_id = _create_legacy_db_before_registry_init(db, inline="hello")
    reg = SQLiteArtifactRegistry(str(db))
    # Artifact exists, current_revision_id is NULL
    art = await reg.get_artifact(artifact_id)
    assert art is not None
    assert art.current_revision_id is None
    # Backfill
    rev = _revision(artifact_id, id="rev-mig", inline_content="hello")
    result = await reg.commit_initial_revision_backfill(artifact_id, rev)
    assert result.id == "rev-mig"
    art2 = await reg.get_artifact(artifact_id)
    assert art2.current_revision_id == "rev-mig"
    revs = await reg.list_revisions(artifact_id)
    assert len(revs.items) == 1
    assert revs.items[0].revision_number == 1
    # Idempotent
    result2 = await reg.commit_initial_revision_backfill(artifact_id, rev)
    assert result2.id == "rev-mig"
    revs2 = await reg.list_revisions(artifact_id)
    assert len(revs2.items) == 1


async def test_rollback_revision_append(tmp_path):
    """A rollback revision (rollback_from_revision_id set) can be appended."""
    reg = SQLiteArtifactRegistry(str(tmp_path / "a.db"))
    art, r1 = await reg.create_artifact_with_initial_revision(
        _text_artifact(id="art-rb2"), _revision("art-rb2", id="r1", inline_content="v1"),
    )
    r2 = await reg.append_revision(
        "art-rb2", _revision("art-rb2", id="r2", revision_number=2,
                             parent_revision_id="r1", inline_content="v2"),
        expected_revision_id="r1",
    )
    # Rollback to r1: parent=r2 (current), rollback_from=r2
    r3 = await reg.append_revision(
        "art-rb2", _revision("art-rb2", id="r3", revision_number=3,
                             parent_revision_id="r2", rollback_from_revision_id="r2",
                             inline_content="v1"),
        expected_revision_id="r2",
    )
    assert r3.id == "r3"
    assert r3.rollback_from_revision_id == "r2"
    art2 = await reg.get_artifact("art-rb2")
    assert art2.current_revision_id == "r3"
