from app.infrastructure.usage.pricing_table import InMemoryPricingProvider


def test_get_pricing_gpt4o():
    p = InMemoryPricingProvider()
    entry = p.get_pricing("gpt-4o-2024-08-06", "openai")
    assert entry is not None
    assert entry.input_cost_per_million == "2.50"
    assert entry.output_cost_per_million == "10.00"


def test_get_pricing_claude():
    p = InMemoryPricingProvider()
    entry = p.get_pricing("claude-3-5-sonnet-20241022", "anthropic")
    assert entry is not None
    assert float(entry.input_cost_per_million) == 3.0


def test_get_pricing_unknown_returns_none():
    p = InMemoryPricingProvider()
    assert p.get_pricing("unknown-model", "unknown") is None


def test_get_pricing_deepseek():
    p = InMemoryPricingProvider()
    entry = p.get_pricing("deepseek-chat", "deepseek")
    assert entry is not None
