from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from app.domain.config_bundle import (
    BUNDLE_SCHEMA_VERSION,
    SECTION_NAMES,
    BundleSource,
    ConfigBundle,
    ImportMode,
)
from app.interfaces.cli.render import (
    make_console,
    render_data,
    resolve_format,
)


_SECRET_MARKERS = ("api_key", "secret", "password", "token")

# Exit codes shared with the host-side migration helper (plan T6 S10):
# 0 ok, 1 partial failure reported by the service, 2 format/argument error.
_EXIT_ARGUMENT_ERROR = 2

# Sections whose natural keys the host importer may pre-scan and hand back.
_PRE_EXISTING_SECTIONS = frozenset(SECTION_NAMES)


def _load_settings() -> Any:
    from app.main import build_application_services

    return build_application_services().settings


def _load_config_bundle_service(*, read_only: bool) -> Any:
    """Dedicated bundle assembly -- never the regular application bootstrap.

    Deferred import for the same reason as ``_load_settings``: the CLI layer
    must not hold a module-level dependency on the composition root, and the
    Interfaces layer must not reach into Infrastructure at all.
    """
    from app.main import build_config_bundle_services

    return build_config_bundle_services(read_only=read_only)


def _is_secret_field(name: str) -> bool:
    lower = name.lower()
    return any(marker in lower for marker in _SECRET_MARKERS) or lower.endswith("_key")


def _to_dict(settings: Any) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for name in dir(settings):
        if name.startswith("_"):
            continue
        value = getattr(settings, name)
        if callable(value):
            continue
        if _is_secret_field(name):
            obj[f"{name}_present"] = bool(value)
        else:
            obj[name] = _coerce(value)
    return obj


def _coerce(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


def run(args) -> int:
    """Sync entry point (matches the repo convention, see commands/provider.py):
    the async bundle service is driven by asyncio.run inside each subcommand, so
    every caller can keep asserting on a plain int."""
    command = getattr(args, "config_command", None)
    if command == "export":
        return _cmd_export(args)
    if command == "import":
        return _cmd_import(args)
    return _cmd_show(args)


def _cmd_show(args) -> int:
    settings = _load_settings()
    obj = _to_dict(settings)
    if args.section:
        prefix = args.section.lower()
        obj = {k: v for k, v in obj.items() if k.lower().startswith(prefix)}
    render_data(obj, make_console(), fmt=resolve_format(args))
    return 0


# ---------------------------------------------------------------------------
# config export
# ---------------------------------------------------------------------------


def _diagnostic(message: str) -> None:
    """Every diagnostic goes to stderr: in --stdout mode stdout must stay a
    single parseable JSON document."""
    print(message, file=sys.stderr, flush=True)


def _bundle_source() -> BundleSource:
    """Deployment identity of the exporting machine. The two roots are the
    compose variables introduced in T1; absent values stay empty strings rather
    than guessing a path."""
    return BundleSource(
        hostname=socket.gethostname(),
        install_root=os.environ.get("N_AGENT_INSTALL_ROOT", ""),
        code_root=os.environ.get("N_AGENT_CODE_ROOT", ""),
    )


def _bundle_to_dict(bundle: ConfigBundle) -> dict[str, Any]:
    return {
        "schema_version": bundle.schema_version,
        "created_at": bundle.created_at,
        "source": {
            "hostname": bundle.source.hostname,
            "install_root": bundle.source.install_root,
            "code_root": bundle.source.code_root,
        },
        "redacted": bundle.redacted,
        "sections": bundle.sections,
    }


def _write_0600(path: Path, document: str) -> None:
    """Create the file exclusively at 0600. O_EXCL closes the race the
    pre-check above cannot: another process must not win the path between the
    existence check and the open."""
    previous_umask = os.umask(0o077)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.write("\n")
    finally:
        os.umask(previous_umask)
    os.chmod(path, 0o600)


def _cmd_export(args) -> int:
    redact = bool(getattr(args, "redact_secrets", False))
    out = getattr(args, "out", None)
    target = Path(out) if out else None
    if target is not None and target.exists():
        _diagnostic(f"error: refusing to overwrite an existing file: {target}")
        return _EXIT_ARGUMENT_ERROR

    service = _load_config_bundle_service(read_only=True)
    try:
        bundle = asyncio.run(
            service.export_bundle(redact_secrets=redact, source=_bundle_source())
        )
    except Exception as exc:
        _diagnostic(f"error: {type(exc).__name__}: {exc}")
        return 1

    document = json.dumps(_bundle_to_dict(bundle), ensure_ascii=False, indent=2)
    if redact:
        _diagnostic(
            "warning: secrets are redacted; the importer will report every "
            "secret carrier as degraded and keep the target value"
        )
    else:
        _diagnostic(
            "warning: this bundle carries plaintext secrets; keep it at 0600 "
            "and delete it once the migration is done"
        )

    if target is None:
        print(document, flush=True)
        return 0
    try:
        _write_0600(target, document)
    except OSError as exc:
        _diagnostic(f"error: cannot write {target}: {exc}")
        return _EXIT_ARGUMENT_ERROR
    _diagnostic(f"written: {target} (0600)")
    return 0


# ---------------------------------------------------------------------------
# config import
# ---------------------------------------------------------------------------


def _resolve_report_format(args) -> str:
    """--format is the spec spelling, --json/--form/--yaml the repo one; the
    parser already rejects conflicting combinations, so this only maps."""
    explicit = getattr(args, "format", None)
    if explicit == "table":
        return "form"
    if explicit == "json":
        return "json"
    return resolve_format(args)


def _read_import_document(args) -> tuple[Any | None, int]:
    if getattr(args, "stdin", False):
        raw = sys.stdin.read()
    else:
        path = getattr(args, "file", None)
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            _diagnostic(f"error: cannot read {path}: {exc}")
            return None, _EXIT_ARGUMENT_ERROR
    try:
        return json.loads(raw), 0
    except json.JSONDecodeError as exc:
        _diagnostic(f"error: input is not valid JSON: {exc}")
        return None, _EXIT_ARGUMENT_ERROR


def _split_internal_envelope(document: Any) -> tuple[Any, dict[str, list[str]] | None, int]:
    """Validate the internal `{bundle, pre_existing}` envelope.

    The host importer pre-scans the target before the bundle lands, so records
    the first scan just created are NOT pre-existing. The envelope is the only
    transport for that context: no docker cp, no container temp file, no env
    variable.
    """
    if not isinstance(document, dict) or set(document) != {"bundle", "pre_existing"}:
        _diagnostic(
            "error: --internal-context expects a {\"bundle\": ..., "
            "\"pre_existing\": ...} envelope"
        )
        return None, None, _EXIT_ARGUMENT_ERROR
    pre_existing = document["pre_existing"]
    if not isinstance(pre_existing, dict):
        _diagnostic("error: pre_existing must be an object of section -> natural keys")
        return None, None, _EXIT_ARGUMENT_ERROR
    for section, keys in pre_existing.items():
        if section not in _PRE_EXISTING_SECTIONS:
            _diagnostic(f"error: pre_existing names an unknown section: {section}")
            return None, None, _EXIT_ARGUMENT_ERROR
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            _diagnostic(f"error: pre_existing[{section}] must be a list of strings")
            return None, None, _EXIT_ARGUMENT_ERROR
    return document["bundle"], pre_existing, 0


def _bundle_from_dict(payload: Any) -> ConfigBundle | None:
    if not isinstance(payload, dict):
        _diagnostic("error: the bundle must be a JSON object")
        return None
    source = payload.get("source") or {}
    if not isinstance(source, dict):
        _diagnostic("error: bundle.source must be an object")
        return None
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        _diagnostic("error: bundle.sections must be an object")
        return None
    return ConfigBundle(
        schema_version=payload.get("schema_version", BUNDLE_SCHEMA_VERSION),
        created_at=str(payload.get("created_at", "")),
        source=BundleSource(
            hostname=str(source.get("hostname", "")),
            install_root=str(source.get("install_root", "")),
            code_root=str(source.get("code_root", "")),
        ),
        redacted=bool(payload.get("redacted", False)),
        sections=sections,
    )


_REPORT_HEADERS = ["section", "natural_key", "outcome", "action", "reasons"]


def _emit_report(report: Any, fmt: str) -> None:
    document = report.to_dict()
    if fmt == "json":
        # Exactly one line, exactly one JSON document: the host helper parses
        # stdout with `python3 -c` and reads counts + items[].action from it.
        print(json.dumps(document, ensure_ascii=False), flush=True)
        return
    if fmt == "yaml":
        render_data(document, make_console(), fmt="yaml")
        return
    rows = [dict(item, reasons="; ".join(item["reasons"])) for item in document["items"]]
    render_data(rows, make_console(), fmt="form", headers=_REPORT_HEADERS)
    counts = ", ".join(f"{name}={value}" for name, value in document["counts"].items())
    _diagnostic(f"mode={document['mode']} exit_code={document['exit_code']} {counts}")


def _cmd_import(args) -> int:
    internal = bool(getattr(args, "internal_context", False))
    use_stdin = bool(getattr(args, "stdin", False))
    if internal and not use_stdin:
        _diagnostic("error: --internal-context is only allowed together with --stdin")
        return _EXIT_ARGUMENT_ERROR

    document, error = _read_import_document(args)
    if error:
        return error

    pre_existing: dict[str, list[str]] | None = None
    if internal:
        document, pre_existing, error = _split_internal_envelope(document)
        if error:
            return error

    bundle = _bundle_from_dict(document)
    if bundle is None:
        return _EXIT_ARGUMENT_ERROR

    try:
        mode = ImportMode(getattr(args, "mode", "merge") or "merge")
    except ValueError:
        _diagnostic(f"error: unknown import mode: {getattr(args, 'mode', None)}")
        return _EXIT_ARGUMENT_ERROR

    dry_run = bool(getattr(args, "dry_run", False))
    # A dry run is a preview and must not be able to write, so it gets the
    # read-only assembly; only a real import gets the write one.
    service = _load_config_bundle_service(read_only=dry_run)
    try:
        report = asyncio.run(
            service.import_bundle(
                bundle,
                mode=mode,
                with_schedules=bool(getattr(args, "with_schedules", False)),
                dry_run=dry_run,
                pre_existing=pre_existing,
            )
        )
    except Exception as exc:
        # Validation errors carry a section and a reason, never a secret.
        _diagnostic(f"error: {type(exc).__name__}: {exc}")
        return _EXIT_ARGUMENT_ERROR

    _emit_report(report, _resolve_report_format(args))
    return report.exit_code
