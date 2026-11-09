# tests/application/test_usage_service.py
import pytest
from app.application.usage_service import UsageService
from app.domain.cost import CatalogResolution, PricingCatalogEntry
from app.domain.usage import (
    CanonicalUsage, UsageCost, PricingEntry, ContextBreakdown,
    OverviewStats, SessionUsageSummary,
)


class FakeRecorder:
    def __init__(self):
        self.calls = []
        self.compressions = []
    async def record_call(self, session_id, model, provider, usage, cost, latency_ms, requested_model=None, trigger_type=None, request_messages=None, response_message=None, tools=None, generation_params=None):
        self.calls.append((session_id, model, provider, usage, cost, latency_ms, requested_model, trigger_type, request_messages, response_message, tools, generation_params))
    async def get_session_stats(self, session_id):
        from app.domain.usage import SessionUsageStats
        return SessionUsageStats(session_id=session_id)
    async def list_records(self, session_id, limit=50): return []
    async def record_compression(self, session_id, before_tokens, after_tokens):
        self.compressions.append((session_id, before_tokens, after_tokens))
    async def list_compressions(self, session_id): return []
    async def get_overview_stats(self):
        return OverviewStats(
            input_tokens=300, output_tokens=150, total_tokens=450,
            api_call_count=2, session_count=2, estimated_cost_usd="0.01",
        )
    async def list_sessions_paginated(self, page, page_size):
        return (
            [SessionUsageSummary(session_id="s1", title="t", created_at="")],
            1,
        )


class FakePricing:
    """PricingProvider 兼容入口的 fake（新计价路径不再消费，仅验证旧构造兼容）。"""

    def get_pricing(self, model, provider):
        if "gpt-4o" in model:
            return PricingEntry(
                model_pattern="gpt-4o", provider="openai",
                input_cost_per_million="5", output_cost_per_million="15",
                cache_read_cost_per_million="2.5", cache_write_cost_per_million="6.25",
                pricing_version="2026-07", source_url="test",
            )
        return None


class FakeCatalog:
    """价格目录解析协议的 fake：与 JsonPricingCatalog 同语义的内存精确匹配。"""

    def __init__(self, entries, pricing_version="test-v1@abc"):
        self._entries = entries
        self._pricing_version = pricing_version

    def resolve(self, identity, operation, facts=None):
        provider, model = identity
        matched = [
            entry for entry in self._entries
            if entry.billing_provider == provider
            and entry.operation == operation
            and (entry.billing_model == model or model in entry.aliases)
        ]
        if len(matched) > 1:
            return CatalogResolution.ambiguous(
                tuple(sorted(e.price_id for e in matched)), self._pricing_version
            )
        if not matched:
            return CatalogResolution.not_found(self._pricing_version)
        return CatalogResolution.unique(matched[0], self._pricing_version)


def _gpt4o_catalog_entry():
    return PricingCatalogEntry(
        price_id="p-gpt4o-test",
        billing_provider="openai",
        billing_model="gpt-4o",
        operation="chat",
        unit_prices={"input": "5", "output": "15", "cache_read": "2.5"},
    )


class FakeBreakdownCalculator:
    def compute(self, sp, tools, msgs, mem):
        return ContextBreakdown(system_prompt=100, tool_definitions=200, memory=50, conversation=500)


@pytest.mark.asyncio
async def test_normalize_usage_openai():
    svc = UsageService(FakeRecorder(), FakePricing(), FakeBreakdownCalculator())
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    u = svc.normalize_usage(raw, "openai")
    assert u.input_tokens == 100
    assert u.output_tokens == 50
    assert u.total_tokens == 150


@pytest.mark.asyncio
async def test_estimate_cost_known_model():
    """新计价路径：Domain 纯计算器 + 目录；knownness 保留自 raw_usage
    （不从 CanonicalUsage 默认 0 推断已知）。"""
    svc = UsageService(
        FakeRecorder(), FakePricing(), FakeBreakdownCalculator(),
        pricing_catalog=FakeCatalog([_gpt4o_catalog_entry()]),
        billing_identity_resolver=lambda model, provider: ("openai", model),
    )
    # raw_usage 与 CanonicalUsage 数值一致（prompt=1000000、cached=0、completion=500000）
    raw = {
        "prompt_tokens": 1000000, "completion_tokens": 500000,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    u = CanonicalUsage(input_tokens=1000000, output_tokens=500000, raw_usage=raw)
    cost = svc.estimate_cost(u, "gpt-4o", "openai")
    assert cost.status == "estimated"
    assert float(cost.amount_usd) == pytest.approx(12.5, rel=0.01)
    assert cost.price_id == "p-gpt4o-test"
    assert cost.pricing_version


@pytest.mark.asyncio
async def test_estimate_cost_unknown_model():
    """受信计价身份缺失：unknown 且保持旧账本数字占位形态。"""
    svc = UsageService(FakeRecorder(), FakePricing(), FakeBreakdownCalculator())
    raw = {"prompt_tokens": 100, "completion_tokens": 50}
    u = CanonicalUsage(input_tokens=100, output_tokens=50, raw_usage=raw)
    cost = svc.estimate_cost(u, "unknown-model", "unknown")
    assert cost.status == "unknown"
    assert float(cost.amount_usd) == 0
    assert cost.price_id is None
    assert cost.components is None


@pytest.mark.asyncio
async def test_estimate_cost_raw_usage_missing_is_unknown():
    """raw_usage 不足（缺席）=> unknown，不从 CanonicalUsage 默认 0 推断已知。"""
    svc = UsageService(
        FakeRecorder(), FakePricing(), FakeBreakdownCalculator(),
        pricing_catalog=FakeCatalog([_gpt4o_catalog_entry()]),
        billing_identity_resolver=lambda model, provider: ("openai", model),
    )
    u = CanonicalUsage(input_tokens=100, output_tokens=50)
    cost = svc.estimate_cost(u, "gpt-4o", "openai")
    assert cost.status == "unknown"
    assert float(cost.amount_usd) == 0


@pytest.mark.asyncio
async def test_record_call_persists():
    recorder = FakeRecorder()
    svc = UsageService(
        recorder, FakePricing(), FakeBreakdownCalculator(),
        pricing_catalog=FakeCatalog([_gpt4o_catalog_entry()]),
        billing_identity_resolver=lambda model, provider: ("openai", model),
    )
    raw = {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    await svc.record_call("sess-1", "gpt-4o", "openai", raw, latency_ms=200, provider_kind="openai")
    assert len(recorder.calls) == 1
    _, model, _, usage, cost, latency, *_ = recorder.calls[0]
    assert model == "gpt-4o"
    assert usage.input_tokens == 100
    assert cost.status == "estimated"
    assert latency == 200


@pytest.mark.asyncio
async def test_record_call_passes_requested_model():
    recorder = FakeRecorder()
    svc = UsageService(recorder, FakePricing(), FakeBreakdownCalculator())
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    await svc.record_call(
        "sess-1", "gpt-4o", "openai", raw, latency_ms=200,
        provider_kind="openai", requested_model="N-Agent",
    )
    assert len(recorder.calls) == 1
    _, _, _, _, _, _, requested, _, _, _, _, _ = recorder.calls[0]
    assert requested == "N-Agent"


@pytest.mark.asyncio
async def test_record_call_passes_trigger_type():
    recorder = FakeRecorder()
    svc = UsageService(recorder, FakePricing(), FakeBreakdownCalculator())
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    await svc.record_call(
        "sess-1", "gpt-4o", "openai", raw, latency_ms=200,
        provider_kind="openai", trigger_type="tool",
    )
    assert len(recorder.calls) == 1
    _, _, _, _, _, _, _, trigger, _, _, _, _ = recorder.calls[0]
    assert trigger == "tool"


@pytest.mark.asyncio
async def test_record_call_passes_request_response_json():
    recorder = FakeRecorder()
    svc = UsageService(recorder, FakePricing(), FakeBreakdownCalculator())
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    req_json = '[{"role":"user","content":"hi"}]'
    resp_json = '{"role":"assistant","content":"hello"}'
    await svc.record_call(
        "sess-1", "gpt-4o", "openai", raw, latency_ms=200,
        provider_kind="openai",
        request_messages=req_json, response_message=resp_json,
    )
    assert len(recorder.calls) == 1
    _, _, _, _, _, _, _, _, req, resp, _, _ = recorder.calls[0]
    assert req == req_json
    assert resp == resp_json


@pytest.mark.asyncio
async def test_record_call_passes_tools():
    recorder = FakeRecorder()
    svc = UsageService(recorder, FakePricing(), FakeBreakdownCalculator())
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    tools_json = '[{"type":"function","function":{"name":"get_time","parameters":{"type":"object","properties":{}}}}]'
    await svc.record_call(
        "sess-1", "gpt-4o", "openai", raw, latency_ms=200,
        provider_kind="openai", tools=tools_json,
    )
    assert len(recorder.calls) == 1
    _, _, _, _, _, _, _, _, _, _, tools, _ = recorder.calls[0]
    assert tools == tools_json


@pytest.mark.asyncio
async def test_record_call_passes_generation_params():
    recorder = FakeRecorder()
    svc = UsageService(recorder, FakePricing(), FakeBreakdownCalculator())
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    gen_json = '{"temperature":0.7,"max_tokens":4096}'
    await svc.record_call(
        "sess-1", "gpt-4o", "openai", raw, latency_ms=200,
        provider_kind="openai", generation_params=gen_json,
    )
    assert len(recorder.calls) == 1
    _, _, _, _, _, _, _, _, _, _, _, gen = recorder.calls[0]
    assert gen == gen_json


@pytest.mark.asyncio
async def test_record_call_skips_empty_usage():
    recorder = FakeRecorder()
    svc = UsageService(recorder, FakePricing(), FakeBreakdownCalculator())
    await svc.record_call("sess-1", "gpt-4o", "openai", {}, latency_ms=200, provider_kind="openai")
    assert len(recorder.calls) == 0  # empty usage -> skip


@pytest.mark.asyncio
async def test_get_overview_stats_delegates():
    svc = UsageService(FakeRecorder(), FakePricing(), FakeBreakdownCalculator())
    stats = await svc.get_overview_stats()
    assert stats.session_count == 2
    assert stats.input_tokens == 300
    assert stats.api_call_count == 2
    assert float(stats.estimated_cost_usd) == pytest.approx(0.01, rel=0.001)


@pytest.mark.asyncio
async def test_list_sessions_paginated_delegates():
    svc = UsageService(FakeRecorder(), FakePricing(), FakeBreakdownCalculator())
    summaries, total = await svc.list_sessions_paginated(page=1, page_size=20)
    assert total == 1
    assert summaries[0].session_id == "s1"
