"""ConfigBundleService -- export/import of the 10 configuration sections
(Application Layer).

Boundary: this module must NOT import app.infrastructure* -- not even lazily
inside a function (tests/test_architecture_boundaries.py walks the whole AST).
Every collaborator is injected through __init__ as a Domain port Protocol or a
sibling Application service.

Section payload shapes follow the spec Data Model table
(.harness/specs/completed/spec-260910-config-bundle.md):
  - collection sections   -> list, empty list when there is no row
  - singleton sections    -> None when there is no persisted row
    external_memory_global_config -> {"enabled_providers": [...]}
    task_config                   -> {"overrides": {...}}

Secret serialization on export:
  - redact_secrets=True  -> every secret carrier becomes None (not "")
  - source has no secret -> "" (explicitly "no secret", distinct from None)
"""
from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from uuid import uuid4

from app.domain.config_bundle import (
    BUNDLE_SCHEMA_VERSION,
    SECTION_NAMES,
    BundleSource,
    ConfigBundle,
    ConfigBundleValidationError,
    ImportAction,
    ImportItemReport,
    ImportMode,
    ImportOutcome,
    ImportReport,
    SecretValue,
)
from app.domain.external_memory import ExternalMemoryConfigRegistry
from app.domain.external_memory_provider import (
    ExternalMemoryProviderRegistry,
    ExternalMemoryProviderType,
)
from app.domain.gateway import GatewayHomeTarget, GatewaySessionRegistry
from app.domain.knowledge import (
    KnowledgeBase,
    KnowledgeBaseRegistry,
    KnowledgeBaseType,
    KnowledgeProbeStatus,
)
from app.domain.mcp import McpProbeStatus, McpSite, McpSiteRegistry, McpTransportType
from app.domain.platform import Platform
from app.domain.plugin import PluginRegistry
from app.domain.provider import ProviderConfig, ProviderRegistry
from app.domain.schedule import (
    ScheduledExecutionPolicy,
    ScheduledExecutionPolicyMode,
    ScheduledTask,
    ScheduledTaskStatus,
)
from app.domain.skill import SkillRegistry
from app.domain.task_config import TaskConfigStore

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from app.application.schedule_service import ScheduleService
    from app.application.task_config_service import TaskConfigService


logger = logging.getLogger(__name__)


# Key-name detection for nested secrets inside free-form JSON payloads
# (plugin config_json, external memory extra_config). Mirrors the host-side
# .env rule: *_PATH keys are container paths, not credentials.
_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|access[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)
_PATH_KEY_SUFFIX = re.compile(r"path$", re.IGNORECASE)


def _is_secret_key(key: str, declared: set[str]) -> bool:
    if key in declared:
        return True
    if _PATH_KEY_SUFFIX.search(key):
        return False
    return bool(_SECRET_KEY_PATTERN.search(key))


def _redact_nested(value: Any, declared: set[str]) -> Any:
    """Recursively blank out secret leaves inside a payload.

    Dict values whose key matches a secret pattern are nulled out ONLY when
    they are scalars (strings, numbers, bools); containment keys such as
    "credentials" are recursed into so nested secrets inside them are caught.
    The surrounding structure is preserved so the importer can distinguish
    "field existed but was redacted" from "field absent".
    """
    if isinstance(value, dict):
        return {key: _maybe_redact_leaf(str(key), item, declared) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_nested(item, declared) for item in value]
    return value


def _maybe_redact_leaf(key: str, item: Any, declared: set[str]) -> Any:
    """Null out a value whose key marks it as a secret.

    A manifest-declared secret field is nulled whole, whatever its shape.
    For heuristic key-name matches, containers are recursed into instead
    (a key like "credentials" names a container, not the secret itself).
    """
    if key in declared:
        return None
    if isinstance(item, (dict, list)):
        return _redact_nested(item, declared)
    if _is_secret_key(key, declared):
        return None
    return item


# Section shapes, used by _validate_bundle.
_COLLECTION_SECTIONS: tuple[str, ...] = (
    "providers",
    "knowledge_bases",
    "mcp_sites",
    "external_memory_providers",
    "plugins",
    "skills",
    "scheduled_tasks",
    "gateway_home_targets",
)
_SINGLETON_SECTIONS: tuple[str, ...] = ("external_memory_global_config", "task_config")
_NATURAL_KEYS: dict[str, str] = {
    "providers": "name",
    "knowledge_bases": "id",
    "mcp_sites": "name",
    "external_memory_providers": "name",
    "plugins": "key",
    "skills": "name",
    "scheduled_tasks": "name",
    "gateway_home_targets": "platform",
}

# "builtin" is a virtual member of the enabled-provider name set: it names the
# in-process memory, not a row in external_memory_providers.
_BUILTIN_MEMORY_NAME = "builtin"

# Recorded as updated_by on the task_config CAS write so the audit trail says
# a migration, not a human, moved the values.
_IMPORT_ACTOR = "config-import"


@dataclass(frozen=True)
class _ImportContext:
    """Per-call import switches. Kept off self so the service stays reentrant."""

    mode: ImportMode
    with_schedules: bool
    dry_run: bool
    pre_existing: dict[str, list[str]] | None

    def pre_existing_for(self, section: str, current: set[str]) -> set[str]:
        """Natural keys the target already had before this run.

        The caller (the host importer) pre-scans the target and passes the sets
        in, because a record the first scan just created is NOT pre-existing and
        must still receive the bundle flags. Without a pre-scan we fall back to
        the current registry contents, which is what a standalone CLI merge
        needs so it does not clobber flags the operator set locally. An
        explicitly supplied empty list means "nothing existed".
        """
        if self.pre_existing is None or section not in self.pre_existing:
            return set(current)
        return set(self.pre_existing[section])


def _validate_enum_field(
    rows: list[dict[str, Any]], field_name: str, enum_cls: Any, section: str
) -> None:
    for row in rows:
        try:
            enum_cls(row.get(field_name))
        except ValueError as exc:
            raise ConfigBundleValidationError(
                section, f"unknown {field_name} {row.get(field_name)!r}"
            ) from exc


def _secret_of(row: dict[str, Any], key: str) -> SecretValue:
    """Map a payload field onto the four secret states."""
    if key not in row:
        return SecretValue.absent()
    return SecretValue.classify(row[key])


def _secret_update_kwargs(
    secret: SecretValue, current: str | None, key: str
) -> tuple[dict[str, Any], str | None]:
    """Registry kwargs that realise a secret state on an existing row.

    Returns the kwargs plus an optional degradation reason. An empty kwargs
    dict means the target secret is already in the requested state, which is
    what keeps an idempotent rerun on action=none.
    """
    if secret.kind == "redacted":
        return {}, f"{key} was redacted in the bundle; target value kept"
    if secret.kind == "unchanged":
        return {}, None
    if secret.kind == "cleared":
        if current in (None, ""):
            return {}, None
        return {f"clear_{key}": True}, None
    value = secret.reveal()
    if current == value:
        return {}, None
    return {key: value}, None


def _mark_written(item: ImportItemReport, action: ImportAction) -> None:
    """Conclude a written item. Any reason recorded so far means degraded."""
    item.action = action
    if item.reasons:
        item.outcome = ImportOutcome.DEGRADED
    elif action is ImportAction.CREATED:
        item.outcome = ImportOutcome.CREATED
    else:
        item.outcome = ImportOutcome.UPDATED


def _failure_origin(exc: Exception) -> str:
    """The innermost traceback frame as ``file:lineno``.

    Deliberately not ``exc_info`` and not a formatted traceback: both render
    the exception message, which may carry a secret value taken from the row
    being written. A code location cannot, so it is the most that may be
    logged here. See debt D057.
    """
    tb = exc.__traceback__
    if tb is None:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}"


def _mark_failed(item: ImportItemReport, exc: Exception) -> None:
    """Record a failure by exception type only -- messages may carry values."""
    item.outcome = ImportOutcome.FAILED
    item.action = ImportAction.NONE
    item.reasons.append(f"write failed: {type(exc).__name__}")
    logger.error(
        "config bundle import: %s/%s failed with %s at %s",
        item.section,
        item.natural_key,
        type(exc).__name__,
        _failure_origin(exc),
    )


def _mcp_equal(existing: McpSite, candidate: McpSite) -> bool:
    return (
        existing.url == candidate.url
        and existing.transport_type is candidate.transport_type
        and existing.command == candidate.command
        and list(existing.args) == list(candidate.args)
        and dict(existing.env) == dict(candidate.env)
        and existing.enabled == candidate.enabled
    )


def _execution_policy_of(row: dict[str, Any]) -> ScheduledExecutionPolicy:
    policy = dict(row.get("execution_policy") or {})
    return ScheduledExecutionPolicy(
        mode=ScheduledExecutionPolicyMode(policy["mode"]),
        tool_exposure_policy=policy.get("tool_exposure_policy") or "safe_only",
        allow_confirm_tools=bool(policy.get("allow_confirm_tools")),
        allowed_tools=tuple(policy.get("allowed_tools") or ()),
    )


def _scheduled_task_equal(existing: ScheduledTask, payload: dict[str, Any]) -> bool:
    """True when an import_update would be a no-op on the target task."""
    return (
        existing.name == payload["name"]
        and existing.prompt == payload["prompt"]
        and existing.schedule.value == payload["cron_expression"]
        and existing.timezone.value == payload["timezone_value"]
        and existing.delivery_target.target_type.value == payload["delivery_target"]
        and dict(existing.delivery_target.context) == payload["delivery_context"]
        and dict(existing.origin) == payload["origin"]
        and existing.execution_policy == payload["execution_policy"]
        and existing.enabled == payload["enabled"]
        and existing.status is payload["status"]
    )


class ConfigBundleService:
    """Reads the 10 configuration tables through Domain ports and builds a
    machine-portable bundle (and, in T4, applies one idempotently)."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        knowledge_registry: KnowledgeBaseRegistry,
        mcp_registry: McpSiteRegistry,
        external_memory_provider_registry: ExternalMemoryProviderRegistry,
        external_memory_config: ExternalMemoryConfigRegistry,
        plugin_registry: PluginRegistry,
        skill_registry: SkillRegistry,
        schedule_service: "ScheduleService",
        gateway_registry: GatewaySessionRegistry,
        task_config_store: TaskConfigStore,
        task_config_service: "TaskConfigService",
    ) -> None:
        self._providers = provider_registry
        self._knowledge = knowledge_registry
        self._mcp = mcp_registry
        self._external_providers = external_memory_provider_registry
        self._external_config = external_memory_config
        self._plugins = plugin_registry
        self._skills = skill_registry
        self._schedules = schedule_service
        self._gateway = gateway_registry
        self._task_config_store = task_config_store
        self._task_config_service = task_config_service

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    async def export_bundle(
        self, *, redact_secrets: bool, source: BundleSource | None = None
    ) -> ConfigBundle:
        """Read every configuration section into a ConfigBundle.

        source is supplied by the caller (CLI / host wrapper); this layer never
        reads the hostname or the environment.
        """
        sections: dict[str, Any] = {
            "providers": await self._export_providers(redact_secrets),
            "knowledge_bases": await self._export_knowledge_bases(redact_secrets),
            "mcp_sites": await self._export_mcp_sites(redact_secrets),
            "external_memory_providers": self._export_external_providers(redact_secrets),
            "external_memory_global_config": self._export_external_global_config(),
            "plugins": await self._export_plugins(redact_secrets),
            "skills": await self._export_skills(),
            "scheduled_tasks": await self._export_scheduled_tasks(),
            "gateway_home_targets": await self._export_gateway_home_targets(),
            "task_config": await self._export_task_config(),
        }
        return ConfigBundle(
            schema_version=BUNDLE_SCHEMA_VERSION,
            created_at=datetime.now(timezone.utc).isoformat(),
            source=source or BundleSource(hostname="", install_root="", code_root=""),
            redacted=redact_secrets,
            sections=sections,
        )

    async def _export_providers(self, redact: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for provider in await self._providers.list_providers():
            api_key: str | None = None
            extra_headers: dict[str, str] | None = None
            if not redact:
                api_key = (await self._providers.get_secret(provider.id)) or ""
                extra_headers = dict(provider.extra_headers) if provider.extra_headers else None
            rows.append(
                {
                    "name": provider.name,
                    "provider_type": provider.provider_type,
                    "base_url": provider.base_url,
                    "model": provider.model,
                    "api_key": api_key,
                    "extra_headers": extra_headers,
                    "supports_vision": provider.supports_vision,
                    "is_active": provider.is_active,
                }
            )
        return rows

    async def _export_knowledge_bases(self, redact: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for base in await self._knowledge.list_bases():
            api_key: str | None = None
            if not redact:
                api_key = (await self._knowledge.get_secret(base.id)) or ""
            rows.append(
                {
                    "id": base.id,
                    "name": base.name,
                    "description": base.description,
                    "base_type": base.base_type.value,
                    "base_url": base.base_url,
                    "dataset_id": base.dataset_id,
                    "api_key": api_key,
                    "enabled": base.enabled,
                    "default_top_k": base.default_top_k,
                    "default_min_score": base.default_min_score,
                }
            )
        return rows

    async def _export_mcp_sites(self, redact: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for site in await self._mcp.list_sites():
            rows.append(
                {
                    "name": site.name,
                    "transport_type": site.transport_type.value,
                    "url": site.url,
                    "command": site.command,
                    "args": list(site.args),
                    # env is a whole-map secret carrier: None when redacted,
                    # {} when the source really has no env entries.
                    "env": None if redact else dict(site.env),
                    "enabled": site.enabled,
                }
            )
        return rows

    def _export_external_providers(self, redact: bool) -> list[dict[str, Any]]:
        # Synchronous Domain port -- do not await.
        rows: list[dict[str, Any]] = []
        for provider in self._external_providers.list_providers():
            api_key: str | None = None
            extra_config = dict(provider.extra_config or {})
            if redact:
                extra_config = _redact_nested(extra_config, set())
            else:
                secret = self._external_providers.get_secret(provider.id)
                api_key = (secret.api_key if secret is not None else None) or ""
            rows.append(
                {
                    "name": provider.name,
                    "provider_type": provider.provider_type.value,
                    "base_url": provider.base_url,
                    "api_key": api_key,
                    "enabled": provider.enabled,
                    "extra_config": extra_config,
                }
            )
        return rows

    def _export_external_global_config(self) -> dict[str, Any] | None:
        # None = never persisted; set() = explicitly "nothing enabled".
        enabled = self._external_config.get_enabled()
        if enabled is None:
            return None
        return {"enabled_providers": sorted(enabled)}

    async def _export_plugins(self, redact: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for plugin in await self._plugins.list_plugins():
            declared = self._declared_plugin_secret_fields(plugin)
            config = dict(plugin.config or {})
            plugin_secrets: dict[str, str] | None
            if redact:
                config = _redact_nested(config, declared)
                plugin_secrets = None
            else:
                plugin_secrets = dict(await self._plugins.get_secret_config(plugin.key))
            rows.append(
                {
                    "key": plugin.key,
                    "name": plugin.name,
                    "version": plugin.version,
                    "kind": plugin.kind.value,
                    "enabled": plugin.enabled,
                    "config_json": config,
                    "plugin_secrets": plugin_secrets,
                }
            )
        return rows

    @staticmethod
    def _declared_plugin_secret_fields(plugin: Any) -> set[str]:
        """Secret field names declared by the scanned manifest plus the
        registry's own secret_refs index."""
        declared: set[str] = set(plugin.secret_refs or {})
        schema = (plugin.manifest or {}).get("config_schema") or {}
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            for name, definition in properties.items():
                if not isinstance(definition, dict):
                    continue
                if definition.get("secret") is True or definition.get("format") == "password":
                    declared.add(str(name))
        return declared

    async def _export_skills(self) -> list[dict[str, Any]]:
        # Only the two availability flags migrate; existence and source stay
        # authoritative on the target machine's scan.
        return [
            {
                "name": skill.name,
                "enabled": skill.enabled,
                "chat_selectable": skill.chat_selectable,
            }
            for skill in await self._skills.list_skills()
        ]

    async def _export_scheduled_tasks(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in await self._schedules.list():
            if task.status is ScheduledTaskStatus.DELETED:
                continue
            rows.append(self._scheduled_task_payload(task))
        return rows

    @staticmethod
    def _scheduled_task_payload(task: ScheduledTask) -> dict[str, Any]:
        policy = task.execution_policy
        return {
            "name": task.name,
            "prompt": task.prompt,
            "cron_expression": task.schedule.value,
            "timezone": task.timezone.value,
            "enabled": task.enabled,
            "delivery_target": task.delivery_target.target_type.value,
            "origin": dict(task.origin),
            "delivery_context": dict(task.delivery_target.context),
            "execution_policy": {
                "mode": policy.mode.value,
                "tool_exposure_policy": policy.tool_exposure_policy,
                "allow_confirm_tools": policy.allow_confirm_tools,
                "allowed_tools": list(policy.allowed_tools),
            },
            "status": task.status.value,
        }

    async def _export_gateway_home_targets(self) -> list[dict[str, Any]]:
        # No list port: iterate the Platform enum.
        rows: list[dict[str, Any]] = []
        for platform in Platform:
            target = await self._gateway.get_home_target(platform)
            if target is None:
                continue
            rows.append(
                {
                    "platform": target.platform.value,
                    "receive_id": target.receive_id,
                    "receive_id_type": target.receive_id_type,
                    "thread_id": target.thread_id,
                    "display_name": target.display_name,
                }
            )
        return rows

    async def _export_task_config(self) -> dict[str, Any] | None:
        stored = await self._task_config_store.get()
        if stored is None:
            return None
        return {"overrides": stored.overrides.to_dict()}

    # ------------------------------------------------------------------
    # import
    # ------------------------------------------------------------------
    async def import_bundle(
        self,
        bundle: ConfigBundle,
        *,
        mode: ImportMode,
        with_schedules: bool,
        dry_run: bool,
        pre_existing: dict[str, list[str]] | None = None,
    ) -> ImportReport:
        """Apply a bundle to this machine idempotently.

        The whole bundle is validated first: any structural problem raises
        ConfigBundleValidationError before a single row is written. After that
        every section is applied independently, so one bad record degrades or
        fails only itself.

        Section order matters: the external memory provider rows must exist
        before the global enabled-name set can be resolved against them.
        """
        self._validate_bundle(bundle)
        ctx = _ImportContext(
            mode=mode,
            with_schedules=with_schedules,
            dry_run=dry_run,
            pre_existing=pre_existing,
        )
        sections = bundle.sections
        items: list[ImportItemReport] = []
        items += await self._apply_section(
            "providers", lambda: self._import_providers(sections["providers"], ctx)
        )
        items += await self._apply_section(
            "knowledge_bases",
            lambda: self._import_knowledge_bases(sections["knowledge_bases"], ctx),
        )
        items += await self._apply_section(
            "mcp_sites", lambda: self._import_mcp_sites(sections["mcp_sites"], ctx)
        )
        items += await self._apply_section(
            "external_memory_providers",
            lambda: self._import_external_providers(
                sections["external_memory_providers"], ctx
            ),
        )
        items += await self._apply_section(
            "external_memory_global_config",
            lambda: self._import_external_global_config(
                sections["external_memory_global_config"],
                sections["external_memory_providers"],
                ctx,
            ),
        )
        items += await self._apply_section(
            "plugins", lambda: self._import_plugins(sections["plugins"], ctx)
        )
        items += await self._apply_section(
            "skills", lambda: self._import_skills(sections["skills"], ctx)
        )
        items += await self._apply_section(
            "scheduled_tasks",
            lambda: self._import_scheduled_tasks(sections["scheduled_tasks"], ctx),
        )
        items += await self._apply_section(
            "gateway_home_targets",
            lambda: self._import_gateway_home_targets(
                sections["gateway_home_targets"], ctx
            ),
        )
        items += await self._apply_section(
            "task_config", lambda: self._import_task_config(sections["task_config"], ctx)
        )
        return ImportReport(mode=mode, items=items)

    async def _apply_section(
        self, name: str, produce: Callable[[], Any]
    ) -> list[ImportItemReport]:
        """Run one section, turning an escaping error into a FAILED item.

        The per-row guards inside each section keep import_bundle's promise
        that one bad record fails only itself. The work around them does not:
        reading the target's current state to resolve a conflict, or applying
        a singleton, sits outside every try. An error there escapes to the
        CLI, which reports any exception as exit 2 -- documented as "bundle
        format error, target untouched". Earlier sections are committed by
        then, so that code tells the operator the opposite of the truth and
        invites a retry against a target they believe is clean. Failing just
        this section keeps the report honest and yields exit 1, which does
        mean "partly applied".

        Validation is deliberately not covered: _validate_bundle runs before
        this and must keep raising, because there exit 2 is accurate.
        """
        try:
            produced = produce()
            result = await produced if inspect.isawaitable(produced) else produced
        except Exception as exc:  # noqa: BLE001
            # "*" stands for the whole section: the failure is not attributable
            # to one record. Only the exception type is recorded -- a message
            # could carry a value from the row being written.
            item = ImportItemReport(name, "*", ImportOutcome.SKIPPED)
            _mark_failed(item, exc)
            return [item]
        return result if isinstance(result, list) else [result]

    # -- validation ----------------------------------------------------
    @staticmethod
    def _validate_bundle(bundle: ConfigBundle) -> None:
        """Reject a structurally invalid bundle before any write happens."""
        if bundle.schema_version != BUNDLE_SCHEMA_VERSION:
            raise ConfigBundleValidationError(
                "bundle",
                f"unsupported schema_version {bundle.schema_version}, "
                f"this build reads {BUNDLE_SCHEMA_VERSION}",
            )
        sections = bundle.sections
        if not isinstance(sections, dict):
            raise ConfigBundleValidationError("bundle", "sections must be an object")
        for name in SECTION_NAMES:
            if name not in sections:
                raise ConfigBundleValidationError(name, "section is missing from the bundle")
        for name in _SINGLETON_SECTIONS:
            value = sections[name]
            if value is not None and not isinstance(value, dict):
                raise ConfigBundleValidationError(name, "section must be an object or null")
        for name in _COLLECTION_SECTIONS:
            rows = sections[name]
            if not isinstance(rows, list):
                raise ConfigBundleValidationError(name, "section must be a list")
            key_field = _NATURAL_KEYS[name]
            seen: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ConfigBundleValidationError(name, "every record must be an object")
                natural_key = row.get(key_field)
                if not isinstance(natural_key, str) or not natural_key:
                    raise ConfigBundleValidationError(
                        name, f"every record needs a non-empty {key_field}"
                    )
                if natural_key in seen:
                    raise ConfigBundleValidationError(
                        name, f"duplicate natural key {key_field}={natural_key}"
                    )
                seen.add(natural_key)
        active = [row for row in sections["providers"] if row.get("is_active")]
        if len(active) > 1:
            raise ConfigBundleValidationError(
                "providers", "at most one provider may be marked active"
            )
        _validate_enum_field(sections["knowledge_bases"], "base_type", KnowledgeBaseType, "knowledge_bases")
        _validate_enum_field(sections["mcp_sites"], "transport_type", McpTransportType, "mcp_sites")
        _validate_enum_field(
            sections["external_memory_providers"], "provider_type",
            ExternalMemoryProviderType, "external_memory_providers",
        )
        _validate_enum_field(sections["gateway_home_targets"], "platform", Platform, "gateway_home_targets")
        _validate_enum_field(sections["scheduled_tasks"], "status", ScheduledTaskStatus, "scheduled_tasks")
        for row in sections["scheduled_tasks"]:
            policy = row.get("execution_policy")
            if not isinstance(policy, dict):
                raise ConfigBundleValidationError(
                    "scheduled_tasks", "execution_policy must be an object"
                )
            try:
                ScheduledExecutionPolicyMode(policy.get("mode"))
            except ValueError as exc:
                raise ConfigBundleValidationError(
                    "scheduled_tasks", f"unknown execution_policy mode {policy.get('mode')!r}"
                ) from exc

    # -- providers -----------------------------------------------------
    async def _import_providers(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("providers", row["name"], ImportOutcome.SKIPPED)
            items.append(item)
            try:
                await self._import_one_provider(row, ctx, item)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                _mark_failed(item, exc)
        return items

    async def _import_one_provider(
        self, row: dict[str, Any], ctx: "_ImportContext", item: ImportItemReport
    ) -> None:
        secret = _secret_of(row, "api_key")
        existing = next(
            (p for p in await self._providers.list_providers() if p.name == row["name"]), None
        )
        current_active = await self._providers.get_active()
        wants_active = bool(row.get("is_active"))
        active_taken = current_active is not None and (
            existing is None or current_active.id != existing.id
        )
        if wants_active and active_taken:
            item.reasons.append(
                "target already has an active provider; imported provider left inactive"
            )
        if existing is None:
            api_key = ""
            if secret.kind == "set":
                api_key = secret.reveal()
            elif secret.kind == "redacted":
                item.reasons.append("api_key was redacted in the bundle; created without a secret")
            now = datetime.now(timezone.utc)
            config = ProviderConfig(
                id=f"prv-{uuid4().hex}",
                name=row["name"],
                provider_type=row.get("provider_type") or "openai",
                base_url=row.get("base_url") or "",
                model=row.get("model") or "",
                api_key_present=bool(api_key),
                is_active=wants_active and not active_taken,
                extra_headers=row.get("extra_headers"),
                created_at=now,
                updated_at=now,
                supports_vision=bool(row.get("supports_vision")),
            )
            if not ctx.dry_run:
                await self._providers.create_provider(config, api_key)
            _mark_written(item, ImportAction.CREATED)
            return

        if ctx.mode is ImportMode.MERGE:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already has this provider; merge keeps it untouched")
            return

        changes: dict[str, Any] = {}
        for field_name, value in (
            ("base_url", row.get("base_url")),
            ("model", row.get("model")),
            ("provider_type", row.get("provider_type")),
            ("extra_headers", row.get("extra_headers")),
        ):
            if value is not None and getattr(existing, field_name) != value:
                changes[field_name] = value
        if bool(row.get("supports_vision")) != existing.supports_vision:
            changes["supports_vision"] = bool(row.get("supports_vision"))
        current_secret = await self._providers.get_secret(existing.id)
        secret_kwargs, secret_reason = _secret_update_kwargs(secret, current_secret, "api_key")
        if secret_reason:
            item.reasons.append(secret_reason)
        needs_active = wants_active and not active_taken and not existing.is_active
        if not changes and not secret_kwargs and not needs_active:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return
        if not ctx.dry_run:
            if changes or secret_kwargs:
                await self._providers.update_provider(existing.id, **changes, **secret_kwargs)
            if needs_active:
                await self._providers.set_active(existing.id)
        _mark_written(item, ImportAction.UPDATED)

    # -- knowledge_bases -----------------------------------------------
    async def _import_knowledge_bases(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("knowledge_bases", row["id"], ImportOutcome.SKIPPED)
            items.append(item)
            try:
                await self._import_one_knowledge_base(row, ctx, item)
            except Exception as exc:  # noqa: BLE001
                _mark_failed(item, exc)
        return items

    async def _import_one_knowledge_base(
        self, row: dict[str, Any], ctx: "_ImportContext", item: ImportItemReport
    ) -> None:
        secret = _secret_of(row, "api_key")
        existing = await self._knowledge.get_base(row["id"])
        if existing is None:
            api_key: str | None = None
            if secret.kind == "set":
                api_key = secret.reveal()
            elif secret.kind == "redacted":
                item.reasons.append("api_key was redacted in the bundle; created without a secret")
            now = datetime.now(timezone.utc)
            base = KnowledgeBase(
                id=row["id"],
                name=row.get("name") or row["id"],
                description=row.get("description") or "",
                base_type=KnowledgeBaseType(row["base_type"]),
                base_url=row.get("base_url") or "",
                dataset_id=row.get("dataset_id") or "",
                api_key_present=bool(api_key),
                enabled=bool(row.get("enabled")),
                default_top_k=row.get("default_top_k"),
                default_min_score=row.get("default_min_score"),
                last_probe_status=KnowledgeProbeStatus.UNKNOWN,
                last_probe_error=None,
                last_probed_at=None,
                created_at=now,
                updated_at=now,
            )
            if not ctx.dry_run:
                await self._knowledge.create_base(base, api_key)
            _mark_written(item, ImportAction.CREATED)
            return

        if ctx.mode is ImportMode.MERGE:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already has this knowledge base; merge keeps it untouched")
            return

        changes: dict[str, Any] = {}
        desired = {
            "name": row.get("name"),
            "description": row.get("description"),
            "base_type": KnowledgeBaseType(row["base_type"]),
            "base_url": row.get("base_url"),
            "dataset_id": row.get("dataset_id"),
            "enabled": bool(row.get("enabled")),
            "default_top_k": row.get("default_top_k"),
            "default_min_score": row.get("default_min_score"),
        }
        for field_name, value in desired.items():
            if value is None:
                # None on top_k / min_score means "no limit configured".
                if field_name == "default_top_k" and existing.default_top_k is not None:
                    changes["clear_default_top_k"] = True
                if field_name == "default_min_score" and existing.default_min_score is not None:
                    changes["clear_default_min_score"] = True
                continue
            if getattr(existing, field_name) != value:
                changes[field_name] = value
        current_secret = await self._knowledge.get_secret(existing.id)
        secret_kwargs, secret_reason = _secret_update_kwargs(secret, current_secret, "api_key")
        if secret_reason:
            item.reasons.append(secret_reason)
        if not changes and not secret_kwargs:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return
        if not ctx.dry_run:
            await self._knowledge.update_base(existing.id, **changes, **secret_kwargs)
        _mark_written(item, ImportAction.UPDATED)

    # -- mcp_sites -----------------------------------------------------
    async def _import_mcp_sites(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("mcp_sites", row["name"], ImportOutcome.SKIPPED)
            items.append(item)
            try:
                await self._import_one_mcp_site(row, ctx, item)
            except Exception as exc:  # noqa: BLE001
                _mark_failed(item, exc)
        return items

    async def _import_one_mcp_site(
        self, row: dict[str, Any], ctx: "_ImportContext", item: ImportItemReport
    ) -> None:
        # env is a whole-map secret carrier: None means the export redacted it.
        raw_env = row.get("env")
        env_redacted = raw_env is None
        env = {} if env_redacted else dict(raw_env)
        existing = await self._mcp.get_site_by_name(row["name"])
        transport = McpTransportType(row["transport_type"])
        args = list(row.get("args") or [])
        if existing is None:
            if env_redacted:
                item.reasons.append("env was redacted in the bundle; created without env values")
            site = McpSite(
                name=row["name"],
                url=row.get("url") or "",
                id=f"mcp-{uuid4().hex}",
                transport_type=transport,
                command=row.get("command"),
                args=args,
                env=env,
                enabled=bool(row.get("enabled")),
                last_probe_status=McpProbeStatus.NEVER,
                last_probe_error=None,
                last_probed_at=None,
            )
            if not ctx.dry_run:
                await self._mcp.create_site(site)
            _mark_written(item, ImportAction.CREATED)
            return

        if ctx.mode is ImportMode.MERGE:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already has this MCP site; merge keeps it untouched")
            return

        if env_redacted:
            item.reasons.append("env was redacted in the bundle; target env kept")
            env = dict(existing.env)
        candidate = McpSite(
            name=row["name"],
            url=row.get("url") or "",
            id=existing.id,
            transport_type=transport,
            command=row.get("command"),
            args=args,
            env=env,
            enabled=bool(row.get("enabled")),
            last_probe_status=existing.last_probe_status,
            last_probe_error=existing.last_probe_error,
            last_probed_at=existing.last_probed_at,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        if _mcp_equal(existing, candidate):
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return
        if not ctx.dry_run:
            await self._mcp.update_site(candidate)
        _mark_written(item, ImportAction.UPDATED)

    # -- external_memory_providers (synchronous port) --------------------
    def _import_external_providers(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        bundle_names = {row["name"] for row in rows}
        # A target provider that is enabled and absent from the bundle owns the
        # single external-query slot: imported providers must not steal it.
        slot_owner = next(
            (
                p
                for p in self._external_providers.list_providers()
                if p.enabled and p.name not in bundle_names
            ),
            None,
        )
        granted = False
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("external_memory_providers", row["name"], ImportOutcome.SKIPPED)
            items.append(item)
            wants_enabled = bool(row.get("enabled"))
            desired_enabled = wants_enabled and slot_owner is None and not granted
            if wants_enabled and not desired_enabled:
                item.reasons.append(
                    "the single external-query slot is already taken on the target; "
                    "imported provider left disabled"
                )
            try:
                self._import_one_external_provider(row, ctx, item, desired_enabled)
            except Exception as exc:  # noqa: BLE001
                _mark_failed(item, exc)
                continue
            if desired_enabled:
                granted = True
        return items

    def _import_one_external_provider(
        self,
        row: dict[str, Any],
        ctx: "_ImportContext",
        item: ImportItemReport,
        desired_enabled: bool,
    ) -> None:
        secret = _secret_of(row, "api_key")
        existing = next(
            (p for p in self._external_providers.list_providers() if p.name == row["name"]), None
        )
        provider_type = ExternalMemoryProviderType(row["provider_type"])
        extra_config = dict(row.get("extra_config") or {})
        if existing is None:
            api_key: str | None = None
            if secret.kind == "set":
                api_key = secret.reveal()
            elif secret.kind == "redacted":
                item.reasons.append("api_key was redacted in the bundle; created without a secret")
            if not ctx.dry_run:
                self._external_providers.create_provider(
                    id=f"emp-{uuid4().hex}",
                    name=row["name"],
                    provider_type=provider_type,
                    base_url=row.get("base_url") or "",
                    api_key=api_key,
                    enabled=desired_enabled,
                    extra_config=extra_config,
                )
            _mark_written(item, ImportAction.CREATED)
            return

        if ctx.mode is ImportMode.MERGE:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append(
                "target already has this external memory provider; merge keeps it untouched"
            )
            return

        changes: dict[str, Any] = {}
        if row.get("base_url") is not None and existing.base_url != row["base_url"]:
            changes["base_url"] = row["base_url"]
        if existing.provider_type is not provider_type:
            changes["provider_type"] = provider_type
        if existing.enabled != desired_enabled:
            changes["enabled"] = desired_enabled
        if dict(existing.extra_config or {}) != extra_config:
            changes["extra_config"] = extra_config
        stored = self._external_providers.get_secret(existing.id)
        current_secret = stored.api_key if stored is not None else None
        secret_kwargs, secret_reason = _secret_update_kwargs(secret, current_secret, "api_key")
        if secret_reason:
            item.reasons.append(secret_reason)
        if not changes and not secret_kwargs:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return
        if not ctx.dry_run:
            self._external_providers.update_provider(existing.id, **changes, **secret_kwargs)
        _mark_written(item, ImportAction.UPDATED)

    # -- external_memory_global_config (synchronous port) ----------------
    def _import_external_global_config(
        self,
        section: dict[str, Any] | None,
        provider_rows: list[dict[str, Any]],
        ctx: "_ImportContext",
    ) -> ImportItemReport:
        item = ImportItemReport(
            "external_memory_global_config", "external_memory_global_config", ImportOutcome.SKIPPED
        )
        if section is None:
            # None means "never configured on the source": writing set() here
            # would silently turn the target's builtin memory off.
            item.reasons.append("the bundle never configured this section; target left untouched")
            return item
        current = self._external_config.get_enabled()
        bundle_names = {row["name"] for row in provider_rows}
        slot_owner = next(
            (
                p
                for p in self._external_providers.list_providers()
                if p.enabled and p.name not in bundle_names
            ),
            None,
        )
        if slot_owner is not None:
            item.outcome = ImportOutcome.DEGRADED
            item.action = ImportAction.NONE
            item.reasons.append(
                "the target's external-query slot is held by a provider that is not in "
                "the bundle; the enabled set was left untouched"
            )
            return item
        resolvable = {p.name for p in self._external_providers.list_providers()}
        resolvable.add(_BUILTIN_MEMORY_NAME)
        desired: list[str] = []
        for name in section.get("enabled_providers") or []:
            if name in resolvable:
                desired.append(name)
            else:
                item.reasons.append(
                    f"enabled provider {name} does not resolve on this machine; dropped"
                )
        if current is not None and set(desired) == current:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return item
        if current is not None and ctx.mode is ImportMode.MERGE:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already configured this section; merge keeps it untouched")
            return item
        if not ctx.dry_run:
            self._external_config.set_enabled(desired)
        _mark_written(item, ImportAction.UPDATED)
        return item

    # -- plugins ---------------------------------------------------------
    async def _import_plugins(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        installed = {p.key for p in await self._plugins.list_plugins()}
        pre_existing = ctx.pre_existing_for("plugins", installed)
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("plugins", row["key"], ImportOutcome.SKIPPED)
            items.append(item)
            try:
                await self._import_one_plugin(row, ctx, item, pre_existing)
            except Exception as exc:  # noqa: BLE001
                _mark_failed(item, exc)
        return items

    async def _import_one_plugin(
        self,
        row: dict[str, Any],
        ctx: "_ImportContext",
        item: ImportItemReport,
        pre_existing: set[str],
    ) -> None:
        key = row["key"]
        existing = await self._plugins.get_plugin(key)
        if existing is None:
            # Plugin existence is owned by the target machine's scan; the
            # bundle only carries enable state, config and secrets.
            item.outcome = ImportOutcome.DEGRADED
            item.action = ImportAction.NONE
            item.reasons.append("plugin is not installed on this machine; nothing applied")
            return
        if row.get("version") and existing.version != row["version"]:
            item.outcome = ImportOutcome.DEGRADED
            item.action = ImportAction.NONE
            item.reasons.append(
                f"version mismatch: bundle {row['version']}, target {existing.version}; "
                "config not applied"
            )
            return
        if ctx.mode is ImportMode.MERGE and key in pre_existing:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already had this plugin; merge keeps its config")
            return
        raw_secrets = row.get("plugin_secrets")
        if raw_secrets is None:
            item.reasons.append("plugin_secrets were redacted in the bundle; target secrets kept")
            secret_updates: dict[str, str] = {}
        else:
            secret_updates = {str(k): str(v) for k, v in dict(raw_secrets).items()}
        config = dict(row.get("config_json") or {})
        enabled = bool(row.get("enabled"))
        current_secrets = await self._plugins.get_secret_config(key)
        secrets_differ = any(current_secrets.get(k) != v for k, v in secret_updates.items())
        config_differs = dict(existing.config or {}) != config
        enabled_differs = existing.enabled != enabled
        if not config_differs and not secrets_differ and not enabled_differs:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return
        if not ctx.dry_run:
            if enabled_differs:
                await self._plugins.set_enabled(key, enabled)
            if config_differs or secrets_differ:
                await self._plugins.update_config(
                    key, config, secret_updates if secret_updates else None
                )
        _mark_written(item, ImportAction.UPDATED)

    # -- skills ----------------------------------------------------------
    async def _import_skills(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        present = {s.name for s in await self._skills.list_skills()}
        pre_existing = ctx.pre_existing_for("skills", present)
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("skills", row["name"], ImportOutcome.SKIPPED)
            items.append(item)
            try:
                await self._import_one_skill(row, ctx, item, pre_existing)
            except Exception as exc:  # noqa: BLE001
                _mark_failed(item, exc)
        return items

    async def _import_one_skill(
        self,
        row: dict[str, Any],
        ctx: "_ImportContext",
        item: ImportItemReport,
        pre_existing: set[str],
    ) -> None:
        name = row["name"]
        existing = await self._skills.get_skill(name)
        if existing is None:
            # Skill existence and content are owned by the target's scan; only
            # the two availability flags migrate, so a missing skill degrades.
            item.outcome = ImportOutcome.DEGRADED
            item.action = ImportAction.NONE
            item.reasons.append("skill is not present on this machine; flags not applied")
            return
        if ctx.mode is ImportMode.MERGE and name in pre_existing:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already had this skill; merge keeps its flags")
            return
        enabled = bool(row.get("enabled"))
        chat_selectable = bool(row.get("chat_selectable"))
        enabled_differs = existing.enabled != enabled
        selectable_differs = existing.chat_selectable != chat_selectable
        if not enabled_differs and not selectable_differs:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return
        if not ctx.dry_run:
            if enabled_differs:
                await self._skills.set_enabled(name, enabled)
            if selectable_differs:
                await self._skills.set_chat_selectable(name, chat_selectable)
        _mark_written(item, ImportAction.UPDATED)

    # -- scheduled_tasks ---------------------------------------------------
    async def _import_scheduled_tasks(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("scheduled_tasks", row["name"], ImportOutcome.SKIPPED)
            items.append(item)
            try:
                await self._import_one_scheduled_task(row, ctx, item)
            except Exception as exc:  # noqa: BLE001
                _mark_failed(item, exc)
        return items

    async def _import_one_scheduled_task(
        self, row: dict[str, Any], ctx: "_ImportContext", item: ImportItemReport
    ) -> None:
        name = row["name"]
        matches = [
            task
            for task in await self._schedules.list()
            if task.name == name and task.status is not ScheduledTaskStatus.DELETED
        ]
        if len(matches) > 1:
            item.outcome = ImportOutcome.FAILED
            item.action = ImportAction.NONE
            item.reasons.append(
                f"{len(matches)} target tasks share this name; refusing to guess which one to update"
            )
            return
        policy = _execution_policy_of(row)
        # A schedule only ever comes back enabled when the operator asked for it
        # AND the source task was runnable.
        enabled = bool(row.get("enabled")) and ctx.with_schedules
        status = ScheduledTaskStatus(row["status"]) if ctx.with_schedules else ScheduledTaskStatus.PAUSED
        payload = {
            "name": name,
            "prompt": row.get("prompt") or "",
            "cron_expression": row.get("cron_expression") or "",
            "timezone_value": row.get("timezone") or "",
            "delivery_target": row.get("delivery_target") or "dashboard",
            "origin": dict(row.get("origin") or {}),
            "delivery_context": dict(row.get("delivery_context") or {}),
            "execution_policy": policy,
            "enabled": enabled,
            "status": status,
        }
        if not matches:
            if not ctx.dry_run:
                await self._schedules.import_create(**payload)
            _mark_written(item, ImportAction.CREATED)
            return

        existing = matches[0]
        if ctx.mode is ImportMode.MERGE:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already has this scheduled task; merge keeps it untouched")
            return
        if _scheduled_task_equal(existing, payload):
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return
        if not ctx.dry_run:
            await self._schedules.import_update(existing.id, **payload)
        _mark_written(item, ImportAction.UPDATED)

    # -- gateway_home_targets ---------------------------------------------
    async def _import_gateway_home_targets(
        self, rows: list[dict[str, Any]], ctx: "_ImportContext"
    ) -> list[ImportItemReport]:
        items: list[ImportItemReport] = []
        for row in rows:
            item = ImportItemReport("gateway_home_targets", row["platform"], ImportOutcome.SKIPPED)
            items.append(item)
            try:
                await self._import_one_home_target(row, ctx, item)
            except Exception as exc:  # noqa: BLE001
                _mark_failed(item, exc)
        return items

    async def _import_one_home_target(
        self, row: dict[str, Any], ctx: "_ImportContext", item: ImportItemReport
    ) -> None:
        platform = Platform(row["platform"])
        existing = await self._gateway.get_home_target(platform)
        candidate = GatewayHomeTarget(
            platform=platform,
            receive_id=row.get("receive_id") or "",
            receive_id_type=row.get("receive_id_type") or "",
            thread_id=row.get("thread_id") or "",
            display_name=row.get("display_name") or "",
            updated_at=datetime.now(timezone.utc),
        )
        if existing is not None:
            if ctx.mode is ImportMode.MERGE:
                item.outcome = ImportOutcome.SKIPPED
                item.action = ImportAction.NONE
                item.reasons.append("target already has a home target; merge keeps it untouched")
                return
            same = (
                existing.receive_id == candidate.receive_id
                and existing.receive_id_type == candidate.receive_id_type
                and existing.thread_id == candidate.thread_id
                and existing.display_name == candidate.display_name
            )
            if same:
                item.outcome = ImportOutcome.SKIPPED
                item.action = ImportAction.NONE
                return
        if not ctx.dry_run:
            await self._gateway.set_home_target(candidate)
        _mark_written(
            item, ImportAction.CREATED if existing is None else ImportAction.UPDATED
        )

    # -- task_config --------------------------------------------------------
    async def _import_task_config(
        self, section: dict[str, Any] | None, ctx: "_ImportContext"
    ) -> ImportItemReport:
        item = ImportItemReport("task_config", "task_config", ImportOutcome.SKIPPED)
        if section is None:
            item.reasons.append("the bundle carries no task_config row; target left untouched")
            return item
        overrides = section.get("overrides")
        if not isinstance(overrides, dict):
            item.outcome = ImportOutcome.FAILED
            item.action = ImportAction.NONE
            item.reasons.append("task_config.overrides must be an object")
            return item
        stored = await self._task_config_store.get()
        if stored is not None and ctx.mode is ImportMode.MERGE:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            item.reasons.append("target already has a task_config row; merge keeps it untouched")
            return item
        if stored is not None and stored.overrides.to_dict() == overrides:
            item.outcome = ImportOutcome.SKIPPED
            item.action = ImportAction.NONE
            return item
        expected_version = stored.version if stored is not None else 0
        try:
            # The service owns validation against THIS machine's env base and
            # the CAS write; this layer never assembles a TaskConfig itself.
            await self._task_config_service.replace_overrides(
                overrides,
                expected_version,
                updated_by=_IMPORT_ACTOR,
                dry_run=ctx.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            _mark_failed(item, exc)
            return item
        _mark_written(
            item, ImportAction.CREATED if stored is None else ImportAction.UPDATED
        )
        return item
