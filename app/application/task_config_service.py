"""TaskConfigService -- resolves env+DB config, validates, updates with CAS,
and implements TaskConfigProvider for hot-reload.

Two read paths:
  - current(): runtime path. read-through store; on Store error returns
    last-known-good (init from validated env) and logs warning. Never raises
    to the runtime -- config must not break running tasks.
  - get_resolved(): management path. Strict -- Store/parse errors propagate.
    The dashboard and edit UI must see the truth, not a masked env fallback.

update(): merge partial into existing overrides (NOT resolved full config),
build candidate resolved, validate (incl. cross-layer dispatch<lease), CAS
save, update last-known-good, best-effort audit.
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.domain.task_config import (
    ResolvedTaskConfig,
    TaskConfig,
    TaskConfigAuditEvent,
    TaskConfigAuditSink,
    TaskConfigConflictError,
    TaskConfigOverrides,
    TaskConfigProvider,
    TaskConfigStore,
    TaskConfigStoreError,
    TaskConfigValidationError,
    TASK_CONFIG_FIELDS,
    merge_overrides,
    validate_task_config,
)

logger = logging.getLogger(__name__)


class TaskConfigService(TaskConfigProvider):
    """Resolves, validates, and updates task config. Implements Provider."""

    def __init__(
        self,
        settings: Settings,
        store: TaskConfigStore,
        audit_sink: TaskConfigAuditSink | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._audit_sink = audit_sink
        self._env_config = _env_config(settings)
        self._dispatch_interval = settings.task_dispatch_interval_seconds
        # last-known-good starts as the validated env snapshot; updated on
        # every successful store read. Used only by current() (runtime path).
        self._last_known_good: TaskConfig = self._env_config

    async def current(self) -> TaskConfig:
        """Runtime hot-reload read. Never raises; falls back to last-known-good."""
        try:
            stored = await self._store.get()
        except TaskConfigStoreError:
            logger.warning("task config store read failed; using last-known-good")
            return self._last_known_good
        if stored is None:
            self._last_known_good = self._env_config
            return self._env_config
        resolved = merge_overrides(self._env_config, stored.overrides)
        try:
            validate_task_config(resolved, self._dispatch_interval)
        except TaskConfigValidationError:
            logger.warning("stored task config overrides invalid; using env")
            self._last_known_good = self._env_config
            return self._env_config
        self._last_known_good = resolved
        return resolved

    async def get_resolved(self) -> ResolvedTaskConfig:
        """Strict management read. Store/parse errors propagate."""
        stored = await self._store.get()
        if stored is None:
            return ResolvedTaskConfig(config=self._env_config, version=0, overridden_fields=())
        resolved = merge_overrides(self._env_config, stored.overrides)
        validate_task_config(resolved, self._dispatch_interval)
        return ResolvedTaskConfig(
            config=resolved,
            version=stored.version,
            overridden_fields=stored.overrides.overridden_fields(),
            updated_at=stored.updated_at,
            updated_by=stored.updated_by,
        )

    async def update(
        self,
        partial: dict[str, Any],
        expected_version: int,
        updated_by: str,
    ) -> ResolvedTaskConfig:
        """Apply a partial override update with CAS + audit."""
        # Validate partial shape.
        if not isinstance(partial, dict) or not partial:
            raise TaskConfigValidationError("patch must be a non-empty object")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise TaskConfigValidationError("expected_version must be a non-negative int")
        if not isinstance(updated_by, str) or not updated_by:
            raise TaskConfigValidationError("updated_by must be a non-empty string")
        patch_overrides: dict[str, int] = {}
        for k, v in partial.items():
            if k not in TASK_CONFIG_FIELDS:
                raise TaskConfigValidationError(f"unknown field: {k}")
            if isinstance(v, bool) or not isinstance(v, int):
                raise TaskConfigValidationError(f"field {k} must be int")
            patch_overrides[k] = v

        # Merge into EXISTING overrides (not resolved full config).
        stored = await self._store.get()
        if stored is None:
            existing = TaskConfigOverrides()
            old_version = 0
        else:
            existing = stored.overrides
            old_version = stored.version
        if old_version != expected_version:
            raise TaskConfigConflictError(
                f"version mismatch: expected {expected_version}, got {old_version}"
            )
        merged_kwargs: dict[str, int | None] = {}
        for f in TASK_CONFIG_FIELDS:
            if f in patch_overrides:
                merged_kwargs[f] = patch_overrides[f]
            else:
                merged_kwargs[f] = getattr(existing, f)
        merged = TaskConfigOverrides(**merged_kwargs)

        # Validate candidate resolved (env + merged overrides).
        candidate = merge_overrides(self._env_config, merged)
        validate_task_config(candidate, self._dispatch_interval)

        # CAS save.
        saved = await self._store.save(merged, expected_version, updated_by)
        self._last_known_good = candidate

        # Best-effort audit: only changed fields' resolved before/after.
        changed: dict[str, tuple[int, int]] = {}
        for f in patch_overrides:
            before = getattr(self._env_config, f) if getattr(existing, f) is None else getattr(existing, f)  # type: ignore[arg-type]
            after = patch_overrides[f]
            if before != after:
                changed[f] = (before, after)
        if self._audit_sink is not None:
            event = TaskConfigAuditEvent(
                actor=updated_by,
                updated_at=saved.updated_at,
                old_version=old_version,
                new_version=saved.version,
                changed_fields=changed,
            )
            try:
                await self._audit_sink.record(event)
            except Exception:
                logger.warning("task config audit sink failed; config already committed")

        return ResolvedTaskConfig(
            config=candidate,
            version=saved.version,
            overridden_fields=merged.overridden_fields(),
            updated_at=saved.updated_at,
            updated_by=saved.updated_by,
        )


def _env_config(settings: Settings) -> TaskConfig:
    """Build the env-base TaskConfig from Settings (the default layer)."""
    return TaskConfig(
        task_max_concurrency=settings.task_max_concurrency,
        task_lease_seconds=settings.task_lease_seconds,
        task_heartbeat_timeout_seconds=settings.task_heartbeat_timeout_seconds,
        task_max_runtime_seconds=settings.task_max_runtime_seconds,
        task_goal_max_turns=settings.task_goal_max_turns,
        task_attachment_max_bytes=settings.task_attachment_max_bytes,
        task_attachment_task_max_bytes=settings.task_attachment_task_max_bytes,
        task_failure_limit=settings.task_failure_limit,
        note_max_codepoints=settings.task_note_max_codepoints,
    )
