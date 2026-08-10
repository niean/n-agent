"""Artifact exporter port and value objects.

Pure domain -- no IO, no third-party libraries.  Defines the
:class:`ArtifactExporter` Protocol (async port) consumed by the Application
layer, the :class:`ContentProfile` enum used to classify revision content for
export routing, and the :class:`ExportedArtifact` frozen value object returned
by exporters.

The exporter port signature is ``export(revision, content_bytes, format,
options)``: the Application layer normalises ``format`` to lowercase before
calling, passes a safe filename stem and output limits through the read-only
``options`` mapping, and the Infrastructure implementation never reads back
the Artifact or ContentStore.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from app.domain.artifact import (
    ArtifactExportError,
    ArtifactKind,
    ArtifactRevision,
)


# ---------------------------------------------------------------------------
# ContentProfile enum
# ---------------------------------------------------------------------------


class ContentProfile(str, Enum):
    """Coarse content profile used to route export format selection.

    Distinct from :class:`ArtifactKind`: a markdown artifact and a plain-text
    artifact may share the ``TEXT`` profile, while a CSV artifact maps to
    ``TABULAR``.  ``BINARY`` covers image/pdf/other kinds that have no
    text-level diff or conversion semantics.
    """

    MARKDOWN = "markdown"
    TABULAR = "tabular"
    TEXT = "text"
    BINARY = "binary"


# ---------------------------------------------------------------------------
# ExportedArtifact value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportedArtifact:
    """Immutable result of an artifact export.

    ``data`` is the raw exported bytes (e.g. HTML, PDF, XLSX).  ``mime`` is
    the MIME type of the exported data (not the source revision's MIME).
    ``filename`` is the suggested download filename including extension.

    All three fields must be non-empty; the ``format`` normalisation and
    size-limit enforcement happen in the Application layer before calling
    the exporter, so an empty field here indicates a programming error.
    """

    data: bytes
    mime: str
    filename: str

    def __post_init__(self) -> None:
        if not self.data:
            raise ArtifactExportError("data must be non-empty bytes")
        if not self.mime:
            raise ArtifactExportError("mime must be non-empty")
        if not self.filename:
            raise ArtifactExportError("filename must be non-empty")


# ---------------------------------------------------------------------------
# ArtifactExporter port (async Protocol)
# ---------------------------------------------------------------------------


class ArtifactExporter(Protocol):
    """Async port for exporting an :class:`ArtifactRevision` to a target format.

    The exporter receives the revision metadata and the already-materialised
    content bytes; it does not read the Artifact or ContentStore directly.
    Format routing uses :meth:`capabilities` to advertise supported output
    formats for a given (kind, mime, content_profile) tuple.
    """

    async def capabilities(
        self,
        kind: ArtifactKind,
        mime: str,
        content_profile: ContentProfile,
    ) -> tuple[str, ...]:
        """Return the tuple of supported output format names.

        Format names are lowercase strings (e.g. ``"html"``, ``"pdf"``,
        ``"xlsx"``).  An empty tuple means the combination has no supported
        export format.
        """
        ...

    async def export(
        self,
        revision: ArtifactRevision,
        content_bytes: bytes,
        format: str,
        options: Mapping[str, object] | None = None,
    ) -> ExportedArtifact:
        """Export ``revision`` content to ``format`` and return the result.

        ``content_bytes`` is the materialised content of the revision (read
        by the Application layer from the ContentStore).  ``format`` is
        pre-normalised to lowercase by the caller.  ``options`` carries
        read-only export parameters such as a safe filename stem and output
        size limits; the exporter must not mutate it.
        """
        ...
