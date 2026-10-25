"""OtelLinkConfigService -- resolves env+DB OTel link config, validates,
updates with CAS, and implements OtelLinkConfigProvider for hot-reload.

Two read paths (aligned with TaskConfigService):
  - current(): runtime path. read-through store; on StoreError or invalid
    stored overrides falls back to last-known-good, then env base, and logs
    a warning without URLs. Never raises to the runtime.
  - get_resolved(): management path. Strict -- store/parse/validation errors
    propagate; invalid stored content surfaces as OtelLinkConfigStoreError,
    not a user-input 422.

update(): merge the partial into EXISTING overrides (None clears a field's
override), validate only overridden fields, CAS save, update last-known-good,
then best-effort audit (recorded even when the resolved value is unchanged,
e.g. env -> same-value custom). The response is built from this update's own
candidate + save metadata; the store is never re-read.

Cache protection: last-known-good carries a version; a late read/save with
an older version never regresses the cache. CancelledError is never
swallowed (only domain exceptions are caught).
"""
from __future__ import annotations

import logging
from typing import Any

from app.domain.otel_link_config import (
    OTEL_LINK_CONFIG_FIELDS,
    OtelLinkConfig,
    OtelLinkConfigAuditEvent,
    OtelLinkConfigAuditSink,
    OtelLinkConfigFieldState,
    OtelLinkConfigOverrides,
    OtelLinkConfigProvider,
    OtelLinkConfigStore,
    OtelLinkConfigStoreError,
    OtelLinkConfigValidationError,
    ResolvedOtelLinkConfig,
    StoredOtelLinkConfig,
    build_otel_link_field_state,
    merge_overrides,
    validate_otel_link_config,
)

logger = logging.getLogger(__name__)


class OtelLinkConfigService(OtelLinkConfigProvider):
    """Resolves, validates, and updates OTel link config. Implements Provider."""

    def __init__(
        self,
        store: OtelLinkConfigStore,
        audit_sink: OtelLinkConfigAuditSink | None = None,
        *,
        metric_base_url: str,
        log_base_url: str,
        trace_base_url: str,
        profile_base_url: str,
    ) -> None:
        self._store = store
        self._audit_sink = audit_sink
        # env base is injected already-mapped from Settings; the new
        # length/parse rules apply only to persisted overrides, never to env.
        self._env_config = OtelLinkConfig(
            metric_base_url=metric_base_url,
            log_base_url=log_base_url,
            trace_base_url=trace_base_url,
            profile_base_url=profile_base_url,
        )
        # last-known-good starts as env (version 0 sentinel); updated only on
        # successful reads/saves with a version >= the cached one.
        self._last_known_good: OtelLinkConfig = self._env_config
        self._lkg_version = 0

    async def current(self) -> OtelLinkConfig:
        """Runtime hot-reload read. Never raises; degrades to lkg/env."""
        try:
            stored = await self._store.get()
        except OtelLinkConfigStoreError:
            logger.warning("otel link config store read failed; using last-known-good")
            return self._last_known_good
        try:
            resolved, version = self._resolve_stored(stored)
        except OtelLinkConfigStoreError:
            logger.warning("stored otel link config invalid; using last-known-good")
            return self._last_known_good
        if version > self._lkg_version:
            self._last_known_good = resolved
            self._lkg_version = version
        return self._last_known_good

    async def get_resolved(self) -> ResolvedOtelLinkConfig:
        """Strict management read. Store/parse/validation errors propagate."""
        stored = await self._store.get()
        resolved, _version = self._resolve_stored(stored)
        if stored is None:
            return ResolvedOtelLinkConfig(config=resolved, version=0, overridden_fields=())
        return ResolvedOtelLinkConfig(
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
    ) -> ResolvedOtelLinkConfig:
        """Apply a partial override update with CAS + best-effort audit."""
        if not isinstance(partial, dict) or not partial:
            raise OtelLinkConfigValidationError("patch must be a non-empty object")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise OtelLinkConfigValidationError("expected_version must be a non-negative int")
        if not isinstance(updated_by, str) or not updated_by:
            raise OtelLinkConfigValidationError("updated_by must be a non-empty string")
        patch: dict[str, str | None] = {}
        for k, v in partial.items():
            if k not in OTEL_LINK_CONFIG_FIELDS:
                raise OtelLinkConfigValidationError(f"unknown field: {k}")
            if v is not None and (isinstance(v, bool) or not isinstance(v, str)):
                raise OtelLinkConfigValidationError(f"field {k} must be str or null")
            patch[k] = v

        # Merge into EXISTING overrides; None clears that field's override.
        stored = await self._store.get()
        if stored is None:
            existing = OtelLinkConfigOverrides()
            before_resolved = self._env_config
            old_version = 0
        else:
            existing = stored.overrides
            # Corrupt persisted overrides are a storage fault: never silently
            # repair them via a patch, even one that overwrites the bad field.
            self._resolve_stored(stored)
            before_resolved = merge_overrides(self._env_config, existing)
            old_version = stored.version
        merged_kwargs: dict[str, str | None] = {}
        for f in OTEL_LINK_CONFIG_FIELDS:
            merged_kwargs[f] = patch[f] if f in patch else getattr(existing, f)
        merged = OtelLinkConfigOverrides(**merged_kwargs)

        # Validate only overridden fields; env base is not subject to the new
        # length/parse rules.
        candidate = merge_overrides(self._env_config, merged)
        validate_otel_link_config(candidate, fields=merged.overridden_fields())

        # CAS save; conflict propagates.
        saved = await self._store.save(merged, expected_version, updated_by)
        if saved.version > self._lkg_version:
            self._last_known_good = candidate
            self._lkg_version = saved.version

        await self._audit(saved, updated_by, old_version, patch, existing, before_resolved, candidate)

        return ResolvedOtelLinkConfig(
            config=candidate,
            version=saved.version,
            overridden_fields=merged.overridden_fields(),
            updated_at=saved.updated_at,
            updated_by=saved.updated_by,
        )

    def _resolve_stored(
        self, stored: StoredOtelLinkConfig | None
    ) -> tuple[OtelLinkConfig, int]:
        """Merge stored overrides onto env base and validate them.

        Returns (resolved, version). No row -> (env base, 0). Invalid stored
        overrides raise OtelLinkConfigStoreError (corrupt persisted content is
        a storage fault, not user input).
        """
        if stored is None:
            return self._env_config, 0
        resolved = merge_overrides(self._env_config, stored.overrides)
        try:
            validate_otel_link_config(resolved, fields=stored.overrides.overridden_fields())
        except OtelLinkConfigValidationError as exc:
            raise OtelLinkConfigStoreError(
                "stored otel link config overrides failed validation"
            ) from exc
        return resolved, stored.version

    async def _audit(
        self,
        saved: StoredOtelLinkConfig,
        updated_by: str,
        old_version: int,
        patch: dict[str, str | None],
        existing: OtelLinkConfigOverrides,
        before_resolved: OtelLinkConfig,
        after_resolved: OtelLinkConfig,
    ) -> None:
        """Best-effort audit. Records any patched field whose override state
        or resolved value changed; failures log a URL-free warning and never
        roll back the committed config."""
        if self._audit_sink is None:
            return
        changed: dict[str, tuple[OtelLinkConfigFieldState, OtelLinkConfigFieldState]] = {}
        for f in patch:
            before_override = getattr(existing, f)
            after_override = getattr(saved.overrides, f)
            before_url = getattr(before_resolved, f)
            after_url = getattr(after_resolved, f)
            if before_override == after_override and before_url == after_url:
                continue
            changed[f] = (
                build_otel_link_field_state(before_override, before_url),
                build_otel_link_field_state(after_override, after_url),
            )
        if not changed:
            return  # no-op update: nothing to audit
        event = OtelLinkConfigAuditEvent(
            actor=updated_by,
            updated_at=saved.updated_at,
            old_version=old_version,
            new_version=saved.version,
            changed_fields=changed,
        )
        try:
            await self._audit_sink.record(event)
        except Exception:
            logger.warning("otel link config audit sink failed; config already committed")
