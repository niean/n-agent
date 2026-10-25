"""Architecture boundary tests for the observability signals (T10).

AST-scanned rules (spec-261024-otel-observability "Error Handling 与约束":
"架构 AST 测试覆盖直接与 from-import，不靠文本匹配"):

1. Domain layer has ZERO opentelemetry imports (any form).
2. Application layer forbids opentelemetry-sdk / exporter / instrumentation
   packages and forbids app.infrastructure imports.
3. ``app/application/telemetry_semantics.py`` only depends on stdlib and
   ``opentelemetry-api`` (top-level ``opentelemetry`` namespaces); it never
   imports business modules or any SDK subpackage.
4. ``app/infrastructure/observability/`` never imports business modules
   (``app.domain`` / ``app.interfaces`` / ``app.application`` except the
   semantics registry ``app.application.telemetry_semantics``).

Both ``import x`` and ``from x import y`` forms are covered, for every
``.py`` file in the respective layer.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

DOMAIN_DIR = APP / "domain"
APPLICATION_DIR = APP / "application"
OBSERVABILITY_DIR = APP / "infrastructure" / "observability"

# opentelemetry namespaces that count as "API only" (no SDK / exporter /
# instrumentation subpackages).
OPENTELEMETRY_API_MODULES = (
    "opentelemetry",
    "opentelemetry.trace",
    "opentelemetry.metrics",
    "opentelemetry.context",
    "opentelemetry.util",
)

# SDK-side opentelemetry packages forbidden in the Application layer.
OPENTELEMETRY_SDK_PREFIXES = (
    "opentelemetry.sdk",
    "opentelemetry.exporter",
    "opentelemetry.instrumentation",
)

# The single Application module the observability Infrastructure package is
# allowed to import (the semantics registry, not a business module).
ALLOWED_APPLICATION_IMPORT = "app.application.telemetry_semantics"


def _python_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _import_entries(path: Path) -> list[tuple[str, str | None]]:
    """Import statements of ``path`` as (module, bound_name) entries.

    ``import a.b`` -> ("a.b", None); ``from a.b import c`` -> ("a.b", "c");
    ``from a.b import *`` -> ("a.b", "*"). Relative imports (level > 0) stay
    package-internal and are skipped: they can never match the absolute
    rules below."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # pragma: no cover - malformed source
        raise AssertionError(f"cannot parse {path}: {exc}") from exc
    entries: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                entries.append((alias.name, None))
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            for alias in node.names:
                entries.append((node.module, alias.name))
    return entries


def _bound_module(entry: tuple[str, str | None]) -> str:
    """The absolute module effectively bound by an import entry.

    ``from a.b import c`` binds ``a.b.c`` (so a bare package import cannot
    hide which submodule is actually used); ``from a.b import *`` binds the
    package itself (conservatively treated as the whole package)."""
    module, name = entry
    if name is None or name == "*":
        return module
    return f"{module}.{name}"


def _matches_any(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".") for prefix in prefixes
    )


def _violations(path: Path, prefixes: tuple[str, ...]) -> list[str]:
    offenders = []
    for entry in _import_entries(path):
        bound = _bound_module(entry)
        if _matches_any(bound, prefixes):
            offenders.append(bound)
    return offenders


# ---------------------------------------------------------------------------
# 1. Domain: zero opentelemetry imports
# ---------------------------------------------------------------------------


def test_domain_layer_exists():
    assert DOMAIN_DIR.is_dir(), "app/domain not found"
    assert _python_files(DOMAIN_DIR), "no python files under app/domain"


def test_domain_has_zero_opentelemetry_imports():
    offenders = _violations_for_tree(DOMAIN_DIR, ("opentelemetry",))
    assert not offenders, (
        "app/domain must not import opentelemetry (domain is "
        f"observability-free): {offenders}"
    )


# ---------------------------------------------------------------------------
# 2. Application: no SDK/exporter/instrumentation, no Infrastructure
# ---------------------------------------------------------------------------


def test_application_layer_exists():
    assert APPLICATION_DIR.is_dir()
    assert _python_files(APPLICATION_DIR)


def test_application_forbids_opentelemetry_sdk_exporter_instrumentation():
    offenders = _violations_for_tree(APPLICATION_DIR, OPENTELEMETRY_SDK_PREFIXES)
    assert not offenders, (
        "app/application must not import opentelemetry SDK/exporter/"
        f"instrumentation (application is API-only): {offenders}"
    )


def test_application_forbids_infrastructure_imports():
    offenders = _violations_for_tree(APPLICATION_DIR, ("app.infrastructure",))
    assert not offenders, (
        "app/application must not import app.infrastructure "
        f"(DDD dependency direction): {offenders}"
    )


# ---------------------------------------------------------------------------
# 3. telemetry_semantics: stdlib + opentelemetry-api only
# ---------------------------------------------------------------------------

SEMANTICS_PATH = APPLICATION_DIR / "telemetry_semantics.py"


def test_telemetry_semantics_module_exists():
    assert SEMANTICS_PATH.is_file(), (
        "app/application/telemetry_semantics.py not found"
    )


def test_telemetry_semantics_imports_only_api_and_stdlib():
    # The pure content projector is the sole permitted Application dependency.
    app_offenders = [name for name in _violations(SEMANTICS_PATH, ("app",))
                     if not _matches_any(name, ("app.application.telemetry_details",))]
    assert not app_offenders, (
        "telemetry_semantics must not import business modules: "
        f"{app_offenders}"
    )
    otel_offenders = [
        bound
        for entry in _import_entries(SEMANTICS_PATH)
        if _matches_any(bound := _bound_module(entry), ("opentelemetry",))
        and not _matches_any(bound, OPENTELEMETRY_API_MODULES)
    ]
    assert not otel_offenders, (
        "telemetry_semantics may only use opentelemetry-api top-level "
        f"namespaces {OPENTELEMETRY_API_MODULES}: {otel_offenders}"
    )


# ---------------------------------------------------------------------------
# 4. observability/ infrastructure: no business imports
# ---------------------------------------------------------------------------


def test_observability_package_exists():
    assert OBSERVABILITY_DIR.is_dir()
    assert _python_files(OBSERVABILITY_DIR)


def _violations_for_tree(directory: Path, prefixes: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for path in _python_files(directory):
        for offender in _violations(path, prefixes):
            found.append(f"{path.relative_to(ROOT)}: {offender}")
    return found


def test_observability_never_imports_business_modules():
    offenders: list[str] = []
    for path in _python_files(OBSERVABILITY_DIR):
        for entry in _import_entries(path):
            bound = _bound_module(entry)
            if _matches_any(bound, ("app.domain", "app.interfaces")):
                offenders.append(f"{path.relative_to(ROOT)}: {bound}")
            elif _matches_any(bound, ("app.application",)):
                if not _matches_any(bound, (ALLOWED_APPLICATION_IMPORT,)):
                    offenders.append(f"{path.relative_to(ROOT)}: {bound}")
    assert not offenders, (
        "observability infrastructure must not import business modules "
        f"(only {ALLOWED_APPLICATION_IMPORT} and app.config allowed): "
        f"{offenders}"
    )


def test_observability_imports_stay_within_config_semantics_and_self():
    """Belt-and-braces: every app.* import of the observability package is
    app.config, the semantics registry, or the package itself."""
    offenders: list[str] = []
    for path in _python_files(OBSERVABILITY_DIR):
        for entry in _import_entries(path):
            bound = _bound_module(entry)
            if not (bound == "app" or bound.startswith("app.")):
                continue
            allowed = _matches_any(
                bound,
                (
                    "app.config",
                    ALLOWED_APPLICATION_IMPORT,
                    "app.infrastructure.observability",
                ),
            )
            if not allowed:
                offenders.append(f"{path.relative_to(ROOT)}: {bound}")
    assert not offenders, (
        f"observability app.* imports must stay within app.config / "
        f"{ALLOWED_APPLICATION_IMPORT} / app.infrastructure.observability: "
        f"{offenders}"
    )


def test_telemetry_details_imports_only_stdlib():
    import sys
    path = APPLICATION_DIR / "telemetry_details.py"
    assert path.is_file()
    offenders = [bound for entry in _import_entries(path)
                 if (bound := _bound_module(entry)).split(".")[0] not in sys.stdlib_module_names]
    assert not offenders, f"pure telemetry projector has non-stdlib imports: {offenders}"
