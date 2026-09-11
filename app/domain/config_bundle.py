"""ConfigBundle -- cross-machine configuration migration values (Domain Layer).

Pure domain: schema version, section catalogue, the four-state secret value
object, and the import report model. No Settings, SQLite, FastAPI, or
infrastructure imports -- only stdlib, per tests/test_architecture_boundaries.py
(which walks the whole AST, so lazy in-function imports are caught too).

Secret four-state semantics (see .harness/specs spec-260910-config-bundle.md):
  - absent from the payload  -> unchanged: do not touch the target secret
  - ""                       -> cleared:   explicitly clear the target secret
  - non-empty string         -> set:       overwrite with this value
  - None                     -> redacted:  exported with --no-secrets; the
                                           importer must not write, and must
                                           report the item as degraded.

ImportItemReport.action vs outcome are orthogonal. outcome is the conclusion
(created/updated/skipped/degraded/failed); action records whether a row was
actually written (created/updated/none). A degraded item may have written
(action=updated, some field unrestorable) or not (action=none). The host-side
importer decides whether to restart the service purely from action, because
degraded recurs on every idempotent rerun while action does not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


BUNDLE_SCHEMA_VERSION = 1

SECTION_NAMES: tuple[str, ...] = (
    "providers",
    "knowledge_bases",
    "mcp_sites",
    "external_memory_providers",
    "external_memory_global_config",
    "plugins",
    "skills",
    "scheduled_tasks",
    "gateway_home_targets",
    "task_config",
)


class ImportMode(StrEnum):
    MERGE = "merge"
    OVERWRITE = "overwrite"


class ImportOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"
    DEGRADED = "degraded"
    FAILED = "failed"


class ImportAction(StrEnum):
    """Whether the target row was actually written."""

    CREATED = "created"
    UPDATED = "updated"
    NONE = "none"


class ConfigBundleValidationError(Exception):
    """Raised when a bundle is structurally invalid. Never carries a secret."""

    def __init__(self, section: str, reason: str) -> None:
        self.section = section
        self.reason = reason
        super().__init__(f"[{section}] {reason}")


@dataclass(frozen=True)
class SecretValue:
    """A secret in one of four states. Never reveals its payload in repr/str."""

    kind: str
    _payload: str | None = None

    @staticmethod
    def absent() -> "SecretValue":
        return SecretValue("unchanged")

    @staticmethod
    def classify(raw: str | None) -> "SecretValue":
        if raw is None:
            return SecretValue("redacted")
        if raw == "":
            return SecretValue("cleared")
        return SecretValue("set", raw)

    def reveal(self) -> str:
        """Return the plaintext. Only legal for kind == 'set'."""
        if self.kind != "set":
            raise ValueError(f"cannot reveal secret of kind {self.kind!r}")
        assert self._payload is not None
        return self._payload

    def __repr__(self) -> str:
        return f"SecretValue(kind={self.kind!r})"

    __str__ = __repr__


@dataclass
class ImportItemReport:
    section: str
    natural_key: str
    outcome: ImportOutcome
    action: ImportAction = ImportAction.NONE
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "natural_key": self.natural_key,
            "outcome": str(self.outcome),
            "action": str(self.action),
            "reasons": list(self.reasons),
        }


@dataclass
class ImportReport:
    mode: ImportMode
    items: list[ImportItemReport] = field(default_factory=list)

    def __post_init__(self) -> None:
        seen: set[tuple[str, str]] = set()
        for item in self.items:
            key = (item.section, item.natural_key)
            if key in seen:
                raise ValueError(
                    f"duplicate natural key in report: {item.section}/{item.natural_key}"
                )
            seen.add(key)

    @property
    def counts(self) -> dict[ImportOutcome, int]:
        return {
            outcome: sum(1 for item in self.items if item.outcome is outcome)
            for outcome in ImportOutcome
        }

    @property
    def exit_code(self) -> int:
        return 1 if any(i.outcome is ImportOutcome.FAILED for i in self.items) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "exit_code": self.exit_code,
            "counts": {str(k): v for k, v in self.counts.items()},
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class BundleSource:
    hostname: str
    install_root: str
    code_root: str


@dataclass
class ConfigBundle:
    """Deliberately NOT frozen: tests and the host importer rewrite sections."""

    schema_version: int
    created_at: str
    source: BundleSource
    redacted: bool
    sections: dict[str, Any] = field(default_factory=dict)
