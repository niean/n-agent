# app/infrastructure/observability/experiment_sampler.py
"""受信实验采样器（plan T13，spec 11.1）。

仅含 OTel SDK 依赖，不 import 任何 app.* 业务模块（observability 包边界：
tests/architecture/test_observability_boundaries.py 锁定）。实验项根采样
的语义：

- 仅当 Context 携带服务端私有的受信实验令牌且父 span 无效（正在创建
  experiment-item 根）时强制 RECORD_AND_SAMPLE，覆盖 ParentBased；
- 子 span 有有效父级，继续 ParentBased 继承；远端 N-KB 继承其自身父级
  决定；
- 受信令牌是模块私有的 Context 键对象，客户端 header/baggage 只能产生
  baggage 键，永远无法构造该键，因此不能开启此覆盖；
- 不替换全局 TracerProvider，不在业务 span 结束后补造采样；OTel 整体
  禁用时 proxy tracer 返回 NonRecordingSpan，实验分析标 unavailable。

组合根接线：``setup._build_sampler`` 将
``ParentBased(TraceIdRatioBased(ratio))`` 包裹进本采样器；实验项根 scope
（app/infrastructure/evaluation/target_runner.py）经 ``set_value`` 写入
受信令牌触发覆盖（Application 经文档化端口注入，不感知实现）。
"""
from __future__ import annotations

from typing import Any

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import SpanKind

# 模块私有的 Context 键（create_key 返回唯一键对象，baggage 无法伪造）
TRUSTED_EXPERIMENT_CONTEXT_KEY = context_api.create_key(
    "nagent.trusted-experiment-root"
)

# 模块私有令牌：只有受信实验根 scope 会写入该值
TRUSTED_EXPERIMENT_TOKEN = object()


class TrustedExperimentSampler(Sampler):
    """受信实验采样器：仅对服务端可信实验上下文中的根 span 强制采样。

    其余情况一律委托 base 采样器（ParentBased 语义：子 span 继承父级决定，
    远端父级继承真实决定，ratio 仅约束无父级的普通根）。
    """

    def __init__(self, base: Sampler) -> None:
        self.base = base

    def get_description(self) -> str:
        return f"TrustedExperimentSampler({self.base.get_description()})"

    def should_sample(
        self,
        parent_context: Any,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Any = None,
        links: Any = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        if self._is_trusted_root(parent_context):
            # 强制采样必须回传 start_span 传入的 attributes（SDK 以采样
            # 结果的 attributes 落盘 span 属性，丢弃即丢失实验身份属性）
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)
        return self.base.should_sample(
            parent_context=parent_context,
            trace_id=trace_id,
            name=name,
            kind=kind,
            attributes=attributes,
            links=links,
            trace_state=trace_state,
        )

    @staticmethod
    def _is_trusted_root(parent_context: Any) -> bool:
        if parent_context is None:
            return False
        try:
            token = parent_context.get(TRUSTED_EXPERIMENT_CONTEXT_KEY)
        except Exception:
            return False
        if token is not TRUSTED_EXPERIMENT_TOKEN:
            return False
        # 仅根（无有效父级）触发覆盖；子 span 继续 ParentBased
        parent_span_context = trace.get_current_span(
            parent_context
        ).get_span_context()
        return not getattr(parent_span_context, "is_valid", False)
