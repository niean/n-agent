"""ArtifactService -- Application-layer orchestration for the Artifact workbench.

Orchestrates Artifact CRUD, content reads, export, publish lifecycle, and
source registration. Sealed by ArtifactPolicy (edit/delete/publish admission)
and InformationFlowService (text release for publish).

Dependencies: Domain ports (ArtifactRegistry, ArtifactContentStore), Domain
policy (ArtifactPolicy), Application services (InformationFlowService,
PolicyAuditService), and an injected HTML converter callable. No FastAPI,
SQLite, path_security, Settings, or Infrastructure imports -- uses an
immutable ArtifactServiceConfig dataclass snapshot. The HTML converter is
injected via the constructor so the Application layer never imports
Infrastructure directly.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from app.application.information_flow_service import InformationFlowService, ReleaseResult
from app.application.policy_audit_service import PolicyAuditService
from app.domain.artifact import (
    Artifact,
    ArtifactAttachmentSource,
    ArtifactContentUnavailableError,
    ArtifactError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactListPage,
    ArtifactNotFoundError,
    ArtifactRegistry,
    ArtifactContentStore,
    ArtifactSource,
    ArtifactStatus,
    ArtifactValidationError,
    PublishedArtifact,
    PublishedArtifactNotFoundError,
    PublishedArtifactStatus,
)
from app.domain.artifact_policy import (
    ArtifactPolicy,
    ArtifactPolicyAction,
    ArtifactPolicyOutcome,
    ArtifactPolicyRequest,
)
from app.domain.information_flow import Classification, ReleaseTarget
from app.domain.policy import (
    PolicyAuditEvent,
    PolicyDecisionKind,
    PolicyOutcome,
)
from app.domain.task import TaskArtifact

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config and errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactServiceConfig:
    """Immutable configuration snapshot (NOT the Settings class).

    Passed by the composition root; the service reads size limits and the
    published_base_url origin from this snapshot. The content store root
    path is NOT included here -- all content IO goes through the injected
    ArtifactContentStore which was already constructed with it.
    """

    artifact_max_bytes: int = 20 * 1024 * 1024
    artifact_publish_max_bytes: int = 10 * 1024 * 1024
    artifact_inline_max_bytes: int = 256 * 1024
    published_base_url: str = ""


class ArtifactTooLargeError(ArtifactError):
    """Raised when content exceeds a configured size limit."""


class PublishBlockedError(ArtifactError):
    """Raised when publish is denied by policy or information flow."""


@dataclass(frozen=True)
class PublishResult:
    """Result of a publish call.

    Attributes:
        published: the PublishedArtifact (newly created or reused).
        share_url: absolute URL (if origin configured) or relative path.
        reused: True when an existing active publish was returned unchanged.
    """

    published: PublishedArtifact
    share_url: str
    reused: bool


# ---------------------------------------------------------------------------
# Text vs binary kind sets (mirror domain internal sets)
# ---------------------------------------------------------------------------

_TEXT_KINDS: frozenset[ArtifactKind] = frozenset({
    ArtifactKind.DOCUMENT,
    ArtifactKind.MARKDOWN,
    ArtifactKind.CODE,
    ArtifactKind.HTML,
    ArtifactKind.DATA,
    ArtifactKind.CSV,
    ArtifactKind.JSON,
    ArtifactKind.TEXT,
})

_MIME_TO_KIND: dict[str, ArtifactKind] = {
    "text/markdown": ArtifactKind.MARKDOWN,
    "text/x-markdown": ArtifactKind.MARKDOWN,
    "text/html": ArtifactKind.HTML,
    "text/csv": ArtifactKind.CSV,
    "application/json": ArtifactKind.JSON,
    "application/pdf": ArtifactKind.PDF,
}

_ACTOR = "dashboard"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _kind_from_mime(mime: str) -> ArtifactKind:
    """Derive ArtifactKind from a MIME type string."""
    normalized = (mime or "").lower().split(";")[0].strip()
    if normalized in _MIME_TO_KIND:
        return _MIME_TO_KIND[normalized]
    if normalized.startswith("image/"):
        return ArtifactKind.IMAGE
    if normalized.startswith("text/"):
        return ArtifactKind.TEXT
    return ArtifactKind.OTHER


# Filename extension -> MIME mapping, used to infer kind/mime when the source
# supplies no content_type. Task artifacts submitted via task_complete carry
# only {name, storage_ref, type} (no mime); without this fallback they were
# classified as OTHER and could not render in the workbench.
_EXT_TO_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}


def _resolve_mime(name: str, mime: str) -> str:
    """Return ``mime`` when present; otherwise infer from filename extension.

    Longest-extension-first so ``.markdown`` wins over ``.md`` if both could
    match. Returns ``""`` when neither mime nor a known extension is available
    (caller then classifies as OTHER via ``_kind_from_mime``).
    """
    if mime:
        return mime
    lowered = (name or "").lower()
    for ext in sorted(_EXT_TO_MIME, key=len, reverse=True):
        if lowered.endswith(ext):
            return _EXT_TO_MIME[ext]
    return ""


def _generate_artifact_id() -> str:
    return secrets.token_urlsafe(16)


def _generate_publish_id() -> str:
    """Generate a high-entropy publish_id (>=128-bit, URL-safe, no padding)."""
    return secrets.token_urlsafe(16)


def _is_text_kind(kind: ArtifactKind) -> bool:
    return kind in _TEXT_KINDS


def _is_owned_ref(content_ref: str | None) -> bool:
    """Check if a content_ref points to Artifact-owned storage (item: scheme)."""
    return content_ref is not None and content_ref.startswith("item:")


def _is_source_ref(content_ref: str | None) -> bool:
    """Check if a content_ref points to a read-only source (attachment/workspace)."""
    if content_ref is None:
        return False
    return content_ref.startswith("attachment:") or content_ref.startswith("workspace:")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ArtifactService:
    """Application-layer orchestration for the Artifact workbench.

    All public methods are async. Actor is v1-fixed to ``dashboard`` (trusted
    ingress); client-supplied actor strings are not accepted.
    """

    def __init__(
        self,
        registry: ArtifactRegistry,
        content_store: ArtifactContentStore,
        policy: ArtifactPolicy,
        information_flow_service: InformationFlowService,
        policy_audit_service: PolicyAuditService,
        config: ArtifactServiceConfig,
        convert_to_html: Callable[[str], str],
        task_session_resolver: Callable[[str], Awaitable[str | None]] | None = None,
        task_exists: Callable[[str], Awaitable[bool]] | None = None,
        task_attachment_delete: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._registry = registry
        self._content_store = content_store
        self._policy = policy
        self._flow = information_flow_service
        self._audit = policy_audit_service
        self._config = config
        self._convert_to_html = convert_to_html
        self._task_session_resolver = task_session_resolver
        self._task_exists_callback = task_exists
        self._task_attachment_delete_callback = task_attachment_delete

    def set_task_session_resolver(
        self, resolver: Callable[[str], Awaitable[str | None]],
    ) -> None:
        """Late-bind the task_id -> session_id resolver.

        The resolver is wired after construction because ``task_registry``
        is created in a separate composition branch (``settings.task_enabled``)
        from ``artifact_service`` (``settings.artifacts_enabled``). Mirrors the
        existing ``set_run_service`` late-bind pattern.
        """
        self._task_session_resolver = resolver

    def set_task_exists_callback(
        self, callback: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Late-bind the task_id -> exists callback for orphan backfill.

        Used by :meth:`backfill_orphaned_task_artifacts` to detect artifacts
        whose source task has been deleted. Mirrors ``set_task_session_resolver``
        late-bind (task_registry lives in the task composition branch).
        """
        self._task_exists_callback = callback

    def set_task_attachment_delete_callback(
        self, callback: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Late-bind the attachment_id -> delete callback for artifact delete.

        When a task_attachment-sourced artifact is deleted from the workbench,
        the underlying TaskAttachment (DB record + file) must be cascade-deleted
        too -- otherwise the task detail page still shows the attachment and the
        next startup ``backfill_attachments`` re-registers it (resurrection).
        Mirrors ``set_task_exists_callback`` late-bind (task_service lives in
        the task composition branch).
        """
        self._task_attachment_delete_callback = callback

    async def _resolve_task_session(self, task_id: str) -> str | None:
        """Resolve a task id to its execution session id via the injected
        resolver. Returns None when no resolver is wired (task subsystem
        disabled); task artifacts then get a NULL source_session_id and simply
        remain invisible to the session-keyed panel query (no regression)."""
        if self._task_session_resolver is None:
            return None
        try:
            return await self._task_session_resolver(task_id)
        except Exception as exc:
            logger.warning(
                "task session resolve failed: task_id=%s error=%s",
                task_id, type(exc).__name__,
            )
            return None

    # ------------------------------------------------------------------
    # List / Get
    # ------------------------------------------------------------------

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
        """List artifacts with stable cursor pagination.

        Limit is clamped to 1..100, default 50.
        """
        clamped = max(1, min(100, limit))
        return await self._registry.list_artifacts(
            source_kind=source_kind,
            source_context_ref=source_context_ref,
            source_session_id=source_session_id,
            kind=kind,
            status=status,
            q=q,
            cursor=cursor,
            limit=clamped,
        )

    async def get_artifact(self, artifact_id: str) -> Artifact:
        """Return artifact metadata only (no content). Raises if missing."""
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        return art

    async def get_artifact_by_source(
        self, source_kind: ArtifactSource, source_ref: str,
    ) -> Artifact | None:
        """Return artifact by (source_kind, source_ref), or None if not registered."""
        return await self._registry.get_by_source(source_kind, source_ref)

    async def get_content(self, artifact_id: str) -> tuple[bytes, Artifact]:
        """Bounded content read. Returns (content_bytes, artifact).

        For inline content, returns the UTF-8 encoded inline bytes.
        For file-backed content, reads via content_store with max_bytes.
        Raises ArtifactNotFoundError if artifact missing.
        Raises ArtifactContentUnavailableError if content unreadable.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.inline_content is not None:
            return art.inline_content.encode("utf-8"), art
        if art.content_ref is None:
            raise ArtifactContentUnavailableError(
                f"artifact has no content: {artifact_id}"
            )
        data = await self._content_store.read(
            art.content_ref, max_bytes=self._config.artifact_max_bytes
        )
        return data, art

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

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
        source_session_id: str | None = None,
        summary: str = "",
        classification: str | None = None,
        labels: tuple[str, ...] | None = None,
        created_by: str = _ACTOR,
    ) -> Artifact:
        """Create a new artifact.

        Exactly one of inline_content / file_data must be provided.
        For manual source, source_ref defaults to the generated artifact_id.
        Content is written first, then registry; on registry failure the new
        owned content is compensated (deleted).
        """
        artifact_id = _generate_artifact_id()
        if source_kind is ArtifactSource.MANUAL and source_ref is None:
            source_ref = artifact_id

        new_content_ref: str | None = None
        new_inline: str | None = None
        size: int
        checksum: str

        if inline_content is not None:
            data = inline_content.encode("utf-8")
            if len(data) > self._config.artifact_inline_max_bytes:
                raise ArtifactTooLargeError(
                    f"inline content {len(data)} exceeds "
                    f"inline_max_bytes {self._config.artifact_inline_max_bytes}"
                )
            size = len(data)
            checksum = _sha256_checksum(data)
            new_inline = inline_content
        elif file_data is not None:
            if len(file_data) > self._config.artifact_max_bytes:
                raise ArtifactTooLargeError(
                    f"file content {len(file_data)} exceeds "
                    f"artifact_max_bytes {self._config.artifact_max_bytes}"
                )
            size = len(file_data)
            checksum = _sha256_checksum(file_data)
            new_content_ref = await self._content_store.write_atomic(
                artifact_id, filename or name, file_data
            )
        else:
            raise ArtifactValidationError(
                "create requires exactly one of inline_content / file_data"
            )

        artifact = Artifact(
            id=artifact_id,
            name=name,
            kind=kind,
            mime=mime,
            content_ref=new_content_ref,
            inline_content=new_inline,
            size=size,
            checksum=checksum,
            source_kind=source_kind,
            source_ref=source_ref,
            source_context_ref=source_context_ref,
            source_session_id=source_session_id,
            summary=summary,
            classification=classification,
            labels=tuple(labels) if labels is not None else None,
            status=ArtifactStatus.DRAFT,
            created_by=created_by,
        )

        try:
            return await self._registry.create_artifact(artifact)
        except Exception:
            if new_content_ref is not None:
                await self._best_effort_delete(new_content_ref)
            raise

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

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
        """Update artifact metadata and/or content.

        Content change goes through edit_admission (archived -> deny).
        For source-backed artifacts (attachment/workspace), first edit
        materializes to owned storage (does NOT modify original source).
        On registry failure, old content remains readable.
        On success, old owned content is best-effort cleaned up.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        # edit_admission
        await self._evaluate_policy(
            art, ArtifactPolicyAction.EDIT, content_available=True,
            active_publish_checksum=None,
        )

        has_content_change = inline_content is not None or file_data is not None
        new_name = name if name is not None else art.name
        new_summary = summary if summary is not None else art.summary
        new_classification = (
            classification if classification is not None else art.classification
        )
        new_labels = tuple(labels) if labels is not None else art.labels

        if not has_content_change:
            updated = replace(
                art,
                name=new_name,
                summary=new_summary,
                classification=new_classification,
                labels=new_labels,
                updated_at=datetime.now(timezone.utc),
            )
            return await self._registry.update_artifact(updated)

        # --- Content change ---
        new_content_ref = art.content_ref
        new_inline = art.inline_content
        new_size = art.size
        new_checksum = art.checksum
        materialized_ref: str | None = None
        temp_new_ref: str | None = None

        # First edit of source-backed artifact: materialize to owned storage.
        # Only needed for file replacement -- inline replacement does not read
        # the old content, so materializing would be wasteful.
        if _is_source_ref(art.content_ref) and file_data is not None:
            materialized_ref = await self._content_store.materialize_source(
                art.source_kind, art.content_ref, art.id
            )
            new_content_ref = materialized_ref
            new_inline = None

        old_owned_ref = art.content_ref if _is_owned_ref(art.content_ref) else None

        try:
            if inline_content is not None:
                data = inline_content.encode("utf-8")
                if len(data) > self._config.artifact_inline_max_bytes:
                    raise ArtifactTooLargeError(
                        f"inline content {len(data)} exceeds "
                        f"inline_max_bytes {self._config.artifact_inline_max_bytes}"
                    )
                new_inline = inline_content
                new_content_ref = None
                new_size = len(data)
                new_checksum = _sha256_checksum(data)
            elif file_data is not None:
                if len(file_data) > self._config.artifact_max_bytes:
                    raise ArtifactTooLargeError(
                        f"file content {len(file_data)} exceeds "
                        f"artifact_max_bytes {self._config.artifact_max_bytes}"
                    )
                temp_new_ref = await self._content_store.write_atomic(
                    art.id, filename or new_name, file_data
                )
                new_content_ref = temp_new_ref
                new_inline = None
                new_size = len(file_data)
                new_checksum = _sha256_checksum(file_data)

            updated = replace(
                art,
                name=new_name,
                summary=new_summary,
                classification=new_classification,
                labels=new_labels,
                content_ref=new_content_ref,
                inline_content=new_inline,
                size=new_size,
                checksum=new_checksum,
                updated_at=datetime.now(timezone.utc),
            )

            result = await self._registry.update_artifact(updated)
        except Exception:
            # Registry failure or validation error: clean up new content,
            # old content remains readable.
            if temp_new_ref is not None:
                await self._best_effort_delete(temp_new_ref)
            if materialized_ref is not None:
                await self._best_effort_delete(materialized_ref)
            raise

        # Success: best-effort cleanup of old owned content and materialized copy.
        if materialized_ref is not None:
            await self._best_effort_delete(materialized_ref)
        if old_owned_ref is not None and old_owned_ref != materialized_ref:
            await self._best_effort_delete(old_owned_ref)

        # Content change invalidates the active publish snapshot: the snapshot
        # now diverges from the artifact, so leaving it active would expose
        # stale content at the public link. Revoke it (public link 410s, state
        # reverts to unpublished) -- the user re-publishes if they want the new
        # content public. Metadata-only edits skip this (snapshot content is
        # unchanged, so the publish stays valid; that branch returns above).
        active_publish = await self._registry.get_active_publish(artifact_id)
        if active_publish is not None:
            await self._registry.revoke_published(artifact_id)

        return result

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_artifact(self, artifact_id: str) -> bool:
        """Delete artifact metadata, then best-effort delete owned content.

        For task_attachment-sourced artifacts, the underlying TaskAttachment
        (source of truth) is cascade-deleted FIRST via the injected
        ``task_attachment_delete`` callback, so the task detail page stays in
        sync and the next startup backfill cannot resurrect the artifact. If the
        callback raises, the artifact metadata is left intact (no resurrection
        risk) and the error propagates. Purges ALL publish records + snapshot
        files for this artifact BEFORE the metadata delete (artifact_id must
        still be set to locate them): rows deleted -> public links 404, then
        snapshot content dirs removed best-effort. No orphaned rows (with NULL
        artifact_id) or snapshot files survive.
        Repeat delete raises ArtifactNotFoundError.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        # delete_admission (always allow)
        await self._evaluate_policy(
            art, ArtifactPolicyAction.DELETE, content_available=True,
            active_publish_checksum=None,
        )

        # Cascade-delete the source TaskAttachment BEFORE the artifact metadata.
        # The task attachment is the source of truth; deleting it first prevents
        # backfill_attachments from resurrecting the artifact on next startup.
        # Propagate on failure so a half-deleted source never leaves the artifact
        # gone while its attachment survives.
        await self._cascade_delete_task_attachment(art)

        # Purge ALL publish records + snapshot files for this artifact BEFORE
        # deleting the artifact metadata: the published_artifacts.artifact_id FK
        # is ON DELETE SET NULL, so once the artifacts row is gone the publishes
        # can no longer be located by artifact_id. Deleting the rows first cuts
        # off the public links (404); snapshot content dirs are then removed
        # best-effort. Full cleanup -- no orphaned rows (with NULL artifact_id)
        # or snapshot files survive the source deletion. Row deletion errors
        # propagate (artifact metadata left intact); file deletion is
        # best-effort (rows already gone, so leftover files are harmless disk
        # waste). No publish -> no-op.
        publishes = await self._registry.list_published(artifact_id)
        if publishes:
            await self._registry.delete_published_by_artifact(artifact_id)
            for pub in publishes:
                await self._best_effort_delete_publish_snapshot(pub.publish_id)

        # Delete metadata first.
        await self._registry.delete_artifact(artifact_id)

        # Best-effort delete owned content.
        if _is_owned_ref(art.content_ref):
            await self._best_effort_delete(art.content_ref)

        return True

    async def _cascade_delete_task_attachment(self, artifact: Artifact) -> None:
        """Cascade-delete the source TaskAttachment for a task_attachment artifact.

        No-op when the callback is not wired (task subsystem disabled -- there
        is no source to delete) or when the artifact is not task_attachment
        -sourced (manual / task_artifact / session). Propagates callback errors
        so the caller (:meth:`delete_artifact`) leaves the artifact metadata
        intact, avoiding backfill resurrection of a half-deleted source.
        """
        if (
            self._task_attachment_delete_callback is None
            or artifact.source_kind is not ArtifactSource.TASK_ATTACHMENT
        ):
            return
        attachment_id = artifact.source_ref
        if not attachment_id:
            return
        await self._task_attachment_delete_callback(attachment_id)

    async def delete_artifacts_by_source_task(self, task_id: str) -> int:
        """Delete every artifact whose ``source_context_ref`` == ``task_id``.

        Used by TaskService.delete_task to cascade-delete a task's artifacts
        (task_attachment + task_artifact) from the separate artifacts DB so they
        no longer appear in the artifact list. Each artifact is removed via
        :meth:`delete_artifact` (policy + metadata + owned content + active
        publish revocation); public links 410 once the source is gone. Per-artifact
        failures are logged and skipped. Returns the number deleted.
        """
        # Collect all ids first (delete mutates the result set); paginate to
        # cover tasks with more than one page of artifacts.
        ids: list[str] = []
        cursor: ArtifactListCursor | None = None
        while True:
            page = await self._registry.list_artifacts(
                source_context_ref=task_id, cursor=cursor, limit=100,
            )
            ids.extend(a.id for a in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        deleted = 0
        for artifact_id in ids:
            try:
                await self.delete_artifact(artifact_id)
                deleted += 1
            except ArtifactNotFoundError:
                pass
            except Exception as exc:  # best-effort: one failure must not abort the rest
                logger.warning(
                    "delete artifact %s for task %s failed: %s",
                    artifact_id, task_id, exc,
                )
        return deleted

    async def backfill_orphaned_task_artifacts(
        self, *, batch_size: int = 100,
    ) -> dict[str, int]:
        """Delete task-sourced artifacts whose source task no longer exists.

        Task artifacts (``task_attachment``/``task_artifact``) carry
        ``source_context_ref = task_id``. Before the delete-task cascade existed,
        deleting a task left its artifacts as orphans that still showed in the
        artifact list. This startup backfill reclaims them: for each task-sourced
        artifact it asks the injected ``task_exists`` callback whether the task
        still lives, and deletes the artifact when it does not.

        Fail-safe: a ``task_exists`` error counts the artifact as ``failed`` and
        leaves it intact (never delete when existence is uncertain). No-op when
        no callback is wired (task subsystem disabled). Returns
        ``{processed, deleted, skipped, failed}``.
        """
        processed = 0
        deleted = 0
        skipped = 0
        failed = 0

        if self._task_exists_callback is None:
            return {"processed": 0, "deleted": 0, "skipped": 0, "failed": 0}

        for source_kind in (ArtifactSource.TASK_ATTACHMENT, ArtifactSource.TASK_ARTIFACT):
            cursor: ArtifactListCursor | None = None
            while True:
                page = await self._registry.list_artifacts(
                    source_kind=source_kind, cursor=cursor, limit=batch_size,
                )
                for art in page.items:
                    processed += 1
                    task_id = art.source_context_ref or ""
                    if not task_id:
                        skipped += 1
                        continue
                    try:
                        exists = await self._task_exists_callback(task_id)
                    except Exception as exc:
                        # Fail-safe: cannot confirm deletion -> skip, do not delete.
                        failed += 1
                        logger.warning(
                            "orphan backfill task_exists failed: "
                            "task_id=%s artifact=%s error=%s",
                            task_id, art.id, exc,
                        )
                        continue
                    if exists:
                        skipped += 1
                        continue
                    try:
                        await self.delete_artifact(art.id)
                        deleted += 1
                    except Exception as exc:
                        failed += 1
                        logger.warning(
                            "orphan backfill delete failed: artifact=%s error=%s",
                            art.id, exc,
                        )
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor

        return {
            "processed": processed,
            "deleted": deleted,
            "skipped": skipped,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    async def export(
        self, artifact_id: str, *, format: str = "original"
    ) -> tuple[bytes, str, str]:
        """Export artifact content.

        original: returns source bytes + original mime + safe filename.
        html: only markdown/document kinds, converted via export_converter.

        Export does NOT modify the artifact.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        if format == "original":
            data, _ = await self.get_content(artifact_id)
            return data, art.mime, art.name

        if format == "html":
            if art.kind not in (ArtifactKind.MARKDOWN, ArtifactKind.DOCUMENT):
                raise ArtifactValidationError(
                    f"html export only supports markdown/document, got {art.kind}"
                )
            data, _ = await self.get_content(artifact_id)
            content_str = data.decode("utf-8")
            html = self._convert_to_html(content_str)
            return html.encode("utf-8"), "text/html", art.name

        raise ArtifactValidationError(f"unsupported export format: {format}")

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(
        self, artifact_id: str
    ) -> PublishResult:
        """Publish an artifact to a public snapshot.

        Flow:
        1. Get artifact + active publish.
        2. ArtifactPolicy.publish_admission.
        3. If deny -> raise.
        4. If reuse -> return existing active publish.
        5. Text: InformationFlowService.release -> use redacted content.
        6. Binary: copy original bytes (policy gated classification=PUBLIC).
        7. Generate publish_id, copy snapshot, register_published.
        8. On registry failure -> compensate delete new snapshot.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        active = await self._registry.get_active_publish(artifact_id)
        active_checksum = active.snapshot_checksum if active is not None else None
        content_available = (
            art.inline_content is not None or bool(art.content_ref)
        )

        outcome = await self._evaluate_policy(
            art,
            ArtifactPolicyAction.PUBLISH,
            content_available=content_available,
            active_publish_checksum=active_checksum,
        )

        # Reuse: return existing active publish.
        if outcome.reuse and active is not None:
            return PublishResult(
                published=active,
                share_url=self._compute_share_url(active.publish_id),
                reused=True,
            )

        # --- Create new snapshot ---
        is_text = _is_text_kind(art.kind)
        snapshot_ref: str | None = None
        snapshot_inline: str | None = None
        snapshot_content: str | None = None
        snapshot_size: int
        snapshot_checksum: str

        if is_text:
            # Read content for InformationFlow release.
            content_str = await self._read_text_content(art)
            classification = self._resolve_classification(art)
            labels = frozenset(art.labels) if art.labels else frozenset()
            # source_session_id is the real session id for task artifacts;
            # source_context_ref holds the task id (not a session) and must
            # not be passed to InformationFlow as session_id.
            session_id = art.source_session_id or ""
            result = self._flow.release(
                content_str,
                target=ReleaseTarget.PUBLIC_ARTIFACT,
                classification=classification,
                origin="artifact",
                labels=labels,
                run_id="",
                session_id=session_id,
            )
            if not result.allowed or result.content is None:
                raise PublishBlockedError(
                    f"publish blocked by information flow: {result.error}"
                )
            snapshot_content = result.content
            snapshot_bytes = snapshot_content.encode("utf-8")
            snapshot_size = len(snapshot_bytes)
            snapshot_checksum = _sha256_checksum(snapshot_bytes)
            # Inline XOR file: small content stored inline only; large content
            # copied to a publish snapshot file.
            if snapshot_size <= self._config.artifact_inline_max_bytes:
                snapshot_inline = snapshot_content
                snapshot_ref = None
            else:
                snapshot_inline = None
                # snapshot_ref will be set below via copy_to_publish_snapshot.
        else:
            # Binary: copy original bytes (policy already gated PUBLIC).
            if art.content_ref is None:
                raise ArtifactContentUnavailableError(
                    f"binary artifact has no content_ref: {artifact_id}"
                )
            # Verify content is readable and compute trusted size/checksum.
            data = await self._content_store.read(
                art.content_ref, max_bytes=self._config.artifact_max_bytes
            )
            snapshot_size = len(data)
            snapshot_checksum = _sha256_checksum(data)
            snapshot_inline = None

        publish_id = _generate_publish_id()

        # Create snapshot file only when content is not stored inline.
        # Text small: snapshot_inline set, skip file creation.
        # Text large: snapshot_inline is None, copy redacted content to file.
        # Binary: snapshot_inline is None, copy original bytes to file.
        if snapshot_inline is None:
            if is_text:
                snapshot_ref = await self._content_store.copy_to_publish_snapshot(
                    "", publish_id, inline=snapshot_content
                )
            else:
                snapshot_ref = await self._content_store.copy_to_publish_snapshot(
                    art.content_ref, publish_id, inline=None
                )

        is_replacement = active is not None and not outcome.reuse
        published = PublishedArtifact(
            publish_id=publish_id,
            artifact_id=artifact_id,
            snapshot_name=art.name,
            snapshot_kind=art.kind,
            snapshot_mime=art.mime,
            snapshot_content_ref=snapshot_ref,
            snapshot_inline_content=snapshot_inline,
            snapshot_size=snapshot_size,
            snapshot_checksum=snapshot_checksum,
            snapshot_summary=art.summary,
            published_at=datetime.now(timezone.utc),
            published_by=_ACTOR,
            status=PublishedArtifactStatus.ACTIVE,
        )

        try:
            await self._registry.register_published(
                published,
                revoke_artifact_id=artifact_id if is_replacement else None,
            )
        except Exception:
            # Compensate: delete the new snapshot, old active preserved.
            if snapshot_ref is not None:
                await self._best_effort_delete(snapshot_ref)
            raise

        return PublishResult(
            published=published,
            share_url=self._compute_share_url(publish_id),
            reused=False,
        )

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    async def revoke_publish(
        self, artifact_id: str
    ) -> PublishedArtifact:
        """Revoke the active publish for an artifact.

        Idempotent: repeat revoke returns the same revoked PublishedArtifact.
        Raises PublishedArtifactNotFoundError if no publish exists.
        Does NOT delete the snapshot content.
        """
        result = await self._registry.revoke_published(artifact_id)
        if result is None:
            raise PublishedArtifactNotFoundError(
                f"no active or revoked publish for artifact: {artifact_id}"
            )
        return result

    # ------------------------------------------------------------------
    # Get published (no source read)
    # ------------------------------------------------------------------

    async def get_published(self, publish_id: str) -> PublishedArtifact:
        """Return a PublishedArtifact by publish_id.

        MUST NOT call get_artifact or read source content.
        Raises PublishedArtifactNotFoundError if missing.
        """
        result = await self._registry.get_published(publish_id)
        if result is None:
            raise PublishedArtifactNotFoundError(
                f"published artifact not found: {publish_id}"
            )
        return result

    async def get_published_content(
        self, publish_id: str,
    ) -> tuple[bytes, PublishedArtifact]:
        """Bounded read of published snapshot content. Read-only.

        Returns (snapshot_bytes, published). Raises
        PublishedArtifactNotFoundError if the published artifact does not
        exist. Does NOT read source Artifact content -- only the published
        snapshot (inline text or snapshot content_ref file).
        """
        published = await self.get_published(publish_id)
        if published.snapshot_inline_content is not None:
            return published.snapshot_inline_content.encode("utf-8"), published
        if published.snapshot_content_ref is None:
            raise ArtifactContentUnavailableError(
                f"published artifact has no content: {publish_id}"
            )
        data = await self._content_store.read(
            published.snapshot_content_ref,
            max_bytes=self._config.artifact_publish_max_bytes,
        )
        return data, published

    def convert_markdown_to_html(self, content: str) -> str:
        """Convert markdown to safe standalone HTML via the injected converter.

        Returns a complete HTML document with strict CSP. The converter is
        injected via the constructor so the Application layer never imports
        Infrastructure directly.
        """
        return self._convert_to_html(content)

    async def get_active_publish(self, artifact_id: str) -> PublishedArtifact | None:
        """Return the active publish for an artifact, or None if not published.

        Read-only: does NOT create, revoke, or modify any publish.
        Raises ArtifactNotFoundError if the artifact itself does not exist.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        return await self._registry.get_active_publish(artifact_id)

    # ------------------------------------------------------------------
    # Source registration
    # ------------------------------------------------------------------

    async def register_from_attachment(
        self, attachment: ArtifactAttachmentSource
    ) -> Artifact | None:
        """Idempotent register from a TaskAttachment source.

        Uses (task_attachment, attachment_id) for dedup.
        Content_ref uses the controlled attachment ref.
        Returns existing artifact if already registered.
        Returns None on failure (best-effort, does not raise).
        """
        existing = await self._registry.get_by_source(
            ArtifactSource.TASK_ATTACHMENT, attachment.attachment_id
        )
        if existing is not None:
            return existing

        content_ref = f"attachment:{attachment.task_id}/{attachment.stored_name}"
        try:
            data = await self._content_store.read(
                content_ref, max_bytes=self._config.artifact_max_bytes
            )
        except Exception as exc:
            logger.warning(
                "register_from_attachment skipped: "
                "source_kind=%s source_ref=%s error=%s",
                ArtifactSource.TASK_ATTACHMENT.value,
                attachment.attachment_id,
                type(exc).__name__,
            )
            return None

        resolved_mime = _resolve_mime(attachment.filename, attachment.content_type)
        kind = _kind_from_mime(resolved_mime)
        size = len(data)
        checksum = _sha256_checksum(data)
        artifact_id = _generate_artifact_id()
        session_id = await self._resolve_task_session(attachment.task_id)

        artifact = Artifact(
            id=artifact_id,
            name=attachment.filename,
            kind=kind,
            mime=resolved_mime,
            content_ref=content_ref,
            inline_content=None,
            size=size,
            checksum=checksum,
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref=attachment.attachment_id,
            source_context_ref=attachment.task_id,
            source_session_id=session_id,
            summary="",
            created_by=attachment.uploaded_by or _ACTOR,
        )

        try:
            return await self._registry.create_artifact(artifact)
        except Exception as exc:
            logger.warning(
                "register_from_attachment failed: "
                "source_kind=%s source_ref=%s error=%s",
                ArtifactSource.TASK_ATTACHMENT.value,
                attachment.attachment_id,
                type(exc).__name__,
            )
            return None

    async def register_from_task_artifact(
        self,
        task_artifact: TaskArtifact,
        task_id: str,
        run_id: int,
        ordinal: int,
    ) -> Artifact | None:
        """Idempotent register from a TaskArtifact.

        Only accepts TaskArtifact. storage_ref must be a workspace: ref.
        Invalid/missing/unreadable -> warning + skip (return None).
        Does NOT affect Task finish.
        """
        existing = await self._registry.get_by_source(
            ArtifactSource.TASK_ARTIFACT,
            Artifact.task_artifact_source_ref(task_id, run_id, ordinal),
        )
        if existing is not None:
            return existing

        # Validate storage_ref is a workspace ref.
        if not isinstance(task_artifact.storage_ref, str) or not task_artifact.storage_ref.startswith("workspace:"):
            logger.warning(
                "register_from_task_artifact skipped: "
                "source_kind=%s source_ref=%s error=%s",
                ArtifactSource.TASK_ARTIFACT.value,
                Artifact.task_artifact_source_ref(task_id, run_id, ordinal),
                "InvalidStorageRef",
            )
            return None

        try:
            data = await self._content_store.read(
                task_artifact.storage_ref,
                max_bytes=self._config.artifact_max_bytes,
            )
        except Exception as exc:
            logger.warning(
                "register_from_task_artifact skipped: "
                "source_kind=%s source_ref=%s error=%s",
                ArtifactSource.TASK_ARTIFACT.value,
                Artifact.task_artifact_source_ref(task_id, run_id, ordinal),
                type(exc).__name__,
            )
            return None

        resolved_mime = _resolve_mime(task_artifact.name, task_artifact.mime)
        kind = _kind_from_mime(resolved_mime)
        size = len(data)
        checksum = _sha256_checksum(data)
        artifact_id = _generate_artifact_id()
        session_id = await self._resolve_task_session(task_id)

        artifact = Artifact(
            id=artifact_id,
            name=task_artifact.name,
            kind=kind,
            mime=resolved_mime,
            content_ref=task_artifact.storage_ref,
            inline_content=None,
            size=size,
            checksum=checksum,
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref=Artifact.task_artifact_source_ref(task_id, run_id, ordinal),
            source_context_ref=task_id,
            source_session_id=session_id,
            summary=task_artifact.summary,
            created_by=_ACTOR,
        )

        try:
            return await self._registry.create_artifact(artifact)
        except Exception as exc:
            logger.warning(
                "register_from_task_artifact failed: "
                "source_kind=%s source_ref=%s error=%s",
                ArtifactSource.TASK_ARTIFACT.value,
                Artifact.task_artifact_source_ref(task_id, run_id, ordinal),
                type(exc).__name__,
            )
            return None

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    async def backfill_attachments(
        self, *, batch_size: int = 100
    ) -> dict[str, int]:
        """Batch-register all existing TaskAttachments as Artifacts.

        Idempotent: repeatable, does not create duplicates.
        Not gated on "artifacts table empty".
        Returns {processed, created, skipped, failed}.
        """
        processed = 0
        created = 0
        skipped = 0
        failed = 0
        after_id: str | None = None

        while True:
            batch = await self._registry.list_attachment_sources(
                after_attachment_id=after_id, limit=batch_size
            )
            if not batch:
                break
            for src in batch:
                processed += 1
                try:
                    existing = await self._registry.get_by_source(
                        ArtifactSource.TASK_ATTACHMENT, src.attachment_id
                    )
                    if existing is not None:
                        skipped += 1
                        continue
                    result = await self.register_from_attachment(src)
                    if result is not None:
                        created += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            after_id = batch[-1].attachment_id
            if len(batch) < batch_size:
                break

        return {
            "processed": processed,
            "created": created,
            "skipped": skipped,
            "failed": failed,
        }

    async def backfill_session_ids(self, *, batch_size: int = 200) -> dict[str, int]:
        """Backfill ``source_session_id`` for existing task-source artifacts.

        Existing task artifacts were registered before the session association
        existed and have ``source_context_ref = task_id`` but NULL
        ``source_session_id``. This resolves each task_id to its execution
        session id (via the injected task_session_resolver, with a
        deterministic uuid5 fallback for deleted tasks) and persists it.

        Idempotent: only touches rows with NULL source_session_id. No-op when
        no resolver is wired. Returns {processed, updated, skipped, failed}.
        """
        processed = 0
        updated = 0
        skipped = 0
        failed = 0

        if self._task_session_resolver is None:
            return {"processed": 0, "updated": 0, "skipped": 0, "failed": 0}

        while True:
            batch = await self._registry.list_task_artifacts_missing_session(
                limit=batch_size,
            )
            if not batch:
                break
            for art in batch:
                processed += 1
                task_id = art.source_context_ref or ""
                if not task_id:
                    skipped += 1
                    continue
                try:
                    session_id = await self._resolve_task_session(task_id)
                    if not session_id:
                        skipped += 1
                        continue
                    updated_art = replace(art, source_session_id=session_id)
                    await self._registry.update_artifact(updated_art)
                    updated += 1
                except Exception:
                    failed += 1
            if len(batch) < batch_size:
                break

        return {
            "processed": processed,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
        }

    async def backfill_kinds(self, *, batch_size: int = 100) -> dict[str, int]:
        """Re-infer kind/mime for artifacts registered with empty mime.

        Existing task artifacts submitted without a content_type have empty
        mime and were classified as OTHER, so .md/.txt/.csv/etc. could not
        render in the workbench (frontend BINARY_KINDS includes 'other').
        Re-derives kind/mime from the filename extension and persists via
        ``update_artifact``. Idempotent: artifacts with non-empty mime or an
        unknown extension are skipped (no false reclassification).
        """
        processed = 0
        updated = 0
        skipped = 0
        failed = 0
        while True:
            batch = await self._registry.list_artifacts_with_empty_mime(
                limit=batch_size,
            )
            if not batch:
                break
            for art in batch:
                processed += 1
                resolved_mime = _resolve_mime(art.name, art.mime)
                if not resolved_mime:
                    # Unknown extension: cannot improve, leave as-is.
                    skipped += 1
                    continue
                try:
                    updated_art = replace(
                        art,
                        mime=resolved_mime,
                        kind=_kind_from_mime(resolved_mime),
                    )
                    await self._registry.update_artifact(updated_art)
                    updated += 1
                except Exception:
                    failed += 1
            if len(batch) < batch_size:
                break
        return {
            "processed": processed,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Share URL
    # ------------------------------------------------------------------

    def _compute_share_url(self, publish_id: str) -> str:
        """Compute share_url from config origin (never from client Host)."""
        origin = self._config.published_base_url
        if origin:
            return f"{origin}/p/{publish_id}"
        return f"/p/{publish_id}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _evaluate_policy(
        self,
        artifact: Artifact,
        action: ArtifactPolicyAction,
        *,
        content_available: bool,
        active_publish_checksum: str | None,
    ) -> ArtifactPolicyOutcome:
        """Evaluate ArtifactPolicy and record audit. Returns outcome."""
        request = ArtifactPolicyRequest(
            artifact=artifact,
            action=action,
            content_available=content_available,
            active_publish_checksum=active_publish_checksum,
            publish_max_bytes=self._config.artifact_publish_max_bytes,
        )
        outcome = self._policy.evaluate(request)
        await self._audit_policy(artifact, action, outcome)
        if outcome.decision is PolicyOutcome.DENY:
            if action is ArtifactPolicyAction.PUBLISH:
                raise PublishBlockedError(f"publish denied: {outcome.reason}")
            raise ArtifactValidationError(f"{action.value} denied: {outcome.reason}")
        return outcome

    async def _audit_policy(
        self,
        artifact: Artifact,
        action: ArtifactPolicyAction,
        outcome: ArtifactPolicyOutcome,
    ) -> None:
        """Record policy audit event (best-effort, does not block)."""
        event = PolicyAuditEvent(
            policy="artifact-policy",
            version="system-v1",
            decision_kind=PolicyDecisionKind.ADMISSION,
            reason=f"action={action.value} artifact_id={artifact.id} "
                   f"outcome={outcome.decision.value} reason={outcome.reason}",
            run_id="",
            session_id="",
            outcome=outcome.decision,
        )
        try:
            await self._audit.record(event)
        except Exception:
            logger.warning("policy audit failed for artifact %s", artifact.id)

    async def _read_text_content(self, artifact: Artifact) -> str:
        """Read text content from inline or content_store."""
        if artifact.inline_content is not None:
            return artifact.inline_content
        if artifact.content_ref is None:
            raise ArtifactContentUnavailableError(
                f"artifact has no content: {artifact.id}"
            )
        data = await self._content_store.read(
            artifact.content_ref, max_bytes=self._config.artifact_max_bytes
        )
        return data.decode("utf-8")

    def _resolve_classification(self, artifact: Artifact) -> Classification:
        """Resolve artifact classification to InformationFlow Classification."""
        if artifact.classification is None:
            return Classification.PUBLIC
        try:
            return Classification(artifact.classification)
        except ValueError:
            return Classification.INTERNAL

    async def _best_effort_delete(self, content_ref: str) -> None:
        """Best-effort delete owned content. Logs on failure, does not raise."""
        try:
            await self._content_store.delete_owned(content_ref)
        except Exception as exc:
            logger.warning(
                "best-effort delete failed for owned content (error=%s)",
                type(exc).__name__,
            )

    async def _best_effort_delete_publish_snapshot(
        self, publish_id: str,
    ) -> None:
        """Best-effort delete a publish snapshot dir. Logs on failure."""
        try:
            await self._content_store.delete_publish_snapshot(publish_id)
        except Exception as exc:
            logger.warning(
                "best-effort delete failed for publish snapshot "
                "(publish_id=%s, error=%s)",
                publish_id, type(exc).__name__,
            )
