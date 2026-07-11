# tests/domain/test_usage_models.py
from app.domain.usage import (
    CanonicalUsage, UsageCost, PricingEntry, SessionUsageStats,
    OverviewStats, SessionUsageSummary, ContextBreakdown,
    UsageRecord, CompressionStat,
    UsageRecorder, PricingProvider, ContextBreakdownCalculator,
)

def test_canonical_usage_defaults():
    u = CanonicalUsage()
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.cache_read_tokens == 0
    assert u.cache_write_tokens == 0
    assert u.reasoning_tokens == 0
    assert u.request_count == 1
    assert u.prompt_tokens == 0
    assert u.total_tokens == 0

def test_canonical_usage_prompt_tokens_includes_cache():
    u = CanonicalUsage(input_tokens=100, cache_read_tokens=50, cache_write_tokens=20)
    assert u.prompt_tokens == 170
    assert u.total_tokens == 170

def test_canonical_usage_total_tokens():
    u = CanonicalUsage(input_tokens=100, output_tokens=50, reasoning_tokens=10)
    assert u.total_tokens == 160

def test_usage_cost_frozen():
    c = UsageCost(amount_usd="0.05", status="estimated", pricing_version="2026-07")
    assert c.status == "estimated"

def test_pricing_entry_fields():
    p = PricingEntry(
        model_pattern="gpt-4o", provider="openai",
        input_cost_per_million="5.00", output_cost_per_million="15.00",
        cache_read_cost_per_million="2.50", cache_write_cost_per_million="6.25",
        pricing_version="2026-07", source_url="https://openai.com/pricing",
    )
    assert p.model_pattern == "gpt-4o"

def test_context_breakdown_total():
    b = ContextBreakdown(system_prompt=500, tool_definitions=300, memory=200, conversation=1000)
    assert b.total == 2000


def test_overview_stats_defaults():
    o = OverviewStats()
    assert o.input_tokens == 0
    assert o.output_tokens == 0
    assert o.total_tokens == 0
    assert o.api_call_count == 0
    assert o.estimated_cost_usd == "0"
    assert o.session_count == 0


def test_overview_stats_frozen():
    o = OverviewStats(input_tokens=100, output_tokens=50, total_tokens=150, session_count=3)
    assert o.session_count == 3
    try:
        o.input_tokens = 999  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised


def test_session_usage_summary_fields():
    s = SessionUsageSummary(session_id="sess-1", title="t", created_at="2026-07-11T00:00:00Z")
    assert s.session_id == "sess-1"
    assert s.api_call_count == 0
    assert s.cost_status == "unknown"


def test_protocols_exist():
    assert hasattr(UsageRecorder, "record_call")
    assert hasattr(UsageRecorder, "get_overview_stats")
    assert hasattr(UsageRecorder, "list_sessions_paginated")
    assert hasattr(PricingProvider, "get_pricing")
    assert hasattr(ContextBreakdownCalculator, "compute")


def test_usage_record_requested_model_optional():
    r = UsageRecord(
        id=1, session_id="s", model="gpt-4o", provider="openai",
        input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
        reasoning_tokens=0, total_tokens=0, estimated_cost_usd="0",
        cost_status="unknown", latency_ms=None, created_at="",
    )
    assert r.requested_model is None
    assert r.tools is None
    assert r.generation_params is None
    r2 = UsageRecord(
        id=2, session_id="s", model="gpt-4o", provider="openai",
        input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
        reasoning_tokens=0, total_tokens=0, estimated_cost_usd="0",
        cost_status="unknown", latency_ms=None, created_at="",
        requested_model="N-Agent",
        tools='[{"type":"function","function":{"name":"x"}}]',
        generation_params='{"temperature":0.7,"max_tokens":4096}',
    )
    assert r2.requested_model == "N-Agent"
    assert r2.tools is not None
    assert r2.generation_params is not None


def test_domain_usage_no_infra_import():
    import ast
    import pathlib
    src = pathlib.Path("app/domain/usage.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.infrastructure"), f"Domain imports Infrastructure: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app.infrastructure"), f"Domain imports Infrastructure: {node.module}"
