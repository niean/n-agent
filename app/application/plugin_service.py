from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from app.application.plugin_dependency import (
    PIP_MISSING,
    PIP_INCOMPATIBLE,
    PIP_OK,
    PIP_SKIPPED,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNSUPPORTED,
    DepAvailability,
    PipCheckResult,
    build_dependency_status,
    check_pip_dependency,
    classify_dep,
    compute_overall_status,
    cycle_error_message,
    topological_order,
)
from app.domain.plugin import (
    Plugin,
    PluginKind,
    PluginManifest,
    PluginNotFoundError,
    PluginRegistry,
    PluginScanError,
    PluginScanStatus,
    PluginSource,
    PluginValidationError,
    new_plugin_id,
)
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)

logger = logging.getLogger(__name__)


@dataclass
class PluginToolRegistration:
    plugin_key: str
    name: str
    schema: dict[str, Any]
    handler: Callable[..., Any]
    toolset: str = "plugin"
    risk_level: RiskLevel = RiskLevel.SAFE
    timeout_seconds: int | None = None
    emoji: str = ""
    check_fn: Callable[[], bool] | None = None
    requires_env: list[Any] | None = None
    is_async: bool = False
    override: bool = False
    description: str = ""
    available: bool = True
    unavailable_reason: str | None = None
    plugin_config: dict[str, Any] = field(default_factory=dict)
    secret_config: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginScanWarning:
    relative_path: str
    reason: str
    detail: str | None = None
    first_path: str | None = None


@dataclass(frozen=True)
class PluginScanResult:
    manifests: list[PluginManifest]
    registrations: dict[str, list[PluginToolRegistration]]
    warnings: list[PluginScanWarning]
    errors: dict[str, str]
    unsupported: dict[str, list[str]]
    hook_registrations: dict[str, list[HookRegistration]] = field(default_factory=dict)
    cli_command_registrations: dict[str, list[PluginCliCommand]] = field(default_factory=dict)


VALID_HOOKS: frozenset[str] = frozenset({
    "on_session_start",
    "on_session_end",
    "on_turn_start",
    "on_turn_end",
    "pre_llm_call",
    "post_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "transform_tool_result",
    "transform_llm_output",
    "on_pre_compress",
    "pre_finalize",
})

_CLI_COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class HookRegistration:
    plugin_key: str
    hook_name: str
    callback: Callable[..., Any]
    registration_index: int


@dataclass(frozen=True)
class PluginCliCommand:
    plugin_key: str
    name: str
    help: str
    description: str
    setup_fn: Callable[..., Any]
    handler_fn: Callable[..., Any] | None
    registration_index: int


@runtime_checkable
class PluginFileLoaderProtocol(Protocol):
    """Loader protocol supporting both the 3-phase primitives and the
    backward-compatible single ``scan()`` method.

    The 3-phase flow (discover -> prepare -> load_and_register) is the
    primary path. The ``scan()`` method is retained for backward compat but
    is no longer called by ``PluginService.scan()``.
    """

    def discover(self) -> Any: ...  # PluginDiscoveryResult

    def prepare(self, candidate: Any) -> Any: ...  # PreparedPlugin

    def load_and_register(
        self,
        prepared: Any,
        plugin_config: dict[str, Any],
        secret_config: dict[str, str],
    ) -> PluginContext: ...

    async def scan(
        self,
        enabled_keys: set[str],
        disabled_keys: set[str],
        config_provider: Callable[[str], dict[str, Any]],
        secret_provider: Callable[[str], dict[str, str]],
    ) -> PluginScanResult: ...


class PluginContext:
    """Hermes-compatible plugin registration context.

    register_tool signature mirrors Hermes PluginContext.register_tool exactly:
    name, toolset, schema, handler, check_fn, requires_env, is_async, description, emoji, override.
    """

    def __init__(
        self,
        plugin_key: str,
        plugin_config: dict[str, Any] | None = None,
        secret_config: dict[str, str] | None = None,
    ):
        self.plugin_key = plugin_key
        self.plugin_config: dict[str, Any] = dict(plugin_config or {})
        self.secret_config: dict[str, str] = dict(secret_config or {})
        self.tool_registrations: list[PluginToolRegistration] = []
        self.hook_registrations: list[HookRegistration] = []
        self.cli_command_registrations: list[PluginCliCommand] = []
        self._registration_index: int = 0
        self.unsupported_capabilities: list[str] = []
        self.warnings: list[str] = []

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        requires_env: list[Any] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        if not name:
            raise PluginValidationError("plugin register_tool: name is required")
        if not isinstance(schema, dict):
            raise PluginValidationError(f"plugin {self.plugin_key}: schema must be a dict")
        normalized_schema = schema
        if "function" in schema and isinstance(schema.get("function"), dict):
            normalized_schema = schema["function"]
        self.tool_registrations.append(
            PluginToolRegistration(
                plugin_key=self.plugin_key,
                name=name,
                schema=normalized_schema,
                handler=handler,
                toolset=toolset or "plugin",
                check_fn=check_fn,
                requires_env=requires_env,
                is_async=is_async,
                description=description,
                emoji=emoji,
                override=override,
                plugin_config=self.plugin_config,
                secret_config=self.secret_config,
            )
        )

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        if not isinstance(hook_name, str):
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_hook: hook_name must be a string"
            )
        if not hook_name:
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_hook: hook_name is required"
            )
        if not callable(callback):
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_hook: callback must be callable"
            )
        if hook_name not in VALID_HOOKS:
            logger.warning(
                "plugin %s: register_hook: unknown hook name %r; "
                "storing for forward-compat (no dispatch point exists)",
                self.plugin_key,
                hook_name,
            )
        index = self._registration_index
        self.hook_registrations.append(
            HookRegistration(
                plugin_key=self.plugin_key,
                hook_name=hook_name,
                callback=callback,
                registration_index=index,
            )
        )
        self._registration_index += 1

    def register_cli_command(
        self,
        name: str,
        help: str,
        setup_fn: Callable[..., Any],
        handler_fn: Callable[..., Any] | None = None,
        description: str = "",
    ) -> None:
        if not isinstance(name, str) or not _CLI_COMMAND_NAME_RE.match(name):
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_cli_command: "
                f"name must match ^[a-z][a-z0-9-]*$"
            )
        if not isinstance(help, str) or not help:
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_cli_command: "
                f"help must be a non-empty string"
            )
        if not callable(setup_fn):
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_cli_command: "
                f"setup_fn must be callable"
            )
        if handler_fn is not None and not callable(handler_fn):
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_cli_command: "
                f"handler_fn must be callable or None"
            )
        if not isinstance(description, str):
            raise PluginValidationError(
                f"plugin {self.plugin_key}: register_cli_command: "
                f"description must be a string"
            )
        index = self._registration_index
        self.cli_command_registrations.append(
            PluginCliCommand(
                plugin_key=self.plugin_key,
                name=name,
                help=help,
                description=description,
                setup_fn=setup_fn,
                handler_fn=handler_fn,
                registration_index=index,
            )
        )
        self._registration_index += 1

    def register_command(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("command")

    def register_platform(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("platform")

    def register_web_search_provider(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("web_search_provider")

    def register_image_gen_provider(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("image_gen_provider")

    def register_video_gen_provider(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("video_gen_provider")

    def register_tts_provider(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("tts_provider")

    def register_transcription_provider(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("transcription_provider")

    def register_dashboard_auth_provider(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("dashboard_auth_provider")

    def register_auxiliary_task(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("auxiliary_task")

    def register_skill(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("skill")

    def _unsupported(self, capability: str) -> None:
        self.unsupported_capabilities.append(capability)
        self.warnings.append(f"unsupported capability: {capability}")

    async def dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"error": f"plugin dispatch_tool unsupported for {name}"}

    @property
    def llm(self) -> Any:
        raise PluginValidationError("plugin llm facade unsupported")


class PluginService:
    def __init__(
        self,
        registry: PluginRegistry,
        loader: PluginFileLoaderProtocol,
        tool_service: Any,
        route_refresher: Callable[[set[str]], None],
        settings: Any,
    ):
        self._registry = registry
        self._loader = loader
        self._tool_service = tool_service
        self._route_refresher = route_refresher
        self._settings = settings
        self._registrations: dict[str, PluginToolRegistration] = {}
        self._plugin_registrations: dict[str, list[PluginToolRegistration]] = {}
        self._manifests_by_key: dict[str, PluginManifest] = {}
        self._hooks: dict[str, tuple[HookRegistration, ...]] = {}
        self._cli_commands: tuple[PluginCliCommand, ...] = ()
        self._scan_lock = asyncio.Lock()
        # T9: atomic publish with generation snapshot + compensating rollback.
        # generation_id increments ONLY after ALL publish steps succeed.
        # last_scan_error records publish/rollback failures (service-level).
        # _last_* track the previous successful generation's committed state
        # for compensating rollback (independent of ToolService internals).
        self.generation_id: int = 0
        self.last_scan_error: str | None = None
        self._last_route_names: set[str] = set()
        self._last_tool_defs: list[ToolDefinition] = []
        self._last_tool_override_names: set[str] = set()

    async def list_plugins(self, include_disabled: bool = True) -> list[Plugin]:
        return await self._registry.list_plugins(include_disabled=include_disabled)

    async def get_plugin(self, key: str) -> Plugin | None:
        return await self._registry.get_plugin(key)

    def list_cli_commands(self) -> list[PluginCliCommand]:
        """Return a list COPY of the immutable CLI command snapshot.

        Callers can mutate the returned list without affecting internal
        state. The internal snapshot ``_cli_commands`` is a tuple
        (immutable), ordered by (plugin load order, registration_index).
        Disabled, admission-failed, and register-failed plugins contribute
        no commands.
        """
        return list(self._cli_commands)

    async def scan(self) -> PluginScanResult:
        """Execute the full admission pipeline:

        1. discover() -> all candidates + priority-resolved winners
        2. compute effective enabled set:
           (registry enabled ∪ settings.plugins_enabled) - settings.plugins_disabled
        3. persist settings-forced enabled state to registry
        4. prepare enabled winners (entry-point ep.load() happens here)
        5. pip check each enabled manifest
        6. topological order + cycle detection
        7. ordered load_and_register with dependency admission:
           a dependency plugin only satisfies dependents if it registers
           successfully AND its own dependencies are available
        8. build dependency_status + overall status for each discovered winner
        9. build PluginScanResult (manifests, registrations, hooks, CLI, errors)
        10. atomic publish with generation snapshot + compensating rollback:
            commit in spec order (ToolService -> routes -> in-memory -> registry);
            on ANY step failure, compensate in reverse order restoring the
            previous successful generation; generation_id increments ONLY
            after ALL publish steps succeed.

        ``_compute_tool_surface``, ``_compute_hooks``, and
        ``_compute_cli_commands`` compute candidate state before publish.
        """
        async with self._scan_lock:
            # 1. Load current registry state
            existing_plugins = await self._registry.list_plugins(include_disabled=True)
            registry_enabled = {p.key for p in existing_plugins if p.enabled}

            # 2. Discover (sync; loader handles file I/O)
            discovery = self._loader.discover()
            winners = discovery.winners
            discovered_keys: set[str] = set(winners.keys())
            discovery_index = {
                key: cand.discovery_index for key, cand in winners.items()
            }

            # 3. Compute effective enabled set + persist settings-forced state
            effective_enabled = await self._compute_and_persist_effective_enabled(
                existing_plugins, registry_enabled, discovered_keys,
            )

            # 4. Prepare enabled winners + collect manifests
            manifests_by_key: dict[str, PluginManifest] = {}
            prepared_by_key: dict[str, Any] = {}
            discovery_failed_keys: set[str] = set()
            unsupported_keys: set[str] = set()
            prepare_warnings: list[PluginScanWarning] = []
            # ``errors`` is initialized here (not in section 7) so that prepare
            # failures can be recorded with stable diagnostic codes, not just
            # warnings. The Plugin building loop reads ``errors.get(key)`` as
            # ``load_error`` to compute STATUS_FAILED.
            errors: dict[str, str] = {}

            for key, candidate in winners.items():
                if candidate.manifest is None:
                    discovery_failed_keys.add(key)
                    continue
                manifests_by_key[key] = candidate.manifest
                if key not in effective_enabled:
                    continue
                manifest = candidate.manifest
                if manifest.kind is not PluginKind.STANDALONE:
                    unsupported_keys.add(key)
                    continue
                try:
                    prepared = self._loader.prepare(candidate)
                    prepared_by_key[key] = prepared
                    # Use the supplemented manifest (from prepare) for all
                    # downstream phases: pip check, topo order, Plugin building.
                    # For entry-point plugins, prepared.manifest carries fields
                    # supplemented from PLUGIN_MANIFEST (pip_dependencies,
                    # requires_plugins, description, etc.); for directory
                    # plugins, prepared.manifest is the same as candidate.manifest.
                    manifests_by_key[key] = prepared.manifest
                    for msg in prepared.warnings:
                        prepare_warnings.append(PluginScanWarning(
                            relative_path=candidate.path,
                            reason="prepare_warning",
                            detail=msg,
                        ))
                except Exception as exc:
                    code = getattr(exc, "code", "prepare_failed")
                    logger.warning(
                        "plugin %s prepare failed: %s", key, exc, exc_info=True,
                    )
                    discovery_failed_keys.add(key)
                    # Record the prepare failure as a stable diagnostic in
                    # ``errors`` (not just a warning) so that the Plugin
                    # building loop computes STATUS_FAILED via ``load_error``
                    # and the public PluginScanResult carries the code.
                    errors[key] = f"{code}: {exc}"
                    prepare_warnings.append(PluginScanWarning(
                        relative_path=candidate.path,
                        reason="prepare_failed",
                        detail=f"{code}: {exc}",
                    ))

            # 5. pip check for enabled standalone manifests
            pip_results_by_key: dict[str, list[PipCheckResult]] = {}
            packaging_warnings_by_key: dict[str, list[str]] = {}
            for key, manifest in manifests_by_key.items():
                if key in unsupported_keys or key in discovery_failed_keys:
                    continue
                if key not in effective_enabled:
                    continue
                results = [check_pip_dependency(spec) for spec in manifest.pip_dependencies]
                pip_results_by_key[key] = results
                packaging_warnings_by_key[key] = []

            # 6. Topological order + cycle detection (graph from enabled winners)
            # Only keys with a discovered manifest participate in the graph.
            graph_keys = (
                (effective_enabled & set(manifests_by_key.keys()))
                - unsupported_keys
                - discovery_failed_keys
            )
            requires_plugins_by_key = {
                key: manifests_by_key[key].requires_plugins
                for key in graph_keys
                if key in manifests_by_key
            }
            topo = topological_order(
                enabled_keys=graph_keys,
                requires_plugins_by_key=requires_plugins_by_key,
                discovery_index=discovery_index,
            )

            # 7. Admission + load in topo order
            load_failed_keys: set[str] = set()
            unavailable_keys: set[str] = set()
            # Nodes that depend on a cycle but aren't cycle members are
            # blocked: they can never be loaded, so mark them unavailable
            # upfront so their own dependents see "required plugin unavailable".
            unavailable_keys.update(topo.blocked_keys)
            admitted_keys: set[str] = set()
            registrations: dict[str, list[PluginToolRegistration]] = {}
            unsupported_caps: dict[str, list[str]] = {}
            hook_registrations: dict[str, list[HookRegistration]] = {}
            cli_command_registrations: dict[str, list[PluginCliCommand]] = {}

            # Map each cycle member to its own cycle's sorted members, so
            # the error message only lists that cycle (not all cycles).
            cycle_member_to_members: dict[str, list[str]] = {}
            for cycle in topo.cycles:
                members = sorted(cycle)
                for member in cycle:
                    cycle_member_to_members[member] = members

            # Build config/secret caches for admitted plugins
            config_cache: dict[str, dict[str, Any]] = {}
            secret_cache: dict[str, dict[str, str]] = {}

            for key in topo.order:
                if key in topo.cycle_members:
                    continue
                manifest = manifests_by_key[key]

                # Check requires_plugins availability
                dep_availability: list[DepAvailability] = []
                deps_ok = True
                for dep_key in manifest.requires_plugins:
                    dep = classify_dep(
                        dep_key,
                        discovered_keys=discovered_keys,
                        enabled_keys=effective_enabled,
                        unsupported_keys=unsupported_keys,
                        cycle_members=set(topo.cycle_members),
                        load_failed_keys=load_failed_keys,
                        unavailable_keys=unavailable_keys,
                    )
                    dep_availability.append(dep)
                    if not dep.available:
                        deps_ok = False

                # Check pip
                pip_results = pip_results_by_key.get(key, [])
                pip_ok = all(
                    r.status in (PIP_OK, PIP_SKIPPED) for r in pip_results
                )

                if not deps_ok or not pip_ok:
                    unavailable_keys.add(key)
                    continue

                prepared = prepared_by_key.get(key)
                if prepared is None:
                    unavailable_keys.add(key)
                    continue

                # Load config/secret (cached)
                if key not in config_cache:
                    plugin = await self._registry.get_plugin(key)
                    config_cache[key] = dict(plugin.config) if plugin else {}
                    secret_cache[key] = await self._registry.get_secret_config(key)

                try:
                    ctx = self._loader.load_and_register(
                        prepared,
                        config_cache[key],
                        secret_cache[key],
                    )
                except Exception as exc:
                    code = getattr(exc, "code", "load_failed")
                    logger.warning(
                        "plugin %s load failed: %s", key, exc, exc_info=True,
                    )
                    load_failed_keys.add(key)
                    errors[key] = f"{code}: {exc}"
                    continue

                admitted_keys.add(key)
                if ctx.tool_registrations:
                    registrations[key] = list(ctx.tool_registrations)
                if ctx.unsupported_capabilities:
                    unsupported_caps[key] = list(ctx.unsupported_capabilities)
                if ctx.hook_registrations:
                    hook_registrations[key] = list(ctx.hook_registrations)
                if ctx.cli_command_registrations:
                    cli_command_registrations[key] = list(ctx.cli_command_registrations)

            # 8. Build Plugin objects with dependency_status + status
            existing_by_key = {p.key: p for p in existing_plugins}
            merged_plugins: list[Plugin] = []
            result_warnings = list(discovery.warnings) + prepare_warnings

            for key, candidate in winners.items():
                # Use the supplemented manifest (from prepare) when available;
                # falls back to candidate.manifest for non-enabled or
                # prepare-failed winners. This ensures entry-point plugins
                # carry their PLUGIN_MANIFEST-supplemented fields (description,
                # pip_dependencies, requires_plugins, etc.) into the stored
                # Plugin object.
                manifest = manifests_by_key.get(key, candidate.manifest)
                existing = existing_by_key.get(key)

                if manifest is None:
                    # discovery failed
                    dep_status = build_dependency_status(
                        pip_results=[], dep_availability=[],
                        external=[], warnings=[],
                    )
                    merged_plugins.append(
                        _merge_manifest_into_plugin(
                            None, existing, STATUS_FAILED,
                            candidate.diagnostic or "discovery_failed",
                            [], dep_status,
                            enabled=key in effective_enabled,
                        )
                    )
                    continue

                # Compute status + dependency_status
                in_cycle = key in topo.cycle_members
                cycle_err = (
                    cycle_error_message(cycle_member_to_members.get(key, [key]))
                    if in_cycle else ""
                )
                is_unsupported = manifest.kind is not PluginKind.STANDALONE

                # Build dep_availability for this plugin
                dep_availability_list: list[DepAvailability] = []
                for dep_key in manifest.requires_plugins:
                    dep = classify_dep(
                        dep_key,
                        discovered_keys=discovered_keys,
                        enabled_keys=effective_enabled,
                        unsupported_keys=unsupported_keys,
                        cycle_members=set(topo.cycle_members),
                        load_failed_keys=load_failed_keys,
                        unavailable_keys=unavailable_keys,
                    )
                    dep_availability_list.append(dep)

                pip_results = pip_results_by_key.get(key, [])
                pkg_warnings = packaging_warnings_by_key.get(key, [])
                load_error = errors.get(key)

                status, error = compute_overall_status(
                    manifest_ok=True,
                    is_unsupported=is_unsupported,
                    in_cycle=in_cycle,
                    cycle_error=cycle_err,
                    dep_availability=dep_availability_list,
                    pip_results=pip_results,
                    load_error=load_error,
                    packaging_warnings=pkg_warnings,
                )

                dep_status = build_dependency_status(
                    pip_results=pip_results,
                    dep_availability=dep_availability_list,
                    external=manifest.external_dependencies,
                    warnings=pkg_warnings,
                )

                merged_plugins.append(
                    _merge_manifest_into_plugin(
                        manifest, existing, status, error,
                        unsupported_caps.get(key, []),
                        dep_status,
                        enabled=(key in effective_enabled),
                    )
                )

            # Mark missing (in registry but not discovered)
            result_keys = set(winners.keys())
            for key, existing in existing_by_key.items():
                if key not in result_keys:
                    merged_plugins.append(_mark_missing(existing))

            # 9. Build PluginScanResult for return (before publish, so candidate
            #    hooks/CLI can be computed from result).
            result = PluginScanResult(
                manifests=[
                    w.manifest for w in winners.values() if w.manifest is not None
                ],
                registrations=registrations,
                warnings=result_warnings,
                errors=errors,
                unsupported=unsupported_caps,
                hook_registrations=hook_registrations,
                cli_command_registrations=cli_command_registrations,
            )

            # 10. Atomic publish with generation snapshot + compensating rollback.
            #    Commit in spec order: ToolService -> routes -> in-memory -> registry.
            #    On ANY step failure: compensate in REVERSE order, restoring the
            #    previous successful generation. generation_id increments ONLY
            #    after ALL publish steps succeed.
            candidate_plugin_registrations = dict(registrations)
            candidate_manifests_by_key = dict(manifests_by_key)
            candidate_hooks = self._compute_hooks(result, effective_enabled)
            candidate_cli_commands = self._compute_cli_commands(result, effective_enabled)
            (
                candidate_winners,
                candidate_available_defs,
                candidate_override_static_names,
                candidate_route_names,
            ) = self._compute_tool_surface(
                plugin_registrations=candidate_plugin_registrations,
                manifests_by_key=candidate_manifests_by_key,
            )

            prev_snapshot = self._snapshot_generation(existing_plugins)

            publish_failed = False
            try:
                # Step 1: ToolService (rollback-able via replace_dynamic_definitions)
                self._tool_service.replace_dynamic_definitions(
                    "plugin",
                    candidate_available_defs,
                    candidate_override_static_names,
                )
                # Step 2: Routes (only after ToolService atomic commit succeeds)
                self._route_refresher(candidate_route_names)
                # Step 3: In-memory snapshot assignment
                self._registrations = candidate_winners
                self._plugin_registrations = candidate_plugin_registrations
                self._manifests_by_key = candidate_manifests_by_key
                self._hooks = candidate_hooks
                self._cli_commands = candidate_cli_commands
                self._last_route_names = set(candidate_route_names)
                self._last_tool_defs = list(candidate_available_defs)
                self._last_tool_override_names = set(candidate_override_static_names)
                # Step 4: Registry (SQLite, NOT atomic with memory)
                await self._registry.replace_all_plugins(merged_plugins)
            except Exception as exc:
                publish_failed = True
                logger.warning(
                    "scan publish failed at step %s: %s",
                    "unknown" if exc is None else type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                rollback_ok = await self._rollback(prev_snapshot)
                if not rollback_ok:
                    logger.error(
                        "scan publish failed and rollback also failed; "
                        "live state may be partially inconsistent"
                    )
                self.last_scan_error = (
                    f"scan publish failed: {type(exc).__name__}: {exc}"
                )

            if not publish_failed:
                self.generation_id += 1
                self.last_scan_error = None

            return result

    async def _compute_and_persist_effective_enabled(
        self,
        existing_plugins: list[Plugin],
        registry_enabled: set[str],
        discovered_keys: set[str],
    ) -> set[str]:
        """Compute effective enabled set and persist settings-forced state.

        Formula: (registry_enabled ∪ settings.plugins_enabled) - settings.plugins_disabled.
        Settings disabled has highest priority for all sources. Settings enabled
        can explicitly enable any source. Keys not in registry or settings
        default to disabled. When settings does NOT force a key, the existing
        registry enabled state is preserved (Dashboard toggle continues to work).
        """
        settings_enabled = set(
            getattr(self._settings, "plugins_enabled", []) or []
        )
        settings_disabled = set(
            getattr(self._settings, "plugins_disabled", []) or []
        )
        existing_keys = {p.key for p in existing_plugins}

        # Persist settings-forced state for existing plugins
        for key in settings_disabled:
            if key in existing_keys and key not in settings_enabled:
                plugin = next(p for p in existing_plugins if p.key == key)
                if plugin.enabled:
                    await self._registry.set_enabled(key, False)
        for key in settings_enabled:
            if key in existing_keys and key not in settings_disabled:
                plugin = next(p for p in existing_plugins if p.key == key)
                if not plugin.enabled:
                    await self._registry.set_enabled(key, True)

        effective = (registry_enabled | settings_enabled) - settings_disabled
        return effective

    def _compute_tool_surface(
        self,
        *,
        plugin_registrations: dict[str, list[PluginToolRegistration]] | None = None,
        manifests_by_key: dict[str, PluginManifest] | None = None,
    ) -> tuple[
        dict[str, PluginToolRegistration],
        list[ToolDefinition],
        set[str],
        set[str],
    ]:
        """Compute the tool surface WITHOUT committing.

        Returns ``(winners, available_defs, override_static_names, route_names)``.

        If ``plugin_registrations`` / ``manifests_by_key`` are provided, uses
        them instead of ``self._plugin_registrations`` /
        ``self._manifests_by_key`` (for candidate evaluation in atomic
        publish). Mutates the registration objects (sets ``available``,
        ``unavailable_reason``).

        Pipeline:
        1. Stable conflict resolution: iterate registrations in plugin load
           order then registration order. For each tool name, the FIRST
           AVAILABLE registration wins; later same-name registrations are
           marked unavailable.
        2. Static conflict + override gate: for each winner whose name
           collides with a static (builtin) tool, apply the override trust
           gate.
        """
        pr = (
            plugin_registrations
            if plugin_registrations is not None
            else self._plugin_registrations
        )
        mk = (
            manifests_by_key
            if manifests_by_key is not None
            else self._manifests_by_key
        )
        static_names = set(self._tool_service.definitions.keys())

        # Step 1: Stable conflict resolution -- first available wins.
        winners: dict[str, PluginToolRegistration] = {}
        for plugin_key, regs in pr.items():
            for reg in regs:
                name = reg.name
                if name in winners:
                    reg.available = False
                    reg.unavailable_reason = (
                        f"conflicts with plugin tool from {winners[name].plugin_key}"
                    )
                    continue
                if reg.requires_env and not self._check_requires_env(reg):
                    reg.available = False
                    reg.unavailable_reason = "missing required env"
                    continue
                try:
                    if reg.check_fn is not None and not bool(reg.check_fn()):
                        reg.available = False
                        reg.unavailable_reason = "check_fn returned false"
                        continue
                except Exception as exc:
                    reg.available = False
                    reg.unavailable_reason = f"check_fn error: {exc}"
                    continue
                winners[name] = reg

        # Step 2: Static conflict + override gate for winners.
        available_defs: list[ToolDefinition] = []
        override_static_names: set[str] = set()
        route_names: set[str] = set()

        for name, reg in winners.items():
            if name in static_names:
                manifest = mk.get(reg.plugin_key)
                source = manifest.source if manifest is not None else None
                if reg.override:
                    if self._tool_override_allowed(reg.plugin_key, source):
                        reg.available = True
                        reg.unavailable_reason = None
                        override_static_names.add(name)
                        available_defs.append(self._to_tool_definition(reg))
                        route_names.add(name)
                    else:
                        reg.available = False
                        reg.unavailable_reason = (
                            "override not permitted; "
                            "add plugin key to plugins_override_allowlist"
                        )
                else:
                    reg.available = False
                    reg.unavailable_reason = "conflicts with static tool"
            else:
                reg.available = True
                reg.unavailable_reason = None
                available_defs.append(self._to_tool_definition(reg))
                route_names.add(name)

        return winners, available_defs, override_static_names, route_names

    def _refresh_tool_surface(self) -> None:
        """Resolve plugin tool conflicts and publish via atomic replace.

        Pipeline:
        1. Compute tool surface via ``_compute_tool_surface`` (conflict
           resolution + override gate).
        2. Publish: call ``replace_dynamic_definitions("plugin", defs,
           override_static_names)`` atomically. Only on success, refresh
           routes and update tracking state. On failure, keep old tool
           surface + routes.
        """
        winners, available_defs, override_static_names, route_names = (
            self._compute_tool_surface()
        )

        try:
            self._tool_service.replace_dynamic_definitions(
                "plugin", available_defs, override_static_names,
            )
        except Exception:
            logger.warning(
                "replace_dynamic_definitions failed; "
                "keeping old tool surface and routes",
                exc_info=True,
            )
            return
        self._registrations = winners
        self._route_refresher(route_names)
        # Update tracking for atomic rollback (T9)
        self._last_tool_defs = list(available_defs)
        self._last_tool_override_names = set(override_static_names)
        self._last_route_names = set(route_names)

    def _tool_override_allowed(
        self, plugin_key: str, source: PluginSource | None,
    ) -> bool:
        """Return True only if the WINNING manifest's source is BUNDLED, or if
        ``plugin_key`` is in ``settings.plugins_override_allowlist`` (exact,
        case-sensitive). Fail-closed (False) otherwise.

        Trust is based on the WINNING manifest's actual source, NOT inherited
        from a shadowed bundled same-key plugin (T7's source-priority means a
        higher-priority source shadows bundled; the winner's source is what
        matters).
        """
        if source is PluginSource.BUNDLED:
            return True
        if source is None:
            return False
        raw = getattr(self._settings, "plugins_override_allowlist", None)
        if isinstance(raw, (list, tuple, set, frozenset)):
            allowlist = list(raw)
        else:
            allowlist = []
        return plugin_key in allowlist

    def _get_plugin_source(self, plugin_key: str) -> PluginSource | None:
        """Resolve the manifest source for ``plugin_key`` from the cached
        winning manifests populated during scan(). Returns None if the manifest
        is not cached (fail-closed in ``_tool_override_allowed``)."""
        manifest = self._manifests_by_key.get(plugin_key)
        if manifest is not None:
            return manifest.source
        return None

    def _compute_hooks(
        self,
        result: PluginScanResult,
        enabled_keys: set[str],
    ) -> dict[str, tuple[HookRegistration, ...]]:
        """Compute the hooks dict WITHOUT committing to ``self._hooks``.

        Replaces wholesale on each successful scan. Disabled or failed
        plugins (not in enabled_keys or present in result.errors) contribute
        no callbacks. Within each hook, callbacks are sorted by (plugin
        load order from manifests, registration_index) for stable dispatch.
        """
        plugin_load_order = {m.key: i for i, m in enumerate(result.manifests)}
        failed_keys = set(result.errors.keys())
        successful_keys = enabled_keys - failed_keys

        hooks_by_name: dict[str, list[HookRegistration]] = {}
        for plugin_key, hook_regs in result.hook_registrations.items():
            if plugin_key not in successful_keys:
                continue
            for reg in hook_regs:
                hooks_by_name.setdefault(reg.hook_name, []).append(reg)

        return {
            hook_name: tuple(
                sorted(
                    regs,
                    key=lambda r: (
                        plugin_load_order.get(r.plugin_key, len(plugin_load_order)),
                        r.registration_index,
                    ),
                )
            )
            for hook_name, regs in hooks_by_name.items()
        }

    def _compute_cli_commands(
        self,
        result: PluginScanResult,
        enabled_keys: set[str],
    ) -> tuple[PluginCliCommand, ...]:
        """Compute the CLI commands tuple WITHOUT committing.

        Returns an immutable tuple ordered by (plugin load order,
        registration_index). Disabled, admission-failed, and register-failed
        plugins contribute no commands.
        """
        plugin_load_order = {m.key: i for i, m in enumerate(result.manifests)}
        failed_keys = set(result.errors.keys())
        successful_keys = enabled_keys - failed_keys

        all_commands: list[PluginCliCommand] = []
        for plugin_key, cli_regs in result.cli_command_registrations.items():
            if plugin_key not in successful_keys:
                continue
            for reg in cli_regs:
                all_commands.append(reg)
        all_commands.sort(
            key=lambda r: (
                plugin_load_order.get(r.plugin_key, len(plugin_load_order)),
                r.registration_index,
            )
        )
        return tuple(all_commands)

    def _aggregate_hooks(
        self,
        result: PluginScanResult,
        enabled_keys: set[str],
    ) -> None:
        """Aggregate hooks from successful plugins, grouped by hook_name.

        Replaces ``_hooks`` wholesale on each successful scan. Disabled or
        failed plugins (not in enabled_keys or present in result.errors)
        contribute no callbacks. Within each hook, callbacks are sorted by
        (plugin load order from manifests, registration_index) for stable
        dispatch order.
        """
        self._hooks = self._compute_hooks(result, enabled_keys)

    def _snapshot_generation(
        self,
        registry_plugins: list[Plugin],
    ) -> dict[str, Any]:
        """Snapshot the current live generation state for compensating rollback.

        Captures the previously-committed state of ToolService defs+suppression,
        routes, in-memory snapshots (_hooks/_cli_commands/_registrations/
        _plugin_registrations/_manifests_by_key), and registry public rows.
        Used by atomic publish to restore the last successful generation on
        any commit step failure.
        """
        return {
            "tool_defs": list(self._last_tool_defs),
            "tool_suppression": set(self._last_tool_override_names),
            "route_names": set(self._last_route_names),
            "registrations": dict(self._registrations),
            "plugin_registrations": {
                k: list(v) for k, v in self._plugin_registrations.items()
            },
            "manifests_by_key": dict(self._manifests_by_key),
            "hooks": {k: v for k, v in self._hooks.items()},
            "cli_commands": tuple(self._cli_commands),
            "registry_plugins": list(registry_plugins),
        }

    async def _rollback(self, snapshot: dict[str, Any]) -> bool:
        """Restore previous generation state in reverse commit order.

        Commit order is: ToolService(1) -> routes(2) -> in-memory(3) -> registry(4).
        Rollback order is the reverse: registry(4) -> in-memory(3) -> routes(2) ->
        ToolService(1). Each step is best-effort: if a restore fails, it is
        logged and the next step is still attempted. Returns ``True`` only if
        ALL restore steps succeeded.

        Cross SQLite/memory: no transaction atomicity is claimed, but the
        caller must only see the last successful generation. If rollback
        itself fails, the caller records a scan failure and does NOT set the
        candidate as live.
        """
        all_ok = True

        # Step 4 reverse: Registry restore (best-effort; SQLite may not
        # support transactional rollback)
        try:
            await self._registry.replace_all_plugins(snapshot["registry_plugins"])
        except Exception:
            all_ok = False
            logger.error(
                "registry rollback failed; registry state may be inconsistent",
                exc_info=True,
            )

        # Step 3 reverse: In-memory restore (assignments; always succeed)
        self._registrations = dict(snapshot["registrations"])
        self._plugin_registrations = {
            k: list(v) for k, v in snapshot["plugin_registrations"].items()
        }
        self._manifests_by_key = dict(snapshot["manifests_by_key"])
        self._hooks = {k: v for k, v in snapshot["hooks"].items()}
        self._cli_commands = tuple(snapshot["cli_commands"])
        self._last_route_names = set(snapshot["route_names"])
        self._last_tool_defs = list(snapshot["tool_defs"])
        self._last_tool_override_names = set(snapshot["tool_suppression"])

        # Step 2 reverse: Routes restore (best-effort)
        try:
            self._route_refresher(set(snapshot["route_names"]))
        except Exception:
            all_ok = False
            logger.error(
                "routes rollback failed; routes may be inconsistent",
                exc_info=True,
            )

        # Step 1 reverse: ToolService restore (best-effort)
        try:
            self._tool_service.replace_dynamic_definitions(
                "plugin",
                list(snapshot["tool_defs"]),
                set(snapshot["tool_suppression"]),
            )
        except Exception:
            all_ok = False
            logger.error(
                "ToolService rollback failed; tool surface may be inconsistent",
                exc_info=True,
            )

        return all_ok

    async def invoke_hook(self, hook_name: str, **kwargs: Any) -> list[Any]:
        """Dispatch ``hook_name`` to all registered callbacks in stable order.

        - Takes a tuple snapshot of callbacks at call start, so concurrent scan
          replacement does not affect an in-flight dispatch.
        - Adds ``hook_schema_version=1`` to kwargs.
        - Shallow-copies list/dict payload values so plugins cannot mutate the
          caller's data in place.
        - Isolates each callback with per-callback ``wait_for`` timeout and
          exception handling. On failure, logs a warning with plugin_key,
          hook_name, and exception type only (no payload content, secret, or
          trusted_metadata).
        - Applies per-hook-type return contracts:
          - observer hooks: all returns ignored, returns ``[]``.
          - ``pre_llm_call``: merges all valid bare-string / ``{"context": str}``
            returns with ``\\n\\n`` separator, returns ``[merged]`` or ``[]``.
          - ``transform_tool_result``: first valid non-None return wins,
            subsequent callbacks are skipped, returns ``[value]`` or ``[]``.
          - ``transform_llm_output``: first non-empty string wins, subsequent
            callbacks are skipped, returns ``[value]`` or ``[]``.

        Late results from sync callbacks that time out in the thread are
        discarded: ``asyncio.wait_for`` cancels the awaitable on timeout, so
        the thread's eventual return value is never retrieved. Python cannot
        force-terminate the thread or undo external side effects; this is a
        residual risk of running trusted in-process plugin code.
        """
        snapshot = self._hooks.get(hook_name, ())
        if not snapshot:
            return []

        callback_kwargs = self._prepare_hook_kwargs(kwargs)
        timeout = float(getattr(self._settings, "plugin_hook_timeout_seconds", 5.0))

        if hook_name == "pre_llm_call":
            return await self._dispatch_pre_llm_call(snapshot, callback_kwargs, timeout)

        if hook_name == "transform_tool_result":
            return await self._dispatch_transform_tool_result(snapshot, callback_kwargs, timeout)

        if hook_name == "transform_llm_output":
            return await self._dispatch_transform_llm_output(snapshot, callback_kwargs, timeout)

        # Observer hooks: call all callbacks, ignore all returns.
        for reg in snapshot:
            await self._invoke_single_callback(reg, hook_name, callback_kwargs, timeout)
        return []

    def _prepare_hook_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Add hook_schema_version and shallow-copy list/dict payload values."""
        prepared = dict(kwargs)
        prepared["hook_schema_version"] = 1
        for key, value in list(prepared.items()):
            if isinstance(value, list):
                prepared[key] = list(value)
            elif isinstance(value, dict):
                prepared[key] = dict(value)
        return prepared

    async def _dispatch_pre_llm_call(
        self,
        snapshot: tuple[HookRegistration, ...],
        kwargs: dict[str, Any],
        timeout: float,
    ) -> list[Any]:
        """Merge all valid context strings from pre_llm_call callbacks."""
        contexts: list[str] = []
        for reg in snapshot:
            value = await self._invoke_single_callback(reg, "pre_llm_call", kwargs, timeout)
            if value is None:
                continue
            ctx = self._extract_pre_llm_context(value, reg)
            if ctx:
                contexts.append(ctx)
        if contexts:
            return ["\n\n".join(contexts)]
        return []

    def _extract_pre_llm_context(self, value: Any, reg: HookRegistration) -> str:
        """Extract a context string from a pre_llm_call return value.

        Accepts a bare string or ``{"context": <string>}``. Invalid returns
        (non-string, dict without "context", non-string context) produce a
        warning and return empty string.
        """
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            ctx = value.get("context")
            if isinstance(ctx, str):
                return ctx
        logger.warning(
            "plugin %s: hook pre_llm_call callback illegal return: %s",
            reg.plugin_key,
            type(value).__name__,
        )
        return ""

    async def _dispatch_transform_tool_result(
        self,
        snapshot: tuple[HookRegistration, ...],
        kwargs: dict[str, Any],
        timeout: float,
    ) -> list[Any]:
        """First valid non-None return wins; skip remaining callbacks."""
        for reg in snapshot:
            value = await self._invoke_single_callback(
                reg, "transform_tool_result", kwargs, timeout
            )
            if value is not None:
                return [value]
        return []

    async def _dispatch_transform_llm_output(
        self,
        snapshot: tuple[HookRegistration, ...],
        kwargs: dict[str, Any],
        timeout: float,
    ) -> list[Any]:
        """First non-empty string return wins; skip remaining callbacks."""
        for reg in snapshot:
            value = await self._invoke_single_callback(
                reg, "transform_llm_output", kwargs, timeout
            )
            if isinstance(value, str) and value:
                return [value]
        return []

    async def _invoke_single_callback(
        self,
        reg: HookRegistration,
        hook_name: str,
        kwargs: dict[str, Any],
        timeout: float,
    ) -> Any:
        """Invoke one hook callback with timeout and exception isolation.

        Returns the callback's return value, or ``None`` on exception or
        timeout. Logs a warning containing only plugin_key, hook_name, and
        the exception type name -- never the payload content, secret, or
        trusted_metadata.
        """
        try:
            if asyncio.iscoroutinefunction(reg.callback):
                result = await asyncio.wait_for(
                    reg.callback(**kwargs), timeout=timeout
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(reg.callback, **kwargs),
                    timeout=timeout,
                )
            return result
        except Exception as exc:
            logger.warning(
                "plugin %s: hook %s callback failed: %s",
                reg.plugin_key,
                hook_name,
                type(exc).__name__,
            )
            return None

    def _to_tool_definition(self, reg: PluginToolRegistration) -> ToolDefinition:
        schema = reg.schema or {}
        parameters = schema.get("parameters") if isinstance(schema, dict) else None
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        return ToolDefinition(
            name=reg.name,
            description=reg.description or schema.get("description", "") or f"plugin tool: {reg.name}",
            input_schema=parameters,
            risk_level=reg.risk_level,
            timeout_seconds=reg.timeout_seconds or getattr(self._settings, "plugin_tool_timeout_seconds", 30),
            enabled=True,
            source_type=ToolSourceType.PLUGIN,
            toolset=reg.toolset,
        )

    def _check_requires_env(self, reg: PluginToolRegistration) -> bool:
        if not reg.requires_env:
            return True
        for item in reg.requires_env:
            if isinstance(item, dict):
                env_name = item.get("name")
            else:
                env_name = str(item)
            if not env_name:
                continue
            if env_name in reg.secret_config and reg.secret_config[env_name]:
                continue
            if env_name in reg.plugin_config and reg.plugin_config[env_name]:
                continue
            if os.environ.get(env_name):
                continue
            return False
        return True

    async def set_enabled(self, key: str, enabled: bool) -> Plugin:
        plugin = await self._registry.get_plugin(key)
        if plugin is None:
            raise PluginNotFoundError(key)
        await self._registry.set_enabled(key, enabled)
        await self.scan()
        updated = await self._registry.get_plugin(key)
        if updated is None:
            raise PluginNotFoundError(key)
        return updated

    async def update_config(
        self,
        key: str,
        config: dict[str, Any],
        secret_updates: dict[str, str] | None = None,
    ) -> Plugin:
        plugin = await self._registry.get_plugin(key)
        if plugin is None:
            raise PluginNotFoundError(key)
        self._validate_config(plugin, config)
        await self._registry.update_config(key, config, secret_updates)
        await self.scan()
        updated = await self._registry.get_plugin(key)
        if updated is None:
            raise PluginNotFoundError(key)
        return updated

    def _validate_config(self, plugin: Plugin, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise PluginValidationError("config must be a dict")
        manifest_raw = plugin.manifest or {}
        config_schema = manifest_raw.get("config_schema") or {}
        schema_type = config_schema.get("type")
        if schema_type and schema_type != "object":
            raise PluginValidationError(f"config_schema type {schema_type!r} unsupported")
        required = config_schema.get("required") or []
        missing = [field for field in required if field not in config]
        if missing:
            raise PluginValidationError(f"missing required config fields: {', '.join(missing)}")

    async def refresh(self) -> None:
        await self.scan()

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        context: ToolExecutionContext | None,
        tool_call_id: str = "",
    ) -> ToolResult:
        reg = self._registrations.get(name)
        if reg is None or not reg.available:
            return ToolResult(tool_call_id, name, ToolResultStatus.ERROR, {"error": "tool not found"})
        if reg.check_fn is not None:
            try:
                ok = await asyncio.to_thread(reg.check_fn)
                if not ok:
                    return ToolResult(
                        tool_call_id, name, ToolResultStatus.ERROR, {"error": "plugin tool not available"}
                    )
            except Exception as exc:
                return ToolResult(
                    tool_call_id, name, ToolResultStatus.ERROR, {"error": f"check_fn failed: {exc}"}
                )
        kwargs = self._build_kwargs(reg, context)
        timeout = reg.timeout_seconds or getattr(self._settings, "plugin_tool_timeout_seconds", 30)
        try:
            if reg.is_async:
                result = await asyncio.wait_for(reg.handler(args, **kwargs), timeout=timeout)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(reg.handler, args, **kwargs), timeout=timeout
                )
        except asyncio.TimeoutError:
            return ToolResult(tool_call_id, name, ToolResultStatus.TIMEOUT, {"error": "plugin tool timeout"})
        except Exception as exc:
            return ToolResult(tool_call_id, name, ToolResultStatus.ERROR, {"error": str(exc)})
        if isinstance(result, dict):
            content = result
        elif isinstance(result, str):
            content = {"content": result}
        else:
            content = {"content": str(result)}
        return ToolResult(tool_call_id, name, ToolResultStatus.SUCCESS, content)

    def _build_kwargs(
        self,
        reg: PluginToolRegistration,
        context: ToolExecutionContext | None,
    ) -> dict[str, Any]:
        session_id = context.session_id if context else None
        metadata = dict(context.metadata) if context else {}
        trusted_metadata = dict(context.trusted_metadata) if context else {}
        return {
            "session_id": session_id,
            "metadata": metadata,
            "trusted_metadata": trusted_metadata,
            "plugin_config": dict(reg.plugin_config),
            "profile_name": trusted_metadata.get("profile_name"),
        }


class PluginToolExecutor:
    def __init__(self, service: PluginService):
        self._service = service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        return await self._service.call_tool(
            request.name, dict(request.arguments), context, request.id
        )


def _merge_manifest_into_plugin(
    manifest: PluginManifest | None,
    existing: Plugin | None,
    status: str,
    error_text: str | None,
    unsupported_caps: list[str],
    dependency_status: dict[str, Any] | None = None,
    *,
    enabled: bool | None = None,
) -> Plugin:
    """Merge a discovered manifest (or discovery failure) into a Plugin.

    When ``manifest`` is None, the candidate failed discovery and the plugin
    is marked FAILED with ``error_text``. ``dependency_status`` is written
    into ``capabilities["dependency_status"]``. For new plugins (existing is
    None), ``enabled`` controls the initial enabled state; for existing
    plugins the registry preserves the prior enabled state via
    ``replace_all_plugins``.
    """
    now = _utc_now()
    dep_status = dependency_status or {}
    if manifest is None:
        # Discovery failed; carry over existing identity if available.
        if existing is None:
            return Plugin(
                id=new_plugin_id(),
                key="",
                name="",
                source=PluginSource.BUNDLED,
                enabled=False,
                capabilities={"dependency_status": dep_status},
                last_scan_status=status,
                last_scan_error=error_text,
                last_scanned_at=now,
                created_at=now,
                updated_at=now,
            )
        return Plugin(
            id=existing.id,
            key=existing.key,
            name=existing.name,
            source=existing.source,
            enabled=existing.enabled,
            version=existing.version,
            description=existing.description,
            author=existing.author,
            kind=existing.kind,
            source_path=existing.source_path,
            config=existing.config,
            secret_refs=existing.secret_refs,
            capabilities={**existing.capabilities, "dependency_status": dep_status},
            manifest=existing.manifest,
            last_scan_status=status,
            last_scan_error=error_text,
            last_scanned_at=now,
            created_at=existing.created_at,
            updated_at=now,
        )
    if existing is None:
        capabilities = {
            "unsupported": unsupported_caps,
            "provides_tools": list(manifest.provides_tools),
            "dependency_status": dep_status,
        }
        return Plugin(
            id=new_plugin_id(),
            key=manifest.key,
            name=manifest.name,
            source=manifest.source,
            enabled=enabled if enabled is not None else False,
            version=manifest.version,
            description=manifest.description,
            author=manifest.author,
            kind=manifest.kind,
            source_path=manifest.path,
            config={},
            secret_refs={},
            capabilities=capabilities,
            manifest=dict(manifest.raw),
            last_scan_status=status,
            last_scan_error=error_text,
            last_scanned_at=now,
            created_at=now,
            updated_at=now,
        )
    capabilities = {
        "unsupported": unsupported_caps,
        "provides_tools": list(manifest.provides_tools),
        "dependency_status": dep_status,
    }
    return Plugin(
        id=existing.id,
        key=manifest.key,
        name=manifest.name,
        source=manifest.source,
        enabled=enabled if enabled is not None else existing.enabled,
        version=manifest.version,
        description=manifest.description,
        author=manifest.author,
        kind=manifest.kind,
        source_path=manifest.path,
        config=existing.config,
        secret_refs=existing.secret_refs,
        capabilities=capabilities,
        manifest=dict(manifest.raw),
        last_scan_status=status,
        last_scan_error=error_text,
        last_scanned_at=now,
        created_at=existing.created_at,
        updated_at=now,
    )


def _mark_missing(plugin: Plugin) -> Plugin:
    now = _utc_now()
    return Plugin(
        id=plugin.id,
        key=plugin.key,
        name=plugin.name,
        source=plugin.source,
        enabled=False,
        version=plugin.version,
        description=plugin.description,
        author=plugin.author,
        kind=plugin.kind,
        source_path=plugin.source_path,
        config=plugin.config,
        secret_refs=plugin.secret_refs,
        capabilities=plugin.capabilities,
        manifest=plugin.manifest,
        last_scan_status=PluginScanStatus.MISSING.value,
        last_scan_error="plugin source no longer present on disk",
        last_scanned_at=now,
        created_at=plugin.created_at,
        updated_at=now,
    )


def _utc_now() -> datetime:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


__all__ = [
    "HookRegistration",
    "PluginCliCommand",
    "PluginContext",
    "PluginFileLoaderProtocol",
    "PluginScanResult",
    "PluginScanWarning",
    "PluginService",
    "PluginToolExecutor",
    "PluginToolRegistration",
    "VALID_HOOKS",
]
