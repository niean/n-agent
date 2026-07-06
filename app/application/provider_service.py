from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from app.domain.provider import (
    ProviderConfig,
    ProviderInUseError,
    ProviderNotFoundError,
    ProviderRegistry,
    ProviderValidationError,
)


SUPPORTED_PROVIDER_TYPES = {"openai-compatible", "anthropic"}


@dataclass
class ProviderCreateInput:
    name: str
    base_url: str
    model: str
    api_key: str
    provider_type: str = "openai-compatible"
    extra_headers: dict[str, str] | None = None
    supports_vision: bool | None = None


@dataclass
class ProviderUpdateInput:
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    provider_type: str | None = None
    api_key: str | None = None
    extra_headers: dict[str, str] | None = None
    supports_vision: bool | None = None


class _ActiveSwapper(Protocol):
    async def swap(self, config: ProviderConfig, api_key: str) -> None:
        ...


class ProviderService:
    def __init__(self, registry: ProviderRegistry, holder: _ActiveSwapper):
        self.registry = registry
        self.holder = holder

    async def list_providers(self) -> list[ProviderConfig]:
        return await self.registry.list_providers()

    async def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return await self.registry.get_provider(provider_id)

    async def create_provider(self, payload: ProviderCreateInput) -> ProviderConfig:
        provider_type = payload.provider_type or "openai-compatible"
        self._validate(payload.name, payload.base_url, payload.model, provider_type)
        if not payload.api_key:
            raise ProviderValidationError("api_key is required for new provider")
        if payload.supports_vision is not None:
            supports_vision = payload.supports_vision
        else:
            supports_vision = provider_type == "openai-compatible"
        now = datetime.now(timezone.utc)
        cfg = ProviderConfig(
            id="",
            name=payload.name.strip(),
            provider_type=provider_type,
            base_url=payload.base_url.strip(),
            model=payload.model.strip(),
            api_key_present=False,
            is_active=False,
            extra_headers=payload.extra_headers,
            created_at=now,
            updated_at=now,
            supports_vision=supports_vision,
        )
        return await self.registry.create_provider(cfg, payload.api_key)

    async def update_provider(self, provider_id: str, patch: ProviderUpdateInput) -> ProviderConfig:
        existing = await self.registry.get_provider(provider_id)
        if existing is None:
            raise ProviderNotFoundError(provider_id)
        name = patch.name if patch.name is not None else existing.name
        base_url = patch.base_url if patch.base_url is not None else existing.base_url
        model = patch.model if patch.model is not None else existing.model
        provider_type = patch.provider_type if patch.provider_type is not None else existing.provider_type
        self._validate(name, base_url, model, provider_type)
        clear_key = patch.api_key == ""
        api_key_value = patch.api_key if (patch.api_key is not None and patch.api_key != "") else None
        cfg = await self.registry.update_provider(
            provider_id,
            name=patch.name,
            base_url=patch.base_url,
            model=patch.model,
            provider_type=patch.provider_type,
            extra_headers=patch.extra_headers,
            api_key=api_key_value,
            clear_api_key=clear_key,
            supports_vision=patch.supports_vision,
        )
        if cfg.is_active:
            secret = await self.registry.get_secret(provider_id) or ""
            await self.holder.swap(cfg, secret)
        return cfg

    async def delete_provider(self, provider_id: str) -> None:
        existing = await self.registry.get_provider(provider_id)
        if existing is None:
            raise ProviderNotFoundError(provider_id)
        if existing.is_active:
            raise ProviderInUseError(provider_id)
        await self.registry.delete_provider(provider_id)

    async def activate_provider(self, provider_id: str) -> ProviderConfig:
        existing = await self.registry.get_provider(provider_id)
        if existing is None:
            raise ProviderNotFoundError(provider_id)
        self._validate_provider_type(existing.provider_type)
        cfg = await self.registry.set_active(provider_id)
        secret = await self.registry.get_secret(provider_id) or ""
        await self.holder.swap(cfg, secret)
        return cfg

    @staticmethod
    def _validate(name: str, base_url: str, model: str, provider_type: str) -> None:
        if not (name and name.strip()):
            raise ProviderValidationError("name is required")
        if not (model and model.strip()):
            raise ProviderValidationError("model is required")
        if not (base_url and base_url.strip()):
            raise ProviderValidationError("base_url is required")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ProviderValidationError("base_url must start with http:// or https://")
        ProviderService._validate_provider_type(provider_type)

    @staticmethod
    def _validate_provider_type(provider_type: str) -> None:
        if provider_type not in SUPPORTED_PROVIDER_TYPES:
            raise ProviderValidationError("provider_type is unsupported")


__all__ = [
    "ProviderCreateInput",
    "ProviderService",
    "ProviderUpdateInput",
]
