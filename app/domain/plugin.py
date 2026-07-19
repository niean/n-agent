from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Protocol
from uuid import uuid4


class PluginSource(str, Enum):
    BUNDLED = "bundled"
    USER = "user"
    PROJECT = "project"
    ENTRY_POINT = "entry_point"


class PluginKind(str, Enum):
    STANDALONE = "standalone"
    BACKEND = "backend"
    EXCLUSIVE = "exclusive"
    PLATFORM = "platform"
    MODEL_PROVIDER = "model-provider"


class PluginScanStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"


def _normalize_string_list(
    raw_value: Any, *, field_name: str, plugin_name: str
) -> list[str]:
    """Strict-normalize a list-of-strings manifest field.

    None -> []. Non-list (including scalar strings) -> PluginValidationError.
    Each item must be a non-empty string; duplicates removed preserving order.
    """
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise PluginValidationError(
            f"plugin {plugin_name}: {field_name} must be a list"
        )
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        if not isinstance(item, str):
            raise PluginValidationError(
                f"plugin {plugin_name}: {field_name} items must be strings"
            )
        stripped = item.strip()
        if not stripped:
            raise PluginValidationError(
                f"plugin {plugin_name}: {field_name} items must be non-empty"
            )
        if stripped in seen:
            continue
        seen.add(stripped)
        result.append(stripped)
    return result


def _normalize_external_dependencies(
    raw_value: Any, *, plugin_name: str
) -> list[dict[str, str]]:
    """Strict-normalize external_dependencies.

    None -> []. Non-list -> PluginValidationError. Each item must be a mapping
    with a non-empty string ``name``; optional ``install``/``check`` must be
    strings when present (null treated as absent). Public projection only
    carries name/install/check; unknown keys are dropped (but preserved in raw).
    """
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise PluginValidationError(
            f"plugin {plugin_name}: external_dependencies must be a list"
        )
    result: list[dict[str, str]] = []
    for item in raw_value:
        if not isinstance(item, dict):
            raise PluginValidationError(
                f"plugin {plugin_name}: external_dependencies items must be mappings"
            )
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PluginValidationError(
                f"plugin {plugin_name}: external_dependencies items must have a non-empty string name"
            )
        normalized: dict[str, str] = {"name": name.strip()}
        for opt_key in ("install", "check"):
            if opt_key in item and item[opt_key] is not None:
                value = item[opt_key]
                if not isinstance(value, str):
                    raise PluginValidationError(
                        f"plugin {plugin_name}: external_dependencies {opt_key} must be a string"
                    )
                normalized[opt_key] = value
        result.append(normalized)
    return result


@dataclass(frozen=True)
class PluginManifest:
    key: str
    name: str
    version: str
    description: str
    source: PluginSource
    path: str
    kind: PluginKind = PluginKind.STANDALONE
    author: str = ""
    requires_env: list[dict[str, Any]] = field(default_factory=list)
    optional_env: list[dict[str, Any]] = field(default_factory=list)
    provides_tools: list[str] = field(default_factory=list)
    provides_hooks: list[str] = field(default_factory=list)
    provides_web_providers: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    pip_dependencies: list[str] = field(default_factory=list)
    external_dependencies: list[dict[str, str]] = field(default_factory=list)
    requires_plugins: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(
        cls,
        raw: dict[str, Any],
        *,
        source: PluginSource,
        key: str,
        path: str,
    ) -> "PluginManifest":
        if not isinstance(raw, dict):
            raise PluginValidationError("plugin.yaml must be a mapping")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise PluginValidationError("plugin.yaml missing required field: name")
        version = str(raw.get("version", "")).strip()
        if not version:
            raise PluginValidationError(f"plugin {name}: missing required field: version")
        description = str(raw.get("description", "")).strip()
        kind_raw = str(raw.get("kind", "standalone")).strip()
        try:
            kind = PluginKind(kind_raw)
        except ValueError as exc:
            raise PluginValidationError(
                f"plugin {name}: invalid kind {kind_raw!r} (allowed: {', '.join(k.value for k in PluginKind)})"
            ) from exc
        return cls(
            key=key,
            name=name,
            version=version,
            description=description,
            source=source,
            path=path,
            kind=kind,
            author=str(raw.get("author", "")).strip(),
            requires_env=list(raw.get("requires_env", []) or []),
            optional_env=list(raw.get("optional_env", []) or []),
            provides_tools=list(raw.get("provides_tools", []) or []),
            provides_hooks=list(raw.get("provides_hooks", []) or []),
            provides_web_providers=list(raw.get("provides_web_providers", []) or []),
            platforms=list(raw.get("platforms", []) or []),
            pip_dependencies=_normalize_string_list(
                raw.get("pip_dependencies"), field_name="pip_dependencies", plugin_name=name
            ),
            external_dependencies=_normalize_external_dependencies(
                raw.get("external_dependencies"), plugin_name=name
            ),
            requires_plugins=_normalize_string_list(
                raw.get("requires_plugins"), field_name="requires_plugins", plugin_name=name
            ),
            config_schema=dict(raw.get("config_schema", {}) or {}),
            raw=dict(raw),
        )


@dataclass(frozen=True)
class Plugin:
    id: str
    key: str
    name: str
    source: PluginSource
    enabled: bool = False
    version: str = ""
    description: str = ""
    author: str = ""
    kind: PluginKind = PluginKind.STANDALONE
    source_path: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    secret_refs: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    last_scan_status: str | None = None
    last_scan_error: str | None = None
    last_scanned_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_public_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "kind": self.kind.value,
            "source": self.source.value,
            "source_path": self.source_path,
            "enabled": self.enabled,
            "config": dict(self.config),
            "secret_refs": {field: bool(value) for field, value in self.secret_refs.items()},
            "capabilities": dict(self.capabilities),
            "last_scan_status": self.last_scan_status,
            "last_scan_error": self.last_scan_error,
            "last_scanned_at": self.last_scanned_at.isoformat() if self.last_scanned_at else None,
        }

    def to_public_detail(self) -> dict[str, Any]:
        view = self.to_public_view()
        view["manifest"] = dict(self.manifest)
        return view

    def with_secret_refs(self, secrets: dict[str, str]) -> "Plugin":
        return replace(self, secret_refs=dict(secrets))


class PluginRegistry(Protocol):
    async def list_plugins(self, include_disabled: bool = True) -> list[Plugin]: ...
    async def get_plugin(self, key: str) -> Plugin | None: ...
    async def upsert_plugin(self, plugin: Plugin) -> Plugin: ...
    async def set_enabled(self, key: str, enabled: bool) -> Plugin: ...
    async def delete_plugin(self, key: str) -> bool: ...
    async def update_config(
        self,
        key: str,
        config: dict[str, Any],
        secret_updates: dict[str, str] | None = None,
    ) -> Plugin: ...
    async def get_secret_config(self, key: str) -> dict[str, str]: ...
    async def replace_all_plugins(self, plugins: Iterable[Plugin]) -> list[Plugin]: ...


class PluginNotFoundError(Exception):
    pass


class PluginValidationError(Exception):
    pass


class PluginScanError(Exception):
    pass


def new_plugin_id() -> str:
    return f"plg-{uuid4()}"
