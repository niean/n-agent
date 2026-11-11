# app/infrastructure/evaluation/otel_sink.py
"""OtelScoreSink -- OTel 结果事件出口适配（Domain ``ScoreSink`` 端口）。

把 evaluation worker 落库的结果经 ``emit_result`` 发布为
``gen_ai.evaluation.result`` LogRecord（Loki 消费），投递状态由
EvaluationDeliveryWorker 以 deliveries 行持久管理（pending -> accepted），
进程崩溃后由 claim 恢复重发（#13：OTel 出口持久投递闭环）。

与 LangfuseScoreSink 的差异：
- 无回读核验：OTel SDK batch export 由 SDK 自身保证传输重试，本适配
  ``supports_readback = False``，调度器不再做 verified 轮询（accepted 即
  投递成功终态）；
- ``send`` 的 emit 返回值契约（evaluation_telemetry.emit_result）：
  "emitted" 正常返回；"noop"（OTel 禁用）按 dead_letter 类隔离上报
  （error_type=otel_disabled，管理员恢复出口后显式 replay），不伪造成
  功；"failed" 按 retry 类退避（SDK 同步拒绝，多为进程内瞬时状态）；
- ``project`` 的 unlinked 判定与 Langfuse 同序：目标无真实 trace 身份 /
  未采样 / OTel 禁用时不发布事件（linkability.otel_enabled 由组合根
  provider 提供，与链接性语义同一来源）。

external_id 为确定性 UUIDv5（project + result_id + projection_version，
sink 维度隔离），仅用于投递行身份与幂等去重，不对应任何外部资源。
"""
from __future__ import annotations

import uuid
from typing import Any, Mapping

from app.application.evaluation_telemetry import emit_result


def compute_otel_external_id(
    project: str, result_id: str, projection_version: str
) -> str:
    """OTel 投递行确定性身份（同 result 同版本重放幂等）。"""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{project}:{result_id}:otel:{projection_version}",
        )
    )


class OtelSinkDeadLetterError(Exception):
    """OTel 出口不可用（禁用等）：dead_letter 隔离，不自动重试。"""

    def __init__(self, message: str, *, error_type: str = "otel_disabled") -> None:
        super().__init__(message)
        self.delivery_outcome = "dead_letter"
        self.error_type = error_type


class OtelSinkRetryableError(Exception):
    """OTel 发射瞬时失败：retry 类，退避重试。"""

    def __init__(self, message: str, *, error_type: str = "network") -> None:
        super().__init__(message)
        self.delivery_outcome = "retry"
        self.error_type = error_type


class OtelScoreSink:
    """Domain ``ScoreSink`` 端口的 OTel 实现（无 HTTP 依赖）。"""

    supports_readback = False

    async def project(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        result = context["result"]
        target = context["target"] or {}
        definition = context["definition"]
        linkability = context.get("linkability") or {}
        projection_version = context["projection_version"]
        if not linkability.get("otel_enabled"):
            return {"outcome": "unlinked"}
        # 评估 Trace 身份（#12）：只取 result 行冻结的 evaluation.run
        # 身份；业务身份经 target Link 关联（两条独立 Trace），不回退
        # 目标 trace_id/span_id 冒充评估 Trace（旧行为 NULL -> 空身份）
        evaluation_trace = context.get("evaluation_trace") or (None, None)
        trace_identity = (
            evaluation_trace[0] if len(evaluation_trace) > 0 else None,
            evaluation_trace[1] if len(evaluation_trace) > 1 else None,
        )
        payload: dict[str, Any] = {
            "result_id": str(result["result_id"]),
            "metric_name": str(definition["metric_name"]),
            "definition_id": str(definition["definition_id"]),
            "definition_version": str(
                result.get("metric_version") or definition["version"]
            ),
            "status": (
                "succeeded"
                if result["status"] == "value"
                else str(result["status"])
            ),
            "target": {
                "service": target.get("service"),
                "kind": target.get("kind"),
                "subject_id": target.get("subject_id"),
                "subject_revision": target.get("subject_revision"),
                "trace_id": target.get("trace_id"),
                "span_id": target.get("span_id"),
            },
            "typed_value": result.get("typed_value"),
            "skip_reason": result.get("skip_reason"),
            "error_type": (
                "worker_error" if result["status"] != "value" else None
            ),
            "explanation": str(result.get("explanation") or ""),
            "evaluation_trace": list(trace_identity),
            "mode": str(context.get("mode") or "online"),
            "metric_profile": (
                f"{definition['definition_id']}"
                f"@{result.get('metric_version') or definition['version']}"
            ),
            "projection_version": projection_version,
        }
        external_id = compute_otel_external_id(
            project="n-agent",
            result_id=str(result["result_id"]),
            projection_version=projection_version,
        )
        return {
            "outcome": "deliverable",
            "external_id": external_id,
            "payload": payload,
        }

    async def send(self, payload: Mapping[str, Any]) -> str:
        """重发已冻结 payload（emit_result 语义，见模块 docstring）。"""
        trace_raw = payload.get("evaluation_trace") or (None, None)
        evaluation_trace = (
            trace_raw[0] if len(trace_raw) > 0 else None,
            trace_raw[1] if len(trace_raw) > 1 else None,
        )
        outcome = emit_result(
            result_id=payload["result_id"],
            metric_name=payload["metric_name"],
            definition_id=payload["definition_id"],
            definition_version=payload["definition_version"],
            status=payload["status"],
            target=payload.get("target") or {},
            typed_value=payload.get("typed_value"),
            skip_reason=payload.get("skip_reason"),
            error_type=payload.get("error_type"),
            explanation=payload.get("explanation") or "",
            evaluation_trace=evaluation_trace,
            mode=payload.get("mode") or "online",
            metric_profile=payload.get("metric_profile"),
        )
        if outcome == "emitted":
            # external_id 不在冻结 payload 内（权威依据在投递行）：确定性
            # UUIDv5 直接从 payload 字段重算，与 project 结果一致
            return compute_otel_external_id(
                project="n-agent",
                result_id=str(payload["result_id"]),
                projection_version=str(
                    payload.get("projection_version") or "v1"
                ),
            )
        if outcome == "noop":
            raise OtelSinkDeadLetterError(
                "OTel 禁用（logger provider 为 no-op），投递保留待恢复出口"
            )
        raise OtelSinkRetryableError(
            "OTel logger.emit 同步失败（SDK 瞬时状态），退避重试"
        )

    async def read_back(self, external_id: str) -> Mapping[str, Any] | None:
        # 无回读能力：调度器经 supports_readback=False 跳过 verified 轮询
        return None

    async def verify(
        self, payload: Mapping[str, Any], read_back: Mapping[str, Any] | None
    ) -> bool:
        return False
