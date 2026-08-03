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
from collections.abc import Callable
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
    ) -> None:
        self._registry = registry
        self._content_store = content_store
        self._policy = policy
        self._flow = information_flow_service
        self._audit = policy_audit_service
        self._config = config
        self._convert_to_html = convert_to_html

    # ------------------------------------------------------------------
    # List / Get
    # ------------------------------------------------------------------

    async def list_artifacts(
        self,
        *,
        source_kind: ArtifactSource | None = None,
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

        return result

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_artifact(self, artifact_id: str) -> bool:
        """Delete artifact metadata first, then best-effort delete owned content.

        Does NOT delete source attachment/workspace files or publish snapshots.
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

        # Delete metadata first.
        await self._registry.delete_artifact(artifact_id)

        # Best-effort delete owned content.
        if _is_owned_ref(art.content_ref):
            await self._best_effort_delete(art.content_ref)

        return True

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
            session_id = art.source_context_ref or ""
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

        kind = _kind_from_mime(attachment.content_type)
        size = len(data)
        checksum = _sha256_checksum(data)
        artifact_id = _generate_artifact_id()

        artifact = Artifact(
            id=artifact_id,
            name=attachment.filename,
            kind=kind,
            mime=attachment.content_type,
            content_ref=content_ref,
            inline_content=None,
            size=size,
            checksum=checksum,
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref=attachment.attachment_id,
            source_context_ref=attachment.task_id,
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

        kind = _kind_from_mime(task_artifact.mime)
        size = len(data)
        checksum = _sha256_checksum(data)
        artifact_id = _generate_artifact_id()

        artifact = Artifact(
            id=artifact_id,
            name=task_artifact.name,
            kind=kind,
            mime=task_artifact.mime,
            content_ref=task_artifact.storage_ref,
            inline_content=None,
            size=size,
            checksum=checksum,
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref=Artifact.task_artifact_source_ref(task_id, run_id, ordinal),
            source_context_ref=task_id,
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
