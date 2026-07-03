from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

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


@runtime_checkable
class PluginFileLoaderProtocol(Protocol):
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

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("hook")

    def register_cli_command(self, *args: Any, **kwargs: Any) -> None:
        self._unsupported("cli_command")

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
        self._scan_lock = asyncio.Lock()

    async def list_plugins(self, include_disabled: bool = True) -> list[Plugin]:
        return await self._registry.list_plugins(include_disabled=include_disabled)

    async def get_plugin(self, key: str) -> Plugin | None:
        return await self._registry.get_plugin(key)

    async def scan(self) -> PluginScanResult:
        async with self._scan_lock:
            plugins = await self._registry.list_plugins(include_disabled=True)
            await self._apply_settings_enabled_state(plugins)
            plugins = await self._registry.list_plugins(include_disabled=True)
            enabled_keys = {p.key for p in plugins if p.enabled}
            disabled_keys = {p.key for p in plugins if not p.enabled}

            config_cache: dict[str, dict[str, Any]] = {}
            secret_cache: dict[str, dict[str, str]] = {}
            for key in enabled_keys | disabled_keys:
                plugin = await self._registry.get_plugin(key)
                config_cache[key] = dict(plugin.config) if plugin else {}
                secret_cache[key] = await self._registry.get_secret_config(key)

            result = await self._loader.scan(
                enabled_keys,
                disabled_keys,
                lambda key: config_cache.get(key, {}),
                lambda key: secret_cache.get(key, {}),
            )

            existing_by_key = {p.key: p for p in plugins}
            merged_plugins: list[Plugin] = []
            result_keys = {m.key for m in result.manifests}
            for manifest in result.manifests:
                existing = existing_by_key.get(manifest.key)
                error_text = result.errors.get(manifest.key)
                unsupported_caps = result.unsupported.get(manifest.key, [])
                status = _compute_scan_status(manifest, error_text, unsupported_caps)
                merged_plugins.append(
                    _merge_manifest_into_plugin(manifest, existing, status, error_text, unsupported_caps)
                )
            for key, existing in existing_by_key.items():
                if key not in result_keys:
                    merged_plugins.append(
                        _mark_missing(existing)
                    )
            await self._registry.replace_all_plugins(merged_plugins)

            self._plugin_registrations = dict(result.registrations)
            self._registrations = {
                reg.name: reg for regs in result.registrations.values() for reg in regs
            }
            self._refresh_tool_surface()
            return result

    def _refresh_tool_surface(self) -> None:
        static_names = {
            d.name for d in self._tool_service.list_definitions() if d.source_type is not ToolSourceType.PLUGIN
        }
        defs: list[ToolDefinition] = []
        route_names: set[str] = set()
        for name, reg in self._registrations.items():
            if reg.name in static_names:
                reg.available = False
                reg.unavailable_reason = (
                    "static tool override not implemented this phase"
                    if reg.override
                    else "conflicts with static tool"
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
            reg.available = True
            reg.unavailable_reason = None
            defs.append(self._to_tool_definition(reg))
            route_names.add(reg.name)
        self._tool_service.set_dynamic_definitions("plugin", defs)
        self._route_refresher(route_names)

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

    async def _apply_settings_enabled_state(self, plugins: list[Plugin]) -> None:
        enabled_list = list(getattr(self._settings, "plugins_enabled", []) or [])
        disabled_list = list(getattr(self._settings, "plugins_disabled", []) or [])
        existing_keys = {p.key for p in plugins}
        for key in disabled_list:
            if key in existing_keys:
                plugin = next(p for p in plugins if p.key == key)
                if plugin.enabled:
                    await self._registry.set_enabled(key, False)
        for key in enabled_list:
            if key in existing_keys:
                plugin = next(p for p in plugins if p.key == key)
                if not plugin.enabled and plugin.source is PluginSource.BUNDLED:
                    await self._registry.set_enabled(key, True)

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


def _compute_scan_status(
    manifest: PluginManifest,
    error_text: str | None,
    unsupported_caps: list[str],
) -> str:
    if error_text:
        return PluginScanStatus.FAILED.value
    if manifest.kind is not PluginKind.STANDALONE:
        return PluginScanStatus.UNSUPPORTED.value
    if unsupported_caps:
        return PluginScanStatus.PARTIAL.value
    return PluginScanStatus.OK.value


def _merge_manifest_into_plugin(
    manifest: PluginManifest,
    existing: Plugin | None,
    status: str,
    error_text: str | None,
    unsupported_caps: list[str],
) -> Plugin:
    now = _utc_now()
    if existing is None:
        return Plugin(
            id=new_plugin_id(),
            key=manifest.key,
            name=manifest.name,
            source=manifest.source,
            enabled=False,
            version=manifest.version,
            description=manifest.description,
            author=manifest.author,
            kind=manifest.kind,
            source_path=manifest.path,
            config={},
            secret_refs={},
            capabilities={"unsupported": unsupported_caps, "provides_tools": list(manifest.provides_tools)},
            manifest=dict(manifest.raw),
            last_scan_status=status,
            last_scan_error=error_text,
            last_scanned_at=now,
            created_at=now,
            updated_at=now,
        )
    return Plugin(
        id=existing.id,
        key=manifest.key,
        name=manifest.name,
        source=manifest.source,
        enabled=existing.enabled,
        version=manifest.version,
        description=manifest.description,
        author=manifest.author,
        kind=manifest.kind,
        source_path=manifest.path,
        config=existing.config,
        secret_refs=existing.secret_refs,
        capabilities={"unsupported": unsupported_caps, "provides_tools": list(manifest.provides_tools)},
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
    "PluginContext",
    "PluginFileLoaderProtocol",
    "PluginScanResult",
    "PluginScanWarning",
    "PluginService",
    "PluginToolExecutor",
    "PluginToolRegistration",
]
