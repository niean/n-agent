"""Strict, fail-closed loader for the host-terminal YAML policy."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import threading
from typing import Any

import yaml

from app.domain.host_terminal_policy import (
    HostCommandRule,
    HostExactArgRule,
    HostOneOfArgRule,
    HostPositionalArgRule,
    HostSkillScriptRule,
    HostTerminalPolicySnapshot,
    HostTerminalResourceLimits,
)


class HostTerminalPolicyLoadError(ValueError):
    """A public, deliberately non-diagnostic policy loading error."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise HostTerminalPolicyLoadError("host_policy_duplicate_field")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)

_TOP_FIELDS = {"schema_version", "version", "limits", "targets"}
_LIMIT_FIELDS = {
    "default_timeout_seconds",
    "max_timeout_seconds",
    "max_stdout_bytes",
    "max_stderr_bytes",
    "max_args",
    "max_arg_length",
    "max_total_args_length",
    "max_concurrency",
}
_COMMAND_FIELDS = {"type", "rule_id", "executable", "args"}
_SKILL_FIELDS = {
    "type",
    "rule_id",
    "skill_name",
    "script_relative_path",
    "sha256",
    "args",
}
_MAX_POLICY_BYTES = 1_048_576


class HostTerminalPolicyLoader:
    """Publishes a new immutable snapshot only after complete validation."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._snapshot: HostTerminalPolicySnapshot | None = None
        self._last_error_code: str | None = None
        self._lock = threading.Lock()

    @property
    def snapshot(self) -> HostTerminalPolicySnapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def last_error_code(self) -> str | None:
        with self._lock:
            return self._last_error_code

    def refresh(self) -> bool:
        """Attempt one reload, retaining the last good snapshot on failure."""
        try:
            candidate = self._load_candidate()
        except HostTerminalPolicyLoadError as exc:
            with self._lock:
                self._last_error_code = exc.reason_code
            return False
        except Exception:
            with self._lock:
                self._last_error_code = "host_policy_load_failed"
            return False
        with self._lock:
            self._snapshot = candidate
            self._last_error_code = None
        return True

    def load(self) -> HostTerminalPolicySnapshot:
        """Load or raise a stable error; useful for fail-fast startup wiring."""
        if not self.refresh():
            raise HostTerminalPolicyLoadError(
                self.last_error_code or "host_policy_load_failed"
            )
        snapshot = self.snapshot
        assert snapshot is not None
        return snapshot

    def _load_candidate(self) -> HostTerminalPolicySnapshot:
        raw = _read_secure_regular_file(self._path)
        try:
            data = yaml.load(raw, Loader=_UniqueKeySafeLoader)
        except HostTerminalPolicyLoadError:
            raise
        except yaml.YAMLError as exc:
            raise HostTerminalPolicyLoadError("host_policy_yaml_invalid") from exc
        try:
            return _parse_snapshot(data, raw)
        except HostTerminalPolicyLoadError:
            raise
        except (TypeError, ValueError) as exc:
            raise HostTerminalPolicyLoadError("host_policy_schema_invalid") from exc


def _read_secure_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostTerminalPolicyLoadError("host_policy_file_unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HostTerminalPolicyLoadError("host_policy_file_unsafe")
    if metadata.st_uid != os.geteuid():
        raise HostTerminalPolicyLoadError("host_policy_file_unsafe")
    if stat.S_IMODE(metadata.st_mode) & ~0o600:
        raise HostTerminalPolicyLoadError("host_policy_file_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) & ~0o600
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise HostTerminalPolicyLoadError("host_policy_file_unsafe")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 65536):
                total += len(chunk)
                if total > _MAX_POLICY_BYTES:
                    raise HostTerminalPolicyLoadError("host_policy_file_too_large")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except HostTerminalPolicyLoadError:
        raise
    except OSError as exc:
        raise HostTerminalPolicyLoadError("host_policy_file_unavailable") from exc


def _object(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
    if any(not isinstance(key, str) for key in value):
        raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
    return value


def _parse_snapshot(data: Any, raw: bytes) -> HostTerminalPolicySnapshot:
    top = _object(data, _TOP_FIELDS)
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise HostTerminalPolicyLoadError("host_policy_version_unsupported")
    limits_data = _object(top["limits"], _LIMIT_FIELDS)
    limits = HostTerminalResourceLimits(**limits_data)
    targets = top["targets"]
    if not isinstance(targets, list) or not targets:
        raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
    commands: list[HostCommandRule] = []
    skills: list[HostSkillScriptRule] = []
    for target in targets:
        if not isinstance(target, dict):
            raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
        kind = target.get("type")
        if kind == "command":
            item = _object(target, _COMMAND_FIELDS)
            commands.append(
                HostCommandRule(
                    rule_id=item["rule_id"],
                    executable=item["executable"],
                    positional_args=_parse_args(item["args"]),
                )
            )
        elif kind == "skill_script":
            item = _object(target, _SKILL_FIELDS)
            skills.append(
                HostSkillScriptRule(
                    rule_id=item["rule_id"],
                    skill_name=item["skill_name"],
                    script_relative_path=item["script_relative_path"],
                    sha256=item["sha256"],
                    positional_args=_parse_args(item["args"]),
                )
            )
        else:
            raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
    return HostTerminalPolicySnapshot(
        schema_version=top["schema_version"],
        version=top["version"],
        content_digest=hashlib.sha256(raw).hexdigest(),
        loaded_at=datetime.now(timezone.utc),
        limits=limits,
        command_rules=tuple(commands),
        skill_script_rules=tuple(skills),
    )


def _parse_args(value: Any) -> tuple[HostPositionalArgRule, ...]:
    if not isinstance(value, list):
        raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
    result: list[HostPositionalArgRule] = []
    for rule in value:
        if not isinstance(rule, dict) or len(rule) != 1:
            raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
        if set(rule) == {"exact"}:
            result.append(HostExactArgRule(rule["exact"]))
        elif set(rule) == {"one_of"} and isinstance(rule["one_of"], list):
            result.append(HostOneOfArgRule(tuple(rule["one_of"])))
        else:
            raise HostTerminalPolicyLoadError("host_policy_schema_invalid")
    return tuple(result)


__all__ = ["HostTerminalPolicyLoadError", "HostTerminalPolicyLoader"]
