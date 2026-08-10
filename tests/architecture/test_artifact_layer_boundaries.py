"""T11: Artifact subsystem architecture boundary tests.

Asserts the DDD layering holds for the Artifact subsystem:
  - Domain (app/domain/artifact*.py) imports no Application/Infrastructure/
    Interfaces and no third-party format libraries (docx/pptx/openpyxl).
  - Application (app/application/artifact*.py) imports no Infrastructure and no
    third-party format libraries.
  - Tool executor (app/infrastructure/tools/artifact_management.py) imports no
    third-party format libraries (format libs live ONLY in the Exporter).
  - The content-profile probe lives in Application; the OfficeArtifactExporter
    implementation lives ONLY in Infrastructure; the ArtifactExporter port
    (Protocol) lives in Domain.
  - Third-party format libs (docx/pptx/openpyxl) appear in exactly one module
    app-wide: app/infrastructure/artifact/exporters.py.

test_policy_boundaries.py separately pins the 16-Policy mesh and the existing
ArtifactPolicyAction; this file does not duplicate that count.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

FORMAT_LIBS = ("docx", "pptx", "openpyxl")
DOMAIN_FORBIDDEN = (
    "app.application",
    "app.infrastructure",
    "app.interfaces",
    *FORMAT_LIBS,
)
APPLICATION_FORBIDDEN = (
    "app.infrastructure",
    *FORMAT_LIBS,
)


def _imported_modules(path: Path) -> set[str]:
    """All imported module names in a file (top-level + nested/lazy imports)."""
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _violations(modules: set[str], forbidden: tuple[str, ...]) -> list[str]:
    return [
        m for m in modules
        if m in forbidden or any(m.startswith(f + ".") for f in forbidden)
    ]


def _artifact_domain_files() -> list[Path]:
    return sorted(APP.glob("domain/artifact*.py"))


def _artifact_application_files() -> list[Path]:
    return sorted(APP.glob("application/artifact*.py"))


# ---------------------------------------------------------------------------
# Domain purity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", _artifact_domain_files(), ids=lambda p: p.name)
def test_artifact_domain_pure(path: Path):
    """Domain artifact files import neither upper layers nor format libs."""
    bad = _violations(_imported_modules(path), DOMAIN_FORBIDDEN)
    assert not bad, f"{path.relative_to(ROOT)} imports forbidden modules: {bad}"


# ---------------------------------------------------------------------------
# Application purity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", _artifact_application_files(), ids=lambda p: p.name)
def test_artifact_application_no_infrastructure_no_format_libs(path: Path):
    """Application artifact files import neither Infrastructure nor format libs."""
    bad = _violations(_imported_modules(path), APPLICATION_FORBIDDEN)
    assert not bad, f"{path.relative_to(ROOT)} imports forbidden modules: {bad}"


# ---------------------------------------------------------------------------
# Tool executor purity
# ---------------------------------------------------------------------------

def test_artifact_tool_executor_no_format_libs():
    """The tool executor must not import format libs (Exporter's job)."""
    path = APP / "infrastructure" / "tools" / "artifact_management.py"
    bad = _violations(_imported_modules(path), FORMAT_LIBS)
    assert not bad, f"artifact_management.py imports format libs: {bad}"


# ---------------------------------------------------------------------------
# Format libs isolated to the Exporter
# ---------------------------------------------------------------------------

def test_format_libs_only_in_exporters_module():
    """docx/pptx/openpyxl appear in exactly one module app-wide: exporters.py."""
    expected = APP / "infrastructure" / "artifact" / "exporters.py"
    offenders: list[Path] = []
    for py in APP.rglob("*.py"):
        if not py.is_file():
            continue
        mods = _imported_modules(py)
        if any(m == f or m.startswith(f + ".") for m in mods for f in FORMAT_LIBS):
            offenders.append(py)
    assert offenders == [expected], (
        f"format libs must be isolated to {expected.relative_to(ROOT)}; "
        f"found in: {[p.relative_to(ROOT) for p in offenders]}"
    )


# ---------------------------------------------------------------------------
# Exporter port vs implementation placement
# ---------------------------------------------------------------------------

def _class_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }


def test_artifact_exporter_port_in_domain():
    """The ArtifactExporter port (Protocol) is defined in Domain."""
    path = APP / "domain" / "artifact_exporter.py"
    assert "ArtifactExporter" in _class_defs(path), (
        "ArtifactExporter Protocol must live in app/domain/artifact_exporter.py"
    )


def test_office_exporter_impl_only_in_infrastructure():
    """OfficeArtifactExporter is defined ONLY in Infrastructure, never in
    Application or Domain."""
    impl_path = APP / "infrastructure" / "artifact" / "exporters.py"
    assert "OfficeArtifactExporter" in _class_defs(impl_path), (
        "OfficeArtifactExporter must be implemented in "
        "app/infrastructure/artifact/exporters.py"
    )
    # No Application/Domain file may define the implementation.
    for py in [*APP.glob("application/*.py"), *APP.glob("domain/*.py")]:
        assert "OfficeArtifactExporter" not in _class_defs(py), (
            f"{py.relative_to(ROOT)} must not define OfficeArtifactExporter "
            "(implementation belongs to Infrastructure)"
        )


# ---------------------------------------------------------------------------
# Content-profile probe placement
# ---------------------------------------------------------------------------

def test_content_profile_probe_in_application():
    """The artifact content-profile probe lives in Application (not Domain/
    Infrastructure). It classifies content kind/mime for export routing."""
    probe = APP / "application" / "artifact_content_profile.py"
    assert probe.is_file(), (
        "content-profile probe must live at app/application/artifact_content_profile.py"
    )


def test_content_profile_probe_not_in_domain_or_infrastructure():
    """No content-profile probe module in Domain or Infrastructure layers."""
    candidates = [
        APP / "domain" / "artifact_content_profile.py",
        APP / "infrastructure" / "artifact_content_profile.py",
        APP / "infrastructure" / "artifact" / "artifact_content_profile.py",
    ]
    present = [str(p.relative_to(ROOT)) for p in candidates if p.is_file()]
    assert not present, (
        f"content-profile probe must not live in Domain/Infrastructure: {present}"
    )
