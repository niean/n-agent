"""TaskConfig -- task-subsystem runtime configuration (Domain Layer).

Pure domain: defines the resolved config, per-field overrides, stored view,
resolved view, and the async Store/Provider ports. No Settings, SQLite,
FastAPI, or Chinese display strings. Validation is a pure function.

A/B/C classification (see .harness/knowledge/03-conventions.md):
  - A class (security invariants) and B class (bootstrap env-only) are NOT
    in TaskConfig. Only the 9 C-class runtime-tunable fields live here.
  - DB stores per-field overrides (TaskConfigOverrides); a None field means
    "DB does not override; follow env". This avoids a partial edit freezing
    the other env values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class TaskConfigValidationError(Exception):
    """Raised when a TaskConfig value or cross-field combination is invalid."""


class TaskConfigConflictError(Exception):
    """Raised when a CAS save fails (version mismatch / first-write race)."""


class TaskConfigStoreError(Exception):
    """Raised when the store cannot read/parse/serialize the config row."""


@dataclass(frozen=True)
class TaskConfig:
    """Resolved task config: all 9 C-class fields as concrete ints."""

    task_max_concurrency: int = 4
    task_lease_seconds: int = 900
    task_heartbeat_timeout_seconds: int = 300
    task_max_runtime_seconds: int = 3600
    task_goal_max_turns: int = 10
    task_attachment_max_bytes: int = 20 * 1024 * 1024
    task_attachment_task_max_bytes: int = 100 * 1024 * 1024
    task_failure_limit: int = 3
    note_max_codepoints: int = 2000


# Field names in stable order. Used for overrides merge + overridden_fields.
TASK_CONFIG_FIELDS: tuple[str, ...] = (
    "task_max_concurrency",
    "task_lease_seconds",
    "task_heartbeat_timeout_seconds",
    "task_max_runtime_seconds",
    "task_goal_max_turns",
    "task_attachment_max_bytes",
    "task_attachment_task_max_bytes",
    "task_failure_limit",
    "note_max_codepoints",
)


@dataclass(frozen=True)
class TaskConfigOverrides:
    """Per-field DB overrides. None means "not overridden; follow env"."""

    task_max_concurrency: int | None = None
    task_lease_seconds: int | None = None
    task_heartbeat_timeout_seconds: int | None = None
    task_max_runtime_seconds: int | None = None
    task_goal_max_turns: int | None = None
    task_attachment_max_bytes: int | None = None
    task_attachment_task_max_bytes: int | None = None
    task_failure_limit: int | None = None
    note_max_codepoints: int | None = None

    def to_dict(self) -> dict[str, int]:
        """Serialize non-None overrides. None fields are omitted (not frozen)."""
        return {f: getattr(self, f) for f in TASK_CONFIG_FIELDS if getattr(self, f) is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TaskConfigOverrides":
        """Parse overrides from a JSON object. Strict: unknown keys, non-int,
        bool, or missing-type values raise TaskConfigStoreError (caller maps)."""
        if not isinstance(data, dict):
            raise TaskConfigStoreError("overrides must be a JSON object")
        kwargs: dict[str, int] = {}
        for k, v in data.items():
            if k not in TASK_CONFIG_FIELDS:
                raise TaskConfigStoreError(f"unknown override key: {k}")
            if isinstance(v, bool) or not isinstance(v, int):
                raise TaskConfigStoreError(f"override {k} must be int, got {type(v).__name__}")
            kwargs[k] = v
        return TaskConfigOverrides(**kwargs)

    def overridden_fields(self) -> tuple[str, ...]:
        return tuple(f for f in TASK_CONFIG_FIELDS if getattr(self, f) is not None)


@dataclass(frozen=True)
class StoredTaskConfig:
    """What the store persists + reads. version>=1 once a row exists."""

    overrides: TaskConfigOverrides
    version: int
    updated_at: str
    updated_by: str


@dataclass(frozen=True)
class ResolvedTaskConfig:
    """Resolved config + metadata for the management surface.

    version=0 + overridden_fields=() when no DB row exists (first-write CAS).
    updated_at/updated_by are None when no DB row; populated from the stored
    row otherwise so the HTTP response can surface them.
    """

    config: TaskConfig
    version: int
    overridden_fields: tuple[str, ...] = ()
    updated_at: str | None = None
    updated_by: str | None = None


def validate_task_config(cfg: TaskConfig, dispatch_interval_seconds: int) -> None:
    """Pure cross-field validation. Raises TaskConfigValidationError on failure.

    Rules (mirror Settings._validate_task_subsystem, single source for both
    env-startup and DB-override layers):
      - every field must be int (bool rejected at construction/parse time);
      - every field >= 1;
      - heartbeat_timeout < lease;
      - attachment_task_max >= attachment_max;
      - dispatch_interval < lease (dispatch is B-class env-only; this cross-layer
        check guards against a DB lease edit that breaks the dispatch cadence).
    """
    for f in TASK_CONFIG_FIELDS:
        val = getattr(cfg, f)
        # bool is a subclass of int; reject defensively (parse layers also guard).
        if isinstance(val, bool) or not isinstance(val, int):
            raise TaskConfigValidationError(f"field {f} must be int")
        if val < 1:
            raise TaskConfigValidationError(f"field {f} must be >= 1, got {val}")
    if cfg.task_heartbeat_timeout_seconds >= cfg.task_lease_seconds:
        raise TaskConfigValidationError(
            "task_heartbeat_timeout_seconds must be less than task_lease_seconds"
        )
    if cfg.task_attachment_task_max_bytes < cfg.task_attachment_max_bytes:
        raise TaskConfigValidationError(
            "task_attachment_task_max_bytes must be >= task_attachment_max_bytes"
        )
    if dispatch_interval_seconds >= cfg.task_lease_seconds:
        raise TaskConfigValidationError(
            "task_lease_seconds must be greater than task_dispatch_interval_seconds"
        )


def merge_overrides(base: TaskConfig, overrides: TaskConfigOverrides) -> TaskConfig:
    """Build a resolved TaskConfig from an env base + DB overrides."""
    kwargs: dict[str, int] = {}
    for f in TASK_CONFIG_FIELDS:
        ov = getattr(overrides, f)
        kwargs[f] = ov if ov is not None else getattr(base, f)
    return TaskConfig(**kwargs)


class TaskConfigStore(Protocol):
    """Async port for the task_config single-row store."""

    async def get(self) -> StoredTaskConfig | None: ...

    async def save(
        self, overrides: TaskConfigOverrides, expected_version: int, updated_by: str,
    ) -> StoredTaskConfig: ...


class TaskConfigProvider(Protocol):
    """Async port: runtime services call current() at use-time for hot-reload."""

    async def current(self) -> TaskConfig: ...


@dataclass(frozen=True)
class TaskConfigAuditEvent:
    """Audit record for a config change. No secrets (C-class values only)."""

    actor: str
    updated_at: str
    old_version: int
    new_version: int
    # changed field -> (before_resolved, after_resolved)
    changed_fields: dict[str, tuple[int, int]] = field(default_factory=dict)


class TaskConfigAuditSink(Protocol):
    """Best-effort async audit sink. Failure must not roll back a committed config."""

    async def record(self, event: TaskConfigAuditEvent) -> None: ...
