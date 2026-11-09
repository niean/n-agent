# app/application/usage_service.py
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Protocol

from app.domain.cost import (
    AttemptCost,
    AttemptUsage,
    CatalogResolution,
    TokenValue,
    UnknownReason,
    calculate_attempt_cost as _calculate_attempt_cost,
)
from app.domain.usage import (
    CanonicalUsage, UsageCost, PricingEntry, SessionUsageStats,
    OverviewStats, SessionUsageSummary, ContextBreakdown,
    UsageRecord, CompressionStat,
    UsageRecorder, PricingProvider, ContextBreakdownCalculator,
)

logger = logging.getLogger(__name__)


class PricingCatalog(Protocol):
    """价格目录解析协议（Infrastructure 的 JsonPricingCatalog 适配此协议）。

    Application 只依赖 Domain 类型与该协议，不导入 Infrastructure 实现。"""

    def resolve(
        self,
        identity: tuple[str, str],
        operation: str,
        facts: Mapping[str, str] | None = None,
    ) -> CatalogResolution: ...


# 受信计价身份解析器：(请求模型, provider kind) -> (billing_provider,
# billing_model)；无法解析时返回 None（identity_unresolved）。
BillingIdentityResolver = Callable[[str, str], tuple[str, str] | None]

# 尝试冻结身份哨兵：区分"调用方未携带身份"（旧调用契约，由解析器在记录时
# 收口）与"携带的身份为 None"（实例选择期已冻结为未解析，不追溯重解析）。
_BILLING_IDENTITY_UNSET = object()

# Safe log event template (T6): registered in the composition root's
# _APP_SAFE_LOG_EVENTS so the OTel log projection can emit its event code.
# Shared constant -- the registration and this call site must byte-match.
API_CALL_LOG_TEMPLATE = (
    "API call: model=%s provider=%s in=%d out=%d cache_r=%d cache_w=%d "
    "reason=%d total=%d norm=%d latency=%dms"
)

# Anthropic cache TTL 拆分事实键（与观测路径 ChatAttempt 的 _CACHE_TTL_FACT_KEYS
# 同词汇；账本与观测共用的子字段级判定入口）。
_CACHE_TTL_FIELDS: tuple[tuple[str, str], ...] = (
    ("ephemeral_1h_input_tokens", "1h"),
    ("ephemeral_5m_input_tokens", "5m"),
)


class _CacheTtlUnsupported:
    """TTL 拆分不能证明"全部 cache_write 属于同一已计价条件"的显式不支持
    哨兵类型（单例 CACHE_TTL_UNSUPPORTED）。覆盖两类情形：混合合法 TTL
    （2 个及以上非零合法子字段），以及单一非零合法子字段之和未覆盖
    cache_creation 总量（评审修复第五轮）。区别于 None（事实缺失/整组非法
    拒绝）。计价入口必须在目录价格匹配前把该哨兵翻译为 unsupported_pricing
    ——无条件价格项不得被未证明的 TTL 条件唯一命中（spec：只有全部用量属于
    同一已计价条件时才可合并，本次混合 TTL 记 unsupported_pricing，不得套用
    单一 TTL 单价）。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CACHE_TTL_UNSUPPORTED"


CACHE_TTL_UNSUPPORTED = _CacheTtlUnsupported()


def _anthropic_usage_view(raw_usage: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic raw usage 形态解包唯一入口：兼容 {"usage": {...}} 嵌套与
    平铺两种形态，返回同一平铺视图。评审修复第四轮：TTL 非法校验、TTL 条件
    事实抽取与四桶解析必须在同一解包视图上进行，嵌套形态不得绕过任一通道
    （此前 TTL 两个助手只读外层 cache_creation，嵌套内负数 TTL 被忽略、
    嵌套内合法单一 TTL 无法命中条件价格项）。"""
    raw = raw_usage if isinstance(raw_usage, dict) else {}
    usage = raw.get("usage", raw)
    return usage if isinstance(usage, dict) else {}


def _cache_ttl_invalid_value(raw_usage: dict[str, Any] | None) -> Any:
    """返回首个"存在但非法"的 TTL 子字段值（bool/浮点/字符串/负数），全部
    合法或缺席时返回 None。账本与观测共用的子字段级非法判定（评审修复第三
    轮：任一在场子字段非法即整组 TTL 事实不可信）。输入经 _anthropic_usage_view
    解包（嵌套形态与平铺同判）。"""
    breakdown = _anthropic_usage_view(raw_usage).get("cache_creation")
    if not isinstance(breakdown, dict):
        return None
    for key, _ttl in _CACHE_TTL_FIELDS:
        value = breakdown.get(key)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            return value
    return None


def cache_ttl_facts(
    raw_usage: dict[str, Any] | None,
) -> "dict[str, str] | _CacheTtlUnsupported | None":
    """Anthropic cache TTL 拆分事实的三态判定（账本与观测共用的唯一事实
    抽取入口，组合根 estimator 闭包同样调用本函数）。

    三态：（a）无 TTL 事实/事实缺失/整组非法（任一在场子字段存在但非法，
    bool/浮点/字符串/负数）-> None，非法子字段不得被"仅取正值"抽取掩盖，
    桶级非法由 attempt_usage_from_raw 原样携带、两条路径对同一 raw_usage
    收口为同一 invalid_usage；（b）恰好一个非零合法 TTL 子字段且其拆分覆盖
    cache_creation 总量 -> 受信条件事实 {"cache_ttl": ...}；（c）混合合法
    TTL（2 个及以上非零合法子字段，零值子字段不代表在用 TTL，不参与混合
    判定），或单一非零合法子字段之和未覆盖总量（评审修复第五轮：spec L98
    "只有全部用量属于同一已计价条件时才可合并"，剩余用量的 TTL 条件未被
    证明，不得套用该 TTL 单价）-> CACHE_TTL_UNSUPPORTED 显式不支持哨兵，
    由计价入口在目录价格匹配前翻译为 unsupported_pricing，无条件价格项不得
    被未证明的 TTL 条件唯一命中。总量为 0 时无需条件（既有零语义不变）；
    总量缺席或非法时跳过覆盖检查，结论由四桶事实在 Domain 计算器收口
    （missing_usage / invalid_usage 先行）。输入经 _anthropic_usage_view
    解包（嵌套形态与平铺同判）。
    """
    view = _anthropic_usage_view(raw_usage)
    breakdown = view.get("cache_creation")
    if not isinstance(breakdown, dict):
        return None
    ttls = set()
    positive_sum = 0
    for key, ttl in _CACHE_TTL_FIELDS:
        value = breakdown.get(key)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            return None
        if value > 0:
            ttls.add(ttl)
            positive_sum += value
    if len(ttls) > 1:
        # 混合合法 TTL：显式不支持哨兵（区别于事实缺失 None），不得回落为
        # 缺失事实——否则无条件条目在目录解析侧可被唯一命中
        return CACHE_TTL_UNSUPPORTED
    if len(ttls) == 1:
        # 评审修复第五轮：拆分必须覆盖总量——唯一非零子字段之和（即该值）
        # 不等于 cache_creation_input_tokens 时，"全部用量属于同一已计价
        # 条件"未被证明，不得套用该 TTL 单价
        total = view.get("cache_creation_input_tokens")
        if type(total) is int and 0 < total != positive_sum:
            return CACHE_TTL_UNSUPPORTED
        return {"cache_ttl": next(iter(ttls))}
    return None


class UsageService:
    def __init__(
        self,
        recorder: UsageRecorder,
        pricing: PricingProvider,
        breakdown_calculator: ContextBreakdownCalculator,
        *,
        pricing_catalog: PricingCatalog | None = None,
        cost_calculator: Callable[
            [AttemptUsage, tuple[str, str] | None, CatalogResolution], AttemptCost,
        ] | None = None,
        billing_identity_resolver: BillingIdentityResolver | None = None,
    ) -> None:
        self._recorder = recorder
        self._pricing = pricing
        self._breakdown = breakdown_calculator
        # 尝试成本生命周期依赖（组合根注入，与 telemetry facade 共享同一
        # 目录与计算器实例；不新增共享运行服务）
        self._pricing_catalog = pricing_catalog
        self._cost_calculator = cost_calculator or _calculate_attempt_cost
        self._identity_resolver = billing_identity_resolver

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

    def attempt_usage_from_raw(
        self, raw_usage: dict[str, Any] | None, provider_kind: str,
    ) -> AttemptUsage:
        """从原始 usage 构造保留原始已知性的四桶事实。

        关键规则：字段存在但非法（bool/浮点/字符串/负数等）原样携带为
        known，由 Domain 纯计算器判 invalid_usage；字段缺席（含 None 值）
        才是 unknown。不从 CanonicalUsage 默认 0 推断已知。OpenAI 协议无
        cache_write 概念，声明为 not_applicable（仅成本事实；既有 Token
        遥测的兼容逻辑独立，不受影响）。
        """
        raw = raw_usage if isinstance(raw_usage, dict) else {}
        if provider_kind == "anthropic":
            # 形态解包唯一入口：TTL 非法校验（助手内部经 _anthropic_usage_view
            # 解包）与四桶解析在同一平铺视图上判定（嵌套形态与平铺同判）
            usage = _anthropic_usage_view(raw_usage)
            cache_write = usage.get("cache_creation_input_tokens")
            ttl_invalid = _cache_ttl_invalid_value(raw_usage)
            if ttl_invalid is not None:
                # 任一在场 TTL 子字段非法：cache_write 桶事实整体不可信，
                # 原样携带非法子字段值（known 契约：合法性由 Domain 纯计算
                # 器判 invalid_usage），与 ChatAttempt 的 TTL 子字段级
                # invalid 收口同判（invalid_usage 先于 missing_usage）
                cache_write = ttl_invalid
            return AttemptUsage(buckets={
                "input": self._known_or_unknown(usage.get("input_tokens")),
                "output": self._known_or_unknown(usage.get("output_tokens")),
                "cache_read": self._known_or_unknown(usage.get("cache_read_input_tokens")),
                "cache_write": self._known_or_unknown(cache_write),
            })
        # OpenAI-compatible（covers openai, deepseek, etc.）
        prompt = raw.get("prompt_tokens")
        completion = raw.get("completion_tokens")
        details = raw.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if prompt is None:
            # prompt 缺席：input 未知（cached 独立有效时不改变 input 未知性）
            input_value = TokenValue.unknown()
        elif not self._is_count(prompt):
            # prompt 存在但非法：原样携带，由计算器判 invalid_usage
            input_value = TokenValue.known(prompt)
        elif cached is not None and self._is_count(cached):
            # prompt 与 cached 均为合法计数：input = prompt - cached；
            # cached > prompt 时差值为负，由计算器判 invalid_usage
            input_value = TokenValue.known(prompt - cached)
        else:
            # prompt 合法但 cached 缺席（或存在但非法）：input 未知。
            # 与 _extract_openai_usage 同语义：缺 cached 绝不以 prompt 充当
            # Ti（否则无 cache_read 单价的目录条目会把整个 prompt 按输入
            # 价计费，违反"缺 cached 仍未知"）；cached 非法时 cache_read
            # 桶已携带非法值，计算器按判定顺序先报 invalid_usage
            input_value = TokenValue.unknown()
        return AttemptUsage(buckets={
            "input": input_value,
            "output": self._known_or_unknown(completion),
            "cache_read": self._known_or_unknown(cached),
            "cache_write": TokenValue.not_applicable(),
        })

    @staticmethod
    def _is_count(value: Any) -> bool:
        return type(value) is int and value >= 0

    @staticmethod
    def _known_or_unknown(value: Any) -> TokenValue:
        if value is None:
            return TokenValue.unknown()
        return TokenValue.known(value)

    def calculate_attempt_cost(
        self,
        attempt_usage: AttemptUsage,
        identity: tuple[str, str] | None,
        facts: Mapping[str, str] | None = None,
    ) -> AttemptCost:
        """Domain 纯计算器 + 目录解析：产出一次尝试的 AttemptCost。

        判定顺序由 Domain 计算器固定（identity_unresolved 最先）。目录未
        注入按 pricing_unavailable 处理；identity 缺失不触发目录查询。
        facts 为 CACHE_TTL_UNSUPPORTED（混合合法 TTL，或 TTL 拆分未覆盖
        总量）时先取得目录可用性结论再收口：目录失效按 pricing_unavailable
        （spec 固定判定顺序第 4 位先于 unsupported_pricing 第 7 位，不得被
        掩盖）；目录可用时以 unsupported 解析结论交 Domain 计算器收口
        unsupported_pricing（invalid/missing 判定仍在计算器内先行）。
        """
        if identity is None:
            return AttemptCost(status="unknown", reason=UnknownReason.identity_unresolved)
        if self._pricing_catalog is None:
            return AttemptCost(status="unknown", reason=UnknownReason.pricing_unavailable)
        if facts is CACHE_TTL_UNSUPPORTED:
            # 混合合法 TTL / TTL 拆分未覆盖总量（评审修复第四、五轮）：先经
            # 协议方法 resolve 探测目录可用性（facts=None 不表达本次 TTL
            # 条件，探测命中结果弃用）——目录失效（哈希不匹配、文件损坏等）
            # 时按 unavailable 收口 pricing_unavailable，不得以 unsupported
            # 掩盖；目录可用时才以 unsupported 解析结论进入 Domain 纯计算
            # 器，invalid_usage / missing_usage 判定仍在计算器内先行收口，
            # 固定判定顺序不变。
            probe = self._pricing_catalog.resolve(identity, "chat", None)
            if probe.status == "unavailable":
                resolution = probe
            else:
                resolution = CatalogResolution.unsupported(
                    getattr(self._pricing_catalog, "pricing_version", None))
        else:
            resolution = self._pricing_catalog.resolve(identity, "chat", facts)
        return self._cost_calculator(attempt_usage, identity, resolution)

    def estimate_attempt_cost(
        self,
        raw_usage: dict[str, Any] | None,
        provider_kind: str,
        identity: tuple[str, str] | None,
    ) -> AttemptCost:
        """一次尝试的成本收口：raw usage 保留已知性后交给纯计算器。

        与观测路径共用 cache_ttl_facts 抽取同一组条件事实，账本与观测对
        同一响应产出同一结论（Anthropic TTL 条目不因路径不同而分叉）。
        """
        attempt_usage = self.attempt_usage_from_raw(raw_usage, provider_kind)
        return self.calculate_attempt_cost(
            attempt_usage, identity, cache_ttl_facts(raw_usage),
        )

    def estimate_cost(
        self, usage: CanonicalUsage, model: str, provider: str,
        *, billing_identity: Any = _BILLING_IDENTITY_UNSET,
    ) -> UsageCost:
        """兼容入口：账本路径成本估算。

        改为调用 Domain 纯计算器 + 版本化目录：计价身份由组合根注入的受信
        解析器提供（缺失按 identity_unresolved）；计价使用保留原始已知性的
        AttemptUsage，raw_usage 不足时 unknown，不从 CanonicalUsage 默认 0
        推断已知。账本兼容：unknown 继续写旧 UsageCost(amount_usd="0",
        status="unknown") 形态，旧 API 数字占位不得被观测当已知成本。

        billing_identity：调用方携带的尝试冻结身份（实例选择期解析并随
        LLMResult 传递）。携带（含 None=启动时未解析）时直接使用，不在记录
        时重读当前配置——与观测路径共用同一计价结果；未携带（哨兵缺省）
        保持旧契约按解析器收口。
        """
        if billing_identity is _BILLING_IDENTITY_UNSET:
            identity = (
                self._identity_resolver(model, provider)
                if self._identity_resolver is not None else None
            )
        else:
            identity = billing_identity
        cost = self.estimate_attempt_cost(usage.raw_usage, provider, identity)
        if cost.status != "estimated":
            return UsageCost(amount_usd="0", status="unknown", pricing_version=None)
        return UsageCost(
            amount_usd=str(cost.amount_usd),
            status="estimated",
            pricing_version=cost.pricing_version,
            price_id=cost.price_id,
            components={
                bucket: str(value) for bucket, value in cost.components.items()
            },
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
        billing_identity: Any = _BILLING_IDENTITY_UNSET,
    ) -> None:
        """Record a single LLM API call for usage tracking.

        Payload parameters (request_messages, response_message, tools) must
        already be sanitized by InformationFlowService (USAGE_RETENTION target).
        When the release is denied, the caller passes None for these fields;
        token/cost data (raw_usage) is always recorded regardless of payload
        release verdict.

        billing_identity：尝试冻结的受信计价身份（实例选择期解析，随
        LLMResult 传递）。携带（含 None=未解析）时账本与观测使用同一身份，
        不在记录时重读切换后的当前配置；哨兵缺省保持旧契约（解析器收口）。
        """
        usage = self.normalize_usage(raw_usage, provider_kind)
        if usage.request_count == 0:
            logger.warning("skip usage record: empty usage for session=%s model=%s", session_id, model)
            return
        cost = self.estimate_cost(usage, model or "", provider or "",
                                  billing_identity=billing_identity)
        logger.info(
            API_CALL_LOG_TEMPLATE,
            model, provider, usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens, usage.cache_write_tokens, usage.reasoning_tokens,
            usage.total_tokens, usage.normalized_tokens, latency_ms or 0,
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
        before_messages: str | None = None,
        after_messages: str | None = None,
    ) -> None:
        try:
            await self._recorder.record_compression(
                session_id, before_tokens, after_tokens,
                before_messages=before_messages,
                after_messages=after_messages,
            )
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
