import asyncio
from datetime import datetime, timezone

import pytest

from app.application.runtime_provider import ActiveProviderHolder
from app.domain.provider import LLMResult, ModelInfo, ProviderConfig


class FakeProvider:
    def __init__(self, key, model):
        self.key = key
        self.model = model
        self.calls = []

    async def list_models(self):
        return [ModelInfo(self.model, self.model, "fake")]

    async def supports_tools(self, model):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls.append((self.key, model))
        return LLMResult(message={"role": "assistant", "content": self.key})


def _cfg(provider_id="p1", model="m1"):
    now = datetime.now(timezone.utc)
    return ProviderConfig(
        id=provider_id,
        name=f"n-{provider_id}",
        provider_type="openai-compatible",
        base_url="http://x",
        model=model,
        api_key_present=True,
        is_active=True,
        extra_headers=None,
        created_at=now,
        updated_at=now,
    )


def test_swap_changes_underlying_provider():
    created = []

    def factory(cfg, api_key):
        provider = FakeProvider(cfg.id, cfg.model)
        created.append(provider)
        return provider

    holder = ActiveProviderHolder(factory)
    asyncio.run(holder.swap(_cfg("a", "ma"), api_key="k1"))
    asyncio.run(holder.chat([], [], False, "", {}))
    asyncio.run(holder.swap(_cfg("b", "mb"), api_key="k2"))
    asyncio.run(holder.chat([], [], False, "", {}))
    assert created[0].calls and created[1].calls
    assert created[0].calls[0][1] == "ma"
    assert created[1].calls[0][1] == "mb"
    assert holder.current_model == "mb"


def test_chat_uses_explicit_model_when_provided():
    captured = {}

    def factory(cfg, _):
        provider = FakeProvider(cfg.id, cfg.model)
        captured["provider"] = provider
        return provider

    holder = ActiveProviderHolder(factory)
    asyncio.run(holder.swap(_cfg("a", "ma"), api_key="k"))
    asyncio.run(holder.chat([], [], False, "explicit", {}))
    assert captured["provider"].calls[0][1] == "explicit"


def test_chat_without_swap_raises():
    holder = ActiveProviderHolder(lambda cfg, key: FakeProvider(cfg.id, cfg.model))
    with pytest.raises(RuntimeError):
        asyncio.run(holder.chat([], [], False, "", {}))
