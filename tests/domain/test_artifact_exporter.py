"""Tests for the ArtifactExporter port and ExportedArtifact value object.

Pure domain tests -- no IO, no Infrastructure. Validates the ExportedArtifact
frozen value-object invariants (non-empty data/mime/filename), ContentProfile
enum values, and the ArtifactExporter Protocol surface.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.artifact import (
    ArtifactExportError,
    ArtifactExportTooLargeError,
    ArtifactExportUnsupportedError,
)
from app.domain.artifact_exporter import (
    ArtifactExporter,
    ContentProfile,
    ExportedArtifact,
)


def test_exported_artifact_frozen():
    ea = ExportedArtifact(data=b"x", mime="text/html", filename="a.html")
    assert ea.data == b"x"
    with pytest.raises(FrozenInstanceError):
        ea.mime = "y"


def test_exported_artifact_rejects_empty_fields():
    with pytest.raises(ArtifactExportError):
        ExportedArtifact(data=b"", mime="text/html", filename="a.html")
    with pytest.raises(ArtifactExportError):
        ExportedArtifact(data=b"x", mime="", filename="a.html")
    with pytest.raises(ArtifactExportError):
        ExportedArtifact(data=b"x", mime="text/html", filename="")


def test_capabilities_signature_is_protocol():
    assert hasattr(ArtifactExporter, "capabilities") and hasattr(ArtifactExporter, "export")


def test_content_profile_enum_values():
    expected = {"markdown", "tabular", "text", "binary"}
    assert {p.value for p in ContentProfile} == expected


def test_export_error_hierarchy_catchable_via_base():
    """Export-specific errors are catchable via ArtifactExportError."""
    for exc_cls in (ArtifactExportTooLargeError, ArtifactExportUnsupportedError):
        assert issubclass(exc_cls, ArtifactExportError)
        try:
            raise exc_cls("test")
        except ArtifactExportError:
            pass
        else:
            pytest.fail(f"{exc_cls.__name__} not catchable via ArtifactExportError")
