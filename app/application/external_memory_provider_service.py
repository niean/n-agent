# app/application/external_memory_provider_service.py
from __future__ import annotations
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from app.application.external_memory_manager import ExternalMemoryManager
from app.domain.external_memory_provider import (
    ExternalMemoryProviderConfig, ExternalMemoryProviderNotFoundError,
    ExternalMemoryProviderRegistry, ExternalMemoryProviderType,
    ExternalMemoryProbeStatus,
)

logger = logging.getLogger(__name__)


class ActiveExternalMemoryReader(Protocol):
    def get_active_provider_names(self) -> list[str]: ...


AdapterFactory = Callable[[dict[str, Any], str | None], Any]


@dataclass(frozen=True)
class ActivateResult:
    config: ExternalMemoryProviderConfig
    tool_surface_refresh_failed: bool


class ExternalMemoryProviderService(ActiveExternalMemoryReader):
    def __init__(
        self,
        *,
        registry: ExternalMemoryProviderRegistry,
        manager: ExternalMemoryManager,
        factories: dict[ExternalMemoryProviderType, AdapterFactory],
        workspace_root: Path,
    ) -> None:
        self._registry = registry
        self._manager = manager
        self._factories = factories
        self._workspace_root = workspace_root

    # --- CRUD ---

    def list(self) -> list[ExternalMemoryProviderConfig]:
        return self._registry.list_providers()

    def get(self, id: str) -> ExternalMemoryProviderConfig:
        cfg = self._registry.get_provider(id)
        if cfg is None:
            raise ExternalMemoryProviderNotFoundError(id)
        return cfg

    def create(
        self, *, name: str, provider_type: ExternalMemoryProviderType,
        base_url: str, api_key: str | None, extra_config: dict[str, Any],
    ) -> ExternalMemoryProviderConfig:
        pid = uuid.uuid4().hex[:12]
        return self._registry.create_provider(
            id=pid, name=name, provider_type=provider_type, base_url=base_url,
            api_key=api_key, enabled=False, extra_config=extra_config,
        )

    def update(
        self, id: str, *, name: str | None = None, base_url: str | None = None,
        api_key: str | None = None, clear_api_key: bool = False,
        extra_config: dict[str, Any] | None = None,
    ) -> tuple[ExternalMemoryProviderConfig, bool | None]:
        cfg = self._registry.update_provider(
            id, name=name, base_url=base_url, api_key=api_key,
            clear_api_key=clear_api_key, extra_config=extra_config,
        )
        if not cfg.enabled:
            return cfg, None
        try:
            secret = self._registry.get_secret(id)
            factory = self._factories[cfg.provider_type]
            adapter = factory(self._build_adapter_config(cfg), secret.api_key if secret else None)
            adapter.initialize(session_id="", project_root=str(self._workspace_root))
        except Exception as exc:
            logger.warning("active provider %s reload failed (old adapter retained): %s", id, exc)
            return cfg, True
        swap_result = self._manager.swap_external_query_provider(adapter)
        return cfg, swap_result["tool_surface_refresh_failed"]

    def delete(self, id: str) -> bool:
        cfg = self.get(id)
        if cfg.enabled:
            # swap(None) 清理 external-query slot；忽略 refresh_failed（删除后续不再用）
            self._manager.swap_external_query_provider(None)
        return self._registry.delete_provider(id)

    # --- activate / probe ---

    def _build_adapter_config(self, cfg: ExternalMemoryProviderConfig) -> dict[str, Any]:
        # base_url 是 provider 顶层字段（SQLite base_url 列），extra_config 是附加配置；
        # adapter 从 config dict 读 base_url，必须合入，否则 Dashboard 配置的地址被静默忽略
        return {"base_url": cfg.base_url, **cfg.extra_config}

    def activate(self, id: str) -> ActivateResult:
        cfg = self.get(id)
        secret = self._registry.get_secret(id)
        factory = self._factories[cfg.provider_type]
        adapter = factory(self._build_adapter_config(cfg), secret.api_key if secret else None)
        adapter.initialize(session_id="", project_root=str(self._workspace_root))
        # registry 层 deactivate 其他
        for other in self._registry.list_providers():
            if other.id != id and other.enabled:
                self._registry.update_provider(other.id, enabled=False)
        # 激活当前
        updated = self._registry.update_provider(id, enabled=True)
        # 装载到 manager（swap 返回 tool_surface_refresh_failed）
        swap_result = self._manager.swap_external_query_provider(adapter)
        return ActivateResult(config=updated, tool_surface_refresh_failed=swap_result["tool_surface_refresh_failed"])

    def deactivate(self, id: str) -> ExternalMemoryProviderConfig:
        cfg = self.get(id)
        if not cfg.enabled:
            return cfg
        self._manager.swap_external_query_provider(None)
        return self._registry.update_provider(id, enabled=False)

    def probe(self, id: str) -> ExternalMemoryProbeStatus:
        cfg = self.get(id)
        secret = self._registry.get_secret(id)
        factory = self._factories[cfg.provider_type]
        adapter = factory(self._build_adapter_config(cfg), secret.api_key if secret else None)
        try:
            adapter.initialize(session_id="", project_root=str(self._workspace_root))
            if not adapter.is_available():
                self._registry.save_probe_status(id, ExternalMemoryProbeStatus.FAILED, "not available")
                return ExternalMemoryProbeStatus.FAILED
            # 轻量 query 验证后端可用
            if cfg.provider_type == ExternalMemoryProviderType.HOLOGRAPHIC:
                adapter.prefetch("probe", session_id="probe")
            elif cfg.provider_type == ExternalMemoryProviderType.MEM0:
                # mem0 probe 联网：handle_tool_call 内部 POST /memories/?page=1&page_size=50
                # 2xx → success=True；401/网络异常 → success=False（handle_tool_call 已 except）
                result = json.loads(adapter.handle_tool_call("mem0_profile", {}))
                if not result.get("success"):
                    err = result.get("error", "probe failed")
                    self._registry.save_probe_status(id, ExternalMemoryProbeStatus.FAILED, err)
                    return ExternalMemoryProbeStatus.FAILED
            elif cfg.provider_type == ExternalMemoryProviderType.HONCHO:
                # honcho probe 联网：GET /v3/workspaces/{wid}/sessions/probe/context
                # 2xx → success=True；401/网络异常 → success=False（probe 内部已 except）
                result = json.loads(adapter.probe())
                if not result.get("success"):
                    err = result.get("error", "probe failed")
                    self._registry.save_probe_status(id, ExternalMemoryProbeStatus.FAILED, err)
                    return ExternalMemoryProbeStatus.FAILED
            self._registry.save_probe_status(id, ExternalMemoryProbeStatus.OK)
            return ExternalMemoryProbeStatus.OK
        except Exception as exc:
            self._registry.save_probe_status(id, ExternalMemoryProbeStatus.FAILED, str(exc))
            return ExternalMemoryProbeStatus.FAILED

    # --- ActiveExternalMemoryReader ---

    def get_active_provider_names(self) -> list[str]:
        # 直接读 manager 内存状态，无 IO（不走 registry/SQLite）
        name = self._manager.get_active_external_query_provider_name()
        return [name] if name else []
