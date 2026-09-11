#!/usr/bin/env python3
"""Deployment-file helpers for the config bundle host scripts.

Deliberately standalone: this module is executed by ``docker/config-export.sh``
and ``docker/config-import.sh`` on the host, so it must never import ``app.*``
and must never require a third-party dependency. Only the standard library.

Sub-commands:
    check-deps                       -- interpreter/stdlib sanity check
    parse-env <file>                 -- dotenv text  -> JSON object on stdout
    get-env <file> <KEY>             -- one value on stdout, exit 1 when absent
    serialize-env                    -- JSON object on stdin -> dotenv on stdout
    strip-secrets                    -- dotenv on stdin -> dotenv with secret
                                        values cleared, layout preserved
    manifest <staging> ...           -- write <staging>/manifest.json at 0600
    digest <dir>                     -- JSON relpath -> sha256 for a tree
    fingerprint [--exclude N] <p>... -- JSON snapshot of source files/dirs
    db-summary <config.json>         -- schema_version + per-section counts

Import side (plan T7):
    preflight <bundle>               -- stream-validate a bundle, never unpack
    host-check ...                   -- refuse mount/policy conflicts before any write
    host-apply <bundle> ...          -- land the host files (a script carried by
                                        the bundle is never executed)
    pre-existing <export.json>       -- target export -> section -> natural keys
    db-envelope <bundle> ...         -- the {bundle, pre_existing} stdin payload
    edit-env <file> <KEY> [--value]  -- set/remove one key, keep the layout
    restart-needed <report.json>     -- "yes" when any item action != none
    report-summary <report.json>     -- human-readable per-item report
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath


BUNDLE_SCHEMA_VERSION = 1

# A key names a credential when it matches one of these markers, unless it ends
# in _PATH: N_AGENT_HOST_TERMINAL_TOKEN_PATH and
# N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH are container paths, not secrets.
_SECRET_MARKERS = ("_API_KEY", "_SECRET", "_TOKEN", "PASSWORD", "ACCESS_KEY", "PRIVATE_KEY")

_UNQUOTED_SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


# ---------------------------------------------------------------------------
# dotenv parsing / serialization
# ---------------------------------------------------------------------------


class Record:
    """One logical line of a dotenv file, keeping its raw text.

    Keeping the raw span is what lets strip-secrets rewrite only the secret
    assignments while leaving comments, blank lines and formatting untouched.
    """

    __slots__ = ("kind", "raw", "key", "value")

    def __init__(self, kind: str, raw: str, key: str | None = None, value: str | None = None):
        self.kind = kind          # "raw" | "assignment"
        self.raw = raw
        self.key = key
        self.value = value


def _unescape_double(body: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            following = body[index + 1]
            mapping = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "$": "$", "'": "'"}
            if following in mapping:
                out.append(mapping[following])
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def scan_env(text: str) -> list[Record]:
    records: list[Record] = []
    index = 0
    length = len(text)
    while index < length:
        line_end = text.find("\n", index)
        if line_end == -1:
            line_end = length
        line = text[index:line_end]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            records.append(Record("raw", text[index:min(line_end + 1, length)]))
            index = line_end + 1
            continue

        body = line
        if body.lstrip().startswith("export "):
            body = body.lstrip()[len("export "):]
        key, separator, value_part = body.partition("=")
        key = key.strip()
        if not separator or not _KEY.match(key):
            records.append(Record("raw", text[index:min(line_end + 1, length)]))
            index = line_end + 1
            continue

        value_part = value_part.lstrip()
        raw_end = line_end
        if value_part[:1] in ('"', "'"):
            quote = value_part[0]
            # Find the closing quote, possibly on a later physical line.
            offset = text.find("=", index) + 1
            while offset < length and text[offset] in " \t":
                offset += 1
            cursor = offset + 1
            while cursor < length:
                if text[cursor] == "\\" and quote == '"':
                    cursor += 2
                    continue
                if text[cursor] == quote:
                    break
                cursor += 1
            body_text = text[offset + 1:cursor]
            value = _unescape_double(body_text) if quote == '"' else body_text
            raw_end = text.find("\n", cursor)
            if raw_end == -1:
                raw_end = length
        else:
            value = value_part
            # An inline comment only starts after whitespace, so "has#hash"
            # keeps its hash.
            hash_index = value.find(" #")
            if hash_index != -1:
                value = value[:hash_index]
            value = value.rstrip()

        raw = text[index:min(raw_end + 1, length)]
        records.append(Record("assignment", raw, key, value))
        index = raw_end + 1
    return records


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for record in scan_env(text):
        if record.kind == "assignment":
            values[record.key] = record.value
    return values


def serialize_value(value: str) -> str:
    if value == "":
        return ""
    if _UNQUOTED_SAFE.match(value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def serialize_env(values: dict[str, str]) -> str:
    return "".join(f"{key}={serialize_value(value)}\n" for key, value in values.items())


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    if upper.endswith("_PATH"):
        return False
    return any(marker in upper for marker in _SECRET_MARKERS)


def strip_secrets(text: str) -> str:
    out: list[str] = []
    for record in scan_env(text):
        if record.kind == "assignment" and is_secret_key(record.key):
            out.append(f"{record.key}=\n")
        else:
            out.append(record.raw)
    return "".join(out)


# ---------------------------------------------------------------------------
# hashing / manifest
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_tree(root: Path, *, skip: set[str] | None = None) -> dict[str, str]:
    skip = skip or set()
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in skip:
            continue
        entries[relative] = sha256_file(path)
    return entries


def fingerprint(paths: list[str], excludes: set[str]) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for raw in paths:
        path = Path(raw)
        if path.is_symlink() or not path.exists():
            snapshot[raw] = {"type": "missing" if not path.exists() else "symlink"}
        elif path.is_file():
            snapshot[raw] = {"type": "file", "sha256": sha256_file(path)}
        elif path.is_dir():
            entries: dict[str, str] = {}
            for child in sorted(path.rglob("*")):
                if any(part in excludes for part in child.relative_to(path).parts):
                    continue
                relative = child.relative_to(path).as_posix()
                if child.is_symlink():
                    entries[relative] = "symlink:" + os.readlink(child)
                elif child.is_file():
                    entries[relative] = sha256_file(child)
                elif child.is_dir():
                    entries[relative + "/"] = "dir"
            snapshot[raw] = {"type": "dir", "entries": entries}
        else:
            snapshot[raw] = {"type": "special"}
    return snapshot


def write_0600(path: Path, text: str) -> None:
    previous = os.umask(0o077)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _cmd_check_deps(_args) -> int:
    if sys.version_info < (3, 8):
        print("error: python3 >= 3.8 is required", file=sys.stderr)
        return 3
    return 0


def _cmd_parse_env(args) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    json.dump(parse_env(text), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _cmd_get_env(args) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    values = parse_env(text)
    if args.key not in values:
        return 1
    sys.stdout.write(values[args.key] + "\n")
    return 0


def _cmd_serialize_env(_args) -> int:
    values = json.load(sys.stdin)
    sys.stdout.write(serialize_env({str(k): str(v) for k, v in values.items()}))
    return 0


def _cmd_strip_secrets(_args) -> int:
    sys.stdout.write(strip_secrets(sys.stdin.read()))
    return 0


def _cmd_manifest(args) -> int:
    staging = Path(args.staging)
    files = digest_tree(staging, skip={"manifest.json"})
    manifest = {
        "schema_version": args.schema_version,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "hostname": args.hostname,
            "install_root": args.install_root,
            "code_root": args.code_root,
        },
        "redacted": args.redacted == "true",
        "files": files,
    }
    write_0600(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return 0


def _cmd_digest(args) -> int:
    json.dump(digest_tree(Path(args.dir)), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_fingerprint(args) -> int:
    json.dump(fingerprint(args.paths, set(args.exclude or [])), sys.stdout,
              ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_db_summary(args) -> int:
    try:
        document = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: unreadable DB section: {type(exc).__name__}", file=sys.stderr)
        return 1
    if document.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        print(f"error: unexpected schema_version: {document.get('schema_version')!r}",
              file=sys.stderr)
        return 1
    sections = document.get("sections")
    if not isinstance(sections, dict) or not sections:
        print("error: the DB export carries no sections", file=sys.stderr)
        return 1
    for name in sorted(sections):
        payload = sections[name]
        if payload is None:
            count = "null"
        elif isinstance(payload, list):
            count = str(len(payload))
        else:
            count = "1"
        print(f"{name}={count}")
    return 0



# ---------------------------------------------------------------------------
# import side (plan T7): bundle preflight
#
# Nothing below ever executes a file carried by the bundle, and nothing is
# written to the target before every check has passed.
# ---------------------------------------------------------------------------

# Natural-key field per collection section. Mirrors the authoritative list in
# app/application/config_bundle_service.py; the host must not import app.*, so
# it is restated here and kept in sync by tests/test_config_bundle_scripts.py.
_NATURAL_KEYS = {
    "providers": "name",
    "knowledge_bases": "id",
    "mcp_sites": "name",
    "external_memory_providers": "name",
    "plugins": "key",
    "skills": "name",
    "scheduled_tasks": "name",
    "gateway_home_targets": "platform",
}
_SINGLETON_SECTIONS = ("external_memory_global_config", "task_config")
_SECTION_NAMES = tuple(_NATURAL_KEYS) + _SINGLETON_SECTIONS

# Spec hard caps. The two env seams may only tighten them, never raise them.
HARD_MAX_MEMBERS = 100_000
HARD_MAX_FILE_BYTES = 256 * 1024 * 1024
HARD_MAX_TOTAL_BYTES = 1024 * 1024 * 1024

# Members whose bytes the checks need; anything larger is hashed streaming.
_BUFFER_LIMIT = 32 * 1024 * 1024
_NESTED_ARCHIVES = ("workspace/skills.tar.gz", "workspace/plugins.tar.gz")
_BUNDLED_SCRIPTS = ("install.sh",)


class PreflightError(Exception):
    """A bundle that must be refused; the message never carries a value."""


class _Budget:
    """One shared quota for the outer archive and every nested archive."""

    def __init__(self, max_members: int, max_file_bytes: int, max_total_bytes: int):
        self.max_members = max_members
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.members = 0
        self.total = 0

    def account(self, where: str, name: str, size: int) -> None:
        self.members += 1
        if self.members > self.max_members:
            raise PreflightError(
                f"{where}: too many members (limit {self.max_members})")
        if size > self.max_file_bytes:
            raise PreflightError(
                f"{where}: member {name} is larger than {self.max_file_bytes} bytes")
        self.total += size
        if self.total > self.max_total_bytes:
            raise PreflightError(
                f"{where}: uncompressed total exceeds {self.max_total_bytes} bytes")


def normalize_member(name: str, *, where: str) -> str:
    """Reject anything that could land outside the extraction root."""
    if not name or name in (".", "./"):
        raise PreflightError(f"{where}: empty member name")
    if "\x00" in name or "\\" in name:
        raise PreflightError(f"{where}: member name is not a plain posix path")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise PreflightError(f"{where}: absolute member path is not allowed: {name}")
    parts = []
    for part in PurePosixPath(name).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise PreflightError(f"{where}: path traversal in member: {name}")
        parts.append(part)
    if not parts:
        raise PreflightError(f"{where}: member name normalises to nothing: {name}")
    return "/".join(parts)


def _reject_special(info, *, where: str) -> None:
    if info.issym() or info.islnk():
        kind = "symlink" if info.issym() else "hard link"
        raise PreflightError(f"{where}: {kind} member is not allowed: {info.name}")
    if info.ischr() or info.isblk() or info.isfifo() or info.isdev():
        raise PreflightError(f"{where}: device/fifo member is not allowed: {info.name}")
    if not (info.isreg() or info.isdir()):
        raise PreflightError(f"{where}: unsupported member type: {info.name}")


def _walk_archive(stream, *, where: str, budget: _Budget,
                  keep: tuple[str, ...] = ()) -> tuple[dict[str, str], dict[str, bytes]]:
    """Stream one tar.gz, validating every member. Returns (sha256 per regular
    member, buffered bytes for the members named in ``keep``)."""
    digests: dict[str, str] = {}
    buffered: dict[str, bytes] = {}
    seen: set[str] = set()
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for info in archive:
            _reject_special(info, where=where)
            if info.isdir() and info.name.rstrip("/") in ("", "."):
                # `tar -czf <out> -C <staging> .` -- which is how
                # docker/config-export.sh packs -- emits a "./" entry for the
                # archive root itself. It names no path and cannot escape the
                # extraction root, so it is not an unnamed member. Charge it to
                # the budget anyway so it cannot be repeated to pad a
                # member-count attack, then skip it.
                budget.account(where, ".", 0)
                continue
            arcname = normalize_member(info.name, where=where)
            if arcname in seen:
                raise PreflightError(f"{where}: duplicate member: {arcname}")
            seen.add(arcname)
            if info.isdir():
                budget.account(where, arcname, 0)
                continue
            budget.account(where, arcname, info.size)
            handle = archive.extractfile(info)
            if handle is None:
                raise PreflightError(f"{where}: member has no content: {arcname}")
            digest = hashlib.sha256()
            want = arcname in keep and info.size <= _BUFFER_LIMIT
            chunks: list[bytes] = []
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if want:
                    chunks.append(chunk)
            digests[arcname] = digest.hexdigest()
            if want:
                buffered[arcname] = b"".join(chunks)
    return digests, buffered


def _require_aware_timestamp(value) -> None:
    if not isinstance(value, str):
        raise PreflightError("manifest: created_at must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PreflightError(f"manifest: created_at is not ISO 8601: {exc}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise PreflightError("manifest: created_at must carry a UTC offset")


def _check_db_section(payload: bytes, schema_version: int) -> None:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"db/config.json: not readable JSON: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise PreflightError("db/config.json: top level must be an object")
    if document.get("schema_version") != schema_version:
        raise PreflightError(
            "db/config.json: schema_version disagrees with manifest.json "
            f"({document.get('schema_version')!r} != {schema_version!r})")
    sections = document.get("sections")
    if not isinstance(sections, dict):
        raise PreflightError("db/config.json: sections must be an object")
    for name in _SECTION_NAMES:
        if name not in sections:
            raise PreflightError(f"db/config.json: section is missing: {name}")
    for name in _SINGLETON_SECTIONS:
        value = sections[name]
        if value is not None and not isinstance(value, dict):
            raise PreflightError(f"db/config.json: {name} must be an object or null")
    for name, key_field in _NATURAL_KEYS.items():
        rows = sections[name]
        if not isinstance(rows, list):
            raise PreflightError(f"db/config.json: {name} must be a list")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise PreflightError(f"db/config.json: {name} rows must be objects")
            key = row.get(key_field)
            if not isinstance(key, str) or not key:
                raise PreflightError(
                    f"db/config.json: {name} rows need a non-empty {key_field}")
            if key in seen:
                raise PreflightError(
                    f"db/config.json: {name} has a duplicate natural key {key_field}")
            seen.add(key)
    active = [row for row in sections["providers"] if row.get("is_active")]
    if len(active) > 1:
        raise PreflightError("db/config.json: at most one provider may be active")


def _check_env_member(arcname: str, payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PreflightError(f"{arcname}: not valid UTF-8") from exc
    for record in scan_env(text):
        if record.kind != "assignment":
            continue
        if "\n" in record.value or "\r" in record.value:
            # Compose cannot represent it and `docker compose` would silently
            # truncate; the key name is safe to print, the value never is.
            raise PreflightError(
                f"{arcname}: value of {record.key} contains a newline and cannot "
                "be represented in a Compose env file")


def preflight_bundle(path: Path, *, max_members: int, max_file_bytes: int,
                     max_total_bytes: int) -> dict:
    if max_members > HARD_MAX_MEMBERS:
        raise PreflightError(
            f"member limit {max_members} is above the hard cap {HARD_MAX_MEMBERS}")
    if max_file_bytes > HARD_MAX_FILE_BYTES:
        raise PreflightError(
            f"file size limit {max_file_bytes} is above the hard cap {HARD_MAX_FILE_BYTES}")
    if max_total_bytes > HARD_MAX_TOTAL_BYTES:
        raise PreflightError(
            f"total size limit {max_total_bytes} is above the hard cap {HARD_MAX_TOTAL_BYTES}")
    if not path.is_file():
        raise PreflightError(f"bundle is not a readable file: {path}")

    budget = _Budget(max_members, max_file_bytes, max_total_bytes)
    keep = ("manifest.json", "db/config.json", "env/docker.env",
            "env/install-root.env") + _NESTED_ARCHIVES
    try:
        with path.open("rb") as stream:
            digests, buffered = _walk_archive(stream, where="bundle", budget=budget,
                                              keep=keep)
    except tarfile.TarError as exc:
        raise PreflightError(f"bundle is not a readable tar.gz: {type(exc).__name__}") from exc

    if "manifest.json" not in digests:
        raise PreflightError("bundle: manifest.json is missing")
    raw_manifest = buffered.get("manifest.json")
    if raw_manifest is None:
        raise PreflightError("bundle: manifest.json is unreasonably large")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"manifest.json: not readable JSON: {type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise PreflightError("manifest.json: top level must be an object")

    schema_version = manifest.get("schema_version")
    if schema_version != BUNDLE_SCHEMA_VERSION:
        raise PreflightError(
            f"manifest.json: unsupported schema_version {schema_version!r} "
            f"(this build understands {BUNDLE_SCHEMA_VERSION})")
    _require_aware_timestamp(manifest.get("created_at"))

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise PreflightError("manifest.json: files must be a non-empty object")
    packed = {name for name in digests if name != "manifest.json"}
    listed = set(files)
    for missing in sorted(packed - listed):
        raise PreflightError(f"manifest.json: packed file is not listed: {missing}")
    for ghost in sorted(listed - packed):
        raise PreflightError(f"manifest.json: listed file is not packed: {ghost}")
    for name in sorted(listed):
        if files[name] != digests[name]:
            raise PreflightError(f"manifest.json: sha256 checksum mismatch for {name}")

    source = manifest.get("source")
    if not isinstance(source, dict):
        raise PreflightError("manifest.json: source must be an object")
    for field in ("install_root", "code_root"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise PreflightError(f"manifest.json: source.{field} must be a non-empty string")

    # Nested archives share the same rules and the same quota.
    for arcname in _NESTED_ARCHIVES:
        if arcname not in digests:
            continue
        payload = buffered.get(arcname)
        if payload is None:
            raise PreflightError(f"{arcname}: too large to validate")
        try:
            _walk_archive(io.BytesIO(payload), where=arcname, budget=budget)
        except tarfile.TarError as exc:
            raise PreflightError(
                f"{arcname}: not a readable tar.gz: {type(exc).__name__}") from exc

    for arcname in ("env/docker.env", "env/install-root.env"):
        if arcname in buffered:
            _check_env_member(arcname, buffered[arcname])
    if "db/config.json" in digests:
        payload = buffered.get("db/config.json")
        if payload is None:
            raise PreflightError("db/config.json: too large to validate")
        _check_db_section(payload, schema_version)

    return {
        "schema_version": schema_version,
        "created_at": manifest.get("created_at"),
        "redacted": bool(manifest.get("redacted")),
        "source": {
            "hostname": source.get("hostname", ""),
            "install_root": source["install_root"],
            "code_root": source["code_root"],
        },
        "members": sorted(digests),
        "member_count": budget.members,
        "total_bytes": budget.total,
        "has_db": "db/config.json" in digests,
        "carries_script": sorted(name for name in _BUNDLED_SCRIPTS if name in digests),
    }


# ---------------------------------------------------------------------------
# import side: host landing
# ---------------------------------------------------------------------------

# Files the container mounts read-only. A directory (or a symlink) sitting at
# one of these paths makes `docker compose up` bind-mount a directory into a
# file, so it has to be refused before anything is written.
RO_MOUNT_FILES = (
    "locals/host-terminal-policy.yaml",
    "locals/host-terminal.token",
    "locals/host-browser.token",
)
SKELETON_DIRS = ("locals", "logs", "workspace", "workspace/skills",
                 "workspace/plugins", "browser-profiles", "secrets")
BACKUP_DIRNAME = ".migration-backups"


class HostError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _path_token_remap(text: str, mapping: list[tuple[str, str]]) -> str:
    """Rewrite the source roots only where they start a filesystem path token.

    A token is a run of non-separator characters. ``/old/code-old/x`` and the
    ``/old/code`` embedded inside ``https://host/old/code/api`` both keep the
    original text: the first is not a path-segment boundary, the second does
    not begin the token.
    """
    pattern = re.compile(r"[^\s\"'=,;()\[\]{}<>]+")

    def replace(match: re.Match) -> str:
        token = match.group(0)
        for source, destination in mapping:
            if token == source:
                return destination
            if token.startswith(source + "/"):
                return destination + token[len(source):]
        return token

    return pattern.sub(replace, text)


def _remap_paths(text: str, source: dict, install_root: str, code_root: str) -> str:
    mapping = sorted(
        [(source["install_root"], install_root), (source["code_root"], code_root)],
        key=lambda pair: len(pair[0]), reverse=True,
    )
    mapping = [(old, new) for old, new in mapping if old and old != new]
    return _path_token_remap(text, mapping) if mapping else text


def _merge_env_text(target_text: str, bundle_values: dict[str, str], *,
                    mode: str, forced: dict[str, str]) -> str:
    """Keep the target's layout, comments and key order.

    merge     -- only keys absent from the target are appended
    overwrite -- keys carried by the bundle are replaced in place
    ``forced`` always wins (the two deployment roots are local facts).
    """
    lines: list[str] = []
    seen: set[str] = set()
    for record in scan_env(target_text):
        if record.kind != "assignment":
            lines.append(record.raw)
            continue
        seen.add(record.key)
        replacement = None
        if record.key in forced:
            replacement = forced[record.key]
        elif mode == "overwrite" and record.key in bundle_values:
            replacement = bundle_values[record.key]
        if replacement is not None and replacement != record.value:
            lines.append(f"{record.key}={serialize_value(replacement)}\n")
        else:
            lines.append(record.raw)
    tail = [f"{key}={serialize_value(value)}\n"
            for key, value in bundle_values.items()
            if key not in seen and key not in forced]
    tail += [f"{key}={serialize_value(value)}\n"
             for key, value in forced.items() if key not in seen]
    text = "".join(lines)
    if tail and text and not text.endswith("\n"):
        text += "\n"
    return text + "".join(tail)


class _HostApply:
    def __init__(self, *, install_root: Path, code_root: Path, repo_dir: Path,
                 mode: str, dry_run: bool, source: dict):
        self.install_root = install_root
        self.code_root = code_root
        self.repo_dir = repo_dir
        self.mode = mode
        self.dry_run = dry_run
        self.source = source
        self.changed = False
        self.actions: list[str] = []
        self.notes: list[str] = []
        self._stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # -- reporting ---------------------------------------------------------
    def record(self, what: str, verb: str) -> None:
        self.actions.append(f"{verb} {what}")
        if verb != "unchanged":
            self.changed = True

    # -- backups -----------------------------------------------------------
    def _backup_dir(self) -> Path:
        target = self.install_root / BACKUP_DIRNAME
        if not self.dry_run:
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o700)
        return target

    def _slug(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.install_root).as_posix()
        except ValueError:
            # Outside the install root: the checkout's own docker/.env. Without
            # a prefix it slugs to "env" exactly like <install-root>/.env, and
            # the two -- one of which holds the provider API key -- would share
            # one backup name, so the first copy written is destroyed.
            relative = f"repo-{path.name}"
        # Only structural characters -- never a value, never a key name.
        return relative.replace("/", "-").lstrip(".") or "file"

    def _unique_backup(self, name: str) -> Path:
        """Backups within one run share a timestamp, so the stamp alone cannot
        separate two files that slug alike. Never overwrite an existing copy."""
        directory = self._backup_dir()
        candidate = directory / name
        counter = 2
        while candidate.exists():
            candidate = directory / f"{name}.{counter}"
            counter += 1
        return candidate

    def backup_file(self, path: Path) -> None:
        if self.dry_run or self.mode != "overwrite" or not path.is_file():
            return
        destination = self._unique_backup(f"{self._slug(path)}.bak-{self._stamp}")
        shutil.copyfile(path, destination)
        destination.chmod(0o600)
        self.notes.append(f"backup: {destination}")

    def backup_tree(self, path: Path) -> None:
        if self.dry_run or not path.is_dir():
            return
        destination = self._unique_backup(f"{self._slug(path)}.bak-{self._stamp}.tar.gz")
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(str(path), arcname=path.name)
        destination.chmod(0o600)
        self.notes.append(f"backup: {destination}")

    # -- primitives --------------------------------------------------------
    def put_text(self, path: Path, text: str, *, label: str) -> None:
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == text:
            self.record(label, "unchanged")
            return
        if current is not None:
            self.backup_file(path)
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_0600(path, text)
        self.record(label, "created" if current is None else "updated")

    def put_bytes(self, path: Path, payload: bytes, *, label: str) -> None:
        self.put_text(path, payload.decode("utf-8"), label=label)


def _load_members(bundle: Path) -> dict[str, bytes]:
    """Read the whole (already preflighted) bundle into memory, regular files
    only. Nothing is ever marked executable and nothing is ever run."""
    members: dict[str, bytes] = {}
    with tarfile.open(str(bundle), mode="r:gz") as archive:
        for info in archive:
            if not info.isreg():
                continue
            handle = archive.extractfile(info)
            if handle is None:
                continue
            members[normalize_member(info.name, where="bundle")] = handle.read()
    return members


def _mount_conflicts(install_root: Path) -> list[str]:
    problems: list[str] = []
    for relative in RO_MOUNT_FILES:
        path = install_root / relative
        if path.is_symlink():
            problems.append(f"{path} is a symlink; the read-only mount source "
                            "must be a regular file")
        elif path.is_dir():
            problems.append(f"{path} is a directory; docker created it as a bind "
                            "mount target. Move it aside by hand (never delete it "
                            "recursively) and rerun")
    for relative in SKELETON_DIRS:
        path = install_root / relative
        if path.is_symlink():
            problems.append(f"{path} is a symlink; refusing to write through it")
    return problems


def _policy_text(members: dict[str, bytes], install_root: Path,
                 repo_dir: Path) -> tuple[bytes | None, str]:
    """(content, origin) -- origin is bundle/target/template/missing."""
    if "locals/host-terminal-policy.yaml" in members:
        return members["locals/host-terminal-policy.yaml"], "bundle"
    existing = install_root / "locals" / "host-terminal-policy.yaml"
    if existing.is_file():
        return None, "target"
    template = repo_dir / "docker" / "host-terminal-policy.yaml.example"
    if template.is_file():
        return template.read_bytes(), "template"
    return None, "missing"


def _report_stale_paths(apply: "_HostApply", staging: Path, kind: str,
                        source: dict) -> None:
    """Flag workspace files that still name the source machine's roots.

    The five config files this script owns are remapped by `remap`, but a
    user's own skill and plugin scripts are copied verbatim: rewriting the
    contents of somebody's code is riskier than leaving it alone. The cost of
    that choice is a path that silently resolves to nothing on the new
    machine, so the one thing the importer must not do is stay quiet about it.

    Only file paths are reported -- never a line of the file, which could be
    the very line holding a credential.
    """
    tokens = [token for token in (source.get("install_root"), source.get("code_root"))
              if token and token not in (str(apply.install_root), str(apply.code_root))]
    if not tokens:
        return
    hits = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary payloads carry no path we could act on
        if any(token in text for token in tokens):
            hits.append(f"workspace/{kind}/{path.relative_to(staging).as_posix()}")
    if not hits:
        return
    shown = ", ".join(hits[:5]) + (f" (+{len(hits) - 5} more)" if len(hits) > 5 else "")
    apply.notes.append(
        f"{len(hits)} file(s) under workspace/{kind} still name the source "
        f"machine's path {tokens[0]} -- {shown}. Their contents are copied "
        "verbatim on purpose; if this machine uses a different root, edit them "
        "by hand.")


def _restore_tree(apply: _HostApply, payload: bytes, kind: str,
                  source: dict | None = None) -> None:
    """Merge one workspace archive into <install_root>/workspace/<kind>."""
    destination_root = apply.install_root / "workspace" / kind
    staging = Path(tempfile.mkdtemp(prefix=".n-agent-import-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for info in archive:
                if info.isdir():
                    (staging / normalize_member(info.name, where=kind)).mkdir(
                        parents=True, exist_ok=True)
                    continue
                if not info.isreg():
                    continue
                target = staging / normalize_member(info.name, where=kind)
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(info)
                if handle is None:
                    continue
                target.write_bytes(handle.read())
                target.chmod(0o600)
        source_root = staging / kind if (staging / kind).is_dir() else staging
        if source is not None:
            _report_stale_paths(apply, source_root, kind, source)
        for entry in sorted(source_root.iterdir()):
            target = destination_root / entry.name
            label = f"workspace/{kind}/{entry.name}"
            if target.exists():
                if apply.mode == "merge":
                    apply.record(label, "unchanged")
                    continue
                if _tree_equal(entry, target):
                    apply.record(label, "unchanged")
                    continue
                apply.backup_tree(target)
                if not apply.dry_run:
                    shutil.rmtree(target)
                    shutil.copytree(entry, target)
                apply.record(label, "updated")
                continue
            if not apply.dry_run:
                destination_root.mkdir(parents=True, exist_ok=True)
                shutil.copytree(entry, target)
            apply.record(label, "created")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _tree_equal(left: Path, right: Path) -> bool:
    return digest_tree(left) == digest_tree(right)


# ---------------------------------------------------------------------------
# import side: commands
# ---------------------------------------------------------------------------


def _limits(args) -> dict[str, int]:
    def read(name: str, fallback: int) -> int:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return fallback
        try:
            value = int(raw)
        except ValueError as exc:
            raise PreflightError(f"{name} is not an integer") from exc
        if value <= 0:
            raise PreflightError(f"{name} must be positive")
        return value

    return {
        "max_members": read("N_AGENT_BUNDLE_MAX_MEMBERS", HARD_MAX_MEMBERS),
        "max_file_bytes": read("N_AGENT_BUNDLE_MAX_FILE_BYTES", HARD_MAX_FILE_BYTES),
        "max_total_bytes": read("N_AGENT_BUNDLE_MAX_TOTAL_BYTES", HARD_MAX_TOTAL_BYTES),
    }


def _cmd_preflight(args) -> int:
    try:
        summary = preflight_bundle(Path(args.bundle), **_limits(args))
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: bundle unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2
    json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_host_check(args) -> int:
    """Every refusal that must happen before the first byte is written."""
    install_root = Path(args.install_root)
    repo_dir = Path(args.repo_dir)
    problems = _mount_conflicts(install_root)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 4
    if args.needs_policy == "true":
        _, origin = _policy_text({}, install_root, repo_dir)
        if origin == "missing":
            print("error: the bundle carries no locals/host-terminal-policy.yaml, the "
                  "target has none and docker/host-terminal-policy.yaml.example is "
                  "absent; refusing to start without a Host Terminal policy",
                  file=sys.stderr)
            return 3
    return 0


def _cmd_host_apply(args) -> int:
    bundle = Path(args.bundle)
    install_root = Path(args.install_root)
    code_root = Path(args.code_root)
    repo_dir = Path(args.repo_dir)
    mode = args.mode
    dry_run = args.dry_run == "true"

    try:
        members = _load_members(bundle)
    except (tarfile.TarError, OSError, PreflightError) as exc:
        print(f"error: bundle unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2

    raw_manifest = members.get("manifest.json")
    if raw_manifest is None:
        print("error: manifest.json disappeared between preflight and apply",
              file=sys.stderr)
        return 2
    # A corrupt manifest is a bundle format problem, so it has to land on 2
    # like every other one. Left unguarded it raises, and the traceback exits
    # 1 -- which config-import.sh reads as "some records failed, the rest were
    # applied", at a point where nothing has been applied at all.
    try:
        source = json.loads(raw_manifest.decode("utf-8"))["source"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"error: manifest.json is not readable: {type(exc).__name__}",
              file=sys.stderr)
        return 2

    problems = _mount_conflicts(install_root)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 4

    policy_payload, policy_origin = _policy_text(members, install_root, repo_dir)
    if policy_origin == "missing":
        print("error: no locals/host-terminal-policy.yaml in the bundle, on the "
              "target, or as docker/host-terminal-policy.yaml.example", file=sys.stderr)
        return 3

    apply = _HostApply(install_root=install_root, code_root=code_root,
                       repo_dir=repo_dir, mode=mode, dry_run=dry_run, source=source)
    redacted = bool(json.loads(raw_manifest.decode("utf-8")).get("redacted"))

    def usable(values: dict[str, str], where: str) -> dict[str, str]:
        """Drop the credential keys a --no-secrets export blanked out.

        An empty value in a redacted bundle means "stripped at export time",
        not "clear this on the target"; clearing would silently break the
        receiving deployment. A full bundle keeps the clear semantics.
        """
        if not redacted:
            return values
        kept: dict[str, str] = {}
        for key, value in values.items():
            if value == "" and is_secret_key(key):
                apply.notes.append(
                    f"degraded: {where}:{key} was stripped by --no-secrets; the "
                    "target value is kept, fill it in by hand if it is empty")
                continue
            kept[key] = value
        return kept

    def remap(payload: bytes) -> bytes:
        return _remap_paths(payload.decode("utf-8"), source,
                            str(install_root), str(code_root)).encode("utf-8")

    # --- skeleton ---------------------------------------------------------
    if not dry_run:
        for relative in SKELETON_DIRS:
            path = install_root / relative
            if not path.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o700)

    # --- the two dotenv files --------------------------------------------
    repo_env = repo_dir / "docker" / ".env"
    if "env/docker.env" in members:
        bundle_values = {key: _remap_paths(value, source, str(install_root),
                                           str(code_root))
                         for key, value in
                         parse_env(remap(members["env/docker.env"]).decode("utf-8")).items()}
        bundle_values = usable(bundle_values, "docker/.env")
        forced = {"N_AGENT_INSTALL_ROOT": str(install_root),
                  "N_AGENT_CODE_ROOT": str(code_root)}
        current = repo_env.read_text(encoding="utf-8") if repo_env.is_file() else ""
        apply.put_text(repo_env,
                       _merge_env_text(current, bundle_values, mode=mode, forced=forced),
                       label="docker/.env")

    install_env = install_root / ".env"
    if "env/install-root.env" in members:
        bundle_values = usable(
            parse_env(remap(members["env/install-root.env"]).decode("utf-8")),
            "<install-root>/.env")
        current = install_env.read_text(encoding="utf-8") if install_env.is_file() else ""
        apply.put_text(install_env,
                       _merge_env_text(current, bundle_values, mode=mode, forced={}),
                       label="<install-root>/.env")

    # --- policy -----------------------------------------------------------
    policy_path = install_root / "locals" / "host-terminal-policy.yaml"
    if policy_origin == "target":
        apply.record("locals/host-terminal-policy.yaml", "unchanged")
    elif policy_origin == "bundle" and policy_path.is_file() and mode == "merge":
        apply.record("locals/host-terminal-policy.yaml", "unchanged")
    else:
        apply.put_bytes(policy_path, remap(policy_payload),
                        label="locals/host-terminal-policy.yaml")

    # --- tokens -----------------------------------------------------------
    for relative in ("locals/host-terminal.token", "locals/host-browser.token"):
        path = install_root / relative
        existing = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        if existing:
            apply.record(relative, "unchanged")
            continue
        carried = members.get(relative, b"").decode("utf-8").strip()
        # Never land an empty token: an empty file silently disables the
        # shared-secret check on the host bridge.
        value = carried or secrets.token_hex(22)
        apply.put_text(path, value + "\n", label=relative)

    # --- oss.env ----------------------------------------------------------
    oss_path = install_root / "secrets" / "oss.env"
    if "secrets/oss.env" in members:
        if oss_path.is_file() and mode == "merge":
            apply.record("secrets/oss.env", "unchanged")
        else:
            apply.put_bytes(oss_path, remap(members["secrets/oss.env"]),
                            label="secrets/oss.env")
    elif oss_path.is_file():
        # An omitted member means "unchanged", never "cleared".
        apply.record("secrets/oss.env", "unchanged")

    # --- workspace --------------------------------------------------------
    for kind, arcname in (("skills", "workspace/skills.tar.gz"),
                          ("plugins", "workspace/plugins.tar.gz")):
        if arcname in members:
            _restore_tree(apply, members[arcname], kind, source)

    # --- the parameterised compose file ----------------------------------
    compose_path = repo_dir / "docker" / "docker-compose.yml"
    if "env/docker-compose.yml" in members:
        if compose_path.is_file() and mode == "merge":
            # The checkout owns the compose file; merge never rewrites code.
            apply.record("docker/docker-compose.yml", "unchanged")
        else:
            apply.put_bytes(compose_path, remap(members["env/docker-compose.yml"]),
                            label="docker/docker-compose.yml")

    # install.sh is informational only: it exists for a human to read and is
    # never executed by this script.
    if "install.sh" in members:
        apply.notes.append("install.sh in the bundle was ignored (never executed)")

    json.dump({"changed": apply.changed, "actions": apply.actions,
               "notes": apply.notes, "dry_run": dry_run,
               "source": source}, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_edit_env(args) -> int:
    """Set or remove one key in a dotenv file, preserving the other lines.

    Used for the migration maintenance switch: a temporary internal key, never
    one of the user's own toggles.
    """
    path = Path(args.file)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    key = args.key
    lines: list[str] = []
    seen = False
    for record in scan_env(text):
        if record.kind == "assignment" and record.key == key:
            seen = True
            if args.value is not None:
                lines.append(f"{key}={serialize_value(args.value)}\n")
            continue
        lines.append(record.raw)
    out = "".join(lines)
    if args.value is not None and not seen:
        if out and not out.endswith("\n"):
            out += "\n"
        out += f"{key}={serialize_value(args.value)}\n"
    if out != text:
        write_0600(path, out)
    return 0


def _cmd_pre_existing(args) -> int:
    """Turn a target-side `config export` document into section -> keys."""
    try:
        raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text("utf-8")
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: unreadable target export: {type(exc).__name__}", file=sys.stderr)
        return 3
    sections = document.get("sections")
    if not isinstance(sections, dict):
        print("error: the target export carries no sections", file=sys.stderr)
        return 3
    out: dict[str, list[str]] = {}
    for name, key_field in _NATURAL_KEYS.items():
        rows = sections.get(name)
        if not isinstance(rows, list):
            continue
        keys = [row.get(key_field) for row in rows if isinstance(row, dict)]
        out[name] = [key for key in keys if isinstance(key, str) and key]
    json.dump(out, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_db_envelope(args) -> int:
    """Build the {bundle, pre_existing} payload for `config import --stdin`."""
    try:
        with tarfile.open(args.bundle, mode="r:gz") as archive:
            handle = None
            for info in archive:
                if info.isreg() and normalize_member(info.name, where="bundle") == "db/config.json":
                    handle = archive.extractfile(info)
                    break
            if handle is None:
                print("error: the bundle carries no db/config.json", file=sys.stderr)
                return 2
            document = json.loads(handle.read().decode("utf-8"))
    except (tarfile.TarError, OSError, json.JSONDecodeError, PreflightError) as exc:
        print(f"error: db/config.json unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.pre_existing in (None, "", "-"):
        pre_existing: dict[str, list[str]] = {name: [] for name in _NATURAL_KEYS}
    else:
        try:
            pre_existing = json.loads(Path(args.pre_existing).read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: pre-existing context unreadable: {type(exc).__name__}",
                  file=sys.stderr)
            return 3
    json.dump({"bundle": document, "pre_existing": pre_existing}, sys.stdout,
              ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _cmd_restart_needed(args) -> int:
    """`yes` when the import actually wrote something.

    Keyed on ImportItemReport.action (created/updated/none) and never on
    outcome: `degraded` reappears on every idempotent rerun and would cause a
    pointless restart each time.
    """
    try:
        document = json.loads(Path(args.file).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: import report unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2
    items = document.get("items")
    if not isinstance(items, list):
        print("error: the import report carries no items", file=sys.stderr)
        return 2
    wrote = sum(1 for item in items
                if isinstance(item, dict) and item.get("action") not in (None, "none"))
    print("yes" if wrote else "no")
    return 0


def _cmd_report_summary(args) -> int:
    """Render the per-section import report without ever echoing a value."""
    try:
        document = json.loads(Path(args.file).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: import report unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2
    counts = document.get("counts") or {}
    print("  counts: " + " ".join(f"{name}={counts.get(name, 0)}" for name in
                                  ("created", "updated", "skipped", "degraded", "failed")))
    for item in document.get("items") or []:
        if not isinstance(item, dict):
            continue
        reasons = ", ".join(str(reason) for reason in item.get("reasons") or [])
        line = (f"  {item.get('section')}/{item.get('natural_key')}: "
                f"{item.get('outcome')} (action={item.get('action')})")
        print(f"{line} -- {reasons}" if reasons else line)
    return int(document.get("exit_code") or 0)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="config_bundle_files.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-deps").set_defaults(func=_cmd_check_deps)

    p = sub.add_parser("parse-env")
    p.add_argument("file")
    p.set_defaults(func=_cmd_parse_env)

    p = sub.add_parser("get-env")
    p.add_argument("file")
    p.add_argument("key")
    p.set_defaults(func=_cmd_get_env)

    sub.add_parser("serialize-env").set_defaults(func=_cmd_serialize_env)
    sub.add_parser("strip-secrets").set_defaults(func=_cmd_strip_secrets)

    p = sub.add_parser("manifest")
    p.add_argument("staging")
    p.add_argument("--schema-version", type=int, default=BUNDLE_SCHEMA_VERSION)
    p.add_argument("--hostname", required=True)
    p.add_argument("--install-root", required=True)
    p.add_argument("--code-root", required=True)
    p.add_argument("--redacted", choices=["true", "false"], required=True)
    p.set_defaults(func=_cmd_manifest)

    p = sub.add_parser("digest")
    p.add_argument("dir")
    p.set_defaults(func=_cmd_digest)

    p = sub.add_parser("fingerprint")
    p.add_argument("--exclude", action="append")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=_cmd_fingerprint)

    p = sub.add_parser("db-summary")
    p.add_argument("file")
    p.set_defaults(func=_cmd_db_summary)

    p = sub.add_parser("preflight")
    p.add_argument("bundle")
    p.set_defaults(func=_cmd_preflight)

    p = sub.add_parser("host-check")
    p.add_argument("--install-root", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--needs-policy", choices=["true", "false"], default="false")
    p.set_defaults(func=_cmd_host_check)

    p = sub.add_parser("host-apply")
    p.add_argument("bundle")
    p.add_argument("--install-root", required=True)
    p.add_argument("--code-root", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--mode", choices=["merge", "overwrite"], required=True)
    p.add_argument("--dry-run", choices=["true", "false"], default="false")
    p.set_defaults(func=_cmd_host_apply)

    p = sub.add_parser("edit-env")
    p.add_argument("file")
    p.add_argument("key")
    p.add_argument("--value")
    p.set_defaults(func=_cmd_edit_env)

    p = sub.add_parser("pre-existing")
    p.add_argument("file")
    p.set_defaults(func=_cmd_pre_existing)

    p = sub.add_parser("db-envelope")
    p.add_argument("bundle")
    p.add_argument("--pre-existing")
    p.set_defaults(func=_cmd_db_envelope)

    p = sub.add_parser("restart-needed")
    p.add_argument("file")
    p.set_defaults(func=_cmd_restart_needed)

    p = sub.add_parser("report-summary")
    p.add_argument("file")
    p.set_defaults(func=_cmd_report_summary)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # An unreadable file or a malformed JSON payload is a format problem,
        # so it belongs on 2 like the rest of them. Unguarded it escapes as a
        # traceback and exits 1, which the callers read as "some records
        # failed, the rest were applied" -- an outcome that never happened.
        # Only the exception type is printed: the message would carry the
        # path, and for the secret files that is already more than the caller
        # needs. Deliberately narrow -- a TypeError is a bug in this script
        # and should still surface as a traceback rather than be relabelled
        # a bundle format error.
        print(f"error: {args.command}: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
