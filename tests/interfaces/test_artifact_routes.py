"""T12: Dashboard management API tests for artifact routes.

Tests the full /chat/artifacts* surface using a fake ArtifactService and
TestClient. Covers list/filter/cursor, POST JSON inline + multipart upload,
GET detail (no content_ref), GET content, PATCH (strict field whitelist),
DELETE, export (original/html), publish lifecycle (POST/GET/DELETE), error
envelope, security headers, and the /artifacts shell route.
"""
from __future__ import annotations

import hashlib
import io
import json
import secrets
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.artifact_service import (
    ArtifactTooLargeError,
    DiffResult,
    PublishBlockedError,
    PublishResult,
    UpdateRevisionResult,
)
from app.domain.artifact import (
    Artifact,
    ArtifactConflictError,
    ArtifactContentUnavailableError,
    ArtifactDiffTooLargeError,
    ArtifactDiffUnsupportedError,
    ArtifactExportError,
    ArtifactExportTooLargeError,
    ArtifactExportUnsupportedError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactListPage,
    ArtifactMigrationIncompleteError,
    ArtifactNotFoundError,
    ArtifactReadTooLargeError,
    ArtifactRevision,
    ArtifactRevisionConflictError,
    ArtifactRevisionNotFoundError,
    ArtifactRevisionValidationError,
    ArtifactSource,
    ArtifactStatus,
    ArtifactValidationError,
    PublishedArtifact,
    PublishedArtifactNotFoundError,
    PublishedArtifactStatus,
    RevisionListCursor,
    RevisionListPage,
)
from app.interfaces.http._content_disposition import build_content_disposition
from app.interfaces.http.dashboard import create_dashboard_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _make_artifact(
    artifact_id: str = "art-1",
    name: str = "test.md",
    kind: ArtifactKind = ArtifactKind.MARKDOWN,
    mime: str = "text/markdown",
    inline_content: str | None = "# Hello",
    content_ref: str | None = None,
    source_kind: ArtifactSource = ArtifactSource.MANUAL,
    source_ref: str | None = None,
    source_context_ref: str | None = None,
    source_session_id: str | None = None,
    summary: str = "",
    classification: str | None = None,
    labels: tuple[str, ...] | None = None,
    status: ArtifactStatus = ArtifactStatus.DRAFT,
    created_by: str = "dashboard",
    binary_data: bytes | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Artifact:
    """Create a valid Artifact with correct checksum/size."""
    if inline_content is not None:
        data = inline_content.encode("utf-8")
        size = len(data)
        checksum = _sha256(data)
    elif content_ref is not None:
        data = binary_data or b"binary-content"
        size = len(data)
        checksum = _sha256(data)
    else:
        raise ValueError("need inline_content or content_ref")

    if source_kind == ArtifactSource.MANUAL and source_ref is None:
        source_ref = artifact_id

    return Artifact(
        id=artifact_id,
        name=name,
        kind=kind,
        mime=mime,
        content_ref=content_ref,
        inline_content=inline_content,
        size=size,
        checksum=checksum,
        source_kind=source_kind,
        source_ref=source_ref,
        source_context_ref=source_context_ref,
        source_session_id=source_session_id,
        summary=summary,
        classification=classification,
        labels=labels,
        status=status,
        created_by=created_by,
        created_at=created_at,
        updated_at=updated_at,
    )


def _make_published(
    publish_id: str = "pub-1",
    artifact_id: str = "art-1",
    name: str = "test.md",
    kind: ArtifactKind = ArtifactKind.MARKDOWN,
    mime: str = "text/markdown",
    inline_content: str = "# Hello",
    status: PublishedArtifactStatus = PublishedArtifactStatus.ACTIVE,
) -> PublishedArtifact:
    data = inline_content.encode("utf-8")
    return PublishedArtifact(
        publish_id=publish_id,
        artifact_id=artifact_id,
        snapshot_name=name,
        snapshot_kind=kind,
        snapshot_mime=mime,
        snapshot_content_ref=None,
        snapshot_inline_content=inline_content,
        snapshot_size=len(data),
        snapshot_checksum=_sha256(data),
        snapshot_summary="",
        published_at=datetime.now(timezone.utc),
        published_by="dashboard",
        status=status,
        revoked_at=datetime.now(timezone.utc) if status == PublishedArtifactStatus.REVOKED else None,
    )


# ---------------------------------------------------------------------------
# Fake ArtifactService
# ---------------------------------------------------------------------------


class FakeArtifactService:
    """In-memory fake ArtifactService for route tests."""

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._binary: dict[str, bytes] = {}  # content_ref -> bytes
        self._active_publishes: dict[str, PublishedArtifact] = {}  # artifact_id -> active
        self._revoked_publishes: dict[str, PublishedArtifact] = {}  # artifact_id -> revoked
        self._revisions: dict[str, list[ArtifactRevision]] = {}  # artifact_id -> revisions
        self._revision_content: dict[str, bytes] = {}  # revision_id -> bytes
        self.create_calls: list[dict[str, Any]] = []
        self._raise_on_get: Exception | None = None
        self._raise_on_list: Exception | None = None
        self._raise_on_update_revision: Exception | None = None
        self._raise_on_rollback: Exception | None = None
        self._raise_on_publish_revision: Exception | None = None
        self._raise_on_export_capabilities: Exception | None = None
        self._raise_on_diff: Exception | None = None

    # -- list --
    async def list_artifacts(
        self,
        *,
        source_kind: ArtifactSource | None = None,
        source_context_ref: str | None = None,
        source_session_id: str | None = None,
        kind: ArtifactKind | None = None,
        status: ArtifactStatus | None = None,
        q: str | None = None,
        cursor: ArtifactListCursor | None = None,
        limit: int = 50,
    ) -> ArtifactListPage:
        if self._raise_on_list is not None:
            raise self._raise_on_list
        items = list(self._artifacts.values())
        if source_kind is not None:
            items = [a for a in items if a.source_kind == source_kind]
        if source_context_ref is not None:
            items = [a for a in items if a.source_context_ref == source_context_ref]
        if source_session_id is not None:
            items = [a for a in items if a.source_session_id == source_session_id]
        if kind is not None:
            items = [a for a in items if a.kind == kind]
        if status is not None:
            items = [a for a in items if a.status == status]
        if q:
            ql = q.lower()
            items = [a for a in items if ql in (a.name or "").lower() or ql in (a.summary or "").lower()]
        items.sort(
            key=lambda a: (a.updated_at or datetime.min.replace(tzinfo=timezone.utc), a.id),
            reverse=True,
        )
        if cursor is not None:
            cursor_key = (
                cursor.updated_at or datetime.min.replace(tzinfo=timezone.utc),
                cursor.artifact_id,
            )
            items = [a for a in items if (a.updated_at or datetime.min.replace(tzinfo=timezone.utc), a.id) < cursor_key]
        clamped = max(1, min(100, limit))
        page_items = items[:clamped]
        next_cursor = None
        if len(items) > clamped:
            last = page_items[-1]
            next_cursor = ArtifactListCursor(
                updated_at=last.updated_at or datetime.now(timezone.utc),
                artifact_id=last.id,
            )
        return ArtifactListPage(items=tuple(page_items), next_cursor=next_cursor)

    # -- get --
    async def get_artifact(self, artifact_id: str) -> Artifact:
        if self._raise_on_get is not None:
            raise self._raise_on_get
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        return art

    async def get_content(self, artifact_id: str) -> tuple[bytes, Artifact]:
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.inline_content is not None:
            return art.inline_content.encode("utf-8"), art
        if art.content_ref is None:
            raise ArtifactContentUnavailableError(f"artifact has no content: {artifact_id}")
        data = self._binary.get(art.content_ref)
        if data is None:
            raise ArtifactContentUnavailableError(f"content not found: {artifact_id}")
        return data, art

    # -- create --
    async def create_artifact(
        self,
        *,
        name: str,
        kind: ArtifactKind,
        mime: str,
        inline_content: str | None = None,
        file_data: bytes | None = None,
        filename: str | None = None,
        source_kind: ArtifactSource = ArtifactSource.MANUAL,
        source_ref: str | None = None,
        source_context_ref: str | None = None,
        summary: str = "",
        classification: str | None = None,
        labels: tuple[str, ...] | None = None,
        created_by: str = "dashboard",
    ) -> Artifact:
        self.create_calls.append({
            "name": name,
            "kind": kind,
            "created_by": created_by,
        })
        artifact_id = secrets.token_urlsafe(16)
        if source_kind == ArtifactSource.MANUAL and source_ref is None:
            source_ref = artifact_id

        if inline_content is not None:
            data = inline_content.encode("utf-8")
            size = len(data)
            checksum = _sha256(data)
            content_ref = None
        elif file_data is not None:
            size = len(file_data)
            checksum = _sha256(file_data)
            content_ref = f"item:{artifact_id}"
            self._binary[content_ref] = file_data
        else:
            raise ArtifactValidationError("create requires exactly one of inline_content / file_data")

        art = Artifact(
            id=artifact_id,
            name=name,
            kind=kind,
            mime=mime,
            content_ref=content_ref,
            inline_content=inline_content,
            size=size,
            checksum=checksum,
            source_kind=source_kind,
            source_ref=source_ref,
            source_context_ref=source_context_ref,
            summary=summary,
            classification=classification,
            labels=tuple(labels) if labels else None,
            status=ArtifactStatus.DRAFT,
            created_by=created_by,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        # Materialize the initial revision (revision_number=1) and backfill
        # current_revision_id, so every created Artifact has a current revision.
        rev_id = secrets.token_urlsafe(16)
        rev = ArtifactRevision(
            id=rev_id,
            artifact_id=artifact_id,
            revision_number=1,
            parent_revision_id=None,
            rollback_from_revision_id=None,
            content_ref=content_ref,
            inline_content=inline_content,
            size=size,
            checksum=checksum,
            mime=mime,
            kind=kind,
            created_at=art.created_at,
            change_summary="initial",
            created_by=created_by,
            source_session_id=None,
            source_run_id=None,
        )
        self._revisions[artifact_id] = [rev]
        self._revision_content[rev_id] = (
            inline_content.encode("utf-8") if inline_content is not None else file_data
        )
        art = dc_replace(art, current_revision_id=rev_id)
        self._artifacts[artifact_id] = art
        return art

    # -- update --
    async def update_artifact(
        self,
        artifact_id: str,
        *,
        name: str | None = None,
        summary: str | None = None,
        classification: str | None = None,
        labels: tuple[str, ...] | None = None,
        inline_content: str | None = None,
        file_data: bytes | None = None,
        filename: str | None = None,
    ) -> Artifact:
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        new_name = name if name is not None else art.name
        new_summary = summary if summary is not None else art.summary
        new_classification = classification if classification is not None else art.classification
        new_labels = tuple(labels) if labels is not None else art.labels
        new_inline = art.inline_content
        new_ref = art.content_ref
        new_size = art.size
        new_checksum = art.checksum
        if inline_content is not None:
            data = inline_content.encode("utf-8")
            new_inline = inline_content
            new_ref = None
            new_size = len(data)
            new_checksum = _sha256(data)
        elif file_data is not None:
            new_ref = f"item:{artifact_id}"
            self._binary[new_ref] = file_data
            new_inline = None
            new_size = len(file_data)
            new_checksum = _sha256(file_data)
        updated = dc_replace(
            art,
            name=new_name,
            summary=new_summary,
            classification=new_classification,
            labels=new_labels,
            inline_content=new_inline,
            content_ref=new_ref,
            size=new_size,
            checksum=new_checksum,
            updated_at=datetime.now(timezone.utc),
        )
        self._artifacts[artifact_id] = updated
        return updated

    # -- delete --
    async def delete_artifact(self, artifact_id: str) -> bool:
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        del self._artifacts[artifact_id]
        return True

    # -- export --
    async def export(
        self, artifact_id: str, *, format: str = "original",
        revision_id: str | None = None,
    ) -> tuple[bytes, str, str]:
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if revision_id is not None:
            data, rev = await self.get_revision_content(artifact_id, revision_id)
            mime = rev.mime
        else:
            data, _ = await self.get_content(artifact_id)
            mime = art.mime
        if format == "original":
            return data, mime, art.name
        if format == "html":
            if art.kind not in (ArtifactKind.MARKDOWN, ArtifactKind.DOCUMENT):
                raise ArtifactValidationError(
                    f"html export only supports markdown/document, got {art.kind}"
                )
            html = f"<html><body>{data.decode('utf-8')}</body></html>"
            return html.encode("utf-8"), "text/html", art.name
        raise ArtifactValidationError(f"unsupported export format: {format}")

    # -- publish --
    async def publish(self, artifact_id: str) -> PublishResult:
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        existing = self._active_publishes.get(artifact_id)
        if existing is not None:
            return PublishResult(
                published=existing,
                share_url=f"/p/{existing.publish_id}",
                reused=True,
            )
        publish_id = secrets.token_urlsafe(16)
        published = _make_published(
            publish_id=publish_id,
            artifact_id=artifact_id,
            name=art.name,
            kind=art.kind,
            mime=art.mime,
            inline_content=art.inline_content or "",
        )
        published = dc_replace(published, published_revision_id=art.current_revision_id)
        self._active_publishes[artifact_id] = published
        return PublishResult(
            published=published,
            share_url=f"/p/{publish_id}",
            reused=False,
        )

    # -- revoke --
    async def revoke_publish(self, artifact_id: str) -> PublishedArtifact:
        active = self._active_publishes.get(artifact_id)
        if active is not None:
            revoked = dc_replace(
                active,
                status=PublishedArtifactStatus.REVOKED,
                revoked_at=datetime.now(timezone.utc),
            )
            del self._active_publishes[artifact_id]
            self._revoked_publishes[artifact_id] = revoked
            return revoked
        # Idempotent: repeat revoke returns same revoked artifact.
        existing_revoked = self._revoked_publishes.get(artifact_id)
        if existing_revoked is not None:
            return existing_revoked
        raise PublishedArtifactNotFoundError(
            f"no active or revoked publish for artifact: {artifact_id}"
        )

    # -- get active publish --
    async def get_active_publish(self, artifact_id: str) -> PublishedArtifact | None:
        return self._active_publishes.get(artifact_id)

    # -- get published by publish_id --
    async def get_published(self, publish_id: str) -> PublishedArtifact:
        for pub in list(self._active_publishes.values()) + list(self._revoked_publishes.values()):
            if pub.publish_id == publish_id:
                return pub
        raise PublishedArtifactNotFoundError(f"published artifact not found: {publish_id}")

    # -- revisions --
    def _find_revision(self, artifact_id: str, revision_id: str) -> ArtifactRevision:
        for rev in self._revisions.get(artifact_id, []):
            if rev.id == revision_id:
                return rev
        raise ArtifactRevisionNotFoundError(f"revision not found: {revision_id}")

    def _derive_sync_state(self, artifact_id: str) -> str:
        art = self._artifacts.get(artifact_id)
        if art is None or art.current_revision_id is None:
            return "unpublished"
        active = self._active_publishes.get(artifact_id)
        if active is None or active.published_revision_id is None:
            return "unpublished"
        if active.published_revision_id == art.current_revision_id:
            return "current"
        return "outdated"

    async def list_revisions(
        self, artifact_id: str, *, cursor: RevisionListCursor | None = None,
        limit: int = 50,
    ) -> RevisionListPage:
        if artifact_id not in self._artifacts:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        revs = list(self._revisions.get(artifact_id, []))
        if cursor is not None:
            idx = next((i for i, r in enumerate(revs) if r.id == cursor.id), None)
            if idx is not None:
                revs = revs[idx + 1:]
        clamped = max(1, min(100, limit))
        page_items = revs[:clamped]
        next_cursor = None
        if len(revs) > clamped:
            last = page_items[-1]
            next_cursor = RevisionListCursor(
                artifact_id=artifact_id,
                revision_number=last.revision_number,
                id=last.id,
            )
        return RevisionListPage(items=tuple(page_items), next_cursor=next_cursor)

    async def get_revision_content(
        self, artifact_id: str, revision_id: str | None = None,
    ) -> tuple[bytes, ArtifactRevision]:
        if artifact_id not in self._artifacts:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if revision_id is None:
            art = self._artifacts[artifact_id]
            if art.current_revision_id is None:
                raise ArtifactMigrationIncompleteError("no current revision")
            revision_id = art.current_revision_id
        rev = self._find_revision(artifact_id, revision_id)
        data = self._revision_content.get(rev.id)
        if data is None:
            raise ArtifactContentUnavailableError(f"content not found for revision: {rev.id}")
        return data, rev

    async def update_revision(
        self, artifact_id: str, *, expected_revision_id: str,
        inline_content: str | None = None, file_data: bytes | None = None,
        workspace_ref: str | None = None, text_patch: list | None = None,
        change_summary: str = "", kind: ArtifactKind | None = None,
        mime: str | None = None,
    ) -> tuple[ArtifactRevision, UpdateRevisionResult]:
        if self._raise_on_update_revision is not None:
            raise self._raise_on_update_revision
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError("no current revision")
        if expected_revision_id != art.current_revision_id:
            raise ArtifactRevisionConflictError("expected_revision_id mismatch")
        revs = self._revisions.setdefault(artifact_id, [])
        current = self._find_revision(artifact_id, art.current_revision_id)
        current_content = self._revision_content.get(current.id, b"")
        if inline_content is not None:
            new_data = inline_content.encode("utf-8")
            new_inline = inline_content
            new_ref = None
        elif file_data is not None:
            new_data = file_data
            new_inline = None
            new_ref = f"item:{artifact_id}"
            self._binary[new_ref] = file_data
        elif text_patch is not None:
            text = current_content.decode("utf-8", "replace") if current_content else ""
            for op in text_patch:
                search = op.get("search", "")
                replace_text = op.get("replace", "")
                mode = op.get("mode", "first")
                if mode == "all":
                    text = text.replace(search, replace_text)
                else:
                    text = text.replace(search, replace_text, 1)
            new_data = text.encode("utf-8")
            new_inline = text
            new_ref = None
        elif workspace_ref is not None:
            new_data = current_content
            new_inline = None
            new_ref = workspace_ref
        else:
            new_data = current_content
            new_inline = current.inline_content
            new_ref = current.content_ref
        new_size = len(new_data)
        new_checksum = _sha256(new_data)
        content_unchanged = new_checksum == current.checksum
        rev_id = secrets.token_urlsafe(16)
        rev = ArtifactRevision(
            id=rev_id,
            artifact_id=artifact_id,
            revision_number=len(revs) + 1,
            parent_revision_id=current.id,
            rollback_from_revision_id=None,
            content_ref=new_ref,
            inline_content=new_inline,
            size=new_size,
            checksum=new_checksum,
            mime=mime or current.mime,
            kind=kind or current.kind,
            created_at=datetime.now(timezone.utc),
            change_summary=change_summary or "",
            created_by=art.created_by,
            source_session_id=None,
            source_run_id=None,
        )
        revs.append(rev)
        self._revision_content[rev_id] = new_data
        art = dc_replace(
            art,
            current_revision_id=rev_id,
            size=new_size,
            checksum=new_checksum,
            inline_content=new_inline,
            content_ref=new_ref,
            updated_at=datetime.now(timezone.utc),
        )
        self._artifacts[artifact_id] = art
        result = UpdateRevisionResult(
            diff_summary=f"v{rev.revision_number}",
            content_unchanged=content_unchanged,
            publish_sync_state=self._derive_sync_state(artifact_id),
        )
        return rev, result

    async def diff_revisions(
        self, artifact_id: str, from_id: str, to_id: str, *, context_lines: int = 3,
    ) -> DiffResult:
        if self._raise_on_diff is not None:
            raise self._raise_on_diff
        if artifact_id not in self._artifacts:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        from_rev = self._find_revision(artifact_id, from_id)
        to_rev = self._find_revision(artifact_id, to_id)
        import difflib
        from_data = self._revision_content.get(from_rev.id, b"")
        to_data = self._revision_content.get(to_rev.id, b"")
        diff = difflib.unified_diff(
            from_data.decode("utf-8", "replace").splitlines(keepends=True),
            to_data.decode("utf-8", "replace").splitlines(keepends=True),
            fromfile=from_id,
            tofile=to_id,
            n=context_lines,
        )
        return DiffResult(diff_text="".join(diff), binary_changed=False)

    async def rollback(
        self, artifact_id: str, target_revision_id: str, *,
        expected_revision_id: str, change_summary: str = "",
    ) -> tuple[ArtifactRevision, UpdateRevisionResult]:
        if self._raise_on_rollback is not None:
            raise self._raise_on_rollback
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError("no current revision")
        if expected_revision_id != art.current_revision_id:
            raise ArtifactRevisionConflictError("expected_revision_id mismatch")
        target = self._find_revision(artifact_id, target_revision_id)
        revs = self._revisions.setdefault(artifact_id, [])
        target_content = self._revision_content.get(target.id, b"")
        rev_id = secrets.token_urlsafe(16)
        rev = ArtifactRevision(
            id=rev_id,
            artifact_id=artifact_id,
            revision_number=len(revs) + 1,
            parent_revision_id=art.current_revision_id,
            rollback_from_revision_id=target.id,
            content_ref=target.content_ref,
            inline_content=target.inline_content,
            size=target.size,
            checksum=target.checksum,
            mime=target.mime,
            kind=target.kind,
            created_at=datetime.now(timezone.utc),
            change_summary=change_summary or f"rollback to v{target.revision_number}",
            created_by=art.created_by,
            source_session_id=None,
            source_run_id=None,
        )
        revs.append(rev)
        self._revision_content[rev_id] = target_content
        art = dc_replace(
            art,
            current_revision_id=rev_id,
            size=target.size,
            checksum=target.checksum,
            inline_content=target.inline_content,
            content_ref=target.content_ref,
            updated_at=datetime.now(timezone.utc),
        )
        self._artifacts[artifact_id] = art
        result = UpdateRevisionResult(
            diff_summary=f"rollback to v{target.revision_number}",
            content_unchanged=False,
            publish_sync_state=self._derive_sync_state(artifact_id),
        )
        return rev, result

    async def get_current_revision(self, artifact_id: str) -> ArtifactRevision:
        if artifact_id not in self._artifacts:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        art = self._artifacts[artifact_id]
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError("no current revision")
        return self._find_revision(artifact_id, art.current_revision_id)

    async def get_publish_sync_state(self, artifact_id: str) -> str:
        if artifact_id not in self._artifacts:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        return self._derive_sync_state(artifact_id)

    async def export_capabilities(
        self, artifact_id: str, *, revision_id: str | None = None,
    ) -> tuple[str, ...]:
        if self._raise_on_export_capabilities is not None:
            raise self._raise_on_export_capabilities
        if artifact_id not in self._artifacts:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if revision_id is not None:
            self._find_revision(artifact_id, revision_id)
        art = self._artifacts[artifact_id]
        caps = ["original"]
        if art.kind in (ArtifactKind.MARKDOWN, ArtifactKind.DOCUMENT):
            caps.append("html")
        return tuple(caps)

    async def publish_revision(
        self, artifact_id: str, *, revision_id: str,
        expected_current_revision_id: str,
    ) -> PublishResult:
        if self._raise_on_publish_revision is not None:
            raise self._raise_on_publish_revision
        art = self._artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError("no current revision")
        if expected_current_revision_id != art.current_revision_id:
            raise ArtifactRevisionConflictError("expected_current_revision_id mismatch")
        if revision_id != art.current_revision_id:
            raise ArtifactRevisionConflictError("revision_id is not the current revision")
        existing = self._active_publishes.get(artifact_id)
        if existing is not None and existing.published_revision_id == revision_id:
            return PublishResult(
                published=existing,
                share_url=f"/p/{existing.publish_id}",
                reused=True,
            )
        publish_id = secrets.token_urlsafe(16)
        published = _make_published(
            publish_id=publish_id,
            artifact_id=artifact_id,
            name=art.name,
            kind=art.kind,
            mime=art.mime,
            inline_content=art.inline_content or "",
        )
        published = dc_replace(published, published_revision_id=revision_id)
        self._active_publishes[artifact_id] = published
        return PublishResult(
            published=published,
            share_url=f"/p/{publish_id}",
            reused=False,
        )


# ---------------------------------------------------------------------------
# Test client setup
# ---------------------------------------------------------------------------


def _make_app(service: FakeArtifactService | None = None) -> FastAPI:
    app = FastAPI()
    router = create_dashboard_router(
        session_service=None,
        tool_service=None,
        model_service=None,
        health_provider=lambda: {},
        artifact_service=service,
    )
    app.include_router(router)
    return app


def _client(service: FakeArtifactService | None = None) -> TestClient:
    return TestClient(_make_app(service))


def _seed_artifacts(service: FakeArtifactService, count: int = 3) -> list[Artifact]:
    arts = []
    for i in range(count):
        art = _make_artifact(
            artifact_id=f"art-{i + 1}",
            name=f"doc-{i + 1}.md",
            kind=ArtifactKind.MARKDOWN,
            inline_content=f"# Document {i + 1}",
            created_at=datetime(2025, 1, i + 1, tzinfo=timezone.utc),
            updated_at=datetime(2025, 1, i + 1, tzinfo=timezone.utc),
        )
        service._artifacts[art.id] = art
        arts.append(art)
    return arts


def _seed_revisioned(
    service: FakeArtifactService,
    artifact_id: str = "art-1",
    name: str = "doc.md",
    kind: ArtifactKind = ArtifactKind.MARKDOWN,
    mime: str = "text/markdown",
    inline_content: str = "# Hello",
    revision_id: str = "rev-1",
    created_at: datetime | None = None,
) -> tuple[Artifact, ArtifactRevision]:
    """Seed an artifact WITH a current revision (for CAS-based content tests)."""
    if created_at is None:
        created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    art = _make_artifact(
        artifact_id, name=name, kind=kind, mime=mime, inline_content=inline_content,
        created_at=created_at, updated_at=created_at,
    )
    data = inline_content.encode("utf-8")
    rev = ArtifactRevision(
        id=revision_id,
        artifact_id=artifact_id,
        revision_number=1,
        parent_revision_id=None,
        rollback_from_revision_id=None,
        content_ref=None,
        inline_content=inline_content,
        size=len(data),
        checksum=_sha256(data),
        mime=mime,
        kind=kind,
        created_at=created_at,
        change_summary="initial",
        created_by="dashboard",
        source_session_id=None,
        source_run_id=None,
    )
    art = dc_replace(art, current_revision_id=revision_id)
    service._artifacts[artifact_id] = art
    service._revisions[artifact_id] = [rev]
    service._revision_content[revision_id] = data
    return art, rev


# ---------------------------------------------------------------------------
# Shell route tests
# ---------------------------------------------------------------------------


class TestShellRoute:
    def test_shell_route_registered_when_service_provided(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.get("/artifacts")
        assert response.status_code == 200
        assert 'id="app-sidebar"' in response.text

    def test_shell_route_not_registered_when_service_none(self):
        client = _client(None)
        response = client.get("/artifacts")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# List tests
# ---------------------------------------------------------------------------


class TestListArtifacts:
    def test_list_empty(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.get("/chat/artifacts")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["next_cursor"] is None

    def test_list_returns_items(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 3)
        client = _client(service)
        response = client.get("/chat/artifacts")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 3
        # Each item must NOT contain content_ref
        for item in data["items"]:
            assert "content_ref" not in item
            assert "inline_content" not in item

    def test_list_with_limit_and_cursor(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 5)
        client = _client(service)
        # First page
        response = client.get("/chat/artifacts?limit=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert data["next_cursor"] is not None
        # Second page (URL-encode the cursor JSON)
        from urllib.parse import quote as _quote
        cursor_str = _quote(json.dumps(data["next_cursor"]))
        response2 = client.get(f"/chat/artifacts?limit=2&cursor={cursor_str}")
        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["items"]) == 2
        # Third page
        cursor_str2 = _quote(json.dumps(data2["next_cursor"]))
        response3 = client.get(f"/chat/artifacts?limit=2&cursor={cursor_str2}")
        assert response3.status_code == 200
        data3 = response3.json()
        assert len(data3["items"]) == 1

    def test_list_filter_by_kind(self):
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact("a1", name="md.md", kind=ArtifactKind.MARKDOWN)
        service._artifacts["a2"] = _make_artifact("a2", name="code.py", kind=ArtifactKind.CODE, mime="text/x-python", inline_content="print(1)")
        client = _client(service)
        response = client.get("/chat/artifacts?kind=markdown")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "markdown"

    def test_list_filter_by_status(self):
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact("a1", status=ArtifactStatus.DRAFT)
        service._artifacts["a2"] = _make_artifact("a2", name="b.md", status=ArtifactStatus.PUBLISHED)
        client = _client(service)
        response = client.get("/chat/artifacts?status=published")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["status"] == "published"

    def test_list_filter_by_source_kind(self):
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact("a1", source_kind=ArtifactSource.MANUAL)
        service._artifacts["a2"] = _make_artifact("a2", name="b.md",
                                                   source_kind=ArtifactSource.TASK_ATTACHMENT,
                                                   source_ref="att-1")
        client = _client(service)
        response = client.get("/chat/artifacts?source_kind=task_attachment")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["source_kind"] == "task_attachment"

    def test_list_search_by_query(self):
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact("a1", name="important-doc.md")
        service._artifacts["a2"] = _make_artifact("a2", name="other.md")
        client = _client(service)
        response = client.get("/chat/artifacts?q=important")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert "important" in items[0]["name"]

    def test_list_filter_by_source_context_ref(self):
        """Filtering by source_context_ref returns only matching artifacts."""
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact(
            "a1", source_kind=ArtifactSource.SESSION,
            source_ref="session-a:1", source_context_ref="session-a",
        )
        service._artifacts["a2"] = _make_artifact(
            "a2", name="b.md", source_kind=ArtifactSource.SESSION,
            source_ref="session-b:2", source_context_ref="session-b",
        )
        client = _client(service)
        response = client.get("/chat/artifacts?source_context_ref=session-a")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "next_cursor" in data
        items = data["items"]
        assert len(items) == 1
        assert items[0]["source_context_ref"] == "session-a"

    def test_list_source_context_ref_empty_string(self):
        """Empty-string source_context_ref only matches empty-string records,
        not NULL or other values."""
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact(
            "a1", source_kind=ArtifactSource.SESSION,
            source_ref="session-a:1", source_context_ref="session-a",
        )
        service._artifacts["a2"] = _make_artifact(
            "a2", name="empty.md", source_context_ref="",
        )
        service._artifacts["a3"] = _make_artifact(
            "a3", name="null.md",
        )
        client = _client(service)
        response = client.get("/chat/artifacts?source_context_ref=")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == "a2"

    def test_list_source_context_ref_omitted(self):
        """Omitting source_context_ref returns all artifacts (no filter)."""
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact(
            "a1", source_kind=ArtifactSource.SESSION,
            source_ref="session-a:1", source_context_ref="session-a",
        )
        service._artifacts["a2"] = _make_artifact("a2", name="b.md")
        client = _client(service)
        response = client.get("/chat/artifacts")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "next_cursor" in data
        assert len(data["items"]) == 2

    def test_list_source_context_ref_with_source_kind(self):
        """source_context_ref AND source_kind combine as joint filters."""
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact(
            "a1", source_kind=ArtifactSource.SESSION,
            source_ref="session-a:1", source_context_ref="session-a",
        )
        service._artifacts["a2"] = _make_artifact(
            "a2", name="b.md", source_kind=ArtifactSource.SESSION,
            source_ref="session-b:2", source_context_ref="session-b",
        )
        service._artifacts["a3"] = _make_artifact(
            "a3", name="c.md", source_context_ref="session-a",
        )
        client = _client(service)
        response = client.get(
            "/chat/artifacts?source_kind=session&source_context_ref=session-a"
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == "a1"
        assert items[0]["source_kind"] == "session"
        assert items[0]["source_context_ref"] == "session-a"

    def test_list_filter_by_source_session_id(self):
        """Filtering by source_session_id returns matching artifacts of any
        task source kind (the conversation panel query)."""
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact(
            "a1", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t1:run:1:artifact:0", source_context_ref="t1",
            source_session_id="task-sess-1",
        )
        service._artifacts["a2"] = _make_artifact(
            "a2", name="b.png", kind=ArtifactKind.IMAGE, mime="image/png",
            inline_content=None, content_ref="item:a2/f", binary_data=b"img",
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-2", source_context_ref="t1",
            source_session_id="task-sess-1",
        )
        service._artifacts["a3"] = _make_artifact(
            "a3", name="c.md", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t2:run:1:artifact:0", source_context_ref="t2",
            source_session_id="task-sess-2",
        )
        client = _client(service)
        response = client.get("/chat/artifacts?source_session_id=task-sess-1")
        assert response.status_code == 200
        items = response.json()["items"]
        assert {i["id"] for i in items} == {"a1", "a2"}
        # both task source kinds found, no source_kind filter applied
        assert {i["source_kind"] for i in items} == {
            "task_artifact", "task_attachment",
        }
        # serialized source_session_id present
        assert all(i["source_session_id"] == "task-sess-1" for i in items)

    def test_list_source_session_id_serialized(self):
        """_artifact_to_dict exposes source_session_id (None when unset)."""
        service = FakeArtifactService()
        service._artifacts["a1"] = _make_artifact(
            "a1", source_session_id="sess-x",
        )
        service._artifacts["a2"] = _make_artifact("a2", name="b.md")
        client = _client(service)
        response = client.get("/chat/artifacts")
        items = {i["id"]: i for i in response.json()["items"]}
        assert items["a1"]["source_session_id"] == "sess-x"
        assert items["a2"]["source_session_id"] is None


# ---------------------------------------------------------------------------
# Create tests
# ---------------------------------------------------------------------------


class TestCreateArtifact:
    def test_create_json_inline(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.post("/chat/artifacts", json={
            "name": "hello.md",
            "kind": "markdown",
            "content": "# Hello World",
            "mime": "text/markdown",
        })
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "hello.md"
        assert data["kind"] == "markdown"
        assert data["status"] == "draft"
        assert "content_ref" not in data
        assert "inline_content" not in data

    def test_create_actor_is_server_fixed(self):
        """actor is server-fixed 'dashboard', NOT read from body."""
        service = FakeArtifactService()
        client = _client(service)
        response = client.post("/chat/artifacts", json={
            "name": "hello.md",
            "kind": "markdown",
            "content": "# Hello",
            "created_by": "hacker",
        })
        assert response.status_code == 201
        assert service.create_calls[-1]["created_by"] == "dashboard"
        assert response.json()["created_by"] == "dashboard"

    def test_create_multipart_upload(self):
        service = FakeArtifactService()
        client = _client(service)
        file_content = b"binary image data"
        response = client.post(
            "/chat/artifacts",
            files={"file": ("photo.png", io.BytesIO(file_content), "image/png")},
            data={
                "name": "photo.png",
                "kind": "image",
                "mime": "image/png",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "photo.png"
        assert data["kind"] == "image"
        assert data["size"] == len(file_content)

    def test_create_unsupported_content_type_returns_415(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.post(
            "/chat/artifacts",
            content="plain text body",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_media_type"

    def test_create_json_missing_required_fields(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.post("/chat/artifacts", json={"name": "test"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "artifact_invalid"

    def test_create_json_invalid_kind(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.post("/chat/artifacts", json={
            "name": "test.md",
            "kind": "invalid_kind",
            "content": "hello",
        })
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "artifact_invalid"


# ---------------------------------------------------------------------------
# Get detail tests
# ---------------------------------------------------------------------------


class TestGetArtifact:
    def test_get_detail_returns_public_view(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "art-1"
        assert "content_ref" not in data
        assert "inline_content" not in data
        assert "source_ref" not in data

    def test_get_detail_not_found(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.get("/chat/artifacts/nonexistent")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "artifact_not_found"


# ---------------------------------------------------------------------------
# Get content tests
# ---------------------------------------------------------------------------


class TestGetContent:
    def test_get_content_text_has_charset(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1/content")
        assert response.status_code == 200
        assert "charset=utf-8" in response.headers.get("content-type", "")
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert "content-disposition" in response.headers

    def test_get_content_binary_no_charset(self):
        service = FakeArtifactService()
        binary_data = b"\x89PNG\r\n\x1a\n fake png"
        art = _make_artifact(
            "img-1",
            name="photo.png",
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            inline_content=None,
            content_ref="item:img-1",
            binary_data=binary_data,
        )
        service._artifacts["img-1"] = art
        service._binary["item:img-1"] = binary_data
        client = _client(service)
        response = client.get("/chat/artifacts/img-1/content")
        assert response.status_code == 200
        ct = response.headers.get("content-type", "")
        assert "image/png" in ct
        assert "charset" not in ct
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_get_content_html_uses_attachment(self):
        """Raw HTML content must use attachment disposition (not inline/top-level)."""
        service = FakeArtifactService()
        art = _make_artifact(
            "html-1",
            name="page.html",
            kind=ArtifactKind.HTML,
            mime="text/html",
            inline_content="<p>hello</p>",
        )
        service._artifacts["html-1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts/html-1/content")
        assert response.status_code == 200
        cd = response.headers.get("content-disposition", "")
        assert "attachment" in cd

    def test_get_content_not_found(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.get("/chat/artifacts/nonexistent/content")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "artifact_not_found"

    def test_get_content_unavailable(self):
        service = FakeArtifactService()
        art = _make_artifact(
            "broken-1",
            name="broken.md",
            kind=ArtifactKind.MARKDOWN,
            inline_content=None,
            content_ref="item:missing",
        )
        service._artifacts["broken-1"] = art
        # Don't store binary data for the content_ref
        client = _client(service)
        response = client.get("/chat/artifacts/broken-1/content")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "artifact_content_unavailable"

    def test_get_content_unicode_filename_does_not_500(self):
        """Non-ASCII (e.g. Chinese) artifact names must not crash the
        content endpoint. HTTP headers are latin-1, so the legacy
        ``filename`` parameter must stay ASCII-only while the real name
        is conveyed via the RFC 5987 ``filename*`` parameter. Bug:
        ``_safe_content_disposition`` put the raw non-ASCII name into
        ``filename="..."`` -> UnicodeEncodeError -> HTTP 500 -> frontend
        "request_failed"."""
        service = FakeArtifactService()
        art = _make_artifact(
            "cn-1",
            name="横向-邮箱归属.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            inline_content="# 横向-邮箱归属\n",
        )
        service._artifacts["cn-1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts/cn-1/content")
        assert response.status_code == 200
        cd = response.headers.get("content-disposition", "")
        # RFC 5987 UTF-8 form carries the real (non-ASCII) name.
        assert "filename*=UTF-8''" in cd
        assert quote("横向-邮箱归属.md", safe="") in cd
        # Legacy filename must be latin-1 encodable so the header builds.
        legacy = cd.split('filename="', 1)[1].split('"', 1)[0]
        legacy.encode("latin-1")  # must not raise


# ---------------------------------------------------------------------------
# PATCH tests
# ---------------------------------------------------------------------------


class TestPatchArtifact:
    def test_patch_json_update_name(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch("/chat/artifacts/art-1", json={"name": "renamed.md"})
        assert response.status_code == 200
        assert response.json()["name"] == "renamed.md"

    def test_patch_json_replace_content(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        response = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# New Content", "expected_revision_id": rev.id},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["size"] == len("# New Content".encode("utf-8"))
        assert data["revision_number"] == 2
        assert data["current_revision_id"] != rev.id
        assert data["id"] == "art-1"

    def test_patch_forbidden_field_id(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch("/chat/artifacts/art-1", json={"id": "hacked"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "artifact_invalid"

    def test_patch_forbidden_field_status(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch("/chat/artifacts/art-1", json={"status": "published"})
        assert response.status_code == 422

    def test_patch_forbidden_field_checksum(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch("/chat/artifacts/art-1", json={"checksum": "sha256:" + "0" * 64})
        assert response.status_code == 422

    def test_patch_forbidden_field_size(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch("/chat/artifacts/art-1", json={"size": 999})
        assert response.status_code == 422

    def test_patch_forbidden_field_created_by(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch("/chat/artifacts/art-1", json={"created_by": "hacker"})
        assert response.status_code == 422

    def test_patch_forbidden_field_source(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch("/chat/artifacts/art-1", json={"source_kind": "task_attachment"})
        assert response.status_code == 422

    def test_patch_multipart_replace_file(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        new_content = b"new binary data"
        response = client.patch(
            "/chat/artifacts/art-1",
            files={"file": ("new.txt", io.BytesIO(new_content), "text/plain")},
            data={"name": "updated.md"},
            headers={"If-Match": f'"{rev.id}"'},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "updated.md"
        assert data["revision_number"] == 2
        assert data["current_revision_id"] != rev.id

    def test_patch_unsupported_content_type(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.patch(
            "/chat/artifacts/art-1",
            content="plain text",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 415


# ---------------------------------------------------------------------------
# Delete tests
# ---------------------------------------------------------------------------


class TestDeleteArtifact:
    def test_delete_artifact(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.delete("/chat/artifacts/art-1")
        assert response.status_code == 204

    def test_delete_not_found(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.delete("/chat/artifacts/nonexistent")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "artifact_not_found"


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------


class TestExportArtifact:
    def test_export_original(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1/export?format=original")
        assert response.status_code == 200
        assert "text/markdown" in response.headers.get("content-type", "")
        assert "charset=utf-8" in response.headers.get("content-type", "")
        assert response.headers.get("x-content-type-options") == "nosniff"
        cd = response.headers.get("content-disposition", "")
        assert "attachment" in cd

    def test_export_html_for_markdown(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1/export?format=html")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert response.headers.get("x-content-type-options") == "nosniff"
        cd = response.headers.get("content-disposition", "")
        assert "attachment" in cd

    def test_export_html_for_document(self):
        service = FakeArtifactService()
        art = _make_artifact(
            "doc-1",
            name="doc.txt",
            kind=ArtifactKind.DOCUMENT,
            mime="text/plain",
            inline_content="A document.",
        )
        service._artifacts["doc-1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts/doc-1/export?format=html")
        assert response.status_code == 200

    def test_export_html_not_available_for_code(self):
        """html export capability only for markdown/document kinds."""
        service = FakeArtifactService()
        art = _make_artifact(
            "code-1",
            name="script.py",
            kind=ArtifactKind.CODE,
            mime="text/x-python",
            inline_content="print(1)",
        )
        service._artifacts["code-1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts/code-1/export?format=html")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "artifact_invalid"

    def test_export_not_found(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.get("/chat/artifacts/nonexistent/export")
        assert response.status_code == 404

    def test_export_safe_content_disposition_filename(self):
        """Content-Disposition filename must be safe (no quotes, newlines)."""
        service = FakeArtifactService()
        art = _make_artifact("a1", name='test".md\n', kind=ArtifactKind.MARKDOWN)
        service._artifacts["a1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts/a1/export")
        assert response.status_code == 200
        cd = response.headers.get("content-disposition", "")
        assert "\n" not in cd
        assert '\\"' not in cd or 'filename*=' in cd  # RFC 5987 form is safe


# ---------------------------------------------------------------------------
# Publish tests
# ---------------------------------------------------------------------------


class TestPublishLifecycle:
    def test_publish_returns_expected_fields(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.post("/chat/artifacts/art-1/publish")
        assert response.status_code == 200
        data = response.json()
        assert "publish_id" in data
        assert "share_path" in data
        assert "share_url" in data
        assert data["reused"] is False
        assert data["share_path"].startswith("/p/")

    def test_publish_idempotent_returns_reused(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        # First publish
        r1 = client.post("/chat/artifacts/art-1/publish")
        assert r1.status_code == 200
        assert r1.json()["reused"] is False
        # Second publish (idempotent)
        r2 = client.post("/chat/artifacts/art-1/publish")
        assert r2.status_code == 200
        assert r2.json()["reused"] is True
        assert r1.json()["publish_id"] == r2.json()["publish_id"]

    def test_publish_not_found(self):
        service = FakeArtifactService()
        client = _client(service)
        response = client.post("/chat/artifacts/nonexistent/publish")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "artifact_not_found"

    def test_get_publish_active(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        # Publish first
        client.post("/chat/artifacts/art-1/publish")
        # Get publish
        response = client.get("/chat/artifacts/art-1/publish")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "active"
        assert "publish_id" in data

    def test_get_publish_no_active_returns_unpublished_not_404(self):
        """No active publish returns {status:"unpublished"}, NOT 404."""
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1/publish")
        assert response.status_code == 200
        assert response.json()["status"] == "unpublished"

    def test_revoke_publish(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        # Publish first
        client.post("/chat/artifacts/art-1/publish")
        # Revoke
        response = client.delete("/chat/artifacts/art-1/publish")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "revoked"

    def test_revoke_when_no_publish_returns_unpublished(self):
        """Revoke when no publish exists returns unpublished status (idempotent)."""
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.delete("/chat/artifacts/art-1/publish")
        assert response.status_code == 200
        assert response.json()["status"] == "unpublished"

    def test_get_publish_after_revoke_returns_unpublished(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        client.post("/chat/artifacts/art-1/publish")
        client.delete("/chat/artifacts/art-1/publish")
        response = client.get("/chat/artifacts/art-1/publish")
        assert response.status_code == 200
        assert response.json()["status"] == "unpublished"


# ---------------------------------------------------------------------------
# Error envelope tests
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    def test_error_envelope_format(self):
        """All errors use {error:{code, message}} envelope."""
        service = FakeArtifactService()
        client = _client(service)
        response = client.get("/chat/artifacts/nonexistent")
        assert response.status_code == 404
        data = response.json()
        assert "error" in data
        assert "code" in data["error"]
        assert "message" in data["error"]

    def test_unexpected_exception_returns_500_without_traceback(self):
        """Unexpected exceptions return stable 500, no traceback/abs-path."""
        service = FakeArtifactService()
        service._raise_on_get = RuntimeError("error at /Users/secret/path.py:42")
        client = _client(service)
        response = client.get("/chat/artifacts/art-1")
        assert response.status_code == 500
        body = json.dumps(response.json())
        assert "/Users/secret/path.py" not in body
        assert "traceback" not in body.lower()
        assert response.json()["error"]["code"] == "artifact_internal_error"

    def test_publish_blocked_returns_422(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)

        original_publish = service.publish

        async def blocked_publish(artifact_id):
            raise PublishBlockedError("publish denied by policy")

        service.publish = blocked_publish
        client = _client(service)
        response = client.post("/chat/artifacts/art-1/publish")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "publish_blocked"

    def test_artifact_conflict_returns_409(self):
        service = FakeArtifactService()

        async def conflict_create(**kwargs):
            raise ArtifactConflictError("conflict")

        service.create_artifact = conflict_create
        client = _client(service)
        response = client.post("/chat/artifacts", json={
            "name": "test.md",
            "kind": "markdown",
            "content": "hello",
        })
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "artifact_conflict"

    def test_too_large_returns_413(self):
        service = FakeArtifactService()

        async def too_large_create(**kwargs):
            raise ArtifactTooLargeError("too large")

        service.create_artifact = too_large_create
        client = _client(service)
        response = client.post("/chat/artifacts", json={
            "name": "big.md",
            "kind": "markdown",
            "content": "x" * 100,
        })
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "artifact_too_large"


# ---------------------------------------------------------------------------
# Content/export security header tests
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_content_has_nosniff(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1/content")
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_export_has_nosniff(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1/export")
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_content_disposition_has_filename(self):
        service = FakeArtifactService()
        _seed_artifacts(service, 1)
        client = _client(service)
        response = client.get("/chat/artifacts/art-1/content")
        cd = response.headers.get("content-disposition", "")
        assert "filename" in cd

    def test_html_content_is_attachment_only(self):
        """Raw HTML content response must use attachment (not same-origin top-level HTML)."""
        service = FakeArtifactService()
        art = _make_artifact(
            "html-1",
            name="page.html",
            kind=ArtifactKind.HTML,
            mime="text/html",
            inline_content="<script>alert(1)</script>",
        )
        service._artifacts["html-1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts/html-1/content")
        assert response.status_code == 200
        cd = response.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "inline" not in cd


# ---------------------------------------------------------------------------
# Detail response content_ref exclusion test
# ---------------------------------------------------------------------------


class TestPublicViewExclusion:
    def test_detail_excludes_content_ref(self):
        service = FakeArtifactService()
        art = _make_artifact(
            "a1",
            name="test.md",
            kind=ArtifactKind.MARKDOWN,
            content_ref=None,
            inline_content="content",
        )
        service._artifacts["a1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts/a1")
        data = response.json()
        assert "content_ref" not in data
        assert "inline_content" not in data
        assert "source_ref" not in data

    def test_list_excludes_content_ref(self):
        service = FakeArtifactService()
        art = _make_artifact(
            "a1",
            name="test.md",
            kind=ArtifactKind.MARKDOWN,
            inline_content="content",
        )
        service._artifacts["a1"] = art
        client = _client(service)
        response = client.get("/chat/artifacts")
        item = response.json()["items"][0]
        assert "content_ref" not in item
        assert "inline_content" not in item
        assert "source_ref" not in item


class TestSafeContentDisposition:
    """Direct unit tests for the Content-Disposition header builder.

    HTTP header values are latin-1; the legacy ``filename`` parameter must
    stay ASCII-only (non-ASCII names fall back to a placeholder) while the
    real name is carried by the RFC 5987 ``filename*`` parameter.
    """

    def test_ascii_filename_preserved_in_legacy_field(self):
        cd = build_content_disposition("report.md", "inline")
        assert cd == 'inline; filename="report.md"; filename*=UTF-8\'\'report.md'

    def test_unicode_filename_legacy_field_is_ascii_only(self):
        cd = build_content_disposition("横向-邮箱归属.md", "inline")
        # Legacy filename must be latin-1 encodable (no UnicodeEncodeError).
        legacy = cd.split('filename="', 1)[1].split('"', 1)[0]
        legacy.encode("latin-1")
        # RFC 5987 form carries the real non-ASCII name.
        assert f"filename*=UTF-8''{quote('横向-邮箱归属.md', safe='')}" in cd
        # Extension preserved in the ASCII fallback so legacy clients keep type.
        assert legacy.endswith(".md")

    def test_unicode_filename_whole_header_latin1_encodable(self):
        """The entire header value must survive latin-1 encoding (what
        Starlette does when writing the response header)."""
        cd = build_content_disposition("横向-邮箱归属.md", "inline")
        cd.encode("latin-1")  # must not raise

    def test_sanitizes_path_separators_and_quotes(self):
        cd = build_content_disposition('evil/.."\\n.md', "attachment")
        legacy = cd.split('filename="', 1)[1].split('"', 1)[0]
        assert "/" not in legacy
        assert "\\" not in legacy
        assert '"' not in legacy

    def test_empty_filename_falls_back(self):
        cd = build_content_disposition("", "attachment")
        assert 'filename="artifact"' in cd


# ---------------------------------------------------------------------------
# T9: Revision routes -- list / content / diff / rollback
# ---------------------------------------------------------------------------


def _patch_content(client, artifact_id: str, expected_rev_id: str, content: str) -> dict:
    """PATCH content with a CAS token; return the artifact-view dict (full detail shape, same as GET)."""
    r = client.patch(
        f"/chat/artifacts/{artifact_id}",
        json={"content": content, "expected_revision_id": expected_rev_id},
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestRevisionsList:
    def test_list_returns_revisions_with_current_and_published(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")
        # Publish so the published marker is set on rev2.
        client.post("/chat/artifacts/art-1/publish")
        r = client.get("/chat/artifacts/art-1/revisions")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        items = data["items"]
        # Ordered oldest -> newest: rev1, rev2.
        assert items[0]["revision_number"] == 1
        assert items[0]["is_current"] is False
        assert items[0]["is_published"] is False
        assert items[1]["revision_number"] == 2
        assert items[1]["is_current"] is True
        assert items[1]["is_published"] is True
        assert data["next_cursor"] is None

    def test_list_cursor_pagination(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")
        rev3 = _patch_content(client, "art-1", rev2["current_revision_id"], "# v3")
        r = client.get("/chat/artifacts/art-1/revisions?limit=2")
        assert r.status_code == 200
        page1 = r.json()
        assert page1["count"] == 2
        assert page1["items"][0]["revision_number"] == 1
        assert page1["items"][1]["revision_number"] == 2
        assert page1["next_cursor"] is not None
        # Cursor is bound to artifact_id.
        assert page1["next_cursor"]["artifact_id"] == "art-1"
        cursor_str = json.dumps(page1["next_cursor"])
        r2 = client.get(f"/chat/artifacts/art-1/revisions?limit=2&cursor={quote(cursor_str)}")
        assert r2.status_code == 200
        page2 = r2.json()
        assert page2["count"] == 1
        assert page2["items"][0]["revision_number"] == 3
        assert page2["next_cursor"] is None

    def test_list_bad_cursor_returns_revision_invalid(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/revisions?cursor=not-json")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_list_cross_artifact_cursor_returns_revision_invalid(self):
        service = FakeArtifactService()
        _seed_revisioned(service, artifact_id="art-1", revision_id="rev-1")
        _seed_revisioned(service, artifact_id="art-2", revision_id="rev-2")
        client = _client(service)
        # Cursor carries art-1's revision id; querying art-2 must reject it.
        cursor = json.dumps({"artifact_id": "art-1", "revision_number": 1, "id": "rev-1"})
        r = client.get(f"/chat/artifacts/art-2/revisions?cursor={quote(cursor)}")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_list_artifact_not_found(self):
        service = FakeArtifactService()
        client = _client(service)
        r = client.get("/chat/artifacts/nope/revisions")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_not_found"

    def test_list_limit_clamped_to_max_100(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/revisions?limit=9999")
        assert r.status_code == 200


class TestRevisionContent:
    def test_get_revision_content_returns_bytes(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.get(f"/chat/artifacts/art-1/revisions/{rev.id}/content")
        assert r.status_code == 200
        assert r.content == b"# Hello"
        assert "text/markdown" in r.headers.get("content-type", "")
        assert r.headers.get("x-content-type-options") == "nosniff"

    def test_get_revision_content_cross_artifact_is_not_found(self):
        service = FakeArtifactService()
        _seed_revisioned(service, artifact_id="art-1", revision_id="rev-1")
        _seed_revisioned(service, artifact_id="art-2", revision_id="rev-2")
        client = _client(service)
        # rev-2 belongs to art-2; querying via art-1 must not leak.
        r = client.get("/chat/artifacts/art-1/revisions/rev-2/content")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_revision_not_found"

    def test_get_revision_content_unknown_revision(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/revisions/unknown/content")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_revision_not_found"

    def test_get_revision_content_artifact_not_found(self):
        service = FakeArtifactService()
        client = _client(service)
        r = client.get("/chat/artifacts/nope/revisions/rev-1/content")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_not_found"


class TestDiffRoute:
    def test_diff_returns_text(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1\nline a")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v1\nline b")
        r = client.post("/chat/artifacts/art-1/diff", json={
            "from_revision_id": rev1.id, "to_revision_id": rev2["current_revision_id"],
        })
        assert r.status_code == 200
        data = r.json()
        assert "diff_text" in data
        assert data["binary_changed"] is False
        assert "line a" in data["diff_text"]
        assert "line b" in data["diff_text"]

    def test_diff_missing_from_revision_id(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/diff", json={"to_revision_id": "rev-1"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_invalid"

    def test_diff_missing_to_revision_id(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/diff", json={"from_revision_id": "rev-1"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_invalid"

    def test_diff_context_lines_clamped(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/diff", json={
            "from_revision_id": "rev-1", "to_revision_id": "rev-1", "context_lines": 9999,
        })
        assert r.status_code == 200

    def test_diff_cross_artifact_revision_not_found(self):
        service = FakeArtifactService()
        _seed_revisioned(service, artifact_id="art-1", revision_id="rev-1")
        _seed_revisioned(service, artifact_id="art-2", revision_id="rev-2")
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/diff", json={
            "from_revision_id": "rev-1", "to_revision_id": "rev-2",
        })
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_revision_not_found"


class TestRollbackRoute:
    def test_rollback_creates_new_version_from_target(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")
        r = client.post("/chat/artifacts/art-1/rollback", json={
            "target_revision_id": rev1.id,
            "expected_revision_id": rev2["current_revision_id"],
            "change_summary": "revert to v1",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["revision_number"] == 3
        assert data["artifact_id"] == "art-1"
        # Content is back to v1.
        content = client.get(f"/chat/artifacts/art-1/revisions/{data['revision_id']}/content")
        assert content.content == b"# v1"

    def test_rollback_conflict_on_stale_expected(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")
        # Stale expected_revision_id (rev1, but current is rev2).
        r = client.post("/chat/artifacts/art-1/rollback", json={
            "target_revision_id": rev1.id, "expected_revision_id": rev1.id,
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "artifact_revision_conflict"

    def test_rollback_missing_target(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/rollback", json={
            "expected_revision_id": "rev-1",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_invalid"

    def test_rollback_target_belongs_to_other_artifact(self):
        service = FakeArtifactService()
        _seed_revisioned(service, artifact_id="art-1", revision_id="rev-1")
        _seed_revisioned(service, artifact_id="art-2", revision_id="rev-2")
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/rollback", json={
            "target_revision_id": "rev-2", "expected_revision_id": "rev-1",
        })
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_revision_not_found"


# ---------------------------------------------------------------------------
# T9: PATCH content CAS (expected_revision_id / If-Match)
# ---------------------------------------------------------------------------


class TestPatchContentCAS:
    def test_patch_content_with_if_match_header(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Via If-Match"},
            headers={"If-Match": f'"{rev.id}"'},
        )
        assert r.status_code == 200
        assert r.json()["revision_number"] == 2

    def test_patch_content_both_tokens_agree(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Both", "expected_revision_id": rev.id},
            headers={"If-Match": f'"{rev.id}"'},
        )
        assert r.status_code == 200
        assert r.json()["revision_number"] == 2

    def test_patch_content_both_tokens_disagree_is_revision_invalid(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Disagree", "expected_revision_id": "rev-1"},
            headers={"If-Match": '"other-rev"'},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_patch_content_no_token_is_conflict(self):
        service = FakeArtifactService()
        _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch("/chat/artifacts/art-1", json={"content": "# No token"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "artifact_revision_conflict"

    def test_patch_content_stale_expected_is_conflict(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        _patch_content(client, "art-1", rev1.id, "# v2")  # current is now rev2
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Stale", "expected_revision_id": rev1.id},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "artifact_revision_conflict"

    def test_patch_content_if_match_weak_etag_rejected(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Weak"},
            headers={"If-Match": f'W/"{rev.id}"'},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_patch_content_if_match_wildcard_rejected(self):
        service = FakeArtifactService()
        _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Wildcard"},
            headers={"If-Match": "*"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_patch_content_if_match_multiple_etags_rejected(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Multi"},
            headers={"If-Match": f'"{rev.id}", "other"'},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_patch_content_if_match_unquoted_rejected(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# Unquoted"},
            headers={"If-Match": rev.id},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_patch_metadata_only_requires_no_token(self):
        service = FakeArtifactService()
        _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch("/chat/artifacts/art-1", json={"name": "renamed.md"})
        assert r.status_code == 200
        assert r.json()["name"] == "renamed.md"

    def test_patch_content_metadata_and_content_together(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={
                "name": "renamed.md", "content": "# New",
                "expected_revision_id": rev.id,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "renamed.md"
        assert data["revision_number"] == 2
        assert data["size"] == len(b"# New")

    def test_patch_content_stale_token_leaves_metadata_untouched(self):
        """M1: a stale CAS token must NOT apply metadata (no partial commit)."""
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1", name="doc.md")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")  # current -> rev2
        # Stale token (rev1) + metadata rename -> 409, name unchanged.
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"name": "renamed.md", "content": "# v3", "expected_revision_id": rev1.id},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "artifact_revision_conflict"
        # Metadata was NOT applied.
        detail = client.get("/chat/artifacts/art-1").json()
        assert detail["name"] == "doc.md"

    def test_patch_content_empty_string_token_is_revision_invalid(self):
        """m1: an empty-string expected_revision_id is invalid (422), not 409."""
        service = FakeArtifactService()
        _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.patch(
            "/chat/artifacts/art-1",
            json={"content": "# X", "expected_revision_id": ""},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_revision_invalid"

    def test_patch_multipart_content_form_field_rejected(self):
        """C1: a multipart 'content' form field must be rejected (content=file)."""
        service = FakeArtifactService()
        _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        # Real multipart (a file is present so the body is multipart), but
        # 'content' is also sent as a form field -- it must be rejected before
        # any file/CAS processing.
        r = client.patch(
            "/chat/artifacts/art-1",
            files={"file": ("x.txt", io.BytesIO(b"data"), "text/plain")},
            data={"content": "# hacked"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_invalid"
        # Content was NOT changed (no CAS, no revision created).
        r2 = client.get("/chat/artifacts/art-1/revisions")
        assert r2.json()["count"] == 1


# ---------------------------------------------------------------------------
# T9: Export capabilities + revision-pinned export
# ---------------------------------------------------------------------------


class TestExportCapabilities:
    def test_capabilities_for_markdown(self):
        service = FakeArtifactService()
        _seed_revisioned(service, kind=ArtifactKind.MARKDOWN)
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/export/capabilities")
        assert r.status_code == 200
        data = r.json()
        assert "original" in data["capabilities"]
        assert "html" in data["capabilities"]

    def test_capabilities_for_code_excludes_html(self):
        service = FakeArtifactService()
        _seed_revisioned(
            service, kind=ArtifactKind.CODE, mime="text/x-python",
            inline_content="print(1)",
        )
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/export/capabilities")
        assert r.status_code == 200
        caps = r.json()["capabilities"]
        assert "original" in caps
        assert "html" not in caps

    def test_capabilities_with_revision_id(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.get(f"/chat/artifacts/art-1/export/capabilities?revision_id={rev.id}")
        assert r.status_code == 200
        assert r.json()["revision_id"] == rev.id

    def test_capabilities_cross_artifact_revision_not_found(self):
        service = FakeArtifactService()
        _seed_revisioned(service, artifact_id="art-1", revision_id="rev-1")
        _seed_revisioned(service, artifact_id="art-2", revision_id="rev-2")
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/export/capabilities?revision_id=rev-2")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_revision_not_found"


class TestExportWithRevision:
    def test_export_specific_revision(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")
        # Export the old revision (rev1) -- content must be v1, not the current v2.
        r = client.get(f"/chat/artifacts/art-1/export?revision_id={rev1.id}")
        assert r.status_code == 200
        assert r.content == b"# v1"

    def test_export_cross_artifact_revision_not_found(self):
        service = FakeArtifactService()
        _seed_revisioned(service, artifact_id="art-1", revision_id="rev-1")
        _seed_revisioned(service, artifact_id="art-2", revision_id="rev-2")
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/export?revision_id=rev-2")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "artifact_revision_not_found"


# ---------------------------------------------------------------------------
# T9: Publish with body (revision_id + expected_current_revision_id)
# ---------------------------------------------------------------------------


class TestPublishBody:
    def test_publish_with_body_publishes_current_revision(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/publish", json={
            "revision_id": rev.id, "expected_current_revision_id": rev.id,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["reused"] is False
        assert data["published_revision_id"] == rev.id

    def test_publish_with_body_stale_current_is_conflict(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")
        # Body claims current is rev1, but it is now rev2.
        r = client.post("/chat/artifacts/art-1/publish", json={
            "revision_id": rev2["current_revision_id"],
            "expected_current_revision_id": rev1.id,
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "artifact_revision_conflict"

    def test_publish_with_body_revision_not_current_is_conflict(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "# v2")
        # Trying to publish rev1 (not current) -> conflict.
        r = client.post("/chat/artifacts/art-1/publish", json={
            "revision_id": rev1.id,
            "expected_current_revision_id": rev2["current_revision_id"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "artifact_revision_conflict"

    def test_publish_with_body_missing_expected_current(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/publish", json={"revision_id": rev.id})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_invalid"

    def test_publish_legacy_no_body_publishes_current(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/publish")
        assert r.status_code == 200
        assert r.json()["published_revision_id"] == rev.id

    def test_publish_idempotent_with_body_reuses(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        body = {"revision_id": rev.id, "expected_current_revision_id": rev.id}
        r1 = client.post("/chat/artifacts/art-1/publish", json=body)
        assert r1.status_code == 200
        assert r1.json()["reused"] is False
        r2 = client.post("/chat/artifacts/art-1/publish", json=body)
        assert r2.status_code == 200
        assert r2.json()["reused"] is True
        assert r1.json()["publish_id"] == r2.json()["publish_id"]


# ---------------------------------------------------------------------------
# T9: Artifact view enrichment (current_revision_id / revision_number / state)
# ---------------------------------------------------------------------------


class TestArtifactViewEnrichment:
    def test_detail_includes_revision_fields(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.get("/chat/artifacts/art-1")
        assert r.status_code == 200
        data = r.json()
        assert data["current_revision_id"] == rev.id
        assert data["revision_number"] == 1
        assert data["publish_sync_state"] == "unpublished"
        # content_ref stays hidden.
        assert "content_ref" not in data
        assert "inline_content" not in data

    def test_detail_publish_sync_state_after_publish(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        client.post("/chat/artifacts/art-1/publish")
        r = client.get("/chat/artifacts/art-1")
        assert r.json()["publish_sync_state"] == "current"

    def test_detail_publish_sync_state_outdated_after_edit(self):
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="# v1")
        client = _client(service)
        client.post("/chat/artifacts/art-1/publish")  # publishes rev1
        _patch_content(client, "art-1", rev1.id, "# v2")  # current -> rev2
        r = client.get("/chat/artifacts/art-1")
        assert r.json()["publish_sync_state"] == "outdated"

    def test_list_includes_revision_fields(self):
        service = FakeArtifactService()
        art, rev = _seed_revisioned(service, inline_content="# Hello")
        client = _client(service)
        r = client.get("/chat/artifacts")
        assert r.status_code == 200
        items = r.json()["items"]
        assert items[0]["current_revision_id"] == rev.id
        assert items[0]["revision_number"] == 1

    def test_create_response_includes_revision_fields(self):
        service = FakeArtifactService()
        client = _client(service)
        r = client.post("/chat/artifacts", json={
            "name": "doc.md", "kind": "markdown", "content": "# Hello",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["current_revision_id"] is not None
        assert data["revision_number"] == 1
        assert data["publish_sync_state"] == "unpublished"


# ---------------------------------------------------------------------------
# T9: Error-code mapping (10 new codes via _exception_to_response)
# ---------------------------------------------------------------------------


class TestRevisionErrorMapping:
    @pytest.mark.parametrize(
        "exc_cls,code,status",
        [
            (ArtifactRevisionNotFoundError, "artifact_revision_not_found", 404),
            (ArtifactRevisionConflictError, "artifact_revision_conflict", 409),
            (ArtifactRevisionValidationError, "artifact_revision_invalid", 422),
            (ArtifactReadTooLargeError, "artifact_read_too_large", 413),
            (ArtifactDiffTooLargeError, "artifact_diff_too_large", 413),
            (ArtifactDiffUnsupportedError, "artifact_diff_unsupported", 422),
            (ArtifactExportUnsupportedError, "artifact_export_unsupported", 422),
            (ArtifactExportTooLargeError, "artifact_export_too_large", 413),
            (ArtifactMigrationIncompleteError, "artifact_migration_incomplete", 503),
        ],
    )
    def test_exception_maps_to_code(self, exc_cls, code, status):
        from app.interfaces.http.artifact_routes import _exception_to_response
        resp = _exception_to_response(exc_cls("detail"))
        assert resp.status_code == status
        body = json.loads(resp.body)
        assert body["error"]["code"] == code

    def test_export_error_maps_to_failed_with_generic_message(self):
        from app.interfaces.http.artifact_routes import _exception_to_response
        resp = _exception_to_response(ArtifactExportError("python-docx: /tmp/secret.docx"))
        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["error"]["code"] == "artifact_export_failed"
        # Generic message; no library internals / abs paths leak.
        assert body["error"]["message"] == "export failed"

    def test_revision_validation_takes_precedence_over_validation(self):
        from app.interfaces.http.artifact_routes import _exception_to_response
        # ArtifactRevisionValidationError is a subclass of ArtifactValidationError;
        # it must map to artifact_revision_invalid, not artifact_invalid.
        resp = _exception_to_response(ArtifactRevisionValidationError("bad"))
        body = json.loads(resp.body)
        assert body["error"]["code"] == "artifact_revision_invalid"
        # The parent still maps to artifact_invalid.
        resp2 = _exception_to_response(ArtifactValidationError("bad"))
        body2 = json.loads(resp2.body)
        assert body2["error"]["code"] == "artifact_invalid"

    def test_export_subclasses_take_precedence_over_export_error(self):
        from app.interfaces.http.artifact_routes import _exception_to_response
        for sub_cls, code in [
            (ArtifactExportUnsupportedError, "artifact_export_unsupported"),
            (ArtifactExportTooLargeError, "artifact_export_too_large"),
        ]:
            resp = _exception_to_response(sub_cls("x"))
            body = json.loads(resp.body)
            assert body["error"]["code"] == code

    def test_diff_route_maps_diff_unsupported(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        service._raise_on_diff = ArtifactDiffUnsupportedError("binary diff unsupported")
        client = _client(service)
        r = client.post("/chat/artifacts/art-1/diff", json={
            "from_revision_id": "rev-1", "to_revision_id": "rev-1",
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "artifact_diff_unsupported"

    def test_capabilities_route_maps_export_failed(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        service._raise_on_export_capabilities = ArtifactExportError("boom")
        client = _client(service)
        r = client.get("/chat/artifacts/art-1/export/capabilities")
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "artifact_export_failed"

    def test_patch_content_maps_migration_incomplete(self):
        service = FakeArtifactService()
        _seed_revisioned(service)
        service._raise_on_update_revision = ArtifactMigrationIncompleteError("no revision")
        client = _client(service)
        r = client.patch("/chat/artifacts/art-1", json={
            "content": "# X", "expected_revision_id": "rev-1",
        })
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "artifact_migration_incomplete"


# ---------------------------------------------------------------------------
# T9: diff safe rendering helper (Content-Disposition / no innerHTML contract)
# ---------------------------------------------------------------------------


class TestDiffSafeRendering:
    def test_diff_text_is_plain_text_not_html(self):
        """diff_text is returned as a JSON string; the Dashboard renders it in
        a <pre>/text node, never via innerHTML (spec line 207)."""
        service = FakeArtifactService()
        art, rev1 = _seed_revisioned(service, inline_content="<script>alert(1)</script>")
        client = _client(service)
        rev2 = _patch_content(client, "art-1", rev1.id, "<script>alert(2)</script>")
        r = client.post("/chat/artifacts/art-1/diff", json={
            "from_revision_id": rev1.id, "to_revision_id": rev2["current_revision_id"],
        })
        assert r.status_code == 200
        # The response is JSON: diff_text is a plain string value, not HTML.
        assert r.headers["content-type"].startswith("application/json")
        data = r.json()
        assert "<script>alert(1)</script>" in data["diff_text"]
        assert "<script>alert(2)</script>" in data["diff_text"]
