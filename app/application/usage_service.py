# app/application/usage_service.py
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.domain.usage import (
    CanonicalUsage, UsageCost, PricingEntry, SessionUsageStats,
    OverviewStats, SessionUsageSummary, ContextBreakdown,
    UsageRecord, CompressionStat,
    UsageRecorder, PricingProvider, ContextBreakdownCalculator,
)

logger = logging.getLogger(__name__)


class UsageService:
    def __init__(
        self,
        recorder: UsageRecorder,
        pricing: PricingProvider,
        breakdown_calculator: ContextBreakdownCalculator,
    ) -> None:
        self._recorder = recorder
        self._pricing = pricing
        self._breakdown = breakdown_calculator

    def normalize_usage(self, raw_usage: dict[str, Any], provider_kind: str) -> CanonicalUsage:
        if not raw_usage:
            return CanonicalUsage(request_count=0, raw_usage=raw_usage)
        if provider_kind == "anthropic":
            return self._normalize_anthropic(raw_usage)
        # OpenAI-compatible (covers openai, deepseek, etc.)
        return self._normalize_openai(raw_usage)

    def _normalize_openai(self, raw: dict[str, Any]) -> CanonicalUsage:
        prompt_tokens = raw.get("prompt_tokens", 0)
        completion_tokens = raw.get("completion_tokens", 0)
        total = raw.get("total_tokens", prompt_tokens + completion_tokens)
        prompt_details = raw.get("prompt_tokens_details") or {}
        cached = prompt_details.get("cached_tokens", 0)
        completion_details = raw.get("completion_tokens_details") or {}
        reasoning = completion_details.get("reasoning_tokens", 0)
        input_tokens = prompt_tokens - cached
        return CanonicalUsage(
            input_tokens=max(input_tokens, 0),
            output_tokens=completion_tokens,
            cache_read_tokens=cached,
            cache_write_tokens=0,
            reasoning_tokens=reasoning,
            request_count=1,
            raw_usage=raw,
        )

    def _normalize_anthropic(self, raw: dict[str, Any]) -> CanonicalUsage:
        usage = raw.get("usage", raw)
        return CanonicalUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            reasoning_tokens=0,
            request_count=1,
            raw_usage=raw,
        )

    def estimate_cost(self, usage: CanonicalUsage, model: str, provider: str) -> UsageCost:
        entry = self._pricing.get_pricing(model, provider)
        if entry is None:
            return UsageCost(amount_usd="0", status="unknown", pricing_version=None)
        cost = Decimal("0")
        cost += Decimal(entry.input_cost_per_million) * Decimal(usage.input_tokens) / Decimal("1000000")
        cost += Decimal(entry.output_cost_per_million) * Decimal(usage.output_tokens) / Decimal("1000000")
        cost += Decimal(entry.cache_read_cost_per_million) * Decimal(usage.cache_read_tokens) / Decimal("1000000")
        cost += Decimal(entry.cache_write_cost_per_million) * Decimal(usage.cache_write_tokens) / Decimal("1000000")
        return UsageCost(
            amount_usd=str(cost),
            status="estimated",
            pricing_version=entry.pricing_version,
        )

    async def record_call(
        self, session_id: str, model: str | None, provider: str | None,
        raw_usage: dict[str, Any], latency_ms: int | None,
        provider_kind: str = "openai",
        requested_model: str | None = None,
        trigger_type: str | None = None,
        request_messages: str | None = None,
        response_message: str | None = None,
        tools: str | None = None,
        generation_params: str | None = None,
    ) -> None:
        usage = self.normalize_usage(raw_usage, provider_kind)
        if usage.request_count == 0:
            logger.warning("skip usage record: empty usage for session=%s model=%s", session_id, model)
            return
        cost = self.estimate_cost(usage, model or "", provider or "")
        logger.info(
            "API call: model=%s provider=%s in=%d out=%d cache_r=%d cache_w=%d reason=%d total=%d latency=%dms",
            model, provider, usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens, usage.cache_write_tokens, usage.reasoning_tokens,
            usage.total_tokens, latency_ms or 0,
        )
        try:
            await self._recorder.record_call(
                session_id, model, provider, usage, cost, latency_ms,
                requested_model=requested_model,
                trigger_type=trigger_type,
                request_messages=request_messages,
                response_message=response_message,
                tools=tools,
                generation_params=generation_params,
            )
        except Exception:
            logger.exception("failed to record usage for session=%s", session_id)

    async def get_session_stats(self, session_id: str) -> SessionUsageStats:
        return await self._recorder.get_session_stats(session_id)

    async def list_records(self, session_id: str, limit: int = 50) -> list[UsageRecord]:
        return await self._recorder.list_records(session_id, limit)

    async def record_compression(
        self, session_id: str, before_tokens: int, after_tokens: int,
    ) -> None:
        try:
            await self._recorder.record_compression(session_id, before_tokens, after_tokens)
        except Exception:
            logger.exception("failed to record compression for session=%s", session_id)

    async def list_compressions(self, session_id: str) -> list[CompressionStat]:
        return await self._recorder.list_compressions(session_id)

    async def get_overview_stats(self) -> OverviewStats:
        return await self._recorder.get_overview_stats()

    async def list_sessions_paginated(
        self, page: int, page_size: int,
    ) -> tuple[list[SessionUsageSummary], int]:
        return await self._recorder.list_sessions_paginated(page, page_size)

    def get_context_breakdown(
        self, system_prompt: str, tool_definitions: list[dict],
        messages: list[dict], external_memory_block: str,
    ) -> ContextBreakdown:
        return self._breakdown.compute(system_prompt, tool_definitions, messages, external_memory_block)
