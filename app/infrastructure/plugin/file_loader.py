from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from app.application.plugin_service import (
    PluginContext,
    PluginScanResult,
    PluginScanWarning,
    PluginToolRegistration,
)
from app.domain.plugin import (
    PluginKind,
    PluginManifest,
    PluginSource,
    PluginValidationError,
)

logger = logging.getLogger(__name__)

PLUGIN_YAML = "plugin.yaml"
PLUGIN_YML = "plugin.yml"
PLUGIN_INIT = "__init__.py"
PLUGIN_NAMESPACE_PREFIX = "n_agent_plugins"
MAX_PLUGINS_PER_SOURCE = 200


@dataclass(frozen=True)
class PluginFileLoaderConfig:
    bundled_root: Path | None = None
    user_root: Path | None = None
    project_root: Path | None = None
    enable_entrypoints: bool = False
    enable_project: bool = False
    safe_mode: bool = False
    max_plugins: int = MAX_PLUGINS_PER_SOURCE
    skip_names: frozenset[str] = frozenset({"platforms"})


class PluginFileLoader:
    def __init__(self, config: PluginFileLoaderConfig):
        self.config = config

    async def scan(
        self,
        enabled_keys: set[str],
        disabled_keys: set[str],
        config_provider: Callable[[str], dict[str, Any]],
        secret_provider: Callable[[str], dict[str, str]],
    ) -> PluginScanResult:
        return await asyncio.to_thread(
            self._scan_sync,
            enabled_keys,
            disabled_keys,
            config_provider,
            secret_provider,
        )

    def _scan_sync(
        self,
        enabled_keys: set[str],
        disabled_keys: set[str],
        config_provider: Callable[[str], dict[str, Any]],
        secret_provider: Callable[[str], dict[str, str]],
    ) -> PluginScanResult:
        manifests_by_key: dict[str, PluginManifest] = {}
        warnings: list[PluginScanWarning] = []

        for source, root in self._iter_sources():
            if root is None:
                continue
            for manifest in self._scan_root(root, source, warnings):
                manifests_by_key[manifest.key] = manifest

        if self.config.enable_entrypoints:
            self._scan_entrypoints(manifests_by_key, warnings)

        if self.config.safe_mode:
            return PluginScanResult(
                manifests=list(manifests_by_key.values()),
                registrations={},
                warnings=warnings or [PluginScanWarning(relative_path="", reason="safe_mode")],
                errors={},
                unsupported={},
            )

        registrations: dict[str, list[PluginToolRegistration]] = {}
        errors: dict[str, str] = {}
        unsupported: dict[str, list[str]] = {}

        for key, manifest in manifests_by_key.items():
            if key not in enabled_keys:
                continue
            if manifest.kind is not PluginKind.STANDALONE:
                continue
            try:
                ctx = self._load_and_register(
                    manifest,
                    config_provider(key) or {},
                    secret_provider(key) or {},
                )
                if ctx.tool_registrations:
                    registrations[key] = ctx.tool_registrations
                if ctx.unsupported_capabilities:
                    unsupported[key] = list(ctx.unsupported_capabilities)
            except Exception as exc:
                logger.warning("plugin %s load failed: %s", key, exc)
                errors[key] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

        return PluginScanResult(
            manifests=list(manifests_by_key.values()),
            registrations=registrations,
            warnings=warnings,
            errors=errors,
            unsupported=unsupported,
        )

    def _iter_sources(self) -> list[tuple[PluginSource, Path | None]]:
        return [
            (PluginSource.BUNDLED, self.config.bundled_root),
            (PluginSource.USER, self.config.user_root),
            (PluginSource.PROJECT, self.config.project_root if self.config.enable_project else None),
        ]

    def _scan_root(
        self,
        root: Path,
        source: PluginSource,
        warnings: list[PluginScanWarning],
    ) -> list[PluginManifest]:
        root = Path(root)
        if not root.exists():
            return []
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warnings.append(PluginScanWarning(relative_path=str(root), reason="mkdir_failed", detail=str(exc)))
            return []
        state = {"count": 0, "max_warned": False, "stop": False}
        return self._scan_directory_level(
            root, source, root, self.config.skip_names, "", 0, state, warnings
        )

    def _scan_directory_level(
        self,
        path: Path,
        source: PluginSource,
        root: Path,
        skip_names: frozenset[str],
        prefix: str,
        depth: int,
        state: dict,
        warnings: list[PluginScanWarning],
    ) -> list[PluginManifest]:
        from app.infrastructure.path_security import validate_within_dir

        manifests: list[PluginManifest] = []
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            warnings.append(PluginScanWarning(relative_path=str(path), reason="iterdir_failed", detail=str(exc)))
            return manifests
        for child in entries:
            if state["stop"]:
                break
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in {"__pycache__"}:
                continue
            if depth == 0 and child.name in skip_names:
                continue
            if state["count"] >= self.config.max_plugins:
                if not state["max_warned"]:
                    warnings.append(PluginScanWarning(relative_path=str(root), reason="max_plugins_exceeded"))
                    state["max_warned"] = True
                state["stop"] = True
                break
            try:
                err = validate_within_dir(child, root)
            except Exception as exc:
                warnings.append(PluginScanWarning(relative_path=str(child), reason="path_escape", detail=str(exc)))
                continue
            if err:
                warnings.append(PluginScanWarning(relative_path=str(child), reason="path_escape", detail=err))
                continue
            manifest_path = self._manifest_path_for(child)
            if manifest_path is not None:
                key = f"{prefix}/{child.name}" if prefix else child.name
                manifest = self._parse_manifest(manifest_path, source, key, child, warnings)
                if manifest is not None:
                    manifests.append(manifest)
                    state["count"] += 1
                continue
            if depth < 1:
                child_prefix = f"{prefix}/{child.name}" if prefix else child.name
                manifests.extend(
                    self._scan_directory_level(
                        child, source, root, skip_names, child_prefix, depth + 1, state, warnings
                    )
                )
        return manifests

    def _manifest_path_for(self, plugin_dir: Path) -> Path | None:
        yaml_path = plugin_dir / PLUGIN_YAML
        if yaml_path.is_file():
            return yaml_path
        yml_path = plugin_dir / PLUGIN_YML
        if yml_path.is_file():
            return yml_path
        return None

    def _parse_manifest(
        self,
        plugin_yaml: Path,
        source: PluginSource,
        key: str,
        plugin_dir: Path,
        warnings: list[PluginScanWarning],
    ) -> PluginManifest | None:
        try:
            raw = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            warnings.append(PluginScanWarning(relative_path=str(plugin_yaml), reason="yaml_parse_error", detail=str(exc)))
            return None
        except OSError as exc:
            warnings.append(PluginScanWarning(relative_path=str(plugin_yaml), reason="read_error", detail=str(exc)))
            return None
        try:
            return PluginManifest.from_yaml(
                raw if isinstance(raw, dict) else {},
                source=source,
                key=key,
                path=str(plugin_dir),
            )
        except PluginValidationError as exc:
            warnings.append(PluginScanWarning(relative_path=str(plugin_yaml), reason="invalid_manifest", detail=str(exc)))
            return None

    def _scan_entrypoints(
        self,
        manifests_by_key: dict[str, PluginManifest],
        warnings: list[PluginScanWarning],
    ) -> None:
        try:
            from importlib.metadata import entry_points
        except ImportError:
            return
        try:
            eps = entry_points()
            groups = []
            if hasattr(eps, "select"):
                for group in ("hermes_agent.plugins", "n_agent.plugins"):
                    groups.extend(eps.select(group=group))
            else:
                for group in ("hermes_agent.plugins", "n_agent.plugins"):
                    groups.extend(eps.get(group, []))
        except Exception as exc:
            warnings.append(PluginScanWarning(relative_path="", reason="entrypoint_scan_failed", detail=str(exc)))
            return
        for ep in groups:
            try:
                module = ep.load()
            except Exception as exc:
                warnings.append(PluginScanWarning(relative_path=ep.name, reason="entrypoint_load_failed", detail=str(exc)))
                continue
            raw = getattr(module, "PLUGIN_MANIFEST", None) or {}
            if not isinstance(raw, dict):
                continue
            try:
                manifest = PluginManifest.from_yaml(
                    raw,
                    source=PluginSource.ENTRY_POINT,
                    key=raw.get("key", ep.name),
                    path=f"entrypoint:{ep.name}",
                )
                manifests_by_key[manifest.key] = manifest
            except PluginValidationError as exc:
                warnings.append(PluginScanWarning(relative_path=ep.name, reason="entrypoint_invalid_manifest", detail=str(exc)))

    def _load_and_register(
        self,
        manifest: PluginManifest,
        plugin_config: dict[str, Any],
        secret_config: dict[str, str],
    ) -> PluginContext:
        import types

        plugin_dir = Path(manifest.path)
        init_file = plugin_dir / PLUGIN_INIT
        if not init_file.is_file():
            raise PluginValidationError(f"plugin {manifest.key}: missing __init__.py at {plugin_dir}")
        safe_key = _safe_module_name(manifest.key)
        module_name = f"{PLUGIN_NAMESPACE_PREFIX}.{safe_key}"
        parent_name = PLUGIN_NAMESPACE_PREFIX
        if parent_name not in sys.modules:
            parent_module = types.ModuleType(parent_name)
            parent_module.__path__ = []  # type: ignore[attr-defined]
            parent_module.__package__ = parent_name
            sys.modules[parent_name] = parent_module
        for name in list(sys.modules.keys()):
            if name == module_name or name.startswith(module_name + "."):
                del sys.modules[name]
        spec = importlib.util.spec_from_file_location(
            module_name,
            init_file,
            submodule_search_locations=[str(plugin_dir)],
        )
        if spec is None or spec.loader is None:
            raise PluginValidationError(f"plugin {manifest.key}: cannot create module spec for {init_file}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = module_name
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            for name in list(sys.modules.keys()):
                if name == module_name or name.startswith(module_name + "."):
                    del sys.modules[name]
            raise
        register_fn = getattr(module, "register", None)
        if not callable(register_fn):
            raise PluginValidationError(f"plugin {manifest.key}: missing register(ctx) entrypoint")
        ctx = PluginContext(
            plugin_key=manifest.key,
            plugin_config=plugin_config,
            secret_config=secret_config,
        )
        register_fn(ctx)
        return ctx


def _safe_module_name(key: str) -> str:
    safe = []
    for ch in str(key):
        if ch.isalnum() or ch == "_":
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe) or "plugin"
