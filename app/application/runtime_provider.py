from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Callable, Mapping

from app.domain.provider import (
    LLMEvent,
    LLMProvider,
    LLMResult,
    ModelInfo,
    ProviderConfig,
    resolve_model,
)


ProviderFactory = Callable[[ProviderConfig, str], LLMProvider]

# 受信计价身份映射：(provider 配置 ID, 请求模型) -> (billing_provider,
# billing_model)。启动期 env-only 精确映射，由组合根解析后注入。
BillingIdentityMap = Mapping[tuple[str, str], tuple[str, str]]


class ActiveProviderHolder:
    """运行时持有 active LLMProvider 实例，支持热替换。"""

    def __init__(
        self,
        factory: ProviderFactory,
        billing_identity_map: BillingIdentityMap | None = None,
    ):
        self._factory = factory
        self._billing_identity_map: dict[tuple[str, str], tuple[str, str]] = (
            dict(billing_identity_map) if billing_identity_map else {}
        )
        self._provider: LLMProvider | None = None
        self._config: ProviderConfig | None = None
        self._lock = asyncio.Lock()

    @property
    def current_model(self) -> str:
        return self._config.model if self._config else ""

    @property
    def current_config(self) -> ProviderConfig | None:
        return self._config

    def billing_identity(self, requested_model: str) -> tuple[str, str] | None:
        """受信计价身份查询：仅按（当前 provider 配置 ID, 请求模型）在启动
        期 env-only 精确映射中查找。运行时 provider 切换（swap/fallback）后
        自动绑定到新配置；未注入映射或无当前配置 => None（identity 未解析，
        成本按 identity_unresolved 处理）。请求 options、URL、响应模型不参
        与映射，Provider 适配器不能覆盖。"""
        config = self._config
        if config is None:
            return None
        return self._billing_identity_map.get((config.id, requested_model))

    async def swap(self, config: ProviderConfig, api_key: str) -> None:
        async with self._lock:
            self._config = config
            self._provider = self._factory(config, api_key or "")

    async def list_models(self) -> list[ModelInfo]:
        return await self._require().list_models()

    async def supports_tools(self, model: str) -> bool:
        return await self._require().supports_tools(model)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        model: str,
        options: dict[str, Any],
    ) -> LLMResult | AsyncIterator[LLMEvent]:
        provider = self._require()
        # config 与 provider 实例在同一作用域快照：身份按实例选择时刻的配置
        # 解析（请求在飞期间 swap 不影响本调用），随后随 LLMResult 携带给
        # 账本路径；观测路径的身份由工厂期绑定到实例的解析器冻结，两条路径
        # 同源（同一调用使用同一计价结果），记录时不重读切换后的当前配置。
        config = self._config
        # 与 Provider 适配器同源解析占位模型（评审修复第三轮）：Provider 在
        # chat 入口经 resolve_model 回退真实模型后才开始遥测（begin_chat_
        # attempt 携带解析结果，身份按真实模型命中映射），身份查找必须使用
        # 同一解析结果——否则映射按真实模型名配置时，账本 identity_unresolved
        # 而观测已命中（同一调用两种成本结论）。resolve_model 幂等，Provider
        # 入口的二次解析结果不变。
        target_model = resolve_model(model or self.current_model, config.model)
        result = await provider.chat(messages, tools, stream, target_model, options)
        if isinstance(result, LLMResult):
            identity = (
                self._billing_identity_map.get((config.id, target_model))
                if config is not None else None
            )
            result = replace(result, billing_identity=identity)
        return result

    def _require(self) -> LLMProvider:
        if self._provider is None:
            raise RuntimeError("ActiveProviderHolder has no active provider configured")
        return self._provider
