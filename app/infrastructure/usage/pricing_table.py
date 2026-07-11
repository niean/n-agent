# app/infrastructure/usage/pricing_table.py
from __future__ import annotations

from app.domain.usage import PricingEntry

_PRICING_VERSION = "2026-07"
_SOURCE = "https://www.openai.com/pricing | https://www.anthropic.com/pricing | https://api-docs.deepseek.com/quick_start/pricing"

_PRICING_TABLE: list[dict] = [
    {
        "model_pattern": "gpt-4o", "provider": "openai",
        "input_cost_per_million": "2.50", "output_cost_per_million": "10.00",
        "cache_read_cost_per_million": "1.25", "cache_write_cost_per_million": "2.50",
    },
    {
        "model_pattern": "gpt-4o-mini", "provider": "openai",
        "input_cost_per_million": "0.15", "output_cost_per_million": "0.60",
        "cache_read_cost_per_million": "0.075", "cache_write_cost_per_million": "0.15",
    },
    {
        "model_pattern": "gpt-4-turbo", "provider": "openai",
        "input_cost_per_million": "10.00", "output_cost_per_million": "30.00",
        "cache_read_cost_per_million": "5.00", "cache_write_cost_per_million": "10.00",
    },
    {
        "model_pattern": "gpt-3.5-turbo", "provider": "openai",
        "input_cost_per_million": "0.50", "output_cost_per_million": "1.50",
        "cache_read_cost_per_million": "0.25", "cache_write_cost_per_million": "0.50",
    },
    {
        "model_pattern": "claude-3-5-sonnet", "provider": "anthropic",
        "input_cost_per_million": "3.00", "output_cost_per_million": "15.00",
        "cache_read_cost_per_million": "0.30", "cache_write_cost_per_million": "3.75",
    },
    {
        "model_pattern": "claude-3-5-haiku", "provider": "anthropic",
        "input_cost_per_million": "0.80", "output_cost_per_million": "4.00",
        "cache_read_cost_per_million": "0.08", "cache_write_cost_per_million": "1.00",
    },
    {
        "model_pattern": "claude-3-opus", "provider": "anthropic",
        "input_cost_per_million": "15.00", "output_cost_per_million": "75.00",
        "cache_read_cost_per_million": "1.50", "cache_write_cost_per_million": "18.75",
    },
    {
        "model_pattern": "deepseek-chat", "provider": "deepseek",
        "input_cost_per_million": "0.14", "output_cost_per_million": "0.28",
        "cache_read_cost_per_million": "0.014", "cache_write_cost_per_million": "0.14",
    },
    {
        "model_pattern": "deepseek-reasoner", "provider": "deepseek",
        "input_cost_per_million": "0.55", "output_cost_per_million": "2.19",
        "cache_read_cost_per_million": "0.055", "cache_write_cost_per_million": "0.55",
    },
]


class InMemoryPricingProvider:
    def __init__(self, table: list[dict] | None = None) -> None:
        self._table = table if table is not None else _PRICING_TABLE

    def get_pricing(self, model: str, provider: str) -> PricingEntry | None:
        model_lower = model.lower()
        # longest prefix match
        best: dict | None = None
        best_len = 0
        for entry in self._table:
            pattern = entry["model_pattern"].lower()
            if pattern in model_lower and len(pattern) > best_len:
                best = entry
                best_len = len(pattern)
        if best is None:
            return None
        return PricingEntry(
            model_pattern=best["model_pattern"],
            provider=best["provider"],
            input_cost_per_million=best["input_cost_per_million"],
            output_cost_per_million=best["output_cost_per_million"],
            cache_read_cost_per_million=best["cache_read_cost_per_million"],
            cache_write_cost_per_million=best["cache_write_cost_per_million"],
            pricing_version=_PRICING_VERSION,
            source_url=_SOURCE,
        )
