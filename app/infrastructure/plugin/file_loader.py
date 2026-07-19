from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from dataclasses import dataclass, field, replace
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

# Entry-point groups, in priority order. ``n_agent.plugins`` wins on same-name
# collisions across groups; ``hermes_agent.plugins`` is retained for compat.
ENTRY_POINT_GROUPS: tuple[str, ...] = ("n_agent.plugins", "hermes_agent.plugins")

# Source priority for fail-closed discovery resolution: higher wins on key
# collision, even when the higher-priority candidate is itself broken.
_SOURCE_PRIORITY: dict[PluginSource, int] = {
    PluginSource.BUNDLED: 0,
    PluginSource.USER: 1,
    PluginSource.PROJECT: 2,
    PluginSource.ENTRY_POINT: 3,
}


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


# ---------------------------------------------------------------------------
# Phase errors
# ---------------------------------------------------------------------------


class PluginLoaderError(Exception):
    """Base for loader phase failures. Carries a stable diagnostic ``code``.

    The public summary uses ``f"{code}: {message}"`` (no traceback); the
    service log keeps the full traceback.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EntrypointLoadFailed(PluginLoaderError):
    """prepare() could not load/supplement an entry-point plugin."""


class DirectoryLoadFailed(PluginLoaderError):
    """load_and_register() could not import a directory plugin's __init__.py."""


class PluginRegisterFailed(PluginLoaderError):
    """register(ctx) missing or raised during load_and_register()."""


# ---------------------------------------------------------------------------
# Discovery / prepare result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A single plugin candidate discovered in one source.

    ``status`` is ``"ok"`` when the manifest parsed, else ``"failed"``. A
    failed candidate still carries ``key``/``source``/``path`` so it can
    participate in fail-closed source resolution; ``manifest`` is ``None``
    and ``diagnostic`` carries a ``"code: message"`` summary.
    """

    key: str
    source: PluginSource
    path: str
    discovery_index: int
    status: str
    diagnostic: str | None = None
    manifest: PluginManifest | None = None
    entry_point: Any = None  # importlib.metadata.EntryPoint for ENTRY_POINT source


@dataclass(frozen=True)
class PluginDiscoveryResult:
    """Phase 1 result: all candidates + priority-resolved winners + warnings."""

    candidates: list[DiscoveryCandidate]
    winners: dict[str, DiscoveryCandidate]
    warnings: list[PluginScanWarning]


class LoaderToken:
    """Opaque, per-scan loader token. Application holds this; only the loader
    interprets it. ``kind`` is ``"directory"`` or ``"entrypoint"``. The payload
    for entrypoints caches the loaded module so load_and_register does not
    call ``ep.load()`` a second time."""

    __slots__ = ("_kind", "_payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self._kind = kind
        self._payload = payload

    @property
    def kind(self) -> str:
        return self._kind


@dataclass(frozen=True)
class PreparedPlugin:
    """Phase 2 result. The token is the only handle to the loaded module; the
    application never holds the module type directly."""

    manifest: PluginManifest
    source: PluginSource
    token: LoaderToken | None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class PluginFileLoader:
    def __init__(self, config: PluginFileLoaderConfig):
        self.config = config

    # -- public orchestrator (kept backward-compatible) ---------------------

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
        discovery = self.discover()
        winners = discovery.winners
        manifests = [w.manifest for w in winners.values() if w.manifest is not None]
        warnings = list(discovery.warnings)

        if self.config.safe_mode:
            return PluginScanResult(
                manifests=manifests,
                registrations={},
                warnings=warnings or [PluginScanWarning(relative_path="", reason="safe_mode")],
                errors={},
                unsupported={},
            )

        registrations: dict[str, list[PluginToolRegistration]] = {}
        errors: dict[str, str] = {}
        unsupported: dict[str, list[str]] = {}

        for key, winner in winners.items():
            if key not in enabled_keys:
                continue
            if winner.manifest is None:
                errors[key] = winner.diagnostic or "discovery_failed"
                continue
            if winner.manifest.kind is not PluginKind.STANDALONE:
                continue
            try:
                prepared = self.prepare(winner)
            except PluginLoaderError as exc:
                logger.warning("plugin %s prepare failed: %s", key, exc, exc_info=True)
                errors[key] = f"{exc.code}: {exc}"
                continue
            # Propagate prepare diagnostics (name/version mismatch, key/source/
            # path drift) to the public warning channel BEFORE load, so they are
            # not lost even when load_and_register later fails.
            for msg in prepared.warnings:
                warnings.append(PluginScanWarning(
                    relative_path=winner.path,
                    reason="prepare_warning",
                    detail=msg,
                ))
            try:
                ctx = self.load_and_register(
                    prepared,
                    config_provider(key) or {},
                    secret_provider(key) or {},
                )
            except PluginLoaderError as exc:
                logger.warning("plugin %s load failed: %s", key, exc, exc_info=True)
                errors[key] = f"{exc.code}: {exc}"
                continue
            if ctx.tool_registrations:
                registrations[key] = ctx.tool_registrations
            if ctx.unsupported_capabilities:
                unsupported[key] = list(ctx.unsupported_capabilities)

        return PluginScanResult(
            manifests=manifests,
            registrations=registrations,
            warnings=warnings,
            errors=errors,
            unsupported=unsupported,
        )

    # -- phase 1: discover ---------------------------------------------------

    def discover(self) -> PluginDiscoveryResult:
        """Read directory YAML + entry-point METADATA only. No ``ep.load()``,
        no plugin code execution. Failed parses become FAILED candidates so
        that higher-priority broken sources fail-closed shadow lower ones."""
        candidates: list[DiscoveryCandidate] = []
        warnings: list[PluginScanWarning] = []
        for source, root in self._iter_sources():
            if root is None:
                continue
            candidates.extend(self._discover_root(root, source, warnings))
        if self.config.enable_entrypoints:
            self._discover_entry_points(candidates, warnings)
        # assign stable discovery indices in discovery order
        indexed = [replace(c, discovery_index=i) for i, c in enumerate(candidates)]
        winners, shadow_warnings = self._resolve_winners(indexed)
        warnings.extend(shadow_warnings)
        return PluginDiscoveryResult(
            candidates=indexed,
            winners=winners,
            warnings=warnings,
        )

    def _iter_sources(self) -> list[tuple[PluginSource, Path | None]]:
        return [
            (PluginSource.BUNDLED, self.config.bundled_root),
            (PluginSource.USER, self.config.user_root),
            (PluginSource.PROJECT, self.config.project_root if self.config.enable_project else None),
        ]

    def _discover_root(
        self,
        root: Path,
        source: PluginSource,
        warnings: list[PluginScanWarning],
    ) -> list[DiscoveryCandidate]:
        root = Path(root)
        if not root.exists():
            return []
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warnings.append(PluginScanWarning(relative_path=str(root), reason="mkdir_failed", detail=str(exc)))
            return []
        state = {"count": 0, "max_warned": False, "stop": False}
        return self._discover_directory_level(
            root, source, root, self.config.skip_names, "", 0, state, warnings
        )

    def _discover_directory_level(
        self,
        path: Path,
        source: PluginSource,
        root: Path,
        skip_names: frozenset[str],
        prefix: str,
        depth: int,
        state: dict,
        warnings: list[PluginScanWarning],
    ) -> list[DiscoveryCandidate]:
        from app.infrastructure.path_security import validate_within_dir

        candidates: list[DiscoveryCandidate] = []
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            warnings.append(PluginScanWarning(relative_path=str(path), reason="iterdir_failed", detail=str(exc)))
            return candidates
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
                cand = self._discover_directory_candidate(manifest_path, source, key, child, warnings)
                candidates.append(cand)
                if cand.manifest is not None:
                    state["count"] += 1
                continue
            if depth < 1:
                child_prefix = f"{prefix}/{child.name}" if prefix else child.name
                candidates.extend(
                    self._discover_directory_level(
                        child, source, root, skip_names, child_prefix, depth + 1, state, warnings
                    )
                )
        return candidates

    def _manifest_path_for(self, plugin_dir: Path) -> Path | None:
        yaml_path = plugin_dir / PLUGIN_YAML
        if yaml_path.is_file():
            return yaml_path
        yml_path = plugin_dir / PLUGIN_YML
        if yml_path.is_file():
            return yml_path
        return None

    def _discover_directory_candidate(
        self,
        plugin_yaml: Path,
        source: PluginSource,
        key: str,
        plugin_dir: Path,
        warnings: list[PluginScanWarning],
    ) -> DiscoveryCandidate:
        """Parse a directory manifest into a candidate. A broken YAML or
        invalid manifest yields a FAILED candidate (not dropped) and a
        backward-compatible warning."""
        try:
            raw = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            warnings.append(PluginScanWarning(relative_path=str(plugin_yaml), reason="yaml_parse_error", detail=str(exc)))
            return DiscoveryCandidate(
                key=key, source=source, path=str(plugin_dir), discovery_index=0,
                status="failed", diagnostic=f"yaml_parse_error: {exc}", manifest=None,
            )
        except OSError as exc:
            warnings.append(PluginScanWarning(relative_path=str(plugin_yaml), reason="read_error", detail=str(exc)))
            return DiscoveryCandidate(
                key=key, source=source, path=str(plugin_dir), discovery_index=0,
                status="failed", diagnostic=f"read_error: {exc}", manifest=None,
            )
        try:
            manifest = PluginManifest.from_yaml(
                raw if isinstance(raw, dict) else {},
                source=source,
                key=key,
                path=str(plugin_dir),
            )
        except PluginValidationError as exc:
            warnings.append(PluginScanWarning(relative_path=str(plugin_yaml), reason="invalid_manifest", detail=str(exc)))
            return DiscoveryCandidate(
                key=key, source=source, path=str(plugin_dir), discovery_index=0,
                status="failed", diagnostic=f"invalid_manifest: {exc}", manifest=None,
            )
        return DiscoveryCandidate(
            key=key, source=source, path=str(plugin_dir), discovery_index=0,
            status="ok", diagnostic=None, manifest=manifest,
        )

    def _entry_points_for(self, groups: tuple[str, ...]) -> list[Any]:
        """Return importlib entry points for the given groups. Tests monkeypatch
        this to inject fake EntryPoint objects."""
        from importlib.metadata import entry_points

        eps = entry_points()
        result: list[Any] = []
        for group in groups:
            if hasattr(eps, "select"):
                result.extend(eps.select(group=group))
            else:
                result.extend(eps.get(group, []))
        return result

    def _entry_point_version(self, ep: Any) -> str:
        dist = getattr(ep, "dist", None)
        version = getattr(dist, "version", None)
        return str(version) if version else "0"

    def _discover_entry_points(
        self,
        candidates: list[DiscoveryCandidate],
        warnings: list[PluginScanWarning],
    ) -> None:
        """Discover entry-point candidates from METADATA only (no ep.load()).

        ``n_agent.plugins`` is scanned first; on a same-name collision with
        ``hermes_agent.plugins`` the n_agent entry wins and a
        ``duplicate_entrypoint`` warning records both groups."""
        seen_names: dict[str, str] = {}
        for group in ENTRY_POINT_GROUPS:
            try:
                eps = self._entry_points_for((group,))
            except Exception as exc:
                warnings.append(PluginScanWarning(
                    relative_path="", reason="entrypoint_scan_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                ))
                continue
            for ep in eps:
                if ep.name in seen_names:
                    warnings.append(PluginScanWarning(
                        relative_path=f"entrypoint:{group}:{ep.name}",
                        reason="duplicate_entrypoint",
                        detail=f"name {ep.name!r} in group {group!r} shadowed by {seen_names[ep.name]!r}",
                    ))
                    continue
                seen_names[ep.name] = group
                path = f"entrypoint:{ep.group}:{ep.name}"
                version = self._entry_point_version(ep)
                try:
                    manifest = PluginManifest.from_yaml(
                        {"name": ep.name, "version": version},
                        source=PluginSource.ENTRY_POINT,
                        key=ep.name,
                        path=path,
                    )
                except PluginValidationError as exc:
                    candidates.append(DiscoveryCandidate(
                        key=ep.name, source=PluginSource.ENTRY_POINT, path=path,
                        discovery_index=0, status="failed",
                        diagnostic=f"entrypoint_invalid_manifest: {exc}", manifest=None,
                        entry_point=ep,
                    ))
                    continue
                candidates.append(DiscoveryCandidate(
                    key=ep.name, source=PluginSource.ENTRY_POINT, path=path,
                    discovery_index=0, status="ok", diagnostic=None,
                    manifest=manifest, entry_point=ep,
                ))

    def _resolve_winners(
        self,
        candidates: list[DiscoveryCandidate],
    ) -> tuple[dict[str, DiscoveryCandidate], list[PluginScanWarning]]:
        """Resolve key collisions by source priority (fail-closed: a broken
        higher-priority candidate still shadows lower-priority ones). Returns
        winners keyed by plugin key plus shadowing warnings."""
        by_key: dict[str, list[DiscoveryCandidate]] = {}
        for cand in candidates:
            by_key.setdefault(cand.key, []).append(cand)
        winners: dict[str, DiscoveryCandidate] = {}
        warnings: list[PluginScanWarning] = []
        for key, group in by_key.items():
            if len(group) == 1:
                winners[key] = group[0]
                continue
            winner = max(group, key=lambda c: _SOURCE_PRIORITY[c.source])
            shadowed = [c for c in group if c is not winner]
            shadowed_str = ", ".join(f"{c.source.value}:{c.path}" for c in shadowed)
            warnings.append(PluginScanWarning(
                relative_path=winner.path,
                reason="source_shadowed",
                detail=f"winner={winner.source.value}:{winner.path}; shadowed={shadowed_str}",
            ))
            winners[key] = winner
        return winners, warnings

    # -- phase 2: prepare ----------------------------------------------------

    def prepare(self, candidate: DiscoveryCandidate) -> PreparedPlugin:
        """Directory plugins: no import (returns manifest as-is). Entry-point
        plugins: call ``ep.load()`` ONCE, supplement the synthetic manifest
        from ``PLUGIN_MANIFEST`` (key/source/path never drift; name/version
        mismatch -> warning, discovery identity wins). The opaque token caches
        the loaded module for load_and_register."""
        if candidate.manifest is None:
            raise PluginLoaderError("discovery_failed", candidate.diagnostic or "discovery failed")
        manifest = candidate.manifest
        if candidate.source is PluginSource.ENTRY_POINT:
            ep = candidate.entry_point
            if ep is None:
                raise EntrypointLoadFailed(
                    "entrypoint_load_failed",
                    f"plugin {manifest.key}: no entry point reference",
                )
            try:
                module = self._load_entrypoint_module(ep)
            except PluginLoaderError:
                raise
            except Exception as exc:
                logger.warning("entrypoint %s load failed: %s", manifest.key, exc, exc_info=True)
                raise EntrypointLoadFailed(
                    "entrypoint_load_failed", f"{type(exc).__name__}: {exc}"
                ) from exc
            raw_pm = getattr(module, "PLUGIN_MANIFEST", None)
            if raw_pm is None:
                raw_pm = {}
            if not isinstance(raw_pm, dict):
                raise EntrypointLoadFailed(
                    "entrypoint_invalid_manifest",
                    f"plugin {manifest.key}: PLUGIN_MANIFEST must be a mapping",
                )
            prep_warnings: list[str] = []
            supplemented = self._supplement_manifest(manifest, raw_pm, prep_warnings)
            register_fn = getattr(module, "register", None)
            token = LoaderToken(
                "entrypoint",
                {
                    "module": module,
                    "register_fn": register_fn,
                    "group": getattr(ep, "group", ""),
                    "name": getattr(ep, "name", manifest.key),
                },
            )
            return PreparedPlugin(
                manifest=supplemented,
                source=candidate.source,
                token=token,
                warnings=prep_warnings,
            )
        # directory plugin: no import here
        token = LoaderToken("directory", {"path": manifest.path})
        return PreparedPlugin(
            manifest=manifest,
            source=candidate.source,
            token=token,
            warnings=[],
        )

    def _load_entrypoint_module(self, ep: Any) -> Any:
        """Load (once) the object referenced by an entry point. The winning
        group/name is retained on the candidate, so this selects the right ep
        without re-scanning groups."""
        return ep.load()

    def _supplement_manifest(
        self,
        manifest: PluginManifest,
        raw_pm: dict[str, Any],
        warnings: list[str],
    ) -> PluginManifest:
        """Merge PLUGIN_MANIFEST into the synthetic discovery manifest.

        Only description/author/kind/provides/dependency fields may be
        supplemented. key/source/path never drift (ignored if present).
        name/version mismatch -> warning, discovery identity wins.
        """
        merged_raw: dict[str, Any] = dict(raw_pm)
        pm_name = str(raw_pm.get("name", "")).strip() if "name" in raw_pm else ""
        if pm_name and pm_name != manifest.name:
            warnings.append(
                f"name mismatch: discovery={manifest.name!r} plugin_manifest={pm_name!r}; discovery identity kept"
            )
        pm_version = str(raw_pm.get("version", "")).strip() if "version" in raw_pm else ""
        if pm_version and pm_version != manifest.version:
            warnings.append(
                f"version mismatch: discovery={manifest.version!r} plugin_manifest={pm_version!r}; discovery identity kept"
            )
        for drift_field in ("key", "source", "path"):
            if drift_field in raw_pm:
                warnings.append(f"{drift_field} drift ignored in PLUGIN_MANIFEST")
        # force discovery identity; supplement fields flow from merged_raw
        merged_raw["name"] = manifest.name
        merged_raw["version"] = manifest.version
        try:
            return PluginManifest.from_yaml(
                merged_raw,
                source=manifest.source,
                key=manifest.key,
                path=manifest.path,
            )
        except PluginValidationError as exc:
            raise EntrypointLoadFailed(
                "entrypoint_invalid_manifest", f"plugin {manifest.key}: {exc}"
            ) from exc

    # -- phase 3: load + register -------------------------------------------

    def load_and_register(
        self,
        prepared: PreparedPlugin,
        plugin_config: dict[str, Any],
        secret_config: dict[str, str],
    ) -> PluginContext:
        """Create a fresh PluginContext and call ``register(ctx)``. Entry-point
        plugins reuse the prepare-cached module (no second ep.load(), no
        directory fallback). Directory plugins load through ``__init__.py``.
        Each plugin gets an independent Context."""
        manifest = prepared.manifest
        ctx = PluginContext(
            plugin_key=manifest.key,
            plugin_config=plugin_config,
            secret_config=secret_config,
        )
        token = prepared.token
        if token is None:
            raise PluginRegisterFailed("register_failed", f"plugin {manifest.key}: no loader token")
        if token.kind == "entrypoint":
            payload = token._payload  # noqa: SLF001 (loader-internal)
            register_fn = payload.get("register_fn")
            if not callable(register_fn):
                raise PluginRegisterFailed(
                    "register_failed",
                    f"plugin {manifest.key}: entrypoint module has no callable register",
                )
            try:
                register_fn(ctx)
            except Exception as exc:
                raise PluginRegisterFailed(
                    "register_failed", f"{type(exc).__name__}: {exc}"
                ) from exc
            return ctx
        # directory plugin: import __init__.py then register
        module = self._load_directory_module(manifest)
        register_fn = getattr(module, "register", None)
        if not callable(register_fn):
            raise PluginRegisterFailed(
                "register_failed", f"plugin {manifest.key}: missing register(ctx) entrypoint"
            )
        try:
            register_fn(ctx)
        except Exception as exc:
            raise PluginRegisterFailed(
                "register_failed", f"{type(exc).__name__}: {exc}"
            ) from exc
        return ctx

    def _load_directory_module(self, manifest: PluginManifest) -> Any:
        """Import a directory plugin's ``__init__.py`` under a sandboxed
        namespace. Extracted from the legacy single-phase load path."""
        import types

        plugin_dir = Path(manifest.path)
        init_file = plugin_dir / PLUGIN_INIT
        if not init_file.is_file():
            raise DirectoryLoadFailed(
                "directory_load_failed",
                f"plugin {manifest.key}: missing __init__.py at {plugin_dir}",
            )
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
            raise DirectoryLoadFailed(
                "directory_load_failed",
                f"plugin {manifest.key}: cannot create module spec for {init_file}",
            )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = module_name
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            for name in list(sys.modules.keys()):
                if name == module_name or name.startswith(module_name + "."):
                    del sys.modules[name]
            raise DirectoryLoadFailed(
                "directory_load_failed", f"{type(exc).__name__}: {exc}"
            ) from exc
        return module


def _safe_module_name(key: str) -> str:
    safe = []
    for ch in str(key):
        if ch.isalnum() or ch == "_":
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe) or "plugin"
