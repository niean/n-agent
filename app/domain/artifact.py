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
    summary: str = ""
    classification: str | None = None
    labels: tuple[str, ...] | None = None
    status: ArtifactStatus = ArtifactStatus.DRAFT
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: str = ""

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

    # --- Backfill batch read ---
    async def list_attachment_sources(
        self,
        *,
        after_attachment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ArtifactAttachmentSource, ...]: ...


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
