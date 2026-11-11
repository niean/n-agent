# app/application/evaluation_telemetry.py
"""评估遥测：gen_ai.evaluation.result 事件、evaluation.run Trace 与
nagent.evaluation.window/producer Gauge（spec 12.1/12.3，plan T10）。

本模块是统一评估闭环的 OTel 遥测出口，仅依赖标准库、opentelemetry-api
与 app.application.telemetry_semantics（复用其 get_meter/get_tracer 与
span 属性脱敏注入），不 import infrastructure/interfaces；存储访问经
只读查询端口（duck-typed，见 refresh_gauges_from_store）由组合根注入
（T16/T20 接线，本模块不自行接线）。

语义边界（spec 12.1，字段集锁定为 n-eval-v1）：
- 结果事件为 OTLP LogRecord：``gen_ai.evaluation.name`` 必填；numeric /
  boolean 发送有限数值（boolean 0/1），categorical 发送 label；explanation
  仅发送短脱敏摘要（脱敏先于限长，复用 telemetry_semantics 的脱敏注入）。
- skipped / error 无 score 字段，携带 ``nagent.evaluation.status`` /
  ``nagent.evaluation.skip_reason`` 与固定 ``error.type`` 扩展。
- 异步评分不修改已结束的业务 span：Evaluation Worker 建立独立
  ``evaluation.run`` Trace，经 Link 指向真实业务目标身份；结果 LogRecord
  以原生 trace_id/span_id 上下文字段关联被评估目标（即使目标 span 已
  结束），另以 nagent.evaluation.evaluation_trace_id/span_id 保存评估
  Trace 引用；无有效目标身份时记录当前评估 Trace 并显式
  ``target_link=missing``。不使用 gen_ai.response.id 冒充 OTel span_id，
  该字段不在 n-eval-v1 字段集内（写入即拒绝）。
- completion_time 是评估时间（LogRecord timestamp）；target_completed_at
  是业务结束时间，独立字段，不回填为同一时间。
- Resource.service.name=n-agent 表示实际生产评估事件的服务（组合根
  LoggerProvider 接线契约，由 LoggerProvider Resource 承载）；被评估者
  （如 n-kb）以 ``nagent.evaluation.target.service`` 表达，不得冒充
  N-KB 发出 OTel。
- Judge 实际 LLM 调用是 evaluation.run 的子 generation：在
  evaluation_run_scope 作用域内经 telemetry_semantics 发起的 chat span
  自然成为其子 span；缺失模型响应 ID 时省略 gen_ai.response.id，绝不
  伪造（该字段在字段集之外）。

指标（spec 12.3）：
- ``nagent.evaluation.window.*`` 八个 ObservableGauge 从账本 current 视图
  的最近 24h evaluated_at 窗口计算快照；质量标签只允许 target.service /
  metric_profile / mode / status；metric_profile 来自最多
  ``MAX_METRIC_PROFILES`` 个活跃定义版本注册表，未注册（或显式停用）的
  分组不导出，不自动产生无限时间序列。
- ``nagent.evaluation.producer.*`` 五个累计高水位 Gauge 无标签（跨
  producer/counter epoch 汇总，覆盖率口径为"自当前 counter epoch 起"），
  与 24h 评分分子分列，不混算。
- dataset_id/case_id/result_id/run_id/session_id/trace_id/模型任意名称/
  query/KB 文档 ID 一律不进入指标标签（测试以哨兵断言锁定）。
- worker 调用数/耗时、投递失败用常规 Counter/Histogram；队列深度用
  Gauge（观测专属，不与质量窗口混算）。名字与单位的 Prometheus 映射见
  docker/observability/metric-name-mapping.md。

Gauge 回调不 await 也不执行阻塞 DB 查询：后台刷新
（``refresh_gauges_from_store``）读取注入 store 的只读 current 快照写入
线程安全的 ``EvaluationSnapshotHolder``，回调只读内存；每个
service/profile/mode 组的导出由单一 owner 进程执行（T20 装配时保证，
CLI 不重复导出）。OTel 禁用（proxy provider）时全部为 no-op，不发布、
不抛错。observability 侧失败与业务隔离：发射失败返回 "failed"，绝不向
评估主流程传播异常。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

from opentelemetry import trace
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.metrics import Observation
from opentelemetry.trace import (
    Link,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)

from app.application import telemetry_semantics as ts
from app.domain.evaluation.state import SkipReason

# ---------------------------------------------------------------------------
# n-eval-v1 字段集（spec 12.1：实施提交锁定，依赖升级不得悄然改变字段）
# ---------------------------------------------------------------------------

EVAL_SCHEMA_VERSION = "n-eval-v1"

EVAL_EVENT_NAME = "gen_ai.evaluation.result"
EVALUATION_SPAN_NAME = "evaluation.run"

# 官方 GenAI 评估事件语义仍是 Development；锁定引用与内容投影（与
# telemetry_semantics.SEMCONV_VERSION 单源对齐），内容 hash 覆盖本模块
# 实现的官方字段投影（合同夹具 tests/application/fixtures/
# evaluation_telemetry_n_eval_v1.json 同步保存，测试复算校验）。
GENAI_SEMCONV_REFERENCE = ts.SEMCONV_VERSION

OFFICIAL_GENAI_EVALUATION_CONTENT: Mapping[str, Any] = {
    "event": EVAL_EVENT_NAME,
    "state": "Development",
    "attributes": {
        "gen_ai.evaluation.name": "required; evaluator/metric name",
        "gen_ai.evaluation.score.value": (
            "numeric/boolean emitted as finite number (boolean 0/1)"
        ),
        "gen_ai.evaluation.score.label": "categorical label",
        "gen_ai.evaluation.explanation": "short redacted summary only",
    },
    "project_extensions": "nagent.evaluation.*",
    "reference": GENAI_SEMCONV_REFERENCE,
}


def _official_content_hash(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


GENAI_SEMCONV_CONTENT_HASH = _official_content_hash(
    OFFICIAL_GENAI_EVALUATION_CONTENT
)

# 结果 LogRecord 的封闭字段集；未注册字段写入即拒绝（ValueError）。
_EVAL_SCHEMA_FIELDS: frozenset[str] = frozenset({
    "event.name",
    "gen_ai.evaluation.name",
    "gen_ai.evaluation.score.value",
    "gen_ai.evaluation.score.label",
    "gen_ai.evaluation.explanation",
    "nagent.evaluation.status",
    "nagent.evaluation.skip_reason",
    "error.type",
    "nagent.evaluation.result_id",
    "nagent.evaluation.target.service",
    "nagent.evaluation.target.kind",
    "nagent.evaluation.target.subject_id",
    "nagent.evaluation.target.subject_revision",
    "nagent.evaluation.target_link",
    "nagent.evaluation.target_completed_at",
    "nagent.evaluation.evaluation_trace_id",
    "nagent.evaluation.evaluation_span_id",
    "nagent.evaluation.metric_profile",
    "nagent.evaluation.mode",
})

EVAL_SCHEMA_HASH = hashlib.sha256(
    "\n".join(sorted(_EVAL_SCHEMA_FIELDS)).encode("utf-8")
).hexdigest()

# spec 13：explanation 全量上限 2000 UTF-8 字节；OTel 事件只发短摘要
#（脱敏先于限长），此处取更紧的进程内摘要上限。
EVALUATION_EXPLANATION_MAX_BYTES: int = 512

# 结果终态（job 侧词汇；result 行 status 为 value/skipped，error 由 job
# 终态表达，不落 result 行）
EVAL_RESULT_STATUSES: tuple[str, ...] = ("succeeded", "skipped", "error")

# 受信有界词汇（超出收敛 other；不白名单化任意标识）
TARGET_SERVICES: frozenset[str] = frozenset({"n-agent", "n-kb"})
EVAL_MODES: frozenset[str] = frozenset({"online", "execute", "replay"})
# 抽样 cohort（#24 分离维度）：random=随机抽样、failure=故障定向；空串
# （旧行/实验/人评样本）与未知值收敛 other，故障定向系列不混入随机系列
EVAL_COHORTS: frozenset[str] = frozenset({"random", "failure"})
EVAL_ERROR_TYPES: frozenset[str] = frozenset({
    "timeout", "format_error", "deadline_exceeded", "attempt_cap_exceeded",
    "budget_exhausted", "network", "rate_limit", "auth", "conflict",
    "dead_letter", "worker_error", "not_projected", "judge_error",
    "cancelled", "other",
})
EVALUATION_SINKS: frozenset[str] = frozenset({"langfuse", "otel"})
WORKER_OUTCOMES: frozenset[str] = frozenset({"succeeded", "skipped", "error"})

_WINDOW_COUNT_MEASURES: tuple[str, ...] = (
    "succeeded", "skipped", "error", "unlinked",
)
_WINDOW_VALUE_MEASURES: tuple[str, ...] = (
    "numeric_sum", "numeric_count", "boolean_pass", "boolean_count",
)
WINDOW_MEASURES: tuple[str, ...] = _WINDOW_COUNT_MEASURES + _WINDOW_VALUE_MEASURES
PRODUCER_MEASURES: tuple[str, ...] = (
    "eligible", "selected", "captured", "capture_error", "queue_full",
)

INSTRUMENT_WINDOW: dict[str, str] = {
    measure: f"nagent.evaluation.window.{measure}" for measure in WINDOW_MEASURES
}
INSTRUMENT_PRODUCER: dict[str, str] = {
    measure: f"nagent.evaluation.producer.{measure}" for measure in PRODUCER_MEASURES
}
INSTRUMENT_WORKER_CALLS = "nagent.evaluation.worker.calls"
INSTRUMENT_WORKER_DURATION = "nagent.evaluation.worker.duration"
INSTRUMENT_WORKER_QUEUE_DEPTH = "nagent.evaluation.worker.queue_depth"
INSTRUMENT_DELIVERY_FAILURES = "nagent.evaluation.delivery.failures"

NAGENT_EVALUATION_OUTCOME = "nagent.evaluation.outcome"
NAGENT_EVALUATION_SINK = "nagent.evaluation.sink"
NAGENT_EVALUATION_SAMPLING_COHORT = "nagent.evaluation.sampling_cohort"

_WINDOW_HOURS_DEFAULT = 24

_MAX_NAME_LENGTH = 64
_MAX_ID_LENGTH = 128

LOGGER_NAME = "nagent.application.evaluation_telemetry"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounded mappers（与 telemetry_semantics 的安全映射同一模式）
# ---------------------------------------------------------------------------


def _bounded(value: object, known: frozenset[str]) -> str:
    if not isinstance(value, str):
        return "other"
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        return "other"
    return cleaned if cleaned in known else "other"


def map_target_service(service: object) -> str:
    return _bounded(service, TARGET_SERVICES)


def map_mode(mode: object) -> str:
    return _bounded(mode, EVAL_MODES)


def map_sampling_cohort(cohort: object) -> str:
    return _bounded(cohort, EVAL_COHORTS)


def map_eval_error_type(error_type: object) -> str:
    return _bounded(error_type, EVAL_ERROR_TYPES)


def map_sink(sink: object) -> str:
    return _bounded(sink, EVALUATION_SINKS)


def map_worker_outcome(outcome: object) -> str:
    return _bounded(outcome, WORKER_OUTCOMES)


def _safe_id(value: object) -> str:
    if isinstance(value, str):
        mapped = ts._safe_id(value)
        if mapped is not None:
            return mapped
    return "other"


# ---------------------------------------------------------------------------
# 字段校验
# ---------------------------------------------------------------------------


def build_eval_attributes(fields: Mapping[str, Any]) -> dict[str, Any]:
    """按 n-eval-v1 封闭字段集校验并投影属性；未注册字段拒绝写入。"""
    unknown = [key for key in fields if key not in _EVAL_SCHEMA_FIELDS]
    if unknown:
        raise ValueError(
            f"未注册的评估字段（n-eval-v1={EVAL_SCHEMA_VERSION} "
            f"schema={EVAL_SCHEMA_HASH[:12]}）: {unknown}"
        )
    return dict(fields)


# ---------------------------------------------------------------------------
# Tracer / meter / logger 访问（fresh 代理；经 telemetry_semantics 单源）
# ---------------------------------------------------------------------------


def get_tracer():
    return ts.get_tracer()


def get_meter():
    return ts.get_meter()


def get_logger():
    """返回真实（非 proxy/noop）logger；OTel 禁用（proxy provider）返回
    None。

    判定顺序（对上游实现耦合最小化，spec 12.1 禁用路径不发布不抛错）：
    1. 公开 API 面 ``NoOpLoggerProvider``（``opentelemetry._logs`` 导出）
       按 isinstance 判定；
    2. api 内部 ``ProxyLoggerProvider``（``opentelemetry._logs._internal``，
       未注册 SDK provider 且无自动装配 env 时的缺省全局 provider）按
       isinstance 判定——该类不在公开导出面上，属于上游私有布局耦合
       （当前钉 opentelemetry 1.45.1，合同夹具同步锁定），上游重命名时
       由夹具版本哨兵暴露；
    3. 导入失败时回退类型名匹配（``ProxyLoggerProvider`` /
       ``_ProxyLoggerProvider``）并保留同一耦合备注。
    """
    try:
        from opentelemetry._logs import NoOpLoggerProvider, get_logger_provider

        provider = get_logger_provider()
    except Exception:
        return None
    noop_types: list[type] = [NoOpLoggerProvider]
    try:
        from opentelemetry._logs._internal import ProxyLoggerProvider

        noop_types.append(ProxyLoggerProvider)
    except Exception:
        pass  # 上游私有布局变化：回退下方类型名匹配
    if isinstance(provider, tuple(noop_types)):
        return None
    if type(provider).__name__ in ("ProxyLoggerProvider", "_ProxyLoggerProvider"):
        # 上游耦合回退（私有类名，随 opentelemetry 1.45.1 钉住）
        return None
    try:
        return provider.get_logger(LOGGER_NAME)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Redaction（脱敏先于限长；复用 telemetry_semantics 的脱敏注入）
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    try:
        sanitized = ts._sanitize_attributes({"_": text})
    except Exception:
        return ""
    value = sanitized.get("_")
    return value if isinstance(value, str) else ""


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """UTF-8 边界安全截断：不切断多字节字符。"""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _short_summary(text: object) -> str:
    if not isinstance(text, str) or not text:
        return ""
    summary = _redact(text).strip()
    return _truncate_utf8(summary, EVALUATION_EXPLANATION_MAX_BYTES)


# ---------------------------------------------------------------------------
# 目标身份（domain EvaluationTarget 或 Mapping 双形态支持）
# ---------------------------------------------------------------------------


def _target_field(target: Any, name: str) -> Any:
    if isinstance(target, Mapping):
        return target.get(name)
    return getattr(target, name, None)


def _is_hex_id(value: Any, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _valid_target_identity(target: Any) -> bool:
    return _is_hex_id(_target_field(target, "trace_id"), 32) and _is_hex_id(
        _target_field(target, "span_id"), 16
    )


# ---------------------------------------------------------------------------
# evaluation.run Trace（Evaluation Worker 作用域）
# ---------------------------------------------------------------------------


@dataclass
class EvaluationRunHandle:
    """评估执行 Trace 句柄：hex 身份供结果 LogRecord 关联保存。"""

    trace_id: str | None = None
    span_id: str | None = None
    is_valid: bool = False
    span: Any = None
    outcome: str | None = None


def _start_eval_span(name: str, links: Sequence[Link]):
    try:
        return get_tracer().start_span(
            name, kind=SpanKind.INTERNAL, links=list(links)
        )
    except Exception:
        return trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)


def _set_attr(span, key: str, value) -> None:
    try:
        for attr, sanitized in ts._sanitize_attributes({key: value}).items():
            try:
                span.set_attribute(attr, sanitized)
            except Exception:
                pass
    except Exception:
        pass


@contextmanager
def evaluation_run_scope(
    target: Any = None,
    *,
    metric_profile: str | None = None,
    mode: str = "online",
) -> Iterator[EvaluationRunHandle]:
    """独立 evaluation.run Trace 作用域（Evaluation Worker scope）。

    目标携带真实 trace_id/span_id 时经 Link 指向业务目标（异步评分不改写
    已结束业务 span）；无有效目标身份时不建 Link，并显式
    nagent.evaluation.target_link=missing。作用域内启动的 span（Judge LLM
    调用的 generation）成为本 Trace 的子 span。OTel 禁用时返回无效句柄，
    不抛错。
    """
    links: list[Link] = []
    link_state = "missing"
    if target is not None and _valid_target_identity(target):
        links = [
            Link(
                SpanContext(
                    trace_id=int(_target_field(target, "trace_id"), 16),
                    span_id=int(_target_field(target, "span_id"), 16),
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
            )
        ]
        link_state = "linked"
    span = _start_eval_span(EVALUATION_SPAN_NAME, links)
    context = span.get_span_context() if span is not None else None
    valid = bool(getattr(context, "is_valid", False))
    handle = EvaluationRunHandle(
        trace_id=format(context.trace_id, "032x") if valid else None,
        span_id=format(context.span_id, "016x") if valid else None,
        is_valid=valid,
        span=span,
    )
    _set_attr(span, "nagent.evaluation.operation", "evaluation.run")
    _set_attr(span, "nagent.evaluation.target_link", link_state)
    if target is not None:
        _set_attr(
            span,
            "nagent.evaluation.target.service",
            map_target_service(_target_field(target, "service")),
        )
        kind = _target_field(target, "kind")
        if kind:
            _set_attr(
                span, "nagent.evaluation.target.kind", str(kind)[:_MAX_NAME_LENGTH]
            )
        subject_id = _target_field(target, "subject_id")
        if subject_id:
            _set_attr(
                span,
                "nagent.evaluation.target.subject_id",
                _safe_id(str(subject_id)),
            )
    if metric_profile:
        _set_attr(
            span,
            "nagent.evaluation.metric_profile",
            str(metric_profile)[:_MAX_NAME_LENGTH],
        )
    _set_attr(span, "nagent.evaluation.mode", map_mode(mode))
    try:
        use = trace.use_span(
            span, end_on_exit=False, record_exception=False,
            set_status_on_exception=False,
        )
        use.__enter__()
    except Exception:
        use = None
    try:
        try:
            yield handle
        except BaseException as exc:
            handle.outcome = "error"
            try:
                ts._mark_span_error(span, exc)
            except Exception:
                pass
            raise
    finally:
        if use is not None:
            try:
                use.__exit__(None, None, None)
            except Exception:
                pass
        outcome = handle.outcome
        if outcome is not None:
            _set_attr(
                span, "nagent.evaluation.outcome", map_worker_outcome(outcome)
            )
            if outcome == "error":
                try:
                    span.set_status(Status(StatusCode.ERROR))
                except Exception:
                    pass
        try:
            span.end()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# gen_ai.evaluation.result LogRecord
# ---------------------------------------------------------------------------


def _finite_score(value: Any) -> float:
    """numeric/boolean -> 有限数值；bool 按 0/1；非有限数拒绝。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        score = float(value)
        if not math.isfinite(score):
            raise ValueError(f"score.value 必须有限: {value!r}")
        return score
    raise ValueError(f"score.value 必须是 numeric/boolean: {value!r}")


def _target_completed_at_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str) and value:
        return value[:512]
    return None


def current_evaluation_ids() -> tuple[str | None, str | None]:
    """当前上下文的 evaluation.run Trace 身份（hex）；不在评估作用域或
    OTel 禁用时返回 (None, None)。供 worker 提交 result 时冻结评估
    Trace 身份（#12 投影来源）与 emit_result 兜底共用。"""
    try:
        context = trace.get_current_span().get_span_context()
        if getattr(context, "is_valid", False):
            return (
                format(context.trace_id, "032x"),
                format(context.span_id, "016x"),
            )
    except Exception:
        pass
    return None, None


def emit_result(
    *,
    result_id: str,
    metric_name: str,
    definition_id: str,
    definition_version: str,
    status: str,
    target: Any,
    typed_value: Any = None,
    skip_reason: Any = None,
    error_type: str | None = None,
    explanation: str = "",
    target_completed_at: Any = None,
    evaluated_at: datetime | None = None,
    evaluation_trace: tuple[str | None, str | None] | None = None,
    mode: str = "online",
    metric_profile: str | None = None,
) -> str:
    """发布一条 ``gen_ai.evaluation.result`` LogRecord（OTLP Log 形态）。

    返回投递语义状态："emitted"（已交给 OTel logger）、"noop"（OTel
    禁用）或 "failed"（发射失败被隔离）。调用方（T16/T20 投递路径）据此
    记录 OTel pending/emitted/failed；本模块不回写任何存储，也绝不向评估
    主流程传播 observability 异常。

    原生 trace_id/span_id 上下文字段关联被评估目标（目标身份有效时），
    否则记录当前评估 Trace 并显式 target_link=missing；completion_time
    （timestamp）是评估时间，业务结束时间在
    nagent.evaluation.target_completed_at 独立保存。
    """
    if status not in EVAL_RESULT_STATUSES:
        raise ValueError(
            f"非法评估终态: {status!r}，仅允许 {EVAL_RESULT_STATUSES}"
        )
    # spec 12.1：gen_ai.evaluation.name 必填——空/空白名拒绝（应用层
    # 校验错误与字段集校验同一 ValueError 契约）
    metric_name_text = (
        metric_name.strip() if isinstance(metric_name, str) else ""
    )
    if not metric_name_text:
        raise ValueError(
            "gen_ai.evaluation.name 必填: metric_name 不能为空或空白"
        )
    if status == "succeeded" and typed_value is None:
        raise ValueError("succeeded 结果必须携带 typed_value")
    if status == "skipped" and skip_reason is None:
        raise ValueError("skipped 结果必须携带 skip_reason")
    if status == "error" and not error_type:
        raise ValueError("error 结果必须携带 error_type")

    profile = metric_profile or f"{definition_id}@{definition_version}"
    fields: dict[str, Any] = {
        "event.name": EVAL_EVENT_NAME,
        "gen_ai.evaluation.name": metric_name_text[:_MAX_NAME_LENGTH],
        "nagent.evaluation.status": status,
        "nagent.evaluation.result_id": str(result_id)[:_MAX_ID_LENGTH],
        "nagent.evaluation.target.service": map_target_service(
            _target_field(target, "service")
        ),
        "nagent.evaluation.target.kind": str(
            _target_field(target, "kind") or "other"
        )[:_MAX_NAME_LENGTH],
        "nagent.evaluation.target.subject_id": _safe_id(
            str(_target_field(target, "subject_id") or "other")
        ),
        "nagent.evaluation.target.subject_revision": _safe_id(
            str(_target_field(target, "subject_revision") or "")
        ),
        "nagent.evaluation.target_link": "missing",
        "nagent.evaluation.metric_profile": str(profile)[:_MAX_NAME_LENGTH],
        "nagent.evaluation.mode": map_mode(mode),
    }
    if status == "succeeded":
        if isinstance(typed_value, str):
            fields["gen_ai.evaluation.score.label"] = typed_value
        else:
            fields["gen_ai.evaluation.score.value"] = _finite_score(typed_value)
    if status == "skipped":
        reason = (
            skip_reason.value
            if isinstance(skip_reason, SkipReason)
            else str(skip_reason)
        )
        fields["nagent.evaluation.skip_reason"] = _bounded(
            reason, frozenset(item.value for item in SkipReason)
        )
    if status == "error":
        fields["error.type"] = map_eval_error_type(error_type)
    summary = _short_summary(explanation)
    if summary:
        fields["gen_ai.evaluation.explanation"] = summary
    completed_text = _target_completed_at_text(target_completed_at)
    if completed_text:
        fields["nagent.evaluation.target_completed_at"] = completed_text
    attributes = build_eval_attributes(fields)

    # 原生上下文：被评估目标身份优先；无有效目标身份时记录当前评估 Trace
    eval_trace_id, eval_span_id = evaluation_trace or (None, None)
    if not eval_trace_id and not eval_span_id:
        eval_trace_id, eval_span_id = current_evaluation_ids()
    if _valid_target_identity(target):
        native_trace_id = int(_target_field(target, "trace_id"), 16)
        native_span_id = int(_target_field(target, "span_id"), 16)
        attributes["nagent.evaluation.target_link"] = "linked"
    elif _is_hex_id(eval_trace_id, 32) and _is_hex_id(eval_span_id, 16):
        native_trace_id = int(eval_trace_id, 16)
        native_span_id = int(eval_span_id, 16)
    else:
        native_trace_id = None
        native_span_id = None
    if _is_hex_id(eval_trace_id, 32):
        attributes["nagent.evaluation.evaluation_trace_id"] = eval_trace_id
    if _is_hex_id(eval_span_id, 16):
        attributes["nagent.evaluation.evaluation_span_id"] = eval_span_id

    # 精确整数换算（不经 float 乘法：float64 尾数 2^53 在 ns 级会引入
    # ~百纳秒舍入误差）；completion_time 是评估时间。
    evaluated_dt = evaluated_at or datetime.now(timezone.utc)
    since_epoch = evaluated_dt.astimezone(timezone.utc) - datetime(
        1970, 1, 1, tzinfo=timezone.utc
    )
    timestamp_ns = (
        (since_epoch.days * 86_400 + since_epoch.seconds) * 1_000_000_000
        + since_epoch.microseconds * 1_000
    )
    severity = SeverityNumber.ERROR if status == "error" else SeverityNumber.INFO
    record = LogRecord(
        timestamp=timestamp_ns,
        observed_timestamp=timestamp_ns,
        trace_id=native_trace_id,
        span_id=native_span_id,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        severity_text=severity.name,
        severity_number=severity,
        body=EVAL_EVENT_NAME,
        attributes=attributes,
        event_name=EVAL_EVENT_NAME,
    )
    logger = get_logger()
    if logger is None:
        return "noop"
    try:
        logger.emit(record)
    except Exception:
        return "failed"
    return "emitted"


# ---------------------------------------------------------------------------
# metric_profile 注册表（<=16 活跃定义版本；超额须显式停用旧 profile）
# ---------------------------------------------------------------------------

MAX_METRIC_PROFILES = 16


class MetricProfileRegistry:
    """活跃 metric_profile 注册表（definition_id@version，上限 16）。

    超额注册返回 None（调用方必须先显式 ``deactivate`` 停用旧 profile 的
    观测导出），不自动淘汰，避免时间序列无界增长。注册表仅约束 Gauge
    导出分组（window.*），与结果投影/账本无耦合。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profiles: dict[str, None] = {}

    @staticmethod
    def profile(definition_id: str, definition_version: str) -> str:
        return f"{definition_id}@{definition_version}"

    def register(self, definition_id: str, definition_version: str) -> str | None:
        profile = self.profile(definition_id, definition_version)
        with self._lock:
            if profile in self._profiles:
                return profile
            if len(self._profiles) >= MAX_METRIC_PROFILES:
                return None
            self._profiles[profile] = None
            return profile

    def deactivate(self, profile: str) -> bool:
        with self._lock:
            existed = profile in self._profiles
            self._profiles.pop(profile, None)
            return existed

    def is_active(self, profile: str) -> bool:
        with self._lock:
            return profile in self._profiles

    def profiles(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._profiles)


# ---------------------------------------------------------------------------
# 快照持有 + Gauge（回调只读内存；后台刷新读 store）
# ---------------------------------------------------------------------------


class EvaluationSnapshotHolder:
    """线程安全的窗口/producer 快照持有者（Gauge 回调的唯一数据源）。

    ``update`` 由后台刷新（读 store 的 async 查询）调用；``get`` 由 Gauge
    回调调用，返回行级深拷贝（调用方拿到的是副本，篡改不影响 Gauge 数
    据源）。快照字段：window（分组行列表）、producer（累计计数）、
    queue_depth（采集队列深度，由捕获路径另行更新）；
    ``refresh_failures`` 是连续刷新失败计数（0 表示当前快照新鲜），
    由 ``refresh_gauges_from_store`` 在 store 查询失败时递增，使持续故障
    可观测而不是表现为冻结的 Gauge。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window: tuple[Mapping[str, Any], ...] = ()
        self._producer: Mapping[str, Any] = {}
        self._queue_depth = 0
        self._refresh_failures = 0

    def update(
        self,
        *,
        window_rows: Sequence[Mapping[str, Any]] | None = None,
        producer: Mapping[str, Any] | None = None,
        queue_depth: int | None = None,
    ) -> None:
        with self._lock:
            # window 仅在显式传入时覆盖：producer-only 合并更新（main
            # 刷新循环的 capture_counters 合并）不得清空窗口行（T20 回归）
            if window_rows is not None:
                self._window = tuple(dict(row) for row in window_rows)
            if producer is not None:
                self._producer = dict(producer)
            if queue_depth is not None:
                self._queue_depth = int(queue_depth)

    def record_refresh_failure(self) -> None:
        with self._lock:
            self._refresh_failures += 1

    def get(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                # 行级拷贝：调用方修改不影响 Gauge 数据源
                "window": tuple(dict(row) for row in self._window),
                "producer": dict(self._producer),
                "queue_depth": self._queue_depth,
                "refresh_failures": self._refresh_failures,
            }


def _window_attributes(row: Mapping[str, Any], status: str) -> dict[str, str]:
    return {
        "target.service": map_target_service(row.get("target_service")),
        "metric_profile": MetricProfileRegistry.profile(
            str(row.get("definition_id") or "other"),
            str(row.get("definition_version") or "0"),
        ),
        "mode": map_mode(row.get("mode")),
        # #24：随机/故障定向 cohort 分离维度（空串/未知收敛 other）
        "sampling_cohort": map_sampling_cohort(row.get("sampling_cohort")),
        "status": status,
    }


def _window_value(row: Mapping[str, Any], measure: str) -> float:
    key = f"{measure}_count" if measure in _WINDOW_COUNT_MEASURES else measure
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    numeric = float(value)
    return numeric if math.isfinite(numeric) else 0.0


class EvaluationGauges:
    """注册 nagent.evaluation.window.* / producer.* / queue_depth Gauge。

    构造时在注入（或 telemetry_semantics 默认）meter 上注册 Observable
    Gauge；回调只读快照持有者的内存数据，不做任何 IO。OTel 禁用（proxy
    meter）时注册为 no-op，不抛错。
    """

    def __init__(
        self,
        *,
        snapshot: EvaluationSnapshotHolder,
        registry: MetricProfileRegistry | None = None,
        meter: Any = None,
    ) -> None:
        self._snapshot = snapshot
        self._registry = registry
        # 注册错误向上传播（组合根装配失败应显式暴露，不静默吞掉导致
        # Gauge 缺失）；OTel 禁用路径的不抛错要求由 proxy meter 的
        # no-op instrument 满足，无需在此捕获。
        meter = meter or get_meter()
        for measure, name in INSTRUMENT_WINDOW.items():
            meter.create_observable_gauge(
                name,
                unit="1",
                callbacks=[self._make_window_callback(measure)],
            )
        for measure, name in INSTRUMENT_PRODUCER.items():
            meter.create_observable_gauge(
                name,
                unit="{job}",
                callbacks=[self._make_producer_callback(measure)],
            )
        meter.create_observable_gauge(
            INSTRUMENT_WORKER_QUEUE_DEPTH,
            unit="{job}",
            callbacks=[self._queue_depth_callback],
        )

    # -- callbacks ---------------------------------------------------------

    def _make_window_callback(self, measure: str) -> Callable[[Any], list]:
        def _callback(_options) -> list[Observation]:
            try:
                active = (
                    self._registry.profiles() if self._registry is not None else None
                )
                rows = self._snapshot.get().get("window") or ()
                status = (
                    measure if measure in _WINDOW_COUNT_MEASURES else "succeeded"
                )
                observations = []
                for row in rows:
                    profile = MetricProfileRegistry.profile(
                        str(row.get("definition_id") or "other"),
                        str(row.get("definition_version") or "0"),
                    )
                    if active is not None and profile not in active:
                        continue
                    observations.append(
                        Observation(
                            value=_window_value(row, measure),
                            attributes=_window_attributes(row, status),
                        )
                    )
                return observations
            except Exception:
                return []

        return _callback

    def _make_producer_callback(self, measure: str) -> Callable[[Any], list]:
        def _callback(_options) -> list[Observation]:
            try:
                producer = self._snapshot.get().get("producer") or {}
                value = producer.get(measure)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return []
                return [Observation(value=float(value))]
            except Exception:
                return []

        return _callback

    def _queue_depth_callback(self, _options) -> list[Observation]:
        try:
            return [
                Observation(
                    value=float(self._snapshot.get().get("queue_depth") or 0)
                )
            ]
        except Exception:
            return []


async def refresh_gauges_from_store(
    holder: EvaluationSnapshotHolder,
    store: Any,
    *,
    now: datetime | None = None,
    window_hours: int = _WINDOW_HOURS_DEFAULT,
) -> None:
    """后台刷新：读取注入 store 的只读 current 快照并更新持有者。

    ``store`` 需提供两个只读查询（SQLiteEvaluationStore 已实现）：
    ``query_window_snapshot(*, window_start) -> list[Mapping]`` 与
    ``query_producer_snapshot() -> Mapping``。``window_start`` 传
    datetime（含非 UTC 时区），由 store 以 ``_iso`` 归一化为 UTC ISO
    字符串——调用方自拼字符串会因本地时区产生字典序错位的窗口边界。

    刷新失败被隔离且可观测：快照保持上一次成功值，失败计数经
    ``holder.record_refresh_failure`` 递增并输出 debug 日志（持续故障
    表现为 refresh_failures 递增，而不是冻结的 Gauge 静默失真）。
    """
    try:
        start = (now or datetime.now(timezone.utc)) - timedelta(
            hours=window_hours
        )
        rows = await store.query_window_snapshot(window_start=start)
        producer = await store.query_producer_snapshot()
    except Exception as exc:
        holder.record_refresh_failure()
        try:
            logger.debug("evaluation gauge refresh failed: %s", exc)
        except Exception:
            pass
        return
    holder.update(window_rows=rows, producer=producer)


# ---------------------------------------------------------------------------
# worker / 投递常规指标（Counter / Histogram；队列深度见 Gauge）
# ---------------------------------------------------------------------------

_instrument_cache: dict[str, Any] = {}
_instrument_cache_lock = threading.Lock()


def _get_instrument(
    factory: Callable[[str, str], Any], name: str, unit: str
) -> Any:
    """按名取缓存 instrument，未命中经 ``factory`` 创建；禁用路径
    （factory 返回 None 或抛错）返回 None 且不缓存，避免以 no-op 覆盖
    provider 就绪后的真实 instrument。"""
    with _instrument_cache_lock:
        cached = _instrument_cache.get(name)
    if cached is not None:
        return cached
    try:
        handle = factory(name, unit)
    except Exception:
        return None
    if handle is None:
        return None
    with _instrument_cache_lock:
        return _instrument_cache.setdefault(name, handle)


def record_worker_call(seconds: float, outcome: str) -> None:
    """记录一次评估 worker 作业处理（调用数 Counter + 耗时 Histogram）。"""
    dimensions = {NAGENT_EVALUATION_OUTCOME: map_worker_outcome(outcome)}
    counter = _get_instrument(
        lambda name, unit: get_meter().create_counter(name, unit=unit),
        INSTRUMENT_WORKER_CALLS,
        "{call}",
    )
    if counter is not None:
        try:
            counter.add(
                1, ts.guard_dimensions(INSTRUMENT_WORKER_CALLS, dimensions)
            )
        except Exception:
            pass
    duration = _get_instrument(
        lambda name, unit: get_meter().create_histogram(name, unit=unit),
        INSTRUMENT_WORKER_DURATION,
        "s",
    )
    if duration is not None and isinstance(seconds, (int, float)):
        try:
            duration.record(
                max(0.0, float(seconds)),
                ts.guard_dimensions(INSTRUMENT_WORKER_DURATION, dimensions),
            )
        except Exception:
            pass


def record_delivery_failure(sink: str, error_type: str) -> None:
    """记录一次结果投递失败（sink 维度有界：langfuse/otel/other）。"""
    dimensions = {
        NAGENT_EVALUATION_SINK: map_sink(sink),
        "error.type": map_eval_error_type(error_type),
    }
    counter = _get_instrument(
        lambda name, unit: get_meter().create_counter(name, unit=unit),
        INSTRUMENT_DELIVERY_FAILURES,
        "{failure}",
    )
    if counter is not None:
        try:
            counter.add(
                1,
                ts.guard_dimensions(INSTRUMENT_DELIVERY_FAILURES, dimensions),
            )
        except Exception:
            pass
