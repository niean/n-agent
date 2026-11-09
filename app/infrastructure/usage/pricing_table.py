# app/infrastructure/usage/pricing_table.py
"""PricingProvider 兼容适配器（Infrastructure 层）。

历史上这里是子串前缀匹配的内置价格表（InMemoryPricingProvider），已退役：
计价匹配权威收敛到版本化价格目录（JsonPricingCatalog），目录按
(billing_provider, billing_model, operation) 大小写敏感精确匹配（含 aliases），
不存在前缀/子串匹配。本模块只保留形状转换：把目录的唯一命中条目投影为
既有 PricingEntry，供 UsageService 的 PricingProvider Protocol 兼容入口消费。

目录失效（unavailable）、零匹配（not_found）、歧义（ambiguous）或条件不可
判定（unsupported）一律返回 None，由消费方按未知价格处理；本适配器不做
任何匹配逻辑。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.usage import PricingEntry

if TYPE_CHECKING:
    from app.infrastructure.usage.pricing_catalog import JsonPricingCatalog


class CatalogPricingProvider:
    """把版本化价格目录适配为 PricingProvider Protocol（get_pricing 入口）。"""

    def __init__(self, catalog: "JsonPricingCatalog") -> None:
        self._catalog = catalog

    def get_pricing(self, model: str, provider: str) -> PricingEntry | None:
        resolution = self._catalog.resolve((provider, model), "chat")
        if resolution.status != "unique" or resolution.entry is None:
            return None
        entry = resolution.entry
        prices = entry.unit_prices

        def _price(bucket: str) -> str:
            # 目录未声明单价的桶按 "0" 投影（仅兼容旧形状；权威计价路径
            # 不经过本投影，直接消费目录条目与 Domain 纯计算器）
            value = prices.get(bucket)
            return value if isinstance(value, str) else "0"

        return PricingEntry(
            model_pattern=entry.billing_model,
            provider=entry.billing_provider,
            input_cost_per_million=_price("input"),
            output_cost_per_million=_price("output"),
            cache_read_cost_per_million=_price("cache_read"),
            cache_write_cost_per_million=_price("cache_write"),
            pricing_version=resolution.pricing_version or "",
            source_url=entry.source_url,
        )
