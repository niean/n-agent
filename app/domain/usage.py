# app/domain/usage.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CanonicalUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    raw_usage: dict[str, Any] | None = None

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens + self.reasoning_tokens


@dataclass(frozen=True)
class UsageCost:
    amount_usd: str  # Decimal as string to preserve precision
    status: str  # "estimated" | "unknown"
    pricing_version: str | None = None


@dataclass(frozen=True)
class PricingEntry:
    model_pattern: str
    provider: str
    input_cost_per_million: str
    output_cost_per_million: str
    cache_read_cost_per_million: str
    cache_write_cost_per_million: str
    pricing_version: str
    source_url: str


@dataclass
class SessionUsageStats:
    session_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    api_call_count: int = 0
    estimated_cost_usd: str = "0"
    cost_status: str = "unknown"


@dataclass(frozen=True)
class OverviewStats:
    """Aggregate usage stats across all sessions."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    api_call_count: int = 0
    estimated_cost_usd: str = "0"
    session_count: int = 0


@dataclass(frozen=True)
class SessionUsageSummary:
    """One row in the paginated sessions table. Combines session metadata
    (id/title/created_at/source) with cumulative usage stats."""
    session_id: str
    title: str
    created_at: str
    source: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    api_call_count: int = 0
    estimated_cost_usd: str = "0"
    cost_status: str = "unknown"


@dataclass(frozen=True)
class ContextBreakdown:
    system_prompt: int = 0
    tool_definitions: int = 0
    memory: int = 0
    conversation: int = 0

    @property
    def total(self) -> int:
        return self.system_prompt + self.tool_definitions + self.memory + self.conversation


@dataclass(frozen=True)
class UsageRecord:
    id: int | None
    session_id: str
    model: str | None
    provider: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    total_tokens: int
    estimated_cost_usd: str
    cost_status: str
    latency_ms: int | None
    created_at: str
    requested_model: str | None = None
    trigger_type: str | None = None
    request_messages: str | None = None
    response_message: str | None = None
    tools: str | None = None
    generation_params: str | None = None


@dataclass(frozen=True)
class CompressionStat:
    id: int | None
    session_id: str
    before_tokens: int
    after_tokens: int
    tokens_saved: int
    compression_ratio: float
    created_at: str


class UsageRecorder(Protocol):
    async def record_call(
        self, session_id: str, model: str | None, provider: str | None,
        usage: CanonicalUsage, cost: UsageCost, latency_ms: int | None,
        requested_model: str | None = None,
        trigger_type: str | None = None,
        request_messages: str | None = None,
        response_message: str | None = None,
        tools: str | None = None,
        generation_params: str | None = None,
    ) -> None: ...
    async def get_session_stats(self, session_id: str) -> SessionUsageStats: ...
    async def list_records(self, session_id: str, limit: int = 50) -> list[UsageRecord]: ...
    async def record_compression(
        self, session_id: str, before_tokens: int, after_tokens: int,
    ) -> None: ...
    async def list_compressions(self, session_id: str) -> list[CompressionStat]: ...
    async def get_overview_stats(self) -> OverviewStats: ...
    async def list_sessions_paginated(
        self, page: int, page_size: int,
    ) -> tuple[list[SessionUsageSummary], int]: ...


class PricingProvider(Protocol):
    def get_pricing(self, model: str, provider: str) -> PricingEntry | None: ...


class ContextBreakdownCalculator(Protocol):
    def compute(
        self, system_prompt: str, tool_definitions: list[dict],
        messages: list[dict], external_memory_block: str,
    ) -> ContextBreakdown: ...
