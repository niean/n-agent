# tests/application/test_attempt_cost_lifecycle.py
"""T3 S1: Application 尝试成本生命周期与组合根装配的失败测试。

覆盖计划 T3 S1 要点：
- 受信计价身份缺失 => identity_unresolved（不从 provider kind 或请求模型猜测）。
- OpenAI 缺 cached_tokens（prompt/completion 已知）=> missing_usage
  （判定顺序 missing_usage 在 pricing 判定之前）。
- 目录未配置 => pricing_unavailable。
- 目录正常 => estimated，与 T1 统一测试价格向量 B 一致
  （input=2、output=10、cache_read=0.2 USD/百万；prompt=150、cached=50、
  completion=20 => Ti=100、To=20、Tic=50 => 总额 0.00041）。
- 账本 unknown 兼容形态：UsageCost(amount_usd="0", status="unknown")。
- 非法 usage 后恢复：非法原始字段提交原样保留（存在但非法不等同于缺席），
  后续合法累计快照恢复可计价。
- Anthropic 条件目录条目（cache_ttl）在事实缺失时 => unsupported_pricing。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.usage_service import UsageService
from app.domain.cost import UnknownReason
from app.domain.usage import CanonicalUsage, ContextBreakdown, OverviewStats
from app.infrastructure.usage.pricing_catalog import (
    JsonPricingCatalog,
    jcs_canonicalize,
)

# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


class FakeRecorder:
    def __init__(self):
        self.calls = []

    async def record_call(self, session_id, model, provider, usage, cost, latency_ms, **kwargs):
        self.calls.append((session_id, model, provider, usage, cost, latency_ms))

    async def get_session_stats(self, session_id):
        from app.domain.usage import SessionUsageStats

        return SessionUsageStats(session_id=session_id)

    async def list_records(self, session_id, limit=50):
        return []

    async def record_compression(self, session_id, before_tokens, after_tokens):
        pass

    async def list_compressions(self, session_id):
        return []

    async def get_overview_stats(self):
        return OverviewStats()

    async def list_sessions_paginated(self, page, page_size):
        return ([], 0)


class FakeBreakdownCalculator:
    def compute(self, sp, tools, msgs, mem):
        return ContextBreakdown()


def _entry(**overrides) -> dict:
    entry = {
        "price_id": "p-openai-test",
        "billing_provider": "openai",
        "billing_model": "gpt-test",
        "operation": "chat",
        "aliases": [],
        "unit_prices": {"input": "2", "output": "10", "cache_read": "0.2", "cache_write": None},
        "source_url": "https://example.com/pricing",
        "verified_at": "2026-11-09",
    }
    entry.update(overrides)
    return entry


def _catalog_text(prices: list[dict], content_version: str = "test-v1") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "content_version": content_version,
            "currency": "USD",
            "prices": prices,
        },
        ensure_ascii=False,
    )


def _load_catalog(tmp_path: Path, prices: list[dict]) -> JsonPricingCatalog:
    text = _catalog_text(prices)
    path = tmp_path / "catalog.json"
    path.write_text(text, encoding="utf-8")
    canonical = jcs_canonicalize(json.loads(text))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return JsonPricingCatalog.load(str(path), expected)


def _service(catalog=None, identity=None) -> UsageService:
    """组装被测 UsageService；identity 为受信解析结果（None 表示无法解析）。"""
    from app.infrastructure.usage.pricing_table import CatalogPricingProvider

    pricing = CatalogPricingProvider(catalog) if catalog is not None else _NullPricing()
    return UsageService(
        FakeRecorder(),
        pricing,
        FakeBreakdownCalculator(),
        pricing_catalog=catalog,
        billing_identity_resolver=(lambda model, provider: identity),
    )


class _NullPricing:
    def get_pricing(self, model, provider):
        return None


# OpenAI 向量 B 原始 usage：prompt=150、cached=50、completion=20
_VECTOR_B_RAW = {
    "prompt_tokens": 150,
    "completion_tokens": 20,
    "prompt_tokens_details": {"cached_tokens": 50},
}
_VECTOR_IDENTITY = ("openai", "gpt-test")


# ---------------------------------------------------------------------------
# S1 用例
# ---------------------------------------------------------------------------


def test_identity_missing_is_identity_unresolved(tmp_path):
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=None)

    cost = svc.estimate_attempt_cost(_VECTOR_B_RAW, "openai", None)

    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.identity_unresolved
    assert cost.amount_usd is None
    assert cost.components == {}


def test_openai_missing_cached_tokens_is_missing_usage(tmp_path):
    """prompt/completion 已知而 cached 缺失：input 桶未知 => missing_usage，
    且该判定先于任何定价判定（known output 在场不改变结论）。"""
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)

    raw = {"prompt_tokens": 150, "completion_tokens": 20}
    cost = svc.estimate_attempt_cost(raw, "openai", _VECTOR_IDENTITY)

    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.missing_usage


def test_openai_missing_cached_no_cache_read_price_still_missing_usage(tmp_path):
    """锁定：prompt 合法而 cached 缺失、且目录条目不声明 cache_read 单价
    时，必须仍是 missing_usage——绝不允许把整个 prompt 按输入价计费
    （不依赖目录形状）。"""
    catalog = _load_catalog(
        tmp_path,
        [_entry(unit_prices={"input": "2", "output": "10"})],
    )
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)

    raw = {"prompt_tokens": 150, "completion_tokens": 20}
    cost = svc.estimate_attempt_cost(raw, "openai", _VECTOR_IDENTITY)

    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.missing_usage
    assert cost.amount_usd is None
    assert cost.components == {}


def test_openai_missing_cached_with_cache_read_price_still_missing_usage(tmp_path):
    """同一行为的对照锁定：目录条目声明 cache_read 单价时结论不变——
    缺 cached 仍为 missing_usage，行为不依赖目录形状。"""
    catalog = _load_catalog(
        tmp_path,
        [_entry(unit_prices={"input": "2", "output": "10", "cache_read": "0.2"})],
    )
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)

    raw = {"prompt_tokens": 150, "completion_tokens": 20}
    cost = svc.estimate_attempt_cost(raw, "openai", _VECTOR_IDENTITY)

    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.missing_usage


def test_catalog_unconfigured_is_pricing_unavailable(tmp_path):
    """目录未注入：身份齐全、用量齐全仍按 pricing_unavailable 处理。"""
    svc = _service(catalog=None, identity=_VECTOR_IDENTITY)

    cost = svc.estimate_attempt_cost(_VECTOR_B_RAW, "openai", _VECTOR_IDENTITY)

    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable


def test_catalog_resolved_estimated_matches_vector_b(tmp_path):
    """向量 B：Ti=100、To=20、Tic=50，价格 2/10/0.2 USD/百万 =>
    总额 Decimal("0.00041")，分项 0.0002/0.0002/0.00001。"""
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)

    cost = svc.estimate_attempt_cost(_VECTOR_B_RAW, "openai", _VECTOR_IDENTITY)

    assert cost.status == "estimated"
    assert cost.reason is None
    assert cost.amount_usd == Decimal("0.00041")
    assert Decimal(cost.components["input"]) == Decimal("0.0002")
    assert Decimal(cost.components["output"]) == Decimal("0.0002")
    assert Decimal(cost.components["cache_read"]) == Decimal("0.00001")
    assert "cache_write" not in cost.components  # 协议不适用桶无分项
    assert cost.price_id == "p-openai-test"
    assert cost.pricing_version
    assert cost.currency == "USD"


def test_ledger_unknown_keeps_legacy_placeholder_shape(tmp_path):
    """账本兼容：unknown 继续写旧 UsageCost(amount_usd="0", status="unknown")
    形态；旧 API 数字占位不携带任何新字段（观测侧不再消费该占位）。"""
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=None)

    usage = CanonicalUsage(input_tokens=100, output_tokens=20, raw_usage=_VECTOR_B_RAW)
    cost = svc.estimate_cost(usage, "gpt-test", "openai")

    assert cost.amount_usd == "0"
    assert cost.status == "unknown"
    assert cost.pricing_version is None
    assert cost.price_id is None
    assert cost.components is None


def test_ledger_estimated_carries_components_and_price_id(tmp_path):
    """账本 estimated 形态扩展：携带 price_id 与分项（十进制字符串）。"""
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)

    usage = CanonicalUsage(
        input_tokens=100, output_tokens=20, cache_read_tokens=50,
        raw_usage=_VECTOR_B_RAW,
    )
    cost = svc.estimate_cost(usage, "gpt-test", "openai")

    assert cost.status == "estimated"
    assert Decimal(cost.amount_usd) == Decimal("0.00041")
    assert Decimal(cost.components["input"]) == Decimal("0.0002")
    assert Decimal(cost.components["cache_read"]) == Decimal("0.00001")
    assert cost.price_id == "p-openai-test"
    assert cost.pricing_version


def test_record_call_uses_attempt_frozen_identity_not_reresolved(tmp_path):
    """账本路径必须使用随调用传入的尝试冻结身份（与观测路径同一计价结果）：
    中途 provider 切换后解析器返回新身份，账本不得重解析（同一调用使用
    同一计价结果）。未携带冻结身份的旧调用契约仍按解析器收口。"""
    catalog = _load_catalog(tmp_path, [
        _entry(),
        _entry(price_id="p-openai-alt", billing_model="gpt-expensive",
               unit_prices={"input": "20", "output": "10", "cache_read": "0.2", "cache_write": None}),
    ])
    recorder = FakeRecorder()
    from app.infrastructure.usage.pricing_table import CatalogPricingProvider

    svc = UsageService(
        recorder, CatalogPricingProvider(catalog), FakeBreakdownCalculator(),
        pricing_catalog=catalog,
        # 模拟"记录时重读 holder 当前配置"：mid-request 切换后返回新身份
        billing_identity_resolver=lambda model, provider: ("openai", "gpt-expensive"),
    )
    # 携带尝试冻结身份（prov-1 实例的向量 B 身份）
    asyncio.run(svc.record_call(
        "s1", "gpt-test", "openai", dict(_VECTOR_B_RAW), 5, provider_kind="openai",
        billing_identity=_VECTOR_IDENTITY))
    cost = recorder.calls[0][4]
    assert cost.status == "estimated"
    assert cost.pricing_version.startswith("test-v1")
    assert cost.price_id == "p-openai-test"
    assert Decimal(cost.amount_usd) == Decimal("0.00041")  # 冻结身份的计价
    # 对照：旧调用契约（未携带冻结身份）仍按解析器在记录时收口
    asyncio.run(svc.record_call(
        "s2", "gpt-test", "openai", dict(_VECTOR_B_RAW), 5, provider_kind="openai"))
    legacy = recorder.calls[1][4]
    assert legacy.status == "estimated"
    assert legacy.price_id == "p-openai-alt"
    assert Decimal(legacy.amount_usd) == Decimal("0.00221")


def test_invalid_usage_is_rejected_then_valid_snapshot_recovers(tmp_path):
    """非法 usage：字段存在但非法按 invalid_usage 拒绝计价；后续合法累计
    快照恢复可计价（替换语义，不残留非法状态）。"""
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)

    invalid_raw = {
        "prompt_tokens": "150",  # 存在但非法（字符串）
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 50},
    }
    rejected = svc.estimate_attempt_cost(invalid_raw, "openai", _VECTOR_IDENTITY)
    assert rejected.status == "unknown"
    assert rejected.reason is UnknownReason.invalid_usage

    recovered = svc.estimate_attempt_cost(_VECTOR_B_RAW, "openai", _VECTOR_IDENTITY)
    assert recovered.status == "estimated"
    assert recovered.amount_usd == Decimal("0.00041")


def test_anthropic_conditioned_entry_without_ttl_fact_is_unsupported(tmp_path):
    """Anthropic 混合 TTL（条件事实缺失）=> unsupported_pricing，不得误判
    为零匹配或以其他条件价成交。"""
    catalog = _load_catalog(
        tmp_path,
        [
            _entry(
                price_id="p-anthropic-ttl",
                billing_provider="anthropic",
                billing_model="claude-test",
                unit_prices={
                    "input": "3", "output": "15",
                    "cache_read": "0.3", "cache_write": "3.75",
                },
                conditions={"cache_ttl": "1h"},
            )
        ],
    )
    svc = _service(catalog=catalog, identity=("anthropic", "claude-test"))

    raw = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
    }
    cost = svc.estimate_attempt_cost(raw, "anthropic", ("anthropic", "claude-test"))

    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.unsupported_pricing


def test_ledger_and_observability_share_ttl_fact_extraction(tmp_path):
    """账本与观测必须复用同一 TTL 事实抽取与计价入口：单一 TTL 的 Anthropic
    用量在账本路径（estimate_attempt_cost）与观测路径（calculate_attempt_cost
    + 共享事实抽取）下产出相同 status 与金额，不得账本 unknown 而观测
    estimated。"""
    from app.application.usage_service import cache_ttl_facts

    catalog = _load_catalog(
        tmp_path,
        [
            _entry(
                price_id="p-anthropic-ttl",
                billing_provider="anthropic",
                billing_model="claude-test",
                unit_prices={
                    "input": "3", "output": "15",
                    "cache_read": "0.3", "cache_write": "3.75",
                },
                conditions={"cache_ttl": "1h"},
            )
        ],
    )
    svc = _service(catalog=catalog, identity=("anthropic", "claude-test"))

    raw = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
        "cache_creation": {"ephemeral_1h_input_tokens": 10},
    }
    identity = ("anthropic", "claude-test")

    ledger_cost = svc.estimate_attempt_cost(raw, "anthropic", identity)
    observability_cost = svc.calculate_attempt_cost(
        svc.attempt_usage_from_raw(raw, "anthropic"), identity, cache_ttl_facts(raw),
    )

    assert ledger_cost.status == "estimated"
    assert ledger_cost.status == observability_cost.status
    # 100*3 + 20*15 + 50*0.3 + 10*3.75（USD/百万）= 0.0006525
    assert ledger_cost.amount_usd == observability_cost.amount_usd == Decimal("0.0006525")


# ---------------------------------------------------------------------------
# 评审修复（第四轮 D1）：混合合法 TTL 不得命中无条件价格项
# ---------------------------------------------------------------------------


def _anthropic_flat_entry(**overrides) -> dict:
    """Anthropic 无条件价格项（无 cache_ttl 条件）：input=3、output=15、
    cache_read=0.3、cache_write=3.75 USD/百万。"""
    return _entry(
        price_id="p-anthropic-flat",
        billing_provider="anthropic",
        billing_model="claude-test",
        unit_prices={"input": "3", "output": "15",
                     "cache_read": "0.3", "cache_write": "3.75"},
        **overrides,
    )


def _anthropic_ttl_entry(**overrides) -> dict:
    """Anthropic 条件价格项（cache_ttl=1h），单价同上。"""
    return _entry(
        price_id="p-anthropic-ttl",
        billing_provider="anthropic",
        billing_model="claude-test",
        unit_prices={"input": "3", "output": "15",
                     "cache_read": "0.3", "cache_write": "3.75"},
        conditions={"cache_ttl": "1h"},
        **overrides,
    )


_MIXED_TTL_RAW = {
    "input_tokens": 100, "output_tokens": 20,
    "cache_read_input_tokens": 50, "cache_creation_input_tokens": 30,
    "cache_creation": {"ephemeral_1h_input_tokens": 10,
                       "ephemeral_5m_input_tokens": 20},
}
_ANTHROPIC_IDENTITY = ("anthropic", "claude-test")


def test_mixed_ttl_with_unconditional_entry_is_unsupported(tmp_path):
    """混合合法 TTL（1h=10、5m=20 均合法）+ 无条件价格项 => unsupported_pricing，
    不得唯一命中无条件条目把全部 cache_write 按单一单价 estimated
    （spec：本次混合 TTL 记 unsupported_pricing，不得套用单一 TTL 单价）。"""
    catalog = _load_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    cost = svc.estimate_attempt_cost(_MIXED_TTL_RAW, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.unsupported_pricing


def test_single_ttl_with_conditional_entry_estimated_regression(tmp_path):
    """回归：单一合法 TTL + 条件价格项 => estimated（语义不变）。"""
    catalog = _load_catalog(tmp_path, [_anthropic_ttl_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = dict(_MIXED_TTL_RAW, cache_creation={"ephemeral_1h_input_tokens": 10},
               cache_creation_input_tokens=10)
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "estimated"
    assert cost.price_id == "p-anthropic-ttl"


def test_no_ttl_with_unconditional_entry_still_estimated(tmp_path):
    """回归：无 TTL 事实 + 无条件价格项 => estimated（语义不变）。
    100*3 + 20*15 + 50*0.3 + 30*3.75（USD/百万）= 0.0007275。"""
    catalog = _load_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = {k: v for k, v in _MIXED_TTL_RAW.items() if k != "cache_creation"}
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "estimated"
    assert cost.amount_usd == Decimal("0.0007275")


# ---------------------------------------------------------------------------
# 评审修复（第四轮 D2）：嵌套 {"usage": {...}} 形态解包一次后统一判定
# ---------------------------------------------------------------------------


def _nested(raw_usage: dict) -> dict:
    return {"usage": raw_usage}


def test_nested_usage_invalid_ttl_is_invalid_usage(tmp_path):
    """嵌套形态内的非法 TTL 子字段（-1）不得被忽略：解包后 TTL 校验与四桶
    解析在同一视图上，cache_write 桶携带非法值收口 invalid_usage（而非按
    无条件条目 estimated）。"""
    catalog = _load_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = _nested({
        "input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10,
        "cache_creation": {"ephemeral_1h_input_tokens": -1},
    })
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.invalid_usage


def test_nested_usage_single_ttl_hits_conditional_entry(tmp_path):
    """嵌套形态内单一合法 TTL 必须命中条件价格项（不得因嵌套丢失 TTL 事实
    而退化为 unsupported_pricing）。"""
    catalog = _load_catalog(tmp_path, [_anthropic_ttl_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = _nested({
        "input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10,
        "cache_creation": {"ephemeral_1h_input_tokens": 10},
    })
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "estimated"
    assert cost.price_id == "p-anthropic-ttl"


def test_nested_usage_mixed_ttl_is_unsupported(tmp_path):
    """嵌套形态内混合合法 TTL 与平铺形态同判 unsupported_pricing（与 D1 成对，
    无条件条目同样不可达）。"""
    catalog = _load_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = _nested({
        "input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 50, "cache_creation_input_tokens": 30,
        "cache_creation": {"ephemeral_1h_input_tokens": 10,
                           "ephemeral_5m_input_tokens": 20},
    })
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.unsupported_pricing


def test_openai_cached_exceeding_prompt_is_invalid_usage(tmp_path):
    """端到端锁定：OpenAI cached_tokens > prompt_tokens => input 差值为负，
    按 invalid_usage 拒绝计价（即使 input/output 其余桶已知且目录可用）。"""
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)

    raw = {
        "prompt_tokens": 150,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 200},
    }
    cost = svc.estimate_attempt_cost(raw, "openai", _VECTOR_IDENTITY)

    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.invalid_usage


# ---------------------------------------------------------------------------
# Provider raw usage 通道：字段存在但非法原样提交，后续合法累计快照恢复
# ---------------------------------------------------------------------------


class _RecordingAttempt:
    """记录 raw usage 通道提交的 fake hook（能力检测协议的全方法实现）。"""

    def __init__(self):
        self.raw_submissions = []

    def set_model_parameters(self, parameters):
        pass

    def set_raw_usage(self, raw_usage):
        self.raw_submissions.append(raw_usage)

    def set_input(self, messages, tools=None, system=None):
        pass

    def set_output(self, message, incomplete=False):
        pass

    def append_output_text(self, text):
        pass

    def append_output_tool_delta(self, index, *, id=None, name=None, arguments=None):
        pass

    def set_error_detail(self, text):
        pass

    def mark_output_incomplete(self):
        pass

    def set_usage(self, input_tokens, output_tokens):
        pass

    def set_cache_usage(self, *, cache_read_tokens=None, cache_write_tokens=None):
        pass

    def set_finish_reasons(self, reasons):
        pass

    def set_response_model(self, model):
        pass

    def set_error(self, error_type):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _chunk(delta_content=None, finish_reason=None, usage=None, with_choice=True):
    choice = (
        SimpleNamespace(
            delta=SimpleNamespace(content=delta_content, tool_calls=None),
            finish_reason=finish_reason,
        )
        if with_choice
        else None
    )
    return SimpleNamespace(choices=[choice] if with_choice else [], usage=usage)


def test_openai_stream_invalid_field_submitted_then_valid_snapshot_recovers(tmp_path):
    """流式 raw usage 通道：字段存在但非法原样提交（不预丢弃、不与缺席混
    淆）；后续合法累计快照恢复，最终视图可计价。"""
    from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider

    attempt = _RecordingAttempt()
    provider = OpenAICompatibleProvider(
        "http://localhost:1", "k", "gpt-test",
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: _async_stream(_FakeStream([
                        _chunk(delta_content="he", usage=SimpleNamespace(
                            prompt_tokens="x", completion_tokens=20,
                        )),
                        _chunk(with_choice=False, usage=SimpleNamespace(
                            prompt_tokens=150, completion_tokens=20,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=50),
                        )),
                    ]))
                )
            )
        ),
        telemetry_callback=lambda model, stream, provider: attempt,
    )

    async def _consume():
        async for _event in await provider.chat([], [], True, "", {}):
            pass

    import asyncio

    asyncio.run(_consume())

    # 第一次提交：字段存在但非法原样保留（不预丢弃）
    assert attempt.raw_submissions[0]["prompt_tokens"] == "x"
    # 最终合法累计快照完整
    final = attempt.raw_submissions[-1]
    assert final == {
        "prompt_tokens": 150,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 50},
    }

    # 生命周期收口：中间非法视图拒绝计价，最终合法快照恢复
    catalog = _load_catalog(tmp_path, [_entry()])
    svc = _service(catalog=catalog, identity=_VECTOR_IDENTITY)
    rejected = svc.estimate_attempt_cost(attempt.raw_submissions[0], "openai", _VECTOR_IDENTITY)
    assert rejected.status == "unknown"
    assert rejected.reason is UnknownReason.invalid_usage
    recovered = svc.estimate_attempt_cost(final, "openai", _VECTOR_IDENTITY)
    assert recovered.status == "estimated"
    assert recovered.amount_usd == Decimal("0.00041")


def test_anthropic_stream_invalid_field_submitted_then_valid_snapshot_recovers(tmp_path):
    """Anthropic 流式 raw usage 通道与 OpenAI 同语义：字段存在但非法原样
    提交（不预丢弃、不与缺席混淆）；后续合法累计快照恢复，最终视图可计价。"""
    from app.infrastructure.llm.anthropic_provider import AnthropicProvider

    attempt = _RecordingAttempt()
    provider = AnthropicProvider(
        "http://localhost:1", "k", "claude-test",
        client=SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **kwargs: _async_stream(_FakeStream([
                    # message_start：input 合法，output 存在但非法
                    SimpleNamespace(
                        type="message_start",
                        message=SimpleNamespace(
                            model="claude-test",
                            usage=SimpleNamespace(
                                input_tokens=100, output_tokens="x",
                                cache_read_input_tokens=50,
                                cache_creation_input_tokens=10,
                            ),
                        ),
                    ),
                    # message_delta：仅携带累计 output_tokens（合法）
                    SimpleNamespace(
                        type="message_delta",
                        delta=SimpleNamespace(stop_reason="end_turn"),
                        usage=SimpleNamespace(output_tokens=25),
                    ),
                ]))
            )
        ),
        telemetry_callback=lambda model, stream, provider: attempt,
    )

    async def _consume():
        async for _event in await provider.chat([], [], True, "", {}):
            pass

    import asyncio

    asyncio.run(_consume())

    # 第一次提交：output_tokens 存在但非法原样保留（不预丢弃），合法桶齐全
    first = attempt.raw_submissions[0]
    assert first["output_tokens"] == "x"
    assert first["input_tokens"] == 100
    assert first["cache_read_input_tokens"] == 50
    assert first["cache_creation_input_tokens"] == 10

    # 生命周期收口：中间非法视图拒绝计价
    catalog = _load_catalog(
        tmp_path,
        [
            _entry(
                price_id="p-anthropic-test",
                billing_provider="anthropic",
                billing_model="claude-test",
                unit_prices={
                    "input": "3", "output": "15",
                    "cache_read": "0.3", "cache_write": "3.75",
                },
            )
        ],
    )
    svc = _service(catalog=catalog, identity=("anthropic", "claude-test"))
    rejected = svc.estimate_attempt_cost(
        first, "anthropic", ("anthropic", "claude-test")
    )
    assert rejected.status == "unknown"
    assert rejected.reason is UnknownReason.invalid_usage

    # 按 ChatAttempt 无擦除语义合并各次提交的合法字段：非法值不残留，
    # 后续合法累计快照恢复 => estimated（100*3 + 25*15 + 50*0.3 + 10*3.75
    # = 0.0003 + 0.000375 + 0.000015 + 0.0000375 = 0.0007275 USD）
    merged = {}
    for submission in attempt.raw_submissions:
        for key, value in submission.items():
            if type(value) is int and value >= 0:
                merged[key] = value
    recovered = svc.estimate_attempt_cost(
        merged, "anthropic", ("anthropic", "claude-test")
    )
    assert recovered.status == "estimated"
    assert recovered.amount_usd == Decimal("0.0007275")
    assert Decimal(recovered.components["input"]) == Decimal("0.0003")
    assert Decimal(recovered.components["output"]) == Decimal("0.000375")
    assert Decimal(recovered.components["cache_read"]) == Decimal("0.000015")
    assert Decimal(recovered.components["cache_write"]) == Decimal("0.0000375")


# ---------------------------------------------------------------------------
# 生命周期锁定：无实际请求的拒绝不进入 ChatAttempt、不产生成本
# ---------------------------------------------------------------------------


class _FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.status = None
        self.ended = 0

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status

    def end(self):
        self.ended += 1


class _FakeTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, kind=None, **_kwargs):
        span = _FakeSpan(name)
        self.spans.append(span)
        return span


class _FakeMeter:
    def create_counter(self, name, unit=None, **_kwargs):
        return SimpleNamespace(add=lambda value, attrs=None: None)

    def create_histogram(self, name, unit=None, **_kwargs):
        return SimpleNamespace(record=lambda value, attrs=None: None)


def test_request_denied_before_provider_call_produces_no_attempt_or_cost(
    tmp_path, monkeypatch,
):
    """锁定：请求在真正到达 provider 之前被拒绝（此处以 LLM 策略拒绝为
    例，InformationFlow 拒绝与预算预留拒绝在同一 seam 之前/同层）时：
    不发生 SDK 调用、不开启任何 ChatAttempt span、成本估算器零触发、
    不产生任何成本相关输出。"""
    import asyncio

    from app.application import telemetry_semantics as ts
    from app.application.agent_graph import AgentGraphRunner
    from app.application.tool_service import ToolService, builtin_tool_definitions
    from app.domain.agent import AgentState
    from app.domain.llm_policy import ModelSelection
    from app.domain.policy import PolicyOutcome
    from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
    from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
    from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
    from app.domain.session import ConversationSession
    from app.infrastructure.tools.builtin import build_builtin_tool_executor

    class _DenyingPolicy:
        def evaluate(self, *args, **kwargs):
            return ModelSelection(
                provider_id="default", model_id="", verdict=PolicyOutcome.DENY,
                reason="capability_deny_test",
            )

    class _CountingCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            raise AssertionError("provider SDK must not be called on denial")

    estimator_invocations = []

    def _estimator(raw_usage, model, provider, buckets):
        estimator_invocations.append((raw_usage, model, provider, buckets))
        return {"amount_usd": 1.0, "status": "estimated", "pricing_version": "v"}

    tracer = _FakeTracer()
    monkeypatch.setattr(ts, "get_tracer", lambda: tracer)
    monkeypatch.setattr(ts, "get_meter", lambda: _FakeMeter())
    telemetry = ts.SemanticsTelemetry()
    telemetry.set_cost_estimator(_estimator)

    completions = _CountingCompletions()
    provider = OpenAICompatibleProvider(
        "http://localhost:1", "k", "gpt-test",
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        telemetry_callback=telemetry.begin_chat_attempt,
    )
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    asyncio.run(store.create_session(ConversationSession(id="sess-deny")))
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(
            build_builtin_tool_executor(tmp_path), builtin_tool_definitions()
        ),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        llm_policy=_DenyingPolicy(),
        telemetry=telemetry,
    )
    state = AgentState(
        session_id="sess-deny",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )

    new_state = asyncio.run(runner.call_llm(state))

    # 拒绝按既有语义落为错误态，业务流未触达 provider
    assert new_state.error == "capability_deny_test"
    assert completions.calls == 0
    # 无任何 ChatAttempt：无 chat span、成本估算器零触发
    assert tracer.spans == []
    assert estimator_invocations == []


def _async_stream(stream):
    async def _create(**kwargs):
        return stream

    return _create()


# ---------------------------------------------------------------------------
# 评审修复（第五轮 D1）：混合 TTL 不得掩盖目录失效——pricing_unavailable
# 先于 unsupported_pricing（spec 固定判定顺序第 4 位先于第 7 位）
# ---------------------------------------------------------------------------


def _load_broken_catalog(tmp_path: Path, prices: list[dict]) -> JsonPricingCatalog:
    """构造失效目录（预期哈希不匹配，available=False）：模拟哈希不匹配、
    文件损坏等目录失效形态。失效目录仍是非空对象，resolve 一律返回
    unavailable（pricing_unavailable）。"""
    text = _catalog_text(prices)
    path = tmp_path / "catalog-broken.json"
    path.write_text(text, encoding="utf-8")
    return JsonPricingCatalog.load(str(path), "0" * 64)


def test_mixed_ttl_with_unavailable_catalog_is_pricing_unavailable(tmp_path):
    """目录失效 + 混合合法 TTL => pricing_unavailable（两因并存取顺序靠前者），
    不得以 unsupported_pricing 掩盖目录失效（判定顺序 pricing_unavailable
    第 4 位先于 unsupported_pricing 第 7 位）。"""
    catalog = _load_broken_catalog(tmp_path, [_anthropic_flat_entry()])
    assert catalog.available is False
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    cost = svc.estimate_attempt_cost(_MIXED_TTL_RAW, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable
    assert cost.pricing_version is None


def test_single_ttl_with_unavailable_catalog_is_pricing_unavailable(tmp_path):
    """回归：目录失效 + 单一合法 TTL（覆盖总量）=> pricing_unavailable。"""
    catalog = _load_broken_catalog(tmp_path, [_anthropic_ttl_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = dict(_MIXED_TTL_RAW, cache_creation={"ephemeral_1h_input_tokens": 10},
               cache_creation_input_tokens=10)
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable


def test_no_ttl_with_unavailable_catalog_is_pricing_unavailable(tmp_path):
    """回归：目录失效 + 无 TTL 事实 => pricing_unavailable。"""
    catalog = _load_broken_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = {k: v for k, v in _MIXED_TTL_RAW.items() if k != "cache_creation"}
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable


# ---------------------------------------------------------------------------
# 评审修复（第五轮 D2）：单一 TTL 拆分必须覆盖 cache_creation 总量——
# "全部用量属于同一已计价条件"未被证明时不得套用该 TTL 单价（spec L98）
# ---------------------------------------------------------------------------


def test_single_ttl_not_covering_total_is_unsupported(tmp_path):
    """单一非零合法 TTL 子字段之和（10）不等于 cache_creation_input_tokens
    总量（30）：剩余 20 个 token 的计价条件未被证明，不得把全部 cache_write
    按 1h 单价 estimated => unsupported_pricing。"""
    catalog = _load_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = dict(_MIXED_TTL_RAW, cache_creation={"ephemeral_1h_input_tokens": 10})
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.unsupported_pricing


def test_single_ttl_not_covering_total_with_unavailable_catalog_is_pricing_unavailable(tmp_path):
    """判定顺序矩阵：拆分未覆盖总量（unsupported 因）+ 目录失效
    （unavailable 因）并存 => pricing_unavailable（第 4 位先于第 7 位）。"""
    catalog = _load_broken_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = dict(_MIXED_TTL_RAW, cache_creation={"ephemeral_1h_input_tokens": 10})
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable


def test_single_ttl_covering_total_with_flat_entry_estimated_regression(tmp_path):
    """回归：单一非零合法 TTL 子字段等于总量（1h=10、总量=10）=> 条件被证明，
    无条件条目可命中 estimated（语义不变）。"""
    catalog = _load_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = dict(_MIXED_TTL_RAW, cache_creation={"ephemeral_1h_input_tokens": 10},
               cache_creation_input_tokens=10)
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "estimated"
    assert cost.amount_usd == Decimal("0.0006525")


def test_zero_cache_write_with_zero_split_is_estimated_zero_regression(tmp_path):
    """零 Token 边缘回归：总量=0 且拆分子字段全零 => 无条件需要证明（既有零
    语义保持），estimated 且 cache_write 分项为 0。"""
    catalog = _load_catalog(tmp_path, [_anthropic_flat_entry()])
    svc = _service(catalog=catalog, identity=_ANTHROPIC_IDENTITY)
    raw = {
        "input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": 0,
                           "ephemeral_5m_input_tokens": 0},
    }
    cost = svc.estimate_attempt_cost(raw, "anthropic", _ANTHROPIC_IDENTITY)
    assert cost.status == "estimated"
    assert cost.components["cache_write"] == Decimal("0")
    assert cost.amount_usd == Decimal("0.000615")
