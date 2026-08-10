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
from difflib import unified_diff

from app.application.information_flow_service import InformationFlowService, ReleaseResult
from app.application.policy_audit_service import PolicyAuditService
from app.domain.artifact import (
    Artifact,
    ArtifactAttachmentSource,
    ArtifactContentUnavailableError,
    ArtifactDeleteGraph,
    ArtifactDiffTooLargeError,
    ArtifactDiffUnsupportedError,
    ArtifactError,
    ArtifactExportError,
    ArtifactExportTooLargeError,
    ArtifactExportUnsupportedError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactListPage,
    ArtifactMigrationIncompleteError,
    ArtifactNotFoundError,
    ArtifactRegistry,
    ArtifactContentStore,
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
from app.domain.artifact_exporter import ArtifactExporter, ContentProfile
from app.application.artifact_content_profile import probe_content_profile
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
    # Diff limits (T5): per-input byte/line caps and total output cap.
    diff_max_bytes: int = 1 * 1024 * 1024
    diff_max_lines: int = 20000
    diff_max_output_chars: int = 200000
    # artifact_read tool: max bytes returned per call (default 64 KiB).
    # Only complete UTF-8 lines are returned; a single line exceeding this
    # limit raises artifact_read_too_large (413) rather than a half line.
    artifact_read_max_bytes: int = 64 * 1024


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


@dataclass(frozen=True)
class UpdateRevisionResult:
    """Result of an update_revision / rollback call.

    Attributes:
        diff_summary: short human-readable summary of the content change.
        content_unchanged: True when the new revision checksum equals the
            parent revision checksum (no effective content change).
        publish_sync_state: derived publish synchronization state --
            ``unpublished`` (no active publish), ``current`` (active publish
            points at this revision), or ``outdated`` (active publish points
            at a different revision).
    """

    diff_summary: str
    content_unchanged: bool
    publish_sync_state: str


@dataclass(frozen=True)
class DiffResult:
    """Result of a diff_revisions call.

    Attributes:
        diff_text: unified diff text for text-kind revision pairs (empty
            for binary pairs or when there is no textual diff).
        binary_changed: True when both revisions are binary kind and their
            content (checksum/size/mime) differs.
    """

    diff_text: str
    binary_changed: bool


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


def _generate_revision_id() -> str:
    """Generate a high-entropy revision id (>=128-bit, URL-safe, no padding)."""
    return secrets.token_urlsafe(16)


def _is_text_kind(kind: ArtifactKind) -> bool:
    return kind in _TEXT_KINDS


def _ensure_nonempty_mime(mime: str, kind: ArtifactKind) -> str:
    """Return ``mime`` when non-empty, else a sensible default for ``kind``.

    ArtifactRevision requires a non-empty mime; sources that carry no
    content_type (unknown extension) would otherwise produce an invalid
    revision.  Text kinds default to ``text/plain``; binary kinds default
    to ``application/octet-stream``.
    """
    if mime:
        return mime
    if _is_text_kind(kind):
        return "text/plain"
    return "application/octet-stream"


def _is_owned_ref(content_ref: str | None) -> bool:
    """Check if a content_ref points to Artifact-owned storage (item: scheme)."""
    return content_ref is not None and content_ref.startswith("item:")


def _is_source_ref(content_ref: str | None) -> bool:
    """Check if a content_ref points to a read-only source (attachment/workspace)."""
    if content_ref is None:
        return False
    return content_ref.startswith("attachment:") or content_ref.startswith("workspace:")


# ---------------------------------------------------------------------------
# Text-patch helpers (used by update_revision text_patch mode)
# ---------------------------------------------------------------------------

_PATCH_MODES = frozenset({"first", "all"})


def _validate_text_patch(patch: list[dict[str, object]]) -> None:
    """Validate a text_patch structure.

    Each item must be a dict with exactly {search, replace, mode}; search
    must be a non-empty string, replace a string, mode one of first/all.
    The list must have 1..100 items.  Raises ArtifactRevisionValidationError
    on any violation (no partial application -- validation runs before any
    content mutation).
    """
    if not isinstance(patch, list):
        raise ArtifactRevisionValidationError("text_patch must be a list")
    if len(patch) < 1 or len(patch) > 100:
        raise ArtifactRevisionValidationError(
            f"text_patch must have 1..100 items, got {len(patch)}"
        )
    for i, item in enumerate(patch):
        if not isinstance(item, dict):
            raise ArtifactRevisionValidationError(
                f"text_patch[{i}] must be a dict"
            )
        keys = set(item.keys())
        if keys != {"search", "replace", "mode"}:
            raise ArtifactRevisionValidationError(
                f"text_patch[{i}] must have exactly "
                f"search/replace/mode, got {sorted(keys)}"
            )
        search = item["search"]
        replace = item["replace"]
        mode = item["mode"]
        if not isinstance(search, str) or search == "":
            raise ArtifactRevisionValidationError(
                f"text_patch[{i}].search must be a non-empty string"
            )
        if not isinstance(replace, str):
            raise ArtifactRevisionValidationError(
                f"text_patch[{i}].replace must be a string"
            )
        if mode not in _PATCH_MODES:
            raise ArtifactRevisionValidationError(
                f"text_patch[{i}].mode must be 'first' or 'all', got {mode!r}"
            )


def _apply_text_patch(text: str, patch: list[dict[str, object]]) -> str:
    """Apply a validated text_patch to ``text`` in order.

    ``first`` replaces the first occurrence; ``all`` replaces every
    occurrence.  Any unmatched search raises ArtifactRevisionValidationError
    (the caller has not persisted anything yet, so this is atomic).
    """
    for item in patch:
        search: str = item["search"]
        replace: str = item["replace"]
        mode: str = item["mode"]
        if mode == "first":
            idx = text.find(search)
            if idx == -1:
                raise ArtifactRevisionValidationError(
                    f"patch search not found (first): {search!r}"
                )
            text = text[:idx] + replace + text[idx + len(search):]
        else:  # mode == "all"
            if search not in text:
                raise ArtifactRevisionValidationError(
                    f"patch search not found (all): {search!r}"
                )
            text = text.replace(search, replace)
    return text


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
        exporter: ArtifactExporter | None = None,
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
        self._exporter = exporter
        # Migration state cache (updated by migrate_revisions, read by
        # migration_status for health_snapshot).
        self._migration_state: str = "ok"
        self._migration_failed_count: int = 0
        self._migration_last_error: str | None = None

    @property
    def config(self) -> ArtifactServiceConfig:
        """Read-only access to the immutable config snapshot."""
        return self._config

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

        Revision-aware: when the artifact has a current_revision_id (migrated),
        reads content from the current Revision (not legacy columns).  When
        unmigrated (current_revision_id is None), falls back to legacy
        inline_content / content_ref (spec 157 allows legacy content read).

        Raises ArtifactNotFoundError if artifact missing.
        Raises ArtifactContentUnavailableError if content unreadable or the
        current revision row is missing despite current_revision_id being set.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        # Migrated: read from the current Revision.
        if art.current_revision_id is not None:
            rev = await self._registry.get_revision(
                artifact_id, art.current_revision_id
            )
            if rev is None:
                raise ArtifactContentUnavailableError(
                    f"current revision not found: {art.current_revision_id}"
                )
            if rev.inline_content is not None:
                return rev.inline_content.encode("utf-8"), art
            if rev.content_ref is None:
                raise ArtifactContentUnavailableError(
                    f"revision has no content: {rev.id}"
                )
            data = await self._content_store.read(
                rev.content_ref, max_bytes=self._config.artifact_max_bytes
            )
            return data, art

        # Unmigrated: read legacy content fields (spec 157).
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
    # Revision read
    # ------------------------------------------------------------------

    async def get_current_revision(self, artifact_id: str) -> ArtifactRevision:
        """Return the current revision of an artifact.

        Raises ArtifactNotFoundError when the artifact does not exist,
        ArtifactMigrationIncompleteError when the artifact has no current
        revision (legacy unmigrated artifact), and ArtifactRevisionNotFoundError
        when the revision row is missing despite current_revision_id being set.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        rev = await self._registry.get_revision(
            artifact_id, art.current_revision_id
        )
        if rev is None:
            raise ArtifactRevisionNotFoundError(
                f"current revision not found: {art.current_revision_id}"
            )
        return rev

    async def get_revision(
        self, artifact_id: str, revision_id: str,
    ) -> ArtifactRevision:
        """Return a specific revision of an artifact.

        Unmigrated artifacts (existing but without a current revision) raise
        ArtifactMigrationIncompleteError so callers know revision features are
        pending migration rather than missing. Cross-artifact or non-existent
        revision ids raise ArtifactRevisionNotFoundError (the registry filters
        by artifact_id, so a foreign revision yields None without leaking which
        artifact owns it).
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is not None and art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        rev = await self._registry.get_revision(artifact_id, revision_id)
        if rev is None:
            raise ArtifactRevisionNotFoundError(
                f"revision not found: {revision_id}"
            )
        return rev

    async def get_revision_content(
        self, artifact_id: str, revision_id: str | None = None,
    ) -> tuple[bytes, ArtifactRevision]:
        """Read the content bytes of a revision (current or specified).

        revision_id=None -> read the current revision's content (mirrors
        ``get_content``'s migrated path; for unmigrated artifacts, reads
        legacy content per spec line 157 and returns a synthetic revision
        view built from the artifact's legacy fields).
        revision_id specified -> ``get_artifact`` (None ->
        ArtifactNotFoundError); migration guard (unmigrated ->
        ArtifactMigrationIncompleteError); ``get_revision`` (None ->
        ArtifactRevisionNotFoundError); read content (inline or
        ``content_store.read``); verify checksum via
        ``_verify_revision_checksum``; return ``(bytes, revision)``.

        Cross-artifact revision_id is already rejected by ``get_revision``
        (returns None -> ArtifactRevisionNotFoundError) without leaking which
        artifact owns it.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        if revision_id is None:
            # Current revision path.
            if art.current_revision_id is None:
                # Unmigrated: read legacy content (spec 157) and synthesize a
                # revision view from the artifact's legacy fields.  This lets
                # artifact_read work on legacy artifacts without forcing a
                # migration first.
                if art.inline_content is not None:
                    data = art.inline_content.encode("utf-8")
                elif art.content_ref is not None:
                    data = await self._content_store.read(
                        art.content_ref,
                        max_bytes=self._config.artifact_max_bytes,
                    )
                else:
                    raise ArtifactContentUnavailableError(
                        f"artifact has no content: {artifact_id}"
                    )
                legacy_rev = ArtifactRevision(
                    id="legacy",
                    artifact_id=artifact_id,
                    revision_number=1,
                    parent_revision_id=None,
                    rollback_from_revision_id=None,
                    content_ref=art.content_ref,
                    inline_content=art.inline_content,
                    size=art.size,
                    checksum=art.checksum,
                    mime=art.mime,
                    kind=art.kind,
                    created_at=art.created_at or datetime.now(timezone.utc),
                    change_summary="",
                    created_by=art.created_by,
                    source_session_id=art.source_session_id,
                    source_run_id=None,
                )
                return data, legacy_rev

            rev = await self._registry.get_revision(
                artifact_id, art.current_revision_id
            )
            if rev is None:
                raise ArtifactRevisionNotFoundError(
                    f"current revision not found: {art.current_revision_id}"
                )
        else:
            if art.current_revision_id is None:
                raise ArtifactMigrationIncompleteError(
                    f"artifact has no revision: {artifact_id}"
                )
            rev = await self._registry.get_revision(artifact_id, revision_id)
            if rev is None:
                raise ArtifactRevisionNotFoundError(
                    f"revision not found: {revision_id}"
                )

        # Read content from the resolved revision.
        if rev.inline_content is not None:
            data = rev.inline_content.encode("utf-8")
        elif rev.content_ref is not None:
            data = await self._content_store.read(
                rev.content_ref,
                max_bytes=self._config.artifact_max_bytes,
            )
        else:
            raise ArtifactContentUnavailableError(
                f"revision has no content: {rev.id}"
            )
        self._verify_revision_checksum(rev, data)
        return data, rev

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
        workspace_ref: str | None = None,
        source_kind: ArtifactSource = ArtifactSource.MANUAL,
        source_ref: str | None = None,
        source_context_ref: str | None = None,
        source_session_id: str | None = None,
        source_run_id: str | None = None,
        summary: str = "",
        classification: str | None = None,
        labels: tuple[str, ...] | None = None,
        created_by: str = _ACTOR,
    ) -> Artifact:
        """Create a new artifact.

        Exactly one of inline_content / file_data / workspace_ref must be
        provided.  workspace_ref must use the ``workspace:`` scheme; the
        content store enforces root confinement and per-component symlink
        rejection, and the content is materialized to a Revision-owned
        ``item:`` path (the source file is never modified).
        For manual source, source_ref defaults to the generated artifact_id.
        ``source_run_id`` is server provenance carried onto the initial
        Revision (spec: chat-created SESSION artifacts take it from trusted
        context, never from client arguments); legacy callers leave it None.
        For manual source, source_ref defaults to the generated artifact_id.
        Content is written first, then registry; on registry failure the new
        owned content is compensated (deleted).
        """
        artifact_id = _generate_artifact_id()
        if source_kind in (ArtifactSource.MANUAL, ArtifactSource.SESSION) and source_ref is None:
            source_ref = artifact_id

        new_content_ref: str | None = None
        new_inline: str | None = None
        size: int
        checksum: str

        content_inputs = sum(
            1 for x in (inline_content, file_data, workspace_ref) if x is not None
        )
        if content_inputs != 1:
            raise ArtifactValidationError(
                "create requires exactly one of "
                "inline_content / file_data / workspace_ref"
            )

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
            # workspace_ref -> read bytes from the workspace source, then
            # materialize to a Revision-owned item: path.  The content store
            # enforces root confinement and per-component symlink rejection.
            if not isinstance(workspace_ref, str) or not workspace_ref.startswith("workspace:"):
                raise ArtifactValidationError(
                    "workspace_ref must use the 'workspace:' scheme"
                )
            raw = await self._content_store.read(
                workspace_ref,
                max_bytes=self._config.artifact_max_bytes,
            )
            if len(raw) > self._config.artifact_max_bytes:
                raise ArtifactTooLargeError(
                    f"workspace content {len(raw)} exceeds "
                    f"artifact_max_bytes {self._config.artifact_max_bytes}"
                )
            size = len(raw)
            checksum = _sha256_checksum(raw)
            new_content_ref = await self._content_store.write_atomic(
                artifact_id, filename or name, raw
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

        # Build the initial revision (revision_number=1) sharing the artifact's
        # content fields.  The registry atomically inserts both rows and
        # backfills ``current_revision_id`` in a single transaction.
        initial_revision = ArtifactRevision(
            id=_generate_revision_id(),
            artifact_id=artifact_id,
            revision_number=1,
            parent_revision_id=None,
            rollback_from_revision_id=None,
            content_ref=new_content_ref,
            inline_content=new_inline,
            size=size,
            checksum=checksum,
            mime=mime,
            kind=kind,
            created_at=datetime.now(timezone.utc),
            change_summary="",
            created_by=created_by,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
        )

        try:
            created_artifact, _ = await (
                self._registry.create_artifact_with_initial_revision(
                    artifact, initial_revision,
                )
            )
            return created_artifact
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
    # Revision write (update / list / diff / rollback)
    # ------------------------------------------------------------------

    async def update_revision(
        self,
        artifact_id: str,
        *,
        expected_revision_id: str,
        inline_content: str | None = None,
        file_data: bytes | None = None,
        workspace_ref: str | None = None,
        text_patch: list[dict[str, object]] | None = None,
        change_summary: str = "",
        kind: ArtifactKind | None = None,
        mime: str | None = None,
    ) -> tuple[ArtifactRevision, UpdateRevisionResult]:
        """Create a new revision from a content update or text patch.

        Exactly one content input (inline_content / file_data / workspace_ref
        / text_patch) must be provided.  Uses optimistic compare-and-set via
        ``registry.append_revision`` -- ``expected_revision_id`` must equal
        the artifact's current revision id.  Does NOT revoke the active
        publish (unlike ``update_artifact``); only derives
        ``publish_sync_state``.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        current = await self._registry.get_revision(
            artifact_id, art.current_revision_id
        )
        if current is None:
            raise ArtifactRevisionNotFoundError(
                f"current revision not found: {art.current_revision_id}"
            )
        if expected_revision_id != current.id:
            raise ArtifactRevisionConflictError(
                f"CAS conflict: expected {expected_revision_id}, "
                f"actual {current.id}"
            )

        # EDIT admission (archived -> deny), mirroring update_artifact.
        await self._evaluate_policy(
            art, ArtifactPolicyAction.EDIT, content_available=True,
            active_publish_checksum=None,
        )

        # Exactly one content input.
        content_inputs = sum(
            1 for x in (inline_content, file_data, workspace_ref, text_patch)
            if x is not None
        )
        if content_inputs != 1:
            raise ArtifactRevisionValidationError(
                "update_revision requires exactly one content input"
            )

        new_content_ref: str | None = None
        new_inline: str | None = None
        new_size: int
        new_checksum: str
        new_kind = kind if kind is not None else current.kind
        new_mime = mime if mime is not None else current.mime
        temp_new_ref: str | None = None

        if text_patch is not None:
            # text_patch preserves parent kind/mime (server ignores kind/mime
            # overrides for patch mode).
            new_kind = current.kind
            new_mime = current.mime
            if not _is_text_kind(current.kind):
                raise ArtifactRevisionValidationError(
                    "text_patch requires a text-kind revision"
                )
            _validate_text_patch(text_patch)
            parent_text = await self._read_revision_text(
                current, max_bytes=self._config.diff_max_bytes
            )
            patched = _apply_text_patch(parent_text, text_patch)
            data = patched.encode("utf-8")
            if len(data) > self._config.artifact_inline_max_bytes:
                raise ArtifactRevisionValidationError(
                    f"patched content {len(data)} exceeds "
                    f"inline_max_bytes "
                    f"{self._config.artifact_inline_max_bytes}"
                )
            if len(data) > self._config.artifact_max_bytes:
                raise ArtifactRevisionValidationError(
                    f"patched content {len(data)} exceeds "
                    f"artifact_max_bytes {self._config.artifact_max_bytes}"
                )
            new_inline = patched
            new_content_ref = None
            new_size = len(data)
            new_checksum = _sha256_checksum(data)
        elif inline_content is not None:
            data = inline_content.encode("utf-8")
            if _is_text_kind(new_kind) and len(data) > (
                self._config.artifact_inline_max_bytes
            ):
                raise ArtifactTooLargeError(
                    f"inline content {len(data)} exceeds "
                    f"inline_max_bytes "
                    f"{self._config.artifact_inline_max_bytes}"
                )
            new_inline = inline_content
            new_content_ref = None
            new_size = len(data)
            new_checksum = _sha256_checksum(data)
        else:
            # file_data or workspace_ref -> materialize to a new item: ref.
            if file_data is not None:
                raw = file_data
            else:
                if not isinstance(workspace_ref, str) or not workspace_ref.startswith("workspace:"):
                    raise ArtifactRevisionValidationError(
                        "workspace_ref must use the 'workspace:' scheme"
                    )
                raw = await self._content_store.read(
                    workspace_ref,
                    max_bytes=self._config.artifact_max_bytes,
                )
            if len(raw) > self._config.artifact_max_bytes:
                raise ArtifactTooLargeError(
                    f"file content {len(raw)} exceeds "
                    f"artifact_max_bytes {self._config.artifact_max_bytes}"
                )
            temp_new_ref = await self._content_store.write_atomic(
                artifact_id,
                f"rev-{_generate_revision_id()}",
                raw,
            )
            new_content_ref = temp_new_ref
            new_inline = None
            new_size = len(raw)
            new_checksum = _sha256_checksum(raw)

        # Binary kinds cannot use inline content.
        if not _is_text_kind(new_kind) and new_inline is not None:
            raise ArtifactRevisionValidationError(
                "binary revision must use content_ref"
            )

        new_revision = ArtifactRevision(
            id=_generate_revision_id(),
            artifact_id=artifact_id,
            revision_number=current.revision_number + 1,
            parent_revision_id=current.id,
            rollback_from_revision_id=None,
            content_ref=new_content_ref,
            inline_content=new_inline,
            size=new_size,
            checksum=new_checksum,
            mime=new_mime,
            kind=new_kind,
            created_at=datetime.now(timezone.utc),
            change_summary=change_summary,
            created_by=_ACTOR,
            source_session_id=current.source_session_id,
            source_run_id=current.source_run_id,
        )

        try:
            committed = await self._registry.append_revision(
                artifact_id, new_revision,
                expected_revision_id=expected_revision_id,
            )
        except Exception:
            if temp_new_ref is not None:
                await self._best_effort_delete(temp_new_ref)
            raise

        content_unchanged = committed.checksum == current.checksum
        publish_state = await self._derive_publish_sync_state(
            artifact_id, committed.id
        )
        diff_summary = (
            f"size {current.size}->{committed.size} bytes; "
            f"checksum {'unchanged' if content_unchanged else 'changed'}"
        )
        return committed, UpdateRevisionResult(
            diff_summary=diff_summary,
            content_unchanged=content_unchanged,
            publish_sync_state=publish_state,
        )

    async def list_revisions(
        self,
        artifact_id: str,
        *,
        cursor: RevisionListCursor | None = None,
        limit: int = 50,
    ) -> RevisionListPage:
        """List revisions for an artifact (newest first).

        Limit is clamped to 1..100.  Returns an empty page when the artifact
        has no revisions (current_revision_id is None -- legacy unmigrated).
        Raises ArtifactNotFoundError when the artifact does not exist.
        Cross-artifact cursors raise ArtifactRevisionValidationError
        (enforced by the registry).
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            return RevisionListPage(items=(), next_cursor=None)
        clamped = max(1, min(100, limit))
        return await self._registry.list_revisions(
            artifact_id, cursor=cursor, limit=clamped,
        )

    async def diff_revisions(
        self,
        artifact_id: str,
        from_id: str,
        to_id: str,
        *,
        context_lines: int = 3,
    ) -> DiffResult:
        """Compute a unified diff between two text revisions, or a binary
        change summary for two binary revisions.

        Raises ArtifactMigrationIncompleteError when the artifact exists but
        has no current revision (legacy unmigrated), ArtifactRevisionNotFoundError
        for missing/cross-artifact revisions, ArtifactRevisionValidationError
        for out-of-range context_lines, ArtifactDiffUnsupportedError for cross
        text/binary pairs or undecodable content, and ArtifactDiffTooLargeError
        when inputs or output exceed configured limits.
        """
        if context_lines < 0 or context_lines > 20:
            raise ArtifactRevisionValidationError(
                f"context_lines must be 0..20, got {context_lines}"
            )
        # Diff is a revision-scoped operation: an unmigrated artifact (existing
        # but without a current revision) must signal migration-incomplete
        # rather than reporting its (non-existent) revisions as not found.
        art = await self._registry.get_artifact(artifact_id)
        if art is not None and art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        from_rev = await self._registry.get_revision(artifact_id, from_id)
        if from_rev is None:
            raise ArtifactRevisionNotFoundError(
                f"revision not found: {from_id}"
            )
        to_rev = await self._registry.get_revision(artifact_id, to_id)
        if to_rev is None:
            raise ArtifactRevisionNotFoundError(
                f"revision not found: {to_id}"
            )

        from_is_text = _is_text_kind(from_rev.kind)
        to_is_text = _is_text_kind(to_rev.kind)
        if from_is_text != to_is_text:
            raise ArtifactDiffUnsupportedError(
                "diff across text/binary kinds is unsupported"
            )
        if not from_is_text:
            # Binary pair: report whether content changed (checksum/size/mime).
            changed = (
                from_rev.checksum != to_rev.checksum
                or from_rev.size != to_rev.size
                or from_rev.mime != to_rev.mime
            )
            return DiffResult(diff_text="", binary_changed=changed)

        # Text pair: read, decode, diff.
        from_bytes = await self._read_revision_bytes_for_diff(from_rev)
        to_bytes = await self._read_revision_bytes_for_diff(to_rev)
        if len(from_bytes) > self._config.diff_max_bytes:
            raise ArtifactDiffTooLargeError(
                f"from revision {len(from_bytes)} exceeds "
                f"diff_max_bytes {self._config.diff_max_bytes}"
            )
        if len(to_bytes) > self._config.diff_max_bytes:
            raise ArtifactDiffTooLargeError(
                f"to revision {len(to_bytes)} exceeds "
                f"diff_max_bytes {self._config.diff_max_bytes}"
            )
        try:
            from_text = from_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactDiffUnsupportedError(
                f"from revision is not valid UTF-8: {exc}"
            ) from exc
        try:
            to_text = to_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactDiffUnsupportedError(
                f"to revision is not valid UTF-8: {exc}"
            ) from exc
        from_lines = from_text.splitlines()
        to_lines = to_text.splitlines()
        if len(from_lines) > self._config.diff_max_lines:
            raise ArtifactDiffTooLargeError(
                f"from revision {len(from_lines)} lines exceeds "
                f"diff_max_lines {self._config.diff_max_lines}"
            )
        if len(to_lines) > self._config.diff_max_lines:
            raise ArtifactDiffTooLargeError(
                f"to revision {len(to_lines)} lines exceeds "
                f"diff_max_lines {self._config.diff_max_lines}"
            )

        diff_lines = list(unified_diff(
            from_lines,
            to_lines,
            fromfile=f"revision-{from_rev.revision_number}",
            tofile=f"revision-{to_rev.revision_number}",
            n=context_lines,
            lineterm="",
        ))
        diff_text = "\n".join(diff_lines)
        if len(diff_text) > self._config.diff_max_output_chars:
            raise ArtifactDiffTooLargeError(
                f"diff output {len(diff_text)} exceeds "
                f"diff_max_output_chars "
                f"{self._config.diff_max_output_chars}"
            )
        return DiffResult(diff_text=diff_text, binary_changed=False)

    async def rollback(
        self,
        artifact_id: str,
        target_revision_id: str,
        *,
        expected_revision_id: str,
        change_summary: str = "",
    ) -> tuple[ArtifactRevision, UpdateRevisionResult]:
        """Roll back to a target revision by creating a new revision whose
        content is copied from the target.

        The new revision has ``parent_revision_id = current.id`` and
        ``rollback_from_revision_id = target.id``.  If the target uses a
        content_ref, its content is materialized to a new owned item: path
        (the target's item file is never overwritten).  CAS via
        ``append_revision``.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        current = await self._registry.get_revision(
            artifact_id, art.current_revision_id
        )
        if current is None:
            raise ArtifactRevisionNotFoundError(
                f"current revision not found: {art.current_revision_id}"
            )
        if expected_revision_id != current.id:
            raise ArtifactRevisionConflictError(
                f"CAS conflict: expected {expected_revision_id}, "
                f"actual {current.id}"
            )

        await self._evaluate_policy(
            art, ArtifactPolicyAction.EDIT, content_available=True,
            active_publish_checksum=None,
        )

        target = await self._registry.get_revision(
            artifact_id, target_revision_id
        )
        if target is None:
            raise ArtifactRevisionNotFoundError(
                f"revision not found: {target_revision_id}"
            )

        new_content_ref: str | None = None
        new_inline: str | None = None
        temp_new_ref: str | None = None

        if target.inline_content is not None:
            new_inline = target.inline_content
            new_content_ref = None
        else:
            # Materialize target content to a new owned item: path.
            data = await self._content_store.read(
                target.content_ref,
                max_bytes=self._config.artifact_max_bytes,
            )
            temp_new_ref = await self._content_store.write_atomic(
                artifact_id,
                f"rb-{_generate_revision_id()}",
                data,
            )
            new_content_ref = temp_new_ref
            new_inline = None

        new_revision = ArtifactRevision(
            id=_generate_revision_id(),
            artifact_id=artifact_id,
            revision_number=current.revision_number + 1,
            parent_revision_id=current.id,
            rollback_from_revision_id=target.id,
            content_ref=new_content_ref,
            inline_content=new_inline,
            size=target.size,
            checksum=target.checksum,
            mime=target.mime,
            kind=target.kind,
            created_at=datetime.now(timezone.utc),
            change_summary=change_summary,
            created_by=_ACTOR,
            source_session_id=current.source_session_id,
            source_run_id=current.source_run_id,
        )

        try:
            committed = await self._registry.append_revision(
                artifact_id, new_revision,
                expected_revision_id=expected_revision_id,
            )
        except Exception:
            if temp_new_ref is not None:
                await self._best_effort_delete(temp_new_ref)
            raise

        content_unchanged = committed.checksum == current.checksum
        publish_state = await self._derive_publish_sync_state(
            artifact_id, committed.id
        )
        diff_summary = (
            f"rollback to revision-{target.revision_number}; "
            f"checksum {'unchanged' if content_unchanged else 'changed'}"
        )
        return committed, UpdateRevisionResult(
            diff_summary=diff_summary,
            content_unchanged=content_unchanged,
            publish_sync_state=publish_state,
        )

    # ------------------------------------------------------------------
    # Revision helpers
    # ------------------------------------------------------------------

    async def _derive_publish_sync_state(
        self, artifact_id: str, current_revision_id: str,
    ) -> str:
        """Derive the publish sync state for the current revision.

        - ``unpublished``: no active publish for the artifact.
        - ``current``: active publish points at this revision.
        - ``outdated``: active publish points at a different revision
          (including legacy publishes with ``published_revision_id is None``).
        """
        active = await self._registry.get_active_publish(artifact_id)
        if active is None:
            return "unpublished"
        if active.published_revision_id == current_revision_id:
            return "current"
        return "outdated"

    async def _read_revision_text(
        self, revision: ArtifactRevision, *, max_bytes: int,
    ) -> str:
        """Read and strictly UTF-8 decode the authoritative content of a
        revision.  Raises ArtifactRevisionValidationError on decode failure
        or ArtifactContentUnavailableError when content is unreadable."""
        if revision.inline_content is not None:
            return revision.inline_content
        if revision.content_ref is None:
            raise ArtifactContentUnavailableError(
                f"revision has no content: {revision.id}"
            )
        data = await self._content_store.read(
            revision.content_ref, max_bytes=max_bytes
        )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactRevisionValidationError(
                f"revision content is not valid UTF-8: {exc}"
            ) from exc

    async def _read_revision_bytes_for_diff(
        self, revision: ArtifactRevision,
    ) -> bytes:
        """Read raw bytes of a revision for diff (uses artifact_max_bytes)."""
        if revision.inline_content is not None:
            return revision.inline_content.encode("utf-8")
        if revision.content_ref is None:
            raise ArtifactContentUnavailableError(
                f"revision has no content: {revision.id}"
            )
        return await self._content_store.read(
            revision.content_ref,
            max_bytes=self._config.artifact_max_bytes,
        )

    def _verify_revision_checksum(
        self, revision: ArtifactRevision, content_bytes: bytes,
    ) -> None:
        """Defense-in-depth: recompute the checksum of the read bytes and
        compare to the revision's stored checksum. A mismatch signals storage
        corruption or tampering -- spec requires export to receive bytes whose
        checksum has been verified, not the revision's declared value trusted
        blindly. Raises ArtifactContentUnavailableError on mismatch.
        """
        actual = _sha256_checksum(content_bytes)
        if actual != revision.checksum:
            raise ArtifactContentUnavailableError(
                f"revision content checksum mismatch for {revision.id}: "
                f"storage corruption detected"
            )

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
        self, artifact_id: str, *, format: str = "original",
        revision_id: str | None = None,
    ) -> tuple[bytes, str, str]:
        """Export artifact content.

        When an exporter is injected, delegates fully to the exporter
        (reads from the resolved revision, uses probe_content_profile for
        format routing).  When no exporter is injected, falls back to
        legacy behaviour (original/html only, reads from the Artifact).

        Unmigrated artifacts (current_revision_id is None, no revision_id
        specified) allow legacy original/html export (spec 157); other
        formats (docx/pptx/xlsx) raise ArtifactMigrationIncompleteError
        because they are Revision-scoped.

        Export is read-only: never modifies Artifact/Revision/publish/files.
        """
        fmt = format.lower()
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        # Unmigrated artifact without a specific revision_id: allow legacy
        # original/html export (spec 157); Revision-scoped formats raise
        # MigrationIncomplete.
        if art.current_revision_id is None and revision_id is None:
            if fmt in ("original", "html"):
                return await self._export_legacy(artifact_id, fmt)
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )

        if self._exporter is None:
            return await self._export_legacy(artifact_id, fmt)

        # --- Full delegation path ---
        _, rev = await self._resolve_revision_for_export(
            artifact_id, revision_id, art=art
        )
        content_bytes = await self._read_revision_bytes_for_diff(rev)
        self._verify_revision_checksum(rev, content_bytes)
        content_profile = probe_content_profile(rev.kind, rev.mime, content_bytes)

        caps = await self._exporter.capabilities(
            rev.kind, rev.mime, content_profile
        )
        if fmt not in caps:
            raise ArtifactExportUnsupportedError(
                f"unsupported export format: {fmt}"
            )

        options = {
            "content_profile": content_profile,
            "artifact_name": art.name,
        }
        try:
            exported = await self._exporter.export(
                rev, content_bytes, fmt, options
            )
        except ArtifactExportError:
            raise
        except Exception as exc:
            raise ArtifactExportError(
                f"export failed for format {fmt}: {exc}"
            ) from exc

        return exported.data, exported.mime, exported.filename

    async def export_capabilities(
        self, artifact_id: str, *, revision_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return the tuple of supported export format names.

        When an exporter is injected, returns its capabilities for the
        resolved revision's content profile (lowercase, deduplicated, sorted).
        When no exporter is injected, returns legacy caps: ``("original",)``
        for any readable artifact, ``("html", "original")`` for
        markdown/document kinds.

        Unmigrated artifacts (current_revision_id is None, no revision_id
        specified) return legacy caps regardless of exporter injection --
        capability query is read-only and must not raise MigrationIncomplete
        for unmigrated artifacts.

        Read-only: never modifies Artifact/Revision/publish/files.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        # Unmigrated artifact without a specific revision_id: return legacy
        # caps.  Capability query is read-only and must not raise
        # MigrationIncomplete for unmigrated artifacts.
        if art.current_revision_id is None and revision_id is None:
            if art.kind in (ArtifactKind.MARKDOWN, ArtifactKind.DOCUMENT):
                return ("html", "original")
            return ("original",)

        if self._exporter is None:
            if art.kind in (ArtifactKind.MARKDOWN, ArtifactKind.DOCUMENT):
                return ("html", "original")
            return ("original",)

        # --- Full delegation path ---
        _, rev = await self._resolve_revision_for_export(
            artifact_id, revision_id, art=art
        )
        content_bytes = await self._read_revision_bytes_for_diff(rev)
        self._verify_revision_checksum(rev, content_bytes)
        content_profile = probe_content_profile(rev.kind, rev.mime, content_bytes)
        caps = await self._exporter.capabilities(
            rev.kind, rev.mime, content_profile
        )
        return tuple(sorted({c.lower() for c in caps}))

    async def _resolve_revision_for_export(
        self, artifact_id: str, revision_id: str | None,
        *, art: Artifact | None = None,
    ) -> tuple[Artifact, ArtifactRevision]:
        """Resolve the artifact and revision for export/capabilities.

        revision_id=None -> current revision.  Raises ArtifactNotFoundError,
        ArtifactMigrationIncompleteError, or ArtifactRevisionNotFoundError.

        When ``art`` is supplied (pre-fetched by the caller), skips the
        redundant registry lookup.
        """
        if art is None:
            art = await self._registry.get_artifact(artifact_id)
            if art is None:
                raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None and revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        target_id = revision_id or art.current_revision_id
        if target_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        rev = await self._registry.get_revision(artifact_id, target_id)
        if rev is None:
            raise ArtifactRevisionNotFoundError(
                f"revision not found: {target_id}"
            )
        return art, rev

    async def _export_legacy(
        self, artifact_id: str, fmt: str,
    ) -> tuple[bytes, str, str]:
        """Legacy export path (no exporter injected).

        Supports only 'original' and 'html' formats, reads from the
        Artifact's content fields (not Revision).  Preserves backward
        compatibility with pre-T6 tests.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")

        if fmt == "original":
            data, _ = await self.get_content(artifact_id)
            return data, art.mime, art.name

        if fmt == "html":
            if art.kind not in (ArtifactKind.MARKDOWN, ArtifactKind.DOCUMENT):
                raise ArtifactValidationError(
                    f"html export only supports markdown/document, got {art.kind}"
                )
            data, _ = await self.get_content(artifact_id)
            content_str = data.decode("utf-8")
            html = self._convert_to_html(content_str)
            return html.encode("utf-8"), "text/html", art.name

        raise ArtifactValidationError(f"unsupported export format: {fmt}")

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(
        self, artifact_id: str
    ) -> PublishResult:
        """Publish an artifact to a public snapshot (body-less HTTP compat).

        Resolves the current revision and delegates to
        :meth:`publish_revision`.  Raises ArtifactMigrationIncompleteError
        when the artifact has no current revision.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        return await self.publish_revision(
            artifact_id,
            revision_id=art.current_revision_id,
            expected_current_revision_id=art.current_revision_id,
        )

    async def publish_revision(
        self,
        artifact_id: str,
        *,
        revision_id: str,
        expected_current_revision_id: str,
    ) -> PublishResult:
        """Publish a specific revision to a public snapshot.

        Flow:
        1. Read artifact; None -> ArtifactNotFoundError; no revision ->
           MigrationIncomplete.
        2. Verify revision_id AND expected_current_revision_id both equal the
           in-transaction current revision (spec: 任一不是事务内当前值都返回
           artifact_revision_conflict). Publishing a historical revision is
           rejected as a conflict.
        3. Read the (current) revision; None -> RevisionNotFoundError.
        4. PUBLISH admission (policy + audit).
        5. Read revision content: text -> InformationFlow release; binary ->
           raw bytes (policy already gated PUBLIC).
        6. Early reuse: if active publish has same final public checksum,
           return it without creating a new snapshot.
        7. Generate publish_id, write staging snapshot.
        8. register_revision_publish (single-tx CAS re-verify + reuse/switch).
        9. DB failure -> compensate delete new snapshot.
        10. Return PublishResult(published, share_url, reused).
        """
        # 1. Read artifact
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        current_id = art.current_revision_id

        # 2. Both revision_id and expected must equal the current revision.
        # This fast-fails before the expensive release/snapshot work; the
        # authoritative CAS re-verification happens inside
        # register_revision_publish's BEGIN IMMEDIATE transaction.
        if revision_id != current_id or expected_current_revision_id != current_id:
            raise ArtifactRevisionConflictError(
                f"publish requires revision_id and expected_current_revision_id "
                f"to equal the current revision {current_id}"
            )

        # 3. Read the (current) revision
        rev = await self._registry.get_revision(artifact_id, revision_id)
        if rev is None:
            raise ArtifactRevisionNotFoundError(
                f"current revision not found: {revision_id}"
            )

        # 4. PUBLISH admission
        active = await self._registry.get_active_publish(artifact_id)
        active_checksum = active.snapshot_checksum if active is not None else None
        content_available = (
            rev.inline_content is not None or bool(rev.content_ref)
        )
        await self._evaluate_policy(
            art,
            ArtifactPolicyAction.PUBLISH,
            content_available=content_available,
            active_publish_checksum=active_checksum,
        )

        # 5. Read revision content + release/copy
        is_text = _is_text_kind(rev.kind)
        snapshot_ref: str | None = None
        snapshot_inline: str | None = None
        snapshot_content: str | None = None
        snapshot_size: int
        snapshot_checksum: str

        if is_text:
            content_str = await self._read_revision_text(
                rev, max_bytes=self._config.artifact_max_bytes
            )
            classification = self._resolve_classification(art)
            labels = frozenset(art.labels) if art.labels else frozenset()
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
            if snapshot_size <= self._config.artifact_inline_max_bytes:
                snapshot_inline = snapshot_content
                snapshot_ref = None
            else:
                snapshot_inline = None
        else:
            if rev.content_ref is None:
                raise ArtifactContentUnavailableError(
                    f"binary revision has no content_ref: {revision_id}"
                )
            data = await self._content_store.read(
                rev.content_ref, max_bytes=self._config.artifact_max_bytes
            )
            snapshot_size = len(data)
            snapshot_checksum = _sha256_checksum(data)
            snapshot_inline = None

        # 6. Early reuse: same final public checksum -> return existing
        if (
            active is not None
            and active.snapshot_checksum == snapshot_checksum
        ):
            return PublishResult(
                published=active,
                share_url=self._compute_share_url(active.publish_id),
                reused=True,
            )

        # 7. Generate publish_id, write staging snapshot
        publish_id = _generate_publish_id()
        if snapshot_inline is None:
            if is_text:
                snapshot_ref = await self._content_store.copy_to_publish_snapshot(
                    "", publish_id, inline=snapshot_content
                )
            else:
                snapshot_ref = await self._content_store.copy_to_publish_snapshot(
                    rev.content_ref, publish_id, inline=None
                )

        # 8. Construct PublishedArtifact and register
        published = PublishedArtifact(
            publish_id=publish_id,
            artifact_id=artifact_id,
            snapshot_name=art.name,
            snapshot_kind=rev.kind,
            snapshot_mime=rev.mime,
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
            registered = await self._registry.register_revision_publish(
                published,
                artifact_id=artifact_id,
                revision_id=revision_id,
                expected_current_revision_id=expected_current_revision_id,
            )
        except Exception:
            # 9. Compensate: delete new snapshot, old active preserved.
            if snapshot_ref is not None:
                await self._best_effort_delete(snapshot_ref)
            raise

        # If register_revision_publish reused an existing active (different
        # publish_id), clean up the orphaned new snapshot.
        reused = registered.publish_id != publish_id
        if reused and snapshot_ref is not None:
            await self._best_effort_delete(snapshot_ref)

        return PublishResult(
            published=registered,
            share_url=self._compute_share_url(registered.publish_id),
            reused=reused,
        )

    async def get_publish_sync_state(self, artifact_id: str) -> str:
        """Return the publish sync state for an artifact's current revision.

        - ``unpublished``: no active publish.
        - ``current``: active publish points at the current revision.
        - ``outdated``: active publish points at a different revision.

        Raises ArtifactNotFoundError when the artifact does not exist.
        Raises ArtifactMigrationIncompleteError when the artifact has no
        current revision.
        """
        art = await self._registry.get_artifact(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        if art.current_revision_id is None:
            raise ArtifactMigrationIncompleteError(
                f"artifact has no revision: {artifact_id}"
            )
        return await self._derive_publish_sync_state(
            artifact_id, art.current_revision_id
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

        Uses (task_attachment, attachment_id) for dedup.  Content is read from
        the attachment source and materialized to a Revision-owned ``item:``
        path (the source ref is never used as content_ref), so source deletion
        or rewrite does not affect registered revisions.  Returns existing
        artifact if already registered.  Returns None on failure (best-effort,
        does not raise).
        """
        existing = await self._registry.get_by_source(
            ArtifactSource.TASK_ATTACHMENT, attachment.attachment_id
        )
        if existing is not None:
            return existing

        source_ref = f"attachment:{attachment.task_id}/{attachment.stored_name}"
        try:
            data = await self._content_store.read(
                source_ref, max_bytes=self._config.artifact_max_bytes
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
        resolved_mime = _ensure_nonempty_mime(resolved_mime, kind)
        size = len(data)
        checksum = _sha256_checksum(data)
        artifact_id = _generate_artifact_id()
        session_id = await self._resolve_task_session(attachment.task_id)

        # Materialize source content to a Revision-owned item: path so the
        # registered content survives source deletion/rewrite.
        try:
            content_ref = await self._content_store.write_atomic(
                artifact_id, attachment.filename, data
            )
        except Exception as exc:
            logger.warning(
                "register_from_attachment materialize failed: "
                "source_kind=%s source_ref=%s error=%s",
                ArtifactSource.TASK_ATTACHMENT.value,
                attachment.attachment_id,
                type(exc).__name__,
            )
            return None

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
        initial_revision = ArtifactRevision(
            id=_generate_revision_id(),
            artifact_id=artifact_id,
            revision_number=1,
            parent_revision_id=None,
            rollback_from_revision_id=None,
            content_ref=content_ref,
            inline_content=None,
            size=size,
            checksum=checksum,
            mime=resolved_mime,
            kind=kind,
            created_at=datetime.now(timezone.utc),
            change_summary="",
            created_by=attachment.uploaded_by or _ACTOR,
            source_session_id=session_id,
            source_run_id=None,
        )

        try:
            created_artifact, _ = await (
                self._registry.create_artifact_with_initial_revision(
                    artifact, initial_revision,
                )
            )
            return created_artifact
        except Exception as exc:
            await self._best_effort_delete(content_ref)
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

        Two content paths:
          - ``storage_ref`` is a ``workspace:`` ref -> read the file from
            workspace_root, materialize to a Revision-owned ``item:`` path
            (the source ref is never used as content_ref, so source deletion
            does not affect registered revisions).
          - Otherwise, for text-kind artifacts, use inline content from
            ``content`` (preferred) or ``summary`` (fallback) -> store as
            ``inline_content`` (no workspace file needed).

        Invalid/missing/unreadable/incompatible-kind -> warning + skip
        (return None). Does NOT affect Task finish.
        """
        source_ref = Artifact.task_artifact_source_ref(task_id, run_id, ordinal)
        existing = await self._registry.get_by_source(
            ArtifactSource.TASK_ARTIFACT, source_ref,
        )
        if existing is not None:
            return existing

        resolved_mime = _resolve_mime(task_artifact.name, task_artifact.mime)
        kind = _kind_from_mime(resolved_mime)
        resolved_mime = _ensure_nonempty_mime(resolved_mime, kind)
        is_workspace_ref = (
            isinstance(task_artifact.storage_ref, str)
            and task_artifact.storage_ref.startswith("workspace:")
        )

        new_content_ref: str | None = None
        new_inline: str | None = None
        size: int = 0
        checksum: str = ""

        if is_workspace_ref:
            try:
                data = await self._content_store.read(
                    task_artifact.storage_ref,
                    max_bytes=self._config.artifact_max_bytes,
                )
            except Exception as exc:
                logger.warning(
                    "register_from_task_artifact skipped: "
                    "source_kind=%s source_ref=%s error=%s",
                    ArtifactSource.TASK_ARTIFACT.value, source_ref,
                    type(exc).__name__,
                )
                return None
            size = len(data)
            checksum = _sha256_checksum(data)
        else:
            # Inline path: text-kind artifacts only. Prefer ``content``,
            # fall back to ``summary`` (worker may put full text there).
            inline_text = task_artifact.content
            if not inline_text and task_artifact.summary:
                inline_text = task_artifact.summary
            if not inline_text or kind not in _TEXT_KINDS:
                logger.warning(
                    "register_from_task_artifact skipped: "
                    "source_kind=%s source_ref=%s error=%s",
                    ArtifactSource.TASK_ARTIFACT.value, source_ref,
                    "InvalidStorageRef",
                )
                return None
            data = inline_text.encode("utf-8")
            if len(data) > self._config.artifact_inline_max_bytes:
                logger.warning(
                    "register_from_task_artifact skipped: "
                    "source_kind=%s source_ref=%s error=%s",
                    ArtifactSource.TASK_ARTIFACT.value, source_ref,
                    "ContentTooLarge",
                )
                return None
            new_inline = inline_text
            size = len(data)
            checksum = _sha256_checksum(data)

        artifact_id = _generate_artifact_id()
        session_id = await self._resolve_task_session(task_id)

        # Materialize workspace content to a Revision-owned item: path so the
        # registered content survives source deletion/rewrite.  Inline text
        # is固化为 inline_content directly (no file needed).
        if is_workspace_ref:
            try:
                new_content_ref = await self._content_store.write_atomic(
                    artifact_id, task_artifact.name, data
                )
            except Exception as exc:
                logger.warning(
                    "register_from_task_artifact materialize failed: "
                    "source_kind=%s source_ref=%s error=%s",
                    ArtifactSource.TASK_ARTIFACT.value, source_ref,
                    type(exc).__name__,
                )
                return None

        artifact = Artifact(
            id=artifact_id,
            name=task_artifact.name,
            kind=kind,
            mime=resolved_mime,
            content_ref=new_content_ref,
            inline_content=new_inline,
            size=size,
            checksum=checksum,
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref=source_ref,
            source_context_ref=task_id,
            source_session_id=session_id,
            summary=task_artifact.summary,
            created_by=_ACTOR,
        )
        initial_revision = ArtifactRevision(
            id=_generate_revision_id(),
            artifact_id=artifact_id,
            revision_number=1,
            parent_revision_id=None,
            rollback_from_revision_id=None,
            content_ref=new_content_ref,
            inline_content=new_inline,
            size=size,
            checksum=checksum,
            mime=resolved_mime,
            kind=kind,
            created_at=datetime.now(timezone.utc),
            change_summary="",
            created_by=_ACTOR,
            source_session_id=session_id,
            source_run_id=None,
        )

        try:
            created_artifact, _ = await (
                self._registry.create_artifact_with_initial_revision(
                    artifact, initial_revision,
                )
            )
            return created_artifact
        except Exception as exc:
            if new_content_ref is not None:
                await self._best_effort_delete(new_content_ref)
            logger.warning(
                "register_from_task_artifact failed: "
                "source_kind=%s source_ref=%s error=%s",
                ArtifactSource.TASK_ARTIFACT.value, source_ref,
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
    # Revision migration backfill
    # ------------------------------------------------------------------

    async def migrate_revisions(self, *, batch_size: int = 100) -> dict[str, int]:
        """Backfill initial revisions for unmigrated artifacts.

        Pages through artifacts with NULL ``current_revision_id``, reads
        their legacy content (inline or file-backed), verifies checksum
        and size against actual bytes, materializes file-backed content to
        Revision-owned ``item:`` paths (never overwriting source files),
        and commits via ``commit_initial_revision_backfill`` (three-state
        idempotent: skip / backfill-pointer / insert+backfill).

        Failure handling (source unreadable, checksum mismatch, size
        exceeds limit, commit error): the new ``item:`` file is
        compensated (deleted), the artifact is counted as failed, and no
        pseudo-Revision is created.  The failure type is recorded for
        health reporting (sanitized, no paths/refs/class names).

        Returns ``{"processed", "migrated", "skipped", "failed"}``.
        """
        processed = 0
        migrated = 0
        skipped = 0
        failed = 0
        failure_types: dict[str, int] = {}

        cursor: ArtifactListCursor | None = None
        while True:
            page = await self._registry.list_revision_migration_candidates(
                cursor=cursor, limit=batch_size,
            )
            for art in page.items:
                processed += 1
                result = await self._migrate_one_artifact(art)
                if result == "migrated":
                    migrated += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
                    failure_types[result] = failure_types.get(result, 0) + 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        # Update migration state cache for health_snapshot.
        self._migration_failed_count = failed
        if failed > 0:
            self._migration_state = "degraded"
            # Sanitized: only stable failure-type names + counts, no
            # paths/refs/exception class names.
            self._migration_last_error = ",".join(
                f"{k}:{v}" for k, v in sorted(failure_types.items())
            ) or "unknown"
        else:
            self._migration_state = "ok"
            self._migration_last_error = None

        return {
            "processed": processed,
            "migrated": migrated,
            "skipped": skipped,
            "failed": failed,
        }

    async def _migrate_one_artifact(self, art: Artifact) -> str:
        """Migrate a single unmigrated artifact.

        Returns ``"migrated"``, ``"skipped"`` (already had a revision),
        or a failure-type string (``"source_unreadable"``,
        ``"checksum_mismatch"``, ``"size_exceeds_limit"``,
        ``"commit_failed"``, ``"unknown"``).
        """
        new_item_ref: str | None = None
        try:
            # Read legacy content and recompute size/checksum from actual
            # bytes (do not blindly trust declared values).
            if art.inline_content is not None:
                data = art.inline_content.encode("utf-8")
            elif art.content_ref is not None:
                try:
                    data = await self._content_store.read(
                        art.content_ref,
                        max_bytes=self._config.artifact_max_bytes,
                    )
                except Exception:
                    return "source_unreadable"
            else:
                return "source_unreadable"

            # Verify size.
            if len(data) > self._config.artifact_max_bytes:
                return "size_exceeds_limit"

            # Verify checksum matches declared value.
            actual_checksum = _sha256_checksum(data)
            if actual_checksum != art.checksum:
                return "checksum_mismatch"

            actual_size = len(data)

            # Materialize file-backed content to a new Revision-owned
            # item: path (never overwrite the source file).  Inline
            # content is固化为 inline_content directly.
            if art.inline_content is not None:
                new_inline = art.inline_content
                new_content_ref: str | None = None
            else:
                new_inline = None
                new_item_ref = await self._content_store.write_atomic(
                    art.id, f"rev-{_generate_revision_id()}", data,
                )
                new_content_ref = new_item_ref

            # Resolve mime (ArtifactRevision requires non-empty mime).
            rev_mime = _ensure_nonempty_mime(art.mime, art.kind)

            revision = ArtifactRevision(
                id=_generate_revision_id(),
                artifact_id=art.id,
                revision_number=1,
                parent_revision_id=None,
                rollback_from_revision_id=None,
                content_ref=new_content_ref,
                inline_content=new_inline,
                size=actual_size,
                checksum=actual_checksum,
                mime=rev_mime,
                kind=art.kind,
                created_at=datetime.now(timezone.utc),
                change_summary="migration backfill",
                created_by="system",
                source_session_id=art.source_session_id,
                source_run_id=None,
            )

            try:
                committed = await self._registry.commit_initial_revision_backfill(
                    art.id, revision,
                )
            except Exception:
                if new_item_ref is not None:
                    await self._best_effort_delete(new_item_ref)
                return "commit_failed"

            # State 1 or 2: existing revision was used (not our insert).
            # Clean up the new item: file we wrote (not needed).
            if committed.id != revision.id:
                if new_item_ref is not None:
                    await self._best_effort_delete(new_item_ref)
                return "skipped"

            return "migrated"
        except Exception:
            if new_item_ref is not None:
                await self._best_effort_delete(new_item_ref)
            return "unknown"

    def migration_status(self) -> dict:
        """Return sanitized migration status for health reporting.

        Returns ``{"state": "ok"|"degraded", "failed_count": N,
        "last_error": str | None}``.  ``last_error`` is sanitized to
        stable failure-type names (no paths/refs/exception class names).
        Defaults to ``{"state": "ok", "failed_count": 0, "last_error": None}``
        when migration has not run.
        """
        return {
            "state": self._migration_state,
            "failed_count": self._migration_failed_count,
            "last_error": self._migration_last_error,
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
