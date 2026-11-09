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


def test_holder_resolves_identity_from_provider_resolved_model():
    """评审修复（第三轮）：占位模型请求的账本身份必须与观测路径同源。

    Provider 在 chat 入口经 resolve_model 把占位模型解析为配置真实模型后才
    开始遥测（begin_chat_attempt 携带解析结果，身份按真实模型命中映射）；
    holder 的身份查找必须使用同一解析结果——映射按真实模型名配置时，账本
    不得因占位名查不到映射而 identity_unresolved（同一调用两种结论）。"""
    holder = ActiveProviderHolder(
        lambda cfg, key: FakeProvider(cfg.id, cfg.model),
        billing_identity_map={("p1", "ma"): ("openai", "ma")},
    )
    asyncio.run(holder.swap(_cfg("p1", "ma"), api_key="k"))
    provider = holder._provider
    result = asyncio.run(holder.chat([], [], False, "n-agent", {}))
    assert result.billing_identity == ("openai", "ma")
    # Provider 收到解析后的真实模型（与观测路径 begin_chat_attempt 同名）
    assert provider.calls[0][1] == "ma"


def test_holder_identity_for_real_override_model_unchanged():
    """真实覆盖模型名：解析为自身，身份按请求模型命中（既有语义不回归）。"""
    holder = ActiveProviderHolder(
        lambda cfg, key: FakeProvider(cfg.id, cfg.model),
        billing_identity_map={("p1", "override"): ("openai", "override")},
    )
    asyncio.run(holder.swap(_cfg("p1", "ma"), api_key="k"))
    result = asyncio.run(holder.chat([], [], False, "override", {}))
    assert result.billing_identity == ("openai", "override")


def test_chat_without_swap_raises():
    holder = ActiveProviderHolder(lambda cfg, key: FakeProvider(cfg.id, cfg.model))
    with pytest.raises(RuntimeError):
        asyncio.run(holder.chat([], [], False, "", {}))
