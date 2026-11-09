# tests/infrastructure/test_pricing_table.py
"""CatalogPricingProvider 测试：子串匹配实现已退役，PricingProvider 兼容
入口由版本化价格目录的唯一命中条目驱动。"""
import hashlib
import json
from pathlib import Path

from app.infrastructure.usage.pricing_catalog import (
    JsonPricingCatalog,
    jcs_canonicalize,
)
from app.infrastructure.usage.pricing_table import CatalogPricingProvider


def _load_catalog(tmp_path: Path, prices: list[dict]) -> JsonPricingCatalog:
    text = json.dumps(
        {
            "schema_version": 1,
            "content_version": "test-v1",
            "currency": "USD",
            "prices": prices,
        },
        ensure_ascii=False,
    )
    path = tmp_path / "catalog.json"
    path.write_text(text, encoding="utf-8")
    canonical = jcs_canonicalize(json.loads(text))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return JsonPricingCatalog.load(str(path), expected)


def _entry(**overrides) -> dict:
    entry = {
        "price_id": "p1",
        "billing_provider": "openai",
        "billing_model": "gpt-4o",
        "operation": "chat",
        "aliases": [],
        "unit_prices": {"input": "2.50", "output": "10.00", "cache_read": "1.25"},
        "source_url": "https://example.com/pricing",
        "verified_at": "2026-11-09",
    }
    entry.update(overrides)
    return entry


def test_get_pricing_unique_entry(tmp_path):
    catalog = _load_catalog(tmp_path, [_entry()])
    p = CatalogPricingProvider(catalog)
    entry = p.get_pricing("gpt-4o", "openai")
    assert entry is not None
    assert entry.input_cost_per_million == "2.50"
    assert entry.output_cost_per_million == "10.00"
    assert entry.cache_read_cost_per_million == "1.25"
    # 目录未声明 cache_write 单价：兼容形状按 "0" 投影
    assert entry.cache_write_cost_per_million == "0"
    assert entry.pricing_version
    assert entry.model_pattern == "gpt-4o"
    assert entry.provider == "openai"


def test_get_pricing_requires_exact_match_not_substring(tmp_path):
    """精确匹配语义：请求模型是目录模型的超集（带日期后缀）不命中，
    子串匹配已退役。"""
    catalog = _load_catalog(tmp_path, [_entry()])
    p = CatalogPricingProvider(catalog)
    assert p.get_pricing("gpt-4o-2024-08-06", "openai") is None


def test_get_pricing_alias_match(tmp_path):
    catalog = _load_catalog(
        tmp_path, [_entry(aliases=["claude-3-5-sonnet"])],
    )
    p = CatalogPricingProvider(catalog)
    entry = p.get_pricing("claude-3-5-sonnet", "anthropic")
    assert entry is None  # provider 不匹配不命中
    entry = p.get_pricing("claude-3-5-sonnet", "openai")
    assert entry is not None
    assert entry.input_cost_per_million == "2.50"


def test_get_pricing_unknown_returns_none(tmp_path):
    catalog = _load_catalog(tmp_path, [_entry()])
    p = CatalogPricingProvider(catalog)
    assert p.get_pricing("unknown-model", "unknown") is None


def test_get_pricing_unavailable_catalog_returns_none(tmp_path):
    """目录失效（哈希不符）一律按未知价格处理。"""
    prices = [_entry()]
    text = json.dumps(
        {
            "schema_version": 1,
            "content_version": "test-v1",
            "currency": "USD",
            "prices": prices,
        },
        ensure_ascii=False,
    )
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    catalog = JsonPricingCatalog.load(str(path), "0" * 64)
    assert catalog.available is False
    p = CatalogPricingProvider(catalog)
    assert p.get_pricing("gpt-4o", "openai") is None


def test_get_pricing_ambiguous_returns_none(tmp_path):
    catalog = _load_catalog(
        tmp_path,
        [_entry(price_id="p1"), _entry(price_id="p2", billing_model="gpt-4o-v2",
                                      aliases=["gpt-4o"])],
    )
    p = CatalogPricingProvider(catalog)
    assert p.get_pricing("gpt-4o", "openai") is None
