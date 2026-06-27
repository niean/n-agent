# app/domain/external_memory_provider.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class ExternalMemoryProviderType(str, Enum):
    MEM0 = "mem0"
    HOLOGRAPHIC = "holographic"
    HONCHO = "honcho"


class ExternalMemoryProbeStatus(str, Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True)
class ExternalMemoryProviderConfig:
    id: str
    name: str
    provider_type: ExternalMemoryProviderType
    base_url: str
    api_key_present: bool
    enabled: bool
    extra_config: dict[str, Any] = field(default_factory=dict)
    probe_status: ExternalMemoryProbeStatus | None = None
    last_probe_error: str | None = None
    last_probed_at: datetime | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ExternalMemoryProviderSecret:
    id: str
    api_key: str | None


class ExternalMemoryProviderNotFoundError(Exception): ...
class DuplicateExternalMemoryProviderError(Exception): ...
class ExternalMemoryProviderInUseError(Exception): ...
class ExternalMemoryProviderValidationError(Exception): ...


class ExternalMemoryProviderRegistry(Protocol):
    def list_providers(self) -> list[ExternalMemoryProviderConfig]: ...
    def get_provider(self, id: str) -> ExternalMemoryProviderConfig | None: ...
    def create_provider(
        self, *, id: str, name: str, provider_type: ExternalMemoryProviderType,
        base_url: str, api_key: str | None, enabled: bool, extra_config: dict[str, Any],
    ) -> ExternalMemoryProviderConfig: ...
    def update_provider(
        self, id: str, *, name: str | None = None, base_url: str | None = None,
        api_key: str | None = None, clear_api_key: bool = False,
        enabled: bool | None = None, extra_config: dict[str, Any] | None = None,
    ) -> ExternalMemoryProviderConfig: ...
    def delete_provider(self, id: str) -> bool: ...
    def get_secret(self, id: str) -> ExternalMemoryProviderSecret | None: ...
    def save_probe_status(
        self, id: str, status: ExternalMemoryProbeStatus, error: str | None = None,
    ) -> None: ...
    def create_tables(self) -> None: ...
