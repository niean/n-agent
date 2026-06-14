import asyncio
from datetime import datetime, timezone

import pytest

from app.application.provider_service import (
    ProviderCreateInput,
    ProviderService,
    ProviderUpdateInput,
)
from app.domain.provider import (
    DuplicateProviderError,
    ProviderConfig,
    ProviderInUseError,
    ProviderNotFoundError,
    ProviderValidationError,
)


class FakeRegistry:
    def __init__(self):
        self.items: dict[str, ProviderConfig] = {}
        self.secrets: dict[str, str | None] = {}
        self._counter = 0

    async def list_providers(self):
        return list(self.items.values())

    async def get_provider(self, pid):
        return self.items.get(pid)

    async def create_provider(self, config, api_key):
        if any(c.name == config.name for c in self.items.values()):
            raise DuplicateProviderError(config.name)
        self._counter += 1
        pid = f"id-{self._counter}"
        cfg = ProviderConfig(
            id=pid,
            name=config.name,
            provider_type=config.provider_type,
            base_url=config.base_url,
            model=config.model,
            api_key_present=bool(api_key),
            is_active=False,
            extra_headers=config.extra_headers,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.items[pid] = cfg
        self.secrets[pid] = api_key or None
        return cfg

    async def update_provider(self, pid, **kw):
        if pid not in self.items:
            raise ProviderNotFoundError(pid)
        cfg = self.items[pid]
        api_key = kw.pop("api_key", None)
        clear = kw.pop("clear_api_key", False)
        if clear:
            self.secrets[pid] = None
        elif api_key is not None:
            self.secrets[pid] = api_key
        merged = {**cfg.__dict__}
        for key, val in kw.items():
            if val is not None:
                merged[key] = val
        merged["api_key_present"] = bool(self.secrets.get(pid))
        merged["updated_at"] = datetime.now(timezone.utc)
        new = ProviderConfig(**merged)
        self.items[pid] = new
        return new

    async def delete_provider(self, pid):
        if pid not in self.items:
            raise ProviderNotFoundError(pid)
        self.items.pop(pid)
        self.secrets.pop(pid, None)

    async def set_active(self, pid):
        if pid not in self.items:
            raise ProviderNotFoundError(pid)
        for k, v in self.items.items():
            self.items[k] = ProviderConfig(**{**v.__dict__, "is_active": (k == pid)})
        return self.items[pid]

    async def get_active(self):
        return next((c for c in self.items.values() if c.is_active), None)

    async def get_secret(self, pid):
        if pid not in self.items:
            raise ProviderNotFoundError(pid)
        return self.secrets.get(pid)


class FakeHolder:
    def __init__(self):
        self.swaps = []

    async def swap(self, cfg, api_key):
        self.swaps.append((cfg.id, api_key))


def _service():
    registry = FakeRegistry()
    holder = FakeHolder()
    return ProviderService(registry, holder), registry, holder


def test_create_validates_and_persists():
    service, registry, _ = _service()
    cfg = asyncio.run(
        service.create_provider(
            ProviderCreateInput(name="A", base_url="http://x", model="m", api_key="k")
        )
    )
    assert cfg.id and cfg.api_key_present is True
    assert registry.secrets[cfg.id] == "k"


def test_create_validation_errors():
    service, _, _ = _service()
    with pytest.raises(ProviderValidationError):
        asyncio.run(
            service.create_provider(
                ProviderCreateInput(name="", base_url="http://x", model="m", api_key="k")
            )
        )
    with pytest.raises(ProviderValidationError):
        asyncio.run(
            service.create_provider(
                ProviderCreateInput(name="A", base_url="ftp://x", model="m", api_key="k")
            )
        )
    with pytest.raises(ProviderValidationError):
        asyncio.run(
            service.create_provider(
                ProviderCreateInput(name="A", base_url="http://x", model="m", api_key="")
            )
        )


def test_update_api_key_three_states():
    service, registry, _ = _service()
    cfg = asyncio.run(
        service.create_provider(
            ProviderCreateInput(name="A", base_url="http://x", model="m", api_key="orig")
        )
    )
    asyncio.run(service.update_provider(cfg.id, ProviderUpdateInput(name="A2")))
    assert registry.secrets[cfg.id] == "orig"
    asyncio.run(service.update_provider(cfg.id, ProviderUpdateInput(api_key="new")))
    assert registry.secrets[cfg.id] == "new"
    asyncio.run(service.update_provider(cfg.id, ProviderUpdateInput(api_key="")))
    assert registry.secrets[cfg.id] is None


def test_activate_triggers_holder_swap():
    service, _, holder = _service()
    cfg = asyncio.run(
        service.create_provider(
            ProviderCreateInput(name="A", base_url="http://x", model="m", api_key="k")
        )
    )
    asyncio.run(service.activate_provider(cfg.id))
    assert holder.swaps and holder.swaps[-1][0] == cfg.id


def test_delete_active_rejected():
    service, _, _ = _service()
    cfg = asyncio.run(
        service.create_provider(
            ProviderCreateInput(name="A", base_url="http://x", model="m", api_key="k")
        )
    )
    asyncio.run(service.activate_provider(cfg.id))
    with pytest.raises(ProviderInUseError):
        asyncio.run(service.delete_provider(cfg.id))


def test_update_active_refreshes_holder():
    service, _, holder = _service()
    cfg = asyncio.run(
        service.create_provider(
            ProviderCreateInput(name="A", base_url="http://x", model="m", api_key="k")
        )
    )
    asyncio.run(service.activate_provider(cfg.id))
    holder.swaps.clear()
    asyncio.run(service.update_provider(cfg.id, ProviderUpdateInput(model="m2")))
    assert holder.swaps and holder.swaps[-1][0] == cfg.id
