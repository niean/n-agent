"""Artifact subdomain: aggregate root, value objects, ports, and errors.

Pure domain -- no FastAPI, SQLite, Pydantic, or Infrastructure imports.
Matches the frozen-dataclass + enum + async-Protocol pattern of ``task.py``
/ ``browser.py`` / ``skill.py``.

The Artifact aggregate models a piece of content produced or referenced by
the system: inline text (document/markdown/code/html/data/csv/json/text) or
binary blobs (image/pdf/other). PublishedArtifact is the immutable snapshot
published for external consumption. Ports (``ArtifactRegistry``,
``ArtifactContentStore``) define the persistence and content-storage
contracts used by the Application layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ArtifactKind(str, Enum):
    """Content kind. Text kinds support inline content; binary kinds require
    an external content_ref."""

    DOCUMENT = "document"
    MARKDOWN = "markdown"
    CODE = "code"
    HTML = "html"
    DATA = "data"
    CSV = "csv"
    JSON = "json"
    IMAGE = "image"
    PDF = "pdf"
    TEXT = "text"
    OTHER = "other"


class ArtifactSource(str, Enum):
    """Origin of the artifact content."""

    TASK_ATTACHMENT = "task_attachment"
    TASK_ARTIFACT = "task_artifact"
    SESSION = "session"
    MANUAL = "manual"


class ArtifactStatus(str, Enum):
    """Artifact lifecycle status."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class PublishedArtifactStatus(str, Enum):
    """Publication status of a PublishedArtifact snapshot."""

    ACTIVE = "active"
    REVOKED = "revoked"


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_TEXT_KINDS = frozenset({
    ArtifactKind.DOCUMENT,
    ArtifactKind.MARKDOWN,
    ArtifactKind.CODE,
    ArtifactKind.HTML,
    ArtifactKind.DATA,
    ArtifactKind.CSV,
    ArtifactKind.JSON,
    ArtifactKind.TEXT,
})

_BINARY_KINDS = frozenset({
    ArtifactKind.IMAGE,
    ArtifactKind.PDF,
    ArtifactKind.OTHER,
})

_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Content-ref scheme allowed for ArtifactRevision.content_ref. Persistent
# revision content is always stored under the ``item:`` namespace; other
# schemes (workspace:, attachment:, published:) are rejected.
_ITEM_REF_PREFIX = "item:"


def _validate_checksum(checksum: str, field_name: str = "checksum") -> None:
    if not isinstance(checksum, str) or not _CHECKSUM_RE.match(checksum):
        raise ArtifactValidationError(
            f"{field_name} must be 'sha256:' + 64 lowercase hex chars"
        )


# ---------------------------------------------------------------------------
# Artifact aggregate root
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    """Artifact aggregate root (frozen dataclass with domain invariants).

    Content model:
      - Text kinds (document/markdown/code/html/data/csv/json/text):
        exactly one of ``inline_content`` / ``content_ref`` must be set.
        When inline, ``size`` is the UTF-8 byte length of ``inline_content``.
      - Binary kinds (image/pdf/other): ``inline_content`` must be None,
        ``content_ref`` must be a non-empty string.

    Source conventions:
      - ``manual``: ``source_ref`` must equal ``id`` (the artifact is its
        own source).
      - ``task_artifact``: use ``task_artifact_source_ref()`` to build the
        source key ``task:{task_id}:run:{run_id}:artifact:{ordinal}``.

    ``to_public_view()`` returns a display-safe dict that excludes
    ``content_ref``, ``inline_content``, ``source_ref``, and any absolute
    paths or internal storage details.
    """

    # Required identity / content fields (no defaults)
    id: str
    name: str
    kind: ArtifactKind
    mime: str
    content_ref: str | None
    inline_content: str | None
    size: int
    checksum: str
    source_kind: ArtifactSource
    source_ref: str

    # Optional metadata (with defaults)
    source_context_ref: str | None = None
    # Session this artifact is associated with (queryable association keyed
    # by session id). Set at registration for task-produced artifacts so the
    # conversation panel can find them by session id. Distinct from
    # ``source_context_ref`` (which holds the task id for task artifacts).
    source_session_id: str | None = None
    summary: str = ""
    classification: str | None = None
    labels: tuple[str, ...] | None = None
    status: ArtifactStatus = ArtifactStatus.DRAFT
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: str = ""
    # Current revision id within the artifact's revision history (None when
    # the artifact has no revisions yet). Updated via domain replacement when
    # a new revision is committed or a rollback is performed.
    current_revision_id: str | None = None

    # -----------------------------------------------------------------
    # Invariants
    # -----------------------------------------------------------------

    def __post_init__(self) -> None:
        # Validate checksum format first (shared by Artifact and PublishedArtifact).
        _validate_checksum(self.checksum)

        # Size must be non-negative.
        if not isinstance(self.size, int) or isinstance(self.size, bool):
            raise ArtifactValidationError("size must be an int")
        if self.size < 0:
            raise ArtifactValidationError("size must be non-negative")

        if self.kind in _TEXT_KINDS:
            has_inline = self.inline_content is not None
            has_ref = bool(self.content_ref)
            # XOR: exactly one must be set.
            if has_inline == has_ref:
                raise ArtifactValidationError(
                    "text artifact requires exactly one of "
                    "inline_content / content_ref"
                )
            if has_inline:
                expected = len(self.inline_content.encode("utf-8"))
                if self.size != expected:
                    raise ArtifactValidationError(
                        f"size must be UTF-8 byte length of inline_content "
                        f"({expected}), got {self.size}"
                    )
        elif self.kind in _BINARY_KINDS:
            if self.inline_content is not None:
                raise ArtifactValidationError(
                    "binary artifact must not have inline_content"
                )
            if not self.content_ref:
                raise ArtifactValidationError(
                    "binary artifact requires non-empty content_ref"
                )
        else:
            raise ArtifactValidationError(f"unknown artifact kind: {self.kind}")

        # Manual source_ref must equal id.
        if self.source_kind is ArtifactSource.MANUAL:
            if self.source_ref != self.id:
                raise ArtifactValidationError(
                    "manual artifact source_ref must equal id"
                )

    # -----------------------------------------------------------------
    # Source key helpers
    # -----------------------------------------------------------------

    @staticmethod
    def task_artifact_source_ref(
        task_id: str, run_id: int, ordinal: int,
    ) -> str:
        """Build the canonical source_ref for a task_artifact artifact.

        Format: ``task:{task_id}:run:{run_id}:artifact:{ordinal}``
        """
        return f"task:{task_id}:run:{run_id}:artifact:{ordinal}"

    # -----------------------------------------------------------------
    # Public view
    # -----------------------------------------------------------------

    def to_public_view(self) -> dict[str, object]:
        """Return a display-safe dict for external consumption.

        Excludes ``content_ref``, ``inline_content``, ``source_ref``,
        snapshot refs, and any internal storage details. Includes
        ``source_context_ref`` (a display-safe source context) but NOT
        the raw ``source_ref`` key.
        """
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "mime": self.mime,
            "size": self.size,
            "checksum": self.checksum,
            "source_kind": self.source_kind,
            "source_context_ref": self.source_context_ref,
            "source_session_id": self.source_session_id,
            "summary": self.summary,
            "classification": self.classification,
            "labels": self.labels,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
        }


# ---------------------------------------------------------------------------
# PublishedArtifact (immutable snapshot entity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishedArtifact:
    """Immutable published snapshot of an Artifact.

    ``artifact_id`` is nullable to support source-deletion scenarios (the
    original artifact row is gone but the published snapshot persists).
    Snapshot fields are immutable by virtue of the frozen dataclass;
    only ``status`` and ``revoked_at`` change via domain replacement.

    ``is_active`` returns True when the publication has not been revoked.
    """

    publish_id: str
    artifact_id: str | None
    snapshot_name: str
    snapshot_kind: ArtifactKind
    snapshot_mime: str
    snapshot_content_ref: str | None
    snapshot_inline_content: str | None
    snapshot_size: int
    snapshot_checksum: str
    snapshot_summary: str = ""
    published_at: datetime | None = None
    published_by: str = ""
    status: PublishedArtifactStatus = PublishedArtifactStatus.ACTIVE
    revoked_at: datetime | None = None
    # Revision id that was published in this snapshot (None when the
    # publication predates the revision feature or publishes the artifact
    # without a specific revision).
    published_revision_id: str | None = None

    def __post_init__(self) -> None:
        _validate_checksum(self.snapshot_checksum, field_name="snapshot_checksum")
        if not isinstance(self.snapshot_size, int) or isinstance(self.snapshot_size, bool):
            raise ArtifactValidationError("snapshot_size must be an int")
        if self.snapshot_size < 0:
            raise ArtifactValidationError("snapshot_size must be non-negative")

    @property
    def is_active(self) -> bool:
        return self.status is PublishedArtifactStatus.ACTIVE


# ---------------------------------------------------------------------------
# ArtifactRevision (immutable revision entity within the Artifact aggregate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRevision:
    """Immutable revision of an Artifact's content.

    A revision captures a snapshot of the artifact content at a point in
    time, supporting version history and rollback.  The first revision of
    an artifact is the *initial* revision (``parent_revision_id`` and
    ``rollback_from_revision_id`` are both ``None``).  Subsequent revisions
    reference their parent.  A *rollback* revision restores a previous
    state: ``rollback_from_revision_id`` names the revision being rolled
    back from, and ``parent_revision_id`` is the current head at rollback
    time (both must be non-None for a rollback revision).

    Content model (mirrors :class:`Artifact`):
      - Text kinds (document/markdown/code/html/data/csv/json/text):
        exactly one of ``inline_content`` / ``content_ref`` must be set.
        When inline, ``size`` is the UTF-8 byte length of
        ``inline_content`` (``""`` is a valid zero-length inline content).
      - Binary kinds (image/pdf/other): ``inline_content`` must be
        ``None``, ``content_ref`` must use the ``item:`` scheme.

    Provenance validation: ``checksum`` follows the ``sha256:`` + 64 hex
    pattern, ``size`` is a non-negative int, ``mime`` is non-empty,
    ``created_at`` is UTC-aware, and ``content_ref`` (when set) must start
    with ``item:`` followed by a non-empty path.

    Cross-revision invariants (parent/rollback belong to the same
    artifact, revision numbers are contiguous) are enforced by the
    Registry inside write transactions, not by this value object.
    """

    id: str
    artifact_id: str
    revision_number: int
    parent_revision_id: str | None
    rollback_from_revision_id: str | None
    content_ref: str | None
    inline_content: str | None
    size: int
    checksum: str
    mime: str
    kind: ArtifactKind
    created_at: datetime
    change_summary: str = ""
    created_by: str = ""
    source_session_id: str | None = None
    source_run_id: str | None = None

    def __post_init__(self) -> None:
        # --- content_ref / inline_content XOR ---
        has_ref = self.content_ref is not None
        has_inline = self.inline_content is not None
        if has_ref == has_inline:  # both set or both None
            raise ArtifactValidationError(
                "revision requires exactly one of content_ref/inline_content"
            )
        if self.content_ref == "":
            raise ArtifactValidationError("content_ref must be non-empty")

        # --- binary kinds must use content_ref (no inline) ---
        if self.kind not in _TEXT_KINDS and self.inline_content is not None:
            raise ArtifactValidationError(
                "binary revision must use content_ref"
            )

        # --- revision_number ---
        if self.revision_number < 1:
            raise ArtifactValidationError("revision_number must be >= 1")

        # --- checksum format (reuse shared validator) ---
        _validate_checksum(self.checksum)

        # --- size: int (not bool), non-negative ---
        if not isinstance(self.size, int) or isinstance(self.size, bool):
            raise ArtifactValidationError("size must be an int")
        if self.size < 0:
            raise ArtifactValidationError("size must be non-negative")

        # --- kind must be a known ArtifactKind ---
        if self.kind not in _TEXT_KINDS and self.kind not in _BINARY_KINDS:
            raise ArtifactValidationError(f"unknown artifact kind: {self.kind}")

        # --- mime non-empty ---
        if not self.mime:
            raise ArtifactValidationError("mime must be non-empty")

        # --- created_at must be UTC-aware ---
        if self.created_at is None:
            raise ArtifactValidationError("created_at must not be None")
        if self.created_at.tzinfo is None:
            raise ArtifactValidationError("created_at must be UTC-aware")

        # --- content_ref scheme: only item: allowed ---
        if self.content_ref is not None:
            if (
                not self.content_ref.startswith(_ITEM_REF_PREFIX)
                or len(self.content_ref) <= len(_ITEM_REF_PREFIX)
            ):
                raise ArtifactValidationError(
                    "content_ref must use 'item:' scheme with a non-empty path"
                )

        # --- inline text: size must match UTF-8 byte length ---
        if self.inline_content is not None and self.kind in _TEXT_KINDS:
            expected = len(self.inline_content.encode("utf-8"))
            if self.size != expected:
                raise ArtifactValidationError(
                    f"size must be UTF-8 byte length of inline_content "
                    f"({expected}), got {self.size}"
                )

        # --- rollback shape: rollback_from requires parent ---
        if (
            self.rollback_from_revision_id is not None
            and self.parent_revision_id is None
        ):
            raise ArtifactValidationError(
                "rollback_from_revision_id requires parent_revision_id"
            )
        # parent/rollback same-artifact ownership and contiguous numbering
        # are validated by the Registry in a write transaction.

    @property
    def is_initial(self) -> bool:
        """True when this is the first revision (no parent, no rollback)."""
        return (
            self.parent_revision_id is None
            and self.rollback_from_revision_id is None
        )


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactListCursor:
    """Stable pagination cursor (updated_at + artifact_id)."""

    updated_at: datetime | None
    artifact_id: str


@dataclass(frozen=True)
class ArtifactListPage:
    """A page of Artifact list results."""

    items: tuple[Artifact, ...]
    next_cursor: ArtifactListCursor | None = None


@dataclass(frozen=True)
class ArtifactAttachmentSource:
    """Backfill source descriptor: the fields needed to register an
    Artifact from a TaskAttachment.

    Mirrors the identity fields of ``TaskAttachment`` so the backfill
    service can batch-read attachments and construct Artifacts without
    importing the Task domain.
    """

    attachment_id: str
    task_id: str
    stored_name: str
    filename: str
    content_type: str
    size: int
    checksum: str
    uploaded_by: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class RevisionListCursor:
    """Stable pagination cursor for revision listing.

    Carries ``artifact_id`` + ``revision_number`` + ``id`` so the Registry
    can resume listing after the row identified by ``(revision_number, id)``.
    The cursor is bound to a specific ``artifact_id``: reusing a cursor
    decoded from a token against a different artifact is a client error and
    must raise :class:`ArtifactRevisionValidationError` at the
    Application/Registry layer. Token encode/decode lives outside the Domain
    (Application/HTTP); this value object only carries the fields.

    ``limit`` clamping (1..100) is enforced consistently at the
    Application/HTTP/Tool layer, not in the Domain.
    """

    artifact_id: str
    revision_number: int
    id: str


@dataclass(frozen=True)
class RevisionListPage:
    """A page of ArtifactRevision list results."""

    items: tuple[ArtifactRevision, ...]
    next_cursor: RevisionListCursor | None = None


@dataclass(frozen=True)
class ArtifactDeleteGraph:
    """Deduplicated content references collected when deleting an Artifact
    and its entire revision graph in a single transaction.

    Returned by :meth:`ArtifactRegistry.delete_artifact_graph` so the caller
    can perform best-effort content cleanup via :class:`ArtifactContentStore`
    after the transaction commits. All refs are deduplicated by the Registry
    before returning; this value object merely holds the result.

    Fields:
      - ``revision_content_refs``: ``item:`` content_refs from every
        revision of the artifact (deduplicated).
      - ``legacy_artifact_content_ref``: the artifact's own ``content_ref``
        (pre-revision legacy content), ``None`` when the artifact used
        inline content.
      - ``publish_snapshot_ids``: ``publish_id`` values whose publish
        snapshots should be removed via the content store.
    """

    revision_content_refs: tuple[str, ...] = ()
    legacy_artifact_content_ref: str | None = None
    publish_snapshot_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Ports (async Protocols)
# ---------------------------------------------------------------------------


class ArtifactRegistry(Protocol):
    """Async port for Artifact and PublishedArtifact persistence.

    All cursor parameters use the domain type ``ArtifactListCursor``,
    never ``Any``. ``list_attachment_sources`` supports batched backfill
    reads from the Task attachment table.
    """

    # --- Artifact CRUD ---
    async def create_artifact(self, artifact: Artifact) -> Artifact: ...
    async def get_artifact(self, artifact_id: str) -> Artifact | None: ...
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
    ) -> ArtifactListPage: ...
    async def update_artifact(self, artifact: Artifact) -> Artifact: ...
    async def delete_artifact(self, artifact_id: str) -> bool: ...
    async def get_by_source(
        self, source_kind: ArtifactSource, source_ref: str,
    ) -> Artifact | None: ...
    async def count_artifacts(self) -> int: ...

    # --- Session backfill ---
    async def list_task_artifacts_missing_session(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        """Return task-source artifacts with NULL source_session_id.

        Used by the session-id backfill to populate the queryable
        session association for existing task artifacts.
        """
        ...

    # --- Kind backfill ---
    async def list_artifacts_with_empty_mime(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        """Return artifacts whose mime is empty (unclassified kind).

        Used by the kind backfill to re-infer kind/mime from the filename
        extension for artifacts registered without a content_type.
        """
        ...

    # --- PublishedArtifact lifecycle ---
    async def register_published(
        self,
        published: PublishedArtifact,
        *,
        revoke_artifact_id: str | None = None,
    ) -> PublishedArtifact: ...
    async def get_published(self, publish_id: str) -> PublishedArtifact | None: ...
    async def get_active_publish(
        self, artifact_id: str,
    ) -> PublishedArtifact | None: ...
    async def list_published(
        self, artifact_id: str | None = None,
    ) -> tuple[PublishedArtifact, ...]: ...
    async def revoke_published(
        self, artifact_id: str,
    ) -> PublishedArtifact | None: ...
    async def delete_published_by_artifact(
        self, artifact_id: str,
    ) -> int:
        """Delete every PublishedArtifact row referencing ``artifact_id``.

        Returns the number of rows deleted. Used by source-artifact deletion
        to purge publish records (and, via the content store, their snapshot
        files) so no orphaned rows survive with a NULL artifact_id.
        """
        ...

    # --- Backfill batch read ---
    async def list_attachment_sources(
        self,
        *,
        after_attachment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ArtifactAttachmentSource, ...]: ...

    # --- Revision lifecycle ---
    async def create_artifact_with_initial_revision(
        self, artifact: Artifact, initial: ArtifactRevision,
    ) -> tuple[Artifact, ArtifactRevision]: ...
    async def append_revision(
        self, artifact_id: str, revision: ArtifactRevision, *,
        expected_revision_id: str,
    ) -> ArtifactRevision:
        """Append a revision with optimistic compare-and-set.

        ``expected_revision_id`` must equal the artifact's current
        ``current_revision_id``; otherwise raise
        :class:`ArtifactRevisionConflictError`.
        """
        ...
    async def get_revision(
        self, artifact_id: str, revision_id: str,
    ) -> ArtifactRevision | None: ...
    async def list_revisions(
        self, artifact_id: str, *, cursor: RevisionListCursor | None = None,
        limit: int = 50,
    ) -> RevisionListPage: ...
    async def delete_artifact_graph(
        self, artifact_id: str,
    ) -> ArtifactDeleteGraph:
        """Delete the artifact and its entire revision graph in one transaction.

        Returns deduplicated Revision/legacy/publish content refs for
        best-effort post-commit cleanup via the content store. Migration
        content reads and item file writes are NOT performed here: the
        Application uses :class:`ArtifactContentStore` for IO, then calls
        this single-transaction port, preserving layering and compensability.
        """
        ...
    async def list_revision_migration_candidates(
        self, *, cursor: ArtifactListCursor | None, limit: int,
    ) -> ArtifactListPage: ...
    async def commit_initial_revision_backfill(
        self, artifact_id: str, revision: ArtifactRevision,
    ) -> ArtifactRevision:
        """Backfill the initial revision under BEGIN IMMEDIATE.

        Three states: a valid ``current_revision_id`` already exists ->
        skip; a revision with ``revision_number=1`` exists -> backfill
        ``current_revision_id`` only; otherwise insert the revision and
        backfill ``current_revision_id``.
        """
        ...
    async def register_revision_publish(
        self, published: PublishedArtifact, *, artifact_id: str,
        revision_id: str, expected_current_revision_id: str,
    ) -> PublishedArtifact:
        """Register a revision publish under BEGIN IMMEDIATE.

        Re-verifies ``current_revision_id`` against
        ``expected_current_revision_id`` and reuses or switches the active
        publish based on the final public checksum.
        """
        ...
    async def count_artifacts_without_revision(self) -> int: ...


class ArtifactContentStore(Protocol):
    """Async port for raw artifact content storage.

    ``content_ref`` is an opaque string returned by ``write_atomic`` and
    consumed by ``read`` / ``delete_owned``. The store is responsible for
    atomicity (write-temp-then-rename) and ownership tracking.
    """

    async def read(self, content_ref: str, *, max_bytes: int) -> bytes: ...
    async def write_atomic(
        self, artifact_id: str, filename: str, data: bytes,
    ) -> str: ...
    async def delete_owned(self, content_ref: str) -> bool: ...
    async def materialize_source(
        self,
        source_kind: ArtifactSource,
        source_ref: str,
        artifact_id: str,
    ) -> str: ...
    async def copy_to_publish_snapshot(
        self, src_ref: str, publish_id: str, *, inline: str | None = None,
    ) -> str: ...
    async def delete_publish_snapshot(self, publish_id: str) -> None:
        """Remove the ``published/{publish_id}/`` snapshot directory.

        Idempotent: a missing directory is a no-op. Deletes only regular
        files within the directory then the directory itself; refuses
        symlinks/subdirs (defense-in-depth). Inverse of
        :meth:`copy_to_publish_snapshot`.
        """
        ...


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class ArtifactError(Exception):
    """Base error for the Artifact subdomain."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an Artifact does not exist."""


class ArtifactValidationError(ArtifactError):
    """Raised when domain validation rejects a field or invariant."""


class ArtifactContentUnavailableError(ArtifactError):
    """Raised when content_ref resolves to missing or unreadable content."""


class PublishedArtifactNotFoundError(ArtifactError):
    """Raised when a PublishedArtifact does not exist."""


class ArtifactConflictError(ArtifactError):
    """Raised on optimistic-lock conflict or duplicate source registration."""


# --- Revision-related errors ---


class ArtifactRevisionNotFoundError(ArtifactError):
    """Raised when an ArtifactRevision does not exist."""


class ArtifactRevisionConflictError(ArtifactError):
    """Raised on revision conflict (409, retryable)."""


class ArtifactRevisionValidationError(ArtifactValidationError):
    """Raised when domain validation rejects a revision field or invariant."""


class ArtifactDiffTooLargeError(ArtifactError):
    """Raised when a diff exceeds the configured size limit."""


class ArtifactDiffUnsupportedError(ArtifactError):
    """Raised when a diff is requested across text/binary or for binary content (422)."""


# --- Export-related errors ---


class ArtifactExportError(ArtifactError):
    """Base error for artifact export failures."""


class ArtifactExportTooLargeError(ArtifactExportError):
    """Raised when exported content exceeds the size limit (413)."""


class ArtifactExportUnsupportedError(ArtifactExportError):
    """Raised when the requested format is not in the exporter capabilities (422)."""


# --- Content read errors ---


class ArtifactReadTooLargeError(ArtifactError):
    """Raised when the first row to return already exceeds the limit (413)."""


# --- Migration errors ---


class ArtifactMigrationIncompleteError(ArtifactError):
    """Raised when a migration is not yet complete (503, retryable)."""
