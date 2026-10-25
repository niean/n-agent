"""Telemetry semantics registry -- Application layer.

Central, pure mapping of N-Agent observability semantics onto the OTel
GenAI semantic conventions draft (GenAI-first policy). This module only
depends on the standard library and ``opentelemetry-api``; it never
imports business modules, the SDK, exporters, or instrumentation
packages, and it never mutates business behavior: span contextmanagers
isolate observability-side failures while letting business exceptions
(including ``CancelledError`` / ``KeyboardInterrupt``) propagate as-is.

Pinned semconv version: SEMCONV_VERSION =
"semantic-conventions-genai main @ 2026-10-24 (Development)". The GenAI
conventions are in Development state; when the draft renames attributes
or instruments, remap them centrally here -- instrumented call sites in
business code must not change.

Convention rules implemented here:
- One canonical name per signal. GenAI-defined signals (agent invoke,
  LLM chat, tool execution, token usage) use only GenAI names; there are
  no ``nagent.*`` equivalent helpers for them.
- ``nagent.*`` only supplements GenAI-uncovered project signals (turn
  source/end reason, iteration, context compression, budget denial,
  risk level, policy outcome).
- Untrusted strings (model, tool, provider, source, outcome, error.type)
  are mapped through bounded fixed sets / registries with a length cap;
  unknown values collapse to ``other``; span names are never built by
  raw concatenation.
- Metric dimension cardinality is guarded per instrument: at most 999
  normal series + 1 fixed overflow series per instrument, counted
  process-wide across all readers.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Dict, Iterator, Optional

from app.application.telemetry_details import (
    project_details, validate_details, flatten_details, StreamDetailsAccumulator,
)

from opentelemetry import trace
from opentelemetry import metrics
from opentelemetry.trace import SpanKind, Status, StatusCode

SEMCONV_VERSION = "semantic-conventions-genai main @ 2026-10-24 (Development)"

DEFAULT_AGENT_NAME = "n-agent"

_TRACER_NAME = f"{DEFAULT_AGENT_NAME}.application.telemetry_semantics"
_METER_NAME = f"{DEFAULT_AGENT_NAME}.application.telemetry_semantics"

_MAX_NAME_LENGTH = 64
_MAX_REGISTERED_TOOLS = 256
_MAX_REGISTERED_MODELS = 256


# ---------------------------------------------------------------------------
# GenAI attribute constants (pinned to the draft above)
# ---------------------------------------------------------------------------

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_TOKEN_MODALITY = "gen_ai.token.modality"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_REQUEST_STREAM = "gen_ai.request.stream"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"


# ---------------------------------------------------------------------------
# nagent.* supplement attributes (GenAI-uncovered project signals only)
# ---------------------------------------------------------------------------

NAGENT_SOURCE = "nagent.source"
NAGENT_END_REASON = "nagent.end_reason"
NAGENT_ITERATION = "nagent.iteration"
NAGENT_RUN_ID = "nagent.run_id"
NAGENT_POLICY_OUTCOME = "nagent.policy_outcome"
NAGENT_RISK_LEVEL = "nagent.risk_level"
NAGENT_USAGE_KNOWN = "nagent.usage_known"
NAGENT_BEFORE_TOKENS = "nagent.before_tokens"
NAGENT_AFTER_TOKENS = "nagent.after_tokens"
NAGENT_OUTCOME = "nagent.outcome"
NAGENT_KIND = "nagent.kind"
NAGENT_SUMMARY = "nagent.summary"
NAGENT_DURATION_MS = "nagent.duration_ms"
NAGENT_MESSAGE_COUNT = "nagent.message_count"
NAGENT_INFERENCE_CALLS = "nagent.inference_calls"
NAGENT_TOOL_CALLS = "nagent.tool_calls"
NAGENT_EXECUTED = "nagent.executed"

EXECUTION_LOG_TEMPLATE = "execution_completed"
logger = logging.getLogger(__name__)
_execution_ids = contextvars.ContextVar("nagent_execution_ids", default=None)
_completion = contextvars.ContextVar("nagent_completion", default=None)
_message_count = contextvars.ContextVar("nagent_message_count", default=None)

# Live execution scene: current operation-scoped facts published by each
# instrumented scope (Spec-261027). asyncio tasks copy the context, so
# concurrent sessions never see each other's scene.
_scene_var: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "nagent_scene", default=None
)

# Facts that must NOT leak into a nested operation's scene (each new
# operation masks them and only publishes its own). Session/request/source
# stay authoritative in ``_execution_ids``; iteration/message_count cascade
# down (they describe the current step, not the operation boundary).
_SCENE_OPERATION_MASK = frozenset({
    "operation", "model", "provider", "stream", "tool", "call_id",
    "risk_level", "policy_outcome", "executed", "outcome", "finish_reason",
    "summary", "error_type", "end_reason", "usage_known",
    "inference_calls", "tool_calls", "input_tokens", "output_tokens",
})


def _safe_id(value):
    return value if isinstance(value, str) and len(value) <= 128 and re.fullmatch(r"[\w.:/-]+", value) else None


def execution_log_snapshot() -> dict:
    return {
        **(_scene_var.get() or {}),
        **(_completion.get() or {}),
        **(_execution_ids.get() or {}),
    }


def _scene_set(overrides: Dict[str, object], *, mask: bool) -> contextvars.Token:
    """Publish a new scene for the current scope.

    With ``mask`` (new operation boundary) the parent's operation facts are
    dropped so a child never inherits e.g. the parent's tool/model; otherwise
    the parent scene is kept (iteration/step scopes). ``None`` overrides are
    skipped. Returns a token; resetting it at scope exit restores the parent
    (intermediate re-publishes without a stored token ride along)."""
    parent = _scene_var.get() or {}
    if mask:
        scene = {k: v for k, v in parent.items() if k not in _SCENE_OPERATION_MASK}
    else:
        scene = dict(parent)
    scene.update({k: v for k, v in overrides.items() if v is not None})
    return _scene_var.set(scene)


def _scene_publish(overrides: Dict[str, object]) -> None:
    """Re-publish scene with ``overrides`` mid-scope (business-confirmed facts
    such as executed/policy_outcome/usage). The scope's enter token still
    restores the parent on exit."""
    current = _scene_var.get() or {}
    current.update({k: v for k, v in overrides.items() if v is not None})
    _scene_var.set(current)


def _complete(span, operation, start, *, details=None, **facts):
    """Emit a fixed structural description only while the completed span is current."""
    try:
        projected = validate_details(details)
        if projected:
            facts['details'] = projected
            for key, value in flatten_details(projected).items():
                try:
                    span.set_attribute(key, value)
                except Exception:
                    pass
        facts = {k: v for k, v in facts.items() if v is not None}
        facts.update(operation=operation, duration_ms=max(0.0, (time.monotonic() - start) * 1000))
        duration = facts["duration_ms"]
        outcome = facts.get("outcome", "other")
        tokens = f"输入 Token {facts.get('input_tokens', '未知')}，输出 Token {facts.get('output_tokens', '未知')}"
        if operation == "invoke_agent":
            summary = (f"Agent 执行：{outcome}，结束原因 {facts.get('end_reason', 'other')}，"
                       f"输入消息 {facts.get('message_count', '未知')} 条，模型调用 {facts.get('inference_calls', 0)} 次，"
                       f"工具调用 {facts.get('tool_calls', 0)} 次；{tokens}；耗时 {duration:.1f} ms。")
        elif operation == "chat":
            summary = (f"模型调用 {facts.get('model', 'other')}（{facts.get('provider', 'other')}）：{outcome}，"
                       f"上下文消息 {facts.get('message_count', '未知')} 条，结束原因 {facts.get('finish_reason', '未知')}；"
                       f"{tokens}；耗时 {duration:.1f} ms。")
        elif operation == "context_compression":
            summary = (f"上下文压缩：{outcome}，Token {facts.get('before_tokens', '未知')} → "
                       f"{facts.get('after_tokens', '未知')}；耗时 {duration:.1f} ms。")
        else:
            summary = (f"工具执行 {facts.get('tool', 'other')}：{outcome}，"
                       f"执行状态 {'已执行' if facts.get('executed') else '未执行'}，"
                       f"策略 {facts.get('policy_outcome', 'other')}；耗时 {duration:.1f} ms。")
        summary = summary[:512]
        facts["summary"] = summary
        attributes = {
            "summary": NAGENT_SUMMARY, "duration_ms": NAGENT_DURATION_MS,
            "message_count": NAGENT_MESSAGE_COUNT, "inference_calls": NAGENT_INFERENCE_CALLS,
            "tool_calls": NAGENT_TOOL_CALLS, "executed": NAGENT_EXECUTED,
            "outcome": NAGENT_OUTCOME, "end_reason": NAGENT_END_REASON,
            "usage_known": NAGENT_USAGE_KNOWN, "input_tokens": GEN_AI_USAGE_INPUT_TOKENS,
            "output_tokens": GEN_AI_USAGE_OUTPUT_TOKENS, "error_type": "error.type",
        }
        for key, attribute in attributes.items():
            if key in facts:
                _set_attr(span, attribute, facts[key])
        token = _completion.set(facts)
        try:
            logger.info(EXECUTION_LOG_TEMPLATE)
        finally:
            _completion.reset(token)
    except Exception:
        pass



class RiskLevel(str, Enum):
    """Fixed risk-level enum, aligned with the Domain RiskLevel values."""

    SAFE = "safe"
    CONFIRM = "confirm"
    DANGEROUS = "dangerous"

    @classmethod
    def from_value(cls, value: object) -> Optional["RiskLevel"]:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                return None
        return None


# ---------------------------------------------------------------------------
# Instruments (spec metric table, item by item)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    name: str
    kind: str  # "counter" | "histogram"
    unit: str
    allowed_dimensions: tuple


INSTRUMENT_INFERENCE_INPUT_TOKENS = "gen_ai.client.inference.usage.input_tokens"
INSTRUMENT_INFERENCE_OUTPUT_TOKENS = "gen_ai.client.inference.usage.output_tokens"
INSTRUMENT_OPERATION_INPUT_TOKENS = "gen_ai.client.inference.operation.input_tokens"
INSTRUMENT_OPERATION_OUTPUT_TOKENS = "gen_ai.client.inference.operation.output_tokens"
INSTRUMENT_INFERENCE_DURATION = "gen_ai.client.inference.duration"
INSTRUMENT_EXECUTE_TOOL_DURATION = "gen_ai.execute_tool.duration"
INSTRUMENT_INVOKE_AGENT_DURATION = "gen_ai.invoke_agent.duration"
INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS = "gen_ai.invoke_agent.inference_calls"
INSTRUMENT_INVOKE_AGENT_TOOL_CALLS = "gen_ai.invoke_agent.tool_calls"
INSTRUMENT_TURNS = "nagent.turns"
INSTRUMENT_CONTEXT_COMPRESSIONS = "nagent.context.compressions"
INSTRUMENT_BUDGET_DENIED = "nagent.budget.denied"


INSTRUMENTS: Dict[str, Instrument] = {
    inst.name: inst
    for inst in (
        Instrument(
            name=INSTRUMENT_INFERENCE_INPUT_TOKENS,
            kind="counter",
            unit="{token}",
            allowed_dimensions=(
                GEN_AI_OPERATION_NAME,
                GEN_AI_PROVIDER_NAME,
                GEN_AI_TOKEN_MODALITY,
                GEN_AI_REQUEST_MODEL,
            ),
        ),
        Instrument(
            name=INSTRUMENT_INFERENCE_OUTPUT_TOKENS,
            kind="counter",
            unit="{token}",
            allowed_dimensions=(
                GEN_AI_OPERATION_NAME,
                GEN_AI_PROVIDER_NAME,
                GEN_AI_TOKEN_MODALITY,
                GEN_AI_REQUEST_MODEL,
            ),
        ),
        Instrument(
            name=INSTRUMENT_OPERATION_INPUT_TOKENS,
            kind="histogram",
            unit="{token}",
            allowed_dimensions=(
                GEN_AI_OPERATION_NAME,
                GEN_AI_PROVIDER_NAME,
                GEN_AI_REQUEST_MODEL,
                "error.type",
            ),
        ),
        Instrument(
            name=INSTRUMENT_OPERATION_OUTPUT_TOKENS,
            kind="histogram",
            unit="{token}",
            allowed_dimensions=(
                GEN_AI_OPERATION_NAME,
                GEN_AI_PROVIDER_NAME,
                GEN_AI_REQUEST_MODEL,
                "error.type",
            ),
        ),
        Instrument(
            name=INSTRUMENT_INFERENCE_DURATION,
            kind="histogram",
            unit="s",
            allowed_dimensions=(
                GEN_AI_OPERATION_NAME,
                GEN_AI_PROVIDER_NAME,
                GEN_AI_REQUEST_MODEL,
                "error.type",
            ),
        ),
        Instrument(
            name=INSTRUMENT_EXECUTE_TOOL_DURATION,
            kind="histogram",
            unit="s",
            allowed_dimensions=(
                GEN_AI_TOOL_NAME,
                GEN_AI_TOOL_TYPE,
                GEN_AI_AGENT_NAME,
                "error.type",
            ),
        ),
        Instrument(
            name=INSTRUMENT_INVOKE_AGENT_DURATION,
            kind="histogram",
            unit="s",
            allowed_dimensions=(GEN_AI_AGENT_NAME, "error.type"),
        ),
        Instrument(
            name=INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS,
            kind="histogram",
            unit="{inference_call}",
            allowed_dimensions=(GEN_AI_AGENT_NAME,),
        ),
        Instrument(
            name=INSTRUMENT_INVOKE_AGENT_TOOL_CALLS,
            kind="histogram",
            unit="{tool_call}",
            allowed_dimensions=(GEN_AI_AGENT_NAME,),
        ),
        Instrument(
            name=INSTRUMENT_TURNS,
            kind="counter",
            unit="1",
            allowed_dimensions=(NAGENT_SOURCE, NAGENT_END_REASON),
        ),
        Instrument(
            name=INSTRUMENT_CONTEXT_COMPRESSIONS,
            kind="counter",
            unit="1",
            allowed_dimensions=(NAGENT_OUTCOME,),
        ),
        Instrument(
            name=INSTRUMENT_BUDGET_DENIED,
            kind="counter",
            unit="1",
            allowed_dimensions=(NAGENT_KIND,),
        ),
    )
}


# Histogram bucket boundaries per the GenAI conventions.

TOKEN_HISTOGRAM_BOUNDARIES = (
    1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144,
    1048576, 4194304, 16777216, 67108864,
)

# Duration histogram boundaries, explicitly declared and aligned with the
# locked SDK's default seconds layout (not left implicit).
DURATION_HISTOGRAM_BOUNDARIES = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75,
    1.0, 2.5, 5.0, 7.5, 10.0,
)


# ---------------------------------------------------------------------------
# Bounded safe mappings (fixed sets + length cap; unknown -> "other")
# ---------------------------------------------------------------------------

_KNOWN_MODELS = frozenset({
    "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3", "o4-mini",
    "claude-sonnet-4", "claude-opus-4", "claude-3-5-haiku",
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5",
    "claude-sonnet-4-5", "claude-opus-4-5",
    "qwen2.5", "qwen2.5-coder", "qwen3", "deepseek-v4-flash",
})

_KNOWN_PROVIDERS = frozenset({
    "openai", "anthropic", "google", "azure", "bedrock", "ollama",
})

_KNOWN_SOURCES = frozenset({
    "api", "cli", "acp", "dashboard", "chat", "http", "task", "schedule",
    "feishu", "dingtalk", "wecom", "curator",
})

_KNOWN_OUTCOMES = frozenset({
    "compressed", "skipped", "failed", "success", "error",
    "denied", "cancelled",
})

# Turn end reasons are their own bounded signal vocabulary, distinct from
# generic outcome values (spec: nagent.turns dims are source/end_reason).
_KNOWN_END_REASONS = frozenset({
    "completed", "max_iterations", "error", "cancelled",
    "budget_denied", "approval_denied", "user_stopped",
    # Values emitted by _END_REASON_TO_TELEMETRY (agent_graph): the Domain
    # EndReason vocabulary that finish_reason cannot distinguish.
    "length", "deadline", "tool_calls",
})

_KNOWN_KINDS = frozenset({
    "budget_exhausted", "budget_exceeded", "per_run_exhausted",
    "sandbox_denied", "llm_denied",
})

_KNOWN_ERROR_TYPES = frozenset({
    "TimeoutError", "ConnectionError", "CancelledError", "ValueError",
    "ValidationError", "RateLimitError", "BadRequestError",
    "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "PolicyDenied",
    # OpenAI / Anthropic SDK transport & status exceptions (both packages
    # share these names); production provider failures must stay diagnosable
    # instead of collapsing to "other".
    "APIError", "APIConnectionError", "APITimeoutError", "APIStatusError",
    "APIResponseValidationError", "InternalServerError", "ConflictError",
    "UnprocessableEntityError", "OverloadedError", "RequestTooLargeError",
    # Safe sentinels (NOT real exception classes): a tool that finished with
    # an error-status result but no exception, and a tool denied before
    # execution. Bounded, low-cardinality, and distinguishable from both
    # successes and real exception types.
    "tool_error", "denied",
})

_KNOWN_TOOLS = frozenset({
    "web_search", "read_file", "write_file", "terminal", "skill_invoke",
    "artifact_save", "artifact_read", "mcp_tool", "memory_write",
    "memory_read", "browser_navigate", "browser_action",
    # builtin / agent tool surface shipped by this project
    "calculator", "get_current_time", "list_directory", "read_text_file",
    "web_fetch", "vision_analyze", "search_knowledge", "skill_manage",
    "manage_schedule", "schedule_query", "execute_code", "host_terminal",
    "delegate_agents", "external_memory", "multi_external_memory",
})

_DATE_SUFFIX_RE = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")

_registered_tools: set[str] = set()
_registered_tools_lock = threading.Lock()

_registered_models: set[str] = set()
_registered_models_lock = threading.Lock()


def _safe_known(value: object, known: frozenset) -> str:
    """Map ``value`` through a fixed set with a length cap; else ``other``."""
    if not isinstance(value, str):
        return "other"
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        return "other"
    return cleaned if cleaned in known else "other"


def map_model_name(model: object) -> str:
    if not isinstance(model, str):
        return "other"
    cleaned = model.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        return "other"
    if cleaned in _KNOWN_MODELS:
        return cleaned
    base = _DATE_SUFFIX_RE.sub("", cleaned)
    if base in _KNOWN_MODELS:
        return base
    with _registered_models_lock:
        if cleaned in _registered_models:
            return cleaned
    if base != cleaned:
        with _registered_models_lock:
            if base in _registered_models:
                return base
    return "other"


def map_provider_name(provider: object) -> str:
    return _safe_known(provider, _KNOWN_PROVIDERS)


def map_source(source: object) -> str:
    return _safe_known(source, _KNOWN_SOURCES)


def map_outcome(outcome: object) -> str:
    return _safe_known(outcome, _KNOWN_OUTCOMES)


def map_end_reason(end_reason: object) -> str:
    return _safe_known(end_reason, _KNOWN_END_REASONS)


def map_kind(kind: object) -> str:
    return _safe_known(kind, _KNOWN_KINDS)


def map_error_type(error_type: object) -> str:
    return _safe_known(error_type, _KNOWN_ERROR_TYPES)


def register_tool_name(tool: str) -> bool:
    """Register a dynamic tool name into the bounded tool registry.

    The registry is capped (``_MAX_REGISTERED_TOOLS``); when full the
    name stays unknown and collapses to ``other``.
    """
    if not isinstance(tool, str):
        return False
    cleaned = tool.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        return False
    with _registered_tools_lock:
        if cleaned in _registered_tools:
            return True
        if len(_registered_tools) >= _MAX_REGISTERED_TOOLS:
            return False
        _registered_tools.add(cleaned)
        return True


def unregister_tool_name(tool: str) -> None:
    if not isinstance(tool, str):
        return
    with _registered_tools_lock:
        _registered_tools.discard(tool.strip())


def map_tool_name(tool: object) -> str:
    if not isinstance(tool, str):
        return "other"
    cleaned = tool.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        return "other"
    if cleaned in _KNOWN_TOOLS:
        return cleaned
    with _registered_tools_lock:
        if cleaned in _registered_tools:
            return cleaned
    return "other"


def register_model_name(model: str) -> bool:
    """Register a configured model name into the bounded model registry.

    Mirror of ``register_tool_name``: configured provider models are registered
    at the composition root so ``map_model_name`` can emit the real value
    instead of collapsing to ``other``. Deliberately NOT auto-registered on
    every chat attempt -- arbitrary request-supplied model strings must stay
    bounded/unregistered (collapsing to ``other``) rather than inflating the
    registry. Cap ``_MAX_REGISTERED_MODELS``; when full, unknown -> ``other``.
    """
    if not isinstance(model, str):
        return False
    cleaned = model.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        return False
    with _registered_models_lock:
        if cleaned in _registered_models:
            return True
        if len(_registered_models) >= _MAX_REGISTERED_MODELS:
            return False
        _registered_models.add(cleaned)
        return True


def unregister_model_name(model: str) -> None:
    if not isinstance(model, str):
        return
    with _registered_models_lock:
        _registered_models.discard(model.strip())


# ---------------------------------------------------------------------------
# Span name helpers (bounded mapping; never raw concatenation)
# ---------------------------------------------------------------------------


def invoke_agent_span_name() -> str:
    return f"invoke_agent {DEFAULT_AGENT_NAME}"


def chat_span_name(model: object) -> str:
    return f"chat {map_model_name(model)}"


def execute_tool_span_name(tool: object) -> str:
    return f"execute_tool {map_tool_name(tool)}"


def compress_span_name() -> str:
    return "nagent.context.compress"


# ---------------------------------------------------------------------------
# Tracer / meter accessors (fresh API proxies; no permanent no-op caching)
# ---------------------------------------------------------------------------


def get_tracer():
    return trace.get_tracer(_TRACER_NAME)


def get_meter():
    return metrics.get_meter(_METER_NAME)


# ---------------------------------------------------------------------------
# Dimension cardinality guard
# ---------------------------------------------------------------------------

_MAX_SERIES_PER_INSTRUMENT = 1000
_MAX_NORMAL_SERIES = _MAX_SERIES_PER_INSTRUMENT - 1
_OVERFLOW_SERIES_KEY = ("__nagent_overflow__",)

# Reserved overflow dimension key (double-underscore namespaced so it can
# never collide with a real dimension).
OVERFLOW_DIMENSIONS: Dict[str, str] = {"__nagent.overflow__": "true"}

_dimension_series: Dict[str, set] = {}
_dimension_guard_lock = threading.Lock()


def guard_dimensions(instrument_name: str, dimensions: Dict[str, str]) -> Dict[str, str]:
    """Guard per-instrument dimension combinations.

    At most 999 distinct normal series + 1 fixed overflow series per
    instrument, counted per process across all readers. Over-cap (or
    already-overflowed) combinations merge into ``OVERFLOW_DIMENSIONS``.
    """
    key = tuple(sorted(dimensions.items()))
    with _dimension_guard_lock:
        series = _dimension_series.setdefault(instrument_name, set())
        if key in series:
            return dict(dimensions)
        if _OVERFLOW_SERIES_KEY in series or len(series) >= _MAX_NORMAL_SERIES:
            # Cap reached (999 normal series): merge into the fixed
            # overflow series for the rest of this process's lifetime.
            series.add(_OVERFLOW_SERIES_KEY)
            return dict(OVERFLOW_DIMENSIONS)
        series.add(key)
        return dict(dimensions)


def dimension_series_count(instrument_name: str) -> int:
    with _dimension_guard_lock:
        return len(_dimension_series.get(instrument_name, ()))


def _reset_dimension_guard_for_tests() -> None:
    with _dimension_guard_lock:
        _dimension_series.clear()


def _reset_registries_for_tests() -> None:
    """Test-only reset of the bounded name registries (tools + models).

    Used by tests that need full access to the registry capacity (e.g. the
    execute-tool dimension-overflow test registers 256 fresh names), where a
    real process would already hold live registrations.
    """
    with _registered_tools_lock:
        _registered_tools.clear()
    with _registered_models_lock:
        _registered_models.clear()


# ---------------------------------------------------------------------------
# Span contextmanagers (observability failure isolated, business exceptions
# propagate untouched; no exception.message/stacktrace recording)
# ---------------------------------------------------------------------------


class _NoopSpan(trace.NonRecordingSpan):
    """Non-recording fallback span (subclass of the API's own type)."""

    def set_attribute(self, *_args, **_kwargs) -> None:  # pragma: no cover
        pass

    def set_status(self, *_args, **_kwargs) -> None:  # pragma: no cover
        pass


_span_attr_sanitizer = None
_span_attr_sanitizer_lock = threading.Lock()


def configure_span_attribute_sanitizer(callable_) -> None:
    """Inject a synchronous str->str sanitizer for dynamic span attributes.

    Composition-root only (spec-261027). The callable receives a bare string
    and returns a redacted string; until injected, dynamic strings are written
    unchanged (no identity fallback can be installed at runtime). Passing
    ``None`` (or a non-callable) clears it."""
    global _span_attr_sanitizer
    with _span_attr_sanitizer_lock:
        _span_attr_sanitizer = callable_ if callable(callable_) else None


def _set_attr(span, key: str, value) -> None:
    try:
        if isinstance(value, str):
            with _span_attr_sanitizer_lock:
                sanitizer = _span_attr_sanitizer
            if sanitizer is not None:
                value = sanitizer(value)
                if not isinstance(value, str):
                    # A non-str return violates the contract; drop the attr so
                    # a bad/adversarial sanitizer can never inject data.
                    return
        span.set_attribute(key, value)
    except Exception:  # observability-only failure must never affect business
        pass


def _safe_error_type(exc: BaseException) -> str:
    return map_error_type(type(exc).__name__)


def _mark_span_error(span, exc: BaseException) -> None:
    """Record a safe error type only -- no messages, no stacktraces."""
    try:
        span.set_status(Status(StatusCode.ERROR))
        span.set_attribute("error.type", _safe_error_type(exc))
    except Exception:
        pass


def _start_span(name: str, kind: SpanKind):
    try:
        span = get_tracer().start_span(name, kind=kind)
        ids = _execution_ids.get() or {}
        for key, attribute in (("session_id", GEN_AI_CONVERSATION_ID), ("request_id", NAGENT_RUN_ID), ("source", NAGENT_SOURCE)):
            if key in ids:
                _set_attr(span, attribute, ids[key])
        return span
    except Exception:
        return _NoopSpan()


@contextmanager
def _span_scope(span):
    """Make ``span`` current without ending it (end is owned by the caller)."""
    use = None
    try:
        use = trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False)
        use.__enter__()
    except Exception:
        use = None
    try:
        yield
    finally:
        if use is not None:
            try:
                use.__exit__(None, None, None)
            except Exception:
                pass


def _end_span(span) -> None:
    try:
        span.end()
    except Exception:
        pass


@contextmanager
def invoke_agent_span(
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    run_id: Optional[str] = None,
    agent: str = DEFAULT_AGENT_NAME,
) -> Iterator[object]:
    span = _start_span(invoke_agent_span_name(), SpanKind.INTERNAL)
    _set_attr(span, GEN_AI_OPERATION_NAME, "invoke_agent")
    _set_attr(span, GEN_AI_AGENT_NAME, agent)
    if session_id:
        _set_attr(span, GEN_AI_CONVERSATION_ID, _safe_id(session_id) or "other")
    if source:
        _set_attr(span, NAGENT_SOURCE, map_source(source))
    if run_id:
        _set_attr(span, NAGENT_RUN_ID, _safe_id(run_id) or "other")
    try:
        with _span_scope(span):
            yield span
    except BaseException as exc:
        _mark_span_error(span, exc)
        raise
    finally:
        _end_span(span)


@contextmanager
def chat_span(
    model: str,
    provider: Optional[str] = None,
    operation: str = "chat",
    stream: Optional[bool] = None,
    response_model: Optional[str] = None,
    iteration: Optional[int] = None,
) -> Iterator[object]:
    span = _start_span(chat_span_name(model), SpanKind.CLIENT)
    _set_attr(span, GEN_AI_OPERATION_NAME, operation)
    if provider:
        _set_attr(span, GEN_AI_PROVIDER_NAME, map_provider_name(provider))
    _set_attr(span, GEN_AI_REQUEST_MODEL, map_model_name(model))
    if response_model:
        _set_attr(span, GEN_AI_RESPONSE_MODEL, map_model_name(response_model))
    if stream is not None:
        _set_attr(span, GEN_AI_REQUEST_STREAM, stream)
    if iteration is not None:
        _set_attr(span, NAGENT_ITERATION, iteration)
    try:
        with _span_scope(span):
            yield span
    except BaseException as exc:
        _mark_span_error(span, exc)
        raise
    finally:
        _end_span(span)


@contextmanager
def execute_tool_span(
    tool: str,
    call_id: Optional[str] = None,
    agent: str = DEFAULT_AGENT_NAME,
    tool_type: str = "function",
    iteration: Optional[int] = None,
    risk_level: Optional[str] = None,
    policy_outcome: Optional[str] = None,
) -> Iterator[object]:
    span = _start_span(execute_tool_span_name(tool), SpanKind.INTERNAL)
    _set_attr(span, GEN_AI_TOOL_NAME, map_tool_name(tool))
    if call_id:
        _set_attr(span, GEN_AI_TOOL_CALL_ID, _safe_id(call_id) or "other")
    _set_attr(span, GEN_AI_TOOL_TYPE, tool_type)
    _set_attr(span, GEN_AI_AGENT_NAME, agent)
    if iteration is not None:
        _set_attr(span, NAGENT_ITERATION, iteration)
    if risk_level is not None:
        level = RiskLevel.from_value(risk_level)
        _set_attr(span, NAGENT_RISK_LEVEL, level.value if level else "other")
    if policy_outcome:
        _set_attr(span, NAGENT_POLICY_OUTCOME, policy_outcome)
    try:
        with _span_scope(span):
            yield span
    except BaseException as exc:
        _mark_span_error(span, exc)
        raise
    finally:
        _end_span(span)


@contextmanager
def compress_span(
    before_tokens: Optional[int] = None,
    after_tokens: Optional[int] = None,
    outcome: str = "compressed",
) -> Iterator[object]:
    # GenAI has no context-compression convention; this is the project
    # supplement signal (single canonical name).
    span = _start_span(compress_span_name(), SpanKind.INTERNAL)
    _set_attr(span, NAGENT_OUTCOME, map_outcome(outcome))
    if before_tokens is not None:
        _set_attr(span, NAGENT_BEFORE_TOKENS, before_tokens)
    if after_tokens is not None:
        _set_attr(span, NAGENT_AFTER_TOKENS, after_tokens)
    try:
        with _span_scope(span):
            yield span
    except BaseException as exc:
        _mark_span_error(span, exc)
        raise
    finally:
        _end_span(span)


# ---------------------------------------------------------------------------
# Metric record helpers (guarded dimensions, observability-only isolation)
# ---------------------------------------------------------------------------


# Instrument handle cache: API proxy instruments re-resolve after the real
# meter provider is set (T3), so caching the handles is safe while tracer/
# meter lookups stay fresh. Only instrument objects are cached.
_instrument_cache: Dict[str, object] = {}
_instrument_cache_lock = threading.Lock()


def _get_instrument(inst: Instrument):
    with _instrument_cache_lock:
        cached = _instrument_cache.get(inst.name)
    if cached is not None:
        return cached
    meter = get_meter()
    if inst.kind == "counter":
        handle = meter.create_counter(inst.name, unit=inst.unit)
    else:
        boundaries = (
            TOKEN_HISTOGRAM_BOUNDARIES
            if inst.unit == "{token}"
            else DURATION_HISTOGRAM_BOUNDARIES
        )
        try:
            # Pinned opentelemetry-api 1.45.1 kwarg name.
            handle = meter.create_histogram(
                inst.name,
                unit=inst.unit,
                explicit_bucket_boundaries_advisory=boundaries,
            )
        except TypeError:  # older OTel API without the kwarg
            handle = meter.create_histogram(inst.name, unit=inst.unit)
    with _instrument_cache_lock:
        return _instrument_cache.setdefault(inst.name, handle)


def _record(instrument_name: str, value, dimensions: Dict[str, str]) -> None:
    try:
        inst = INSTRUMENTS[instrument_name]
        guarded = guard_dimensions(instrument_name, dimensions)
        handle = _get_instrument(inst)
        if inst.kind == "counter":
            handle.add(value, guarded)
        else:
            handle.record(value, guarded)
    except Exception:
        pass


def _error_dim(error_type: Optional[str]) -> Dict[str, str]:
    """Dimensions for error.type; the key is omitted when there is no error.

    ``None`` is an invalid OTel attribute value and would make a real SDK
    record() raise, silently dropping success-path metrics.
    """
    if error_type is None:
        return {}
    return {"error.type": map_error_type(error_type)}


def record_turn(source: object, end_reason: object) -> None:
    _record(
        INSTRUMENT_TURNS,
        1,
        {NAGENT_SOURCE: map_source(source), NAGENT_END_REASON: map_end_reason(end_reason)},
    )


def record_context_compression(outcome: object) -> None:
    _record(INSTRUMENT_CONTEXT_COMPRESSIONS, 1, {NAGENT_OUTCOME: map_outcome(outcome)})


def record_budget_denied(kind: object) -> None:
    _record(INSTRUMENT_BUDGET_DENIED, 1, {NAGENT_KIND: map_kind(kind)})


def record_inference_duration(
    model: object,
    provider: object,
    operation: str,
    seconds: float,
    error_type: Optional[str] = None,
) -> None:
    _record(
        INSTRUMENT_INFERENCE_DURATION,
        seconds,
        {
            GEN_AI_OPERATION_NAME: operation,
            GEN_AI_PROVIDER_NAME: map_provider_name(provider),
            GEN_AI_REQUEST_MODEL: map_model_name(model),
            **_error_dim(error_type),
        },
    )


def record_execute_tool_duration(
    tool: object,
    tool_type: str,
    agent: str,
    seconds: float,
    error_type: Optional[str] = None,
) -> None:
    _record(
        INSTRUMENT_EXECUTE_TOOL_DURATION,
        seconds,
        {
            GEN_AI_TOOL_NAME: map_tool_name(tool),
            GEN_AI_TOOL_TYPE: tool_type,
            GEN_AI_AGENT_NAME: agent,
            **_error_dim(error_type),
        },
    )


def record_invoke_agent_durations_and_calls(
    agent: str,
    seconds: float,
    error_type: Optional[str],
    inference_calls: int,
    tool_calls: int,
) -> None:
    error_dims = _error_dim(error_type)
    _record(INSTRUMENT_INVOKE_AGENT_DURATION, seconds, {GEN_AI_AGENT_NAME: agent, **error_dims})
    _record(
        INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS,
        inference_calls,
        {GEN_AI_AGENT_NAME: agent},
    )
    _record(
        INSTRUMENT_INVOKE_AGENT_TOOL_CALLS,
        tool_calls,
        {GEN_AI_AGENT_NAME: agent},
    )


def record_inference_tokens(
    model: object,
    provider: object,
    operation: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    error_type: Optional[str] = None,
    modality: Optional[str] = None,
) -> None:
    """Record token usage.

    Only known, non-negative components are recorded -- a missing usage
    is never fabricated as 0 (callers mark ``nagent.usage_known=false``
    on the span instead).
    """
    base = {
        GEN_AI_OPERATION_NAME: operation,
        GEN_AI_PROVIDER_NAME: map_provider_name(provider),
        GEN_AI_REQUEST_MODEL: map_model_name(model),
    }
    if modality is not None:
        base[GEN_AI_TOKEN_MODALITY] = modality
    op_dims = dict(base)
    op_dims.update(_error_dim(error_type))
    if input_tokens is not None and input_tokens >= 0:
        _record(INSTRUMENT_INFERENCE_INPUT_TOKENS, input_tokens, dict(base))
        _record(INSTRUMENT_OPERATION_INPUT_TOKENS, input_tokens, op_dims)
    if output_tokens is not None and output_tokens >= 0:
        _record(INSTRUMENT_INFERENCE_OUTPUT_TOKENS, output_tokens, dict(base))
        _record(INSTRUMENT_OPERATION_OUTPUT_TOKENS, output_tokens, op_dims)


# ---------------------------------------------------------------------------
# Run context (ContextVar): per-run stats + current iteration
#
# asyncio tasks copy the context at creation, so values set inside one run
# are invisible to concurrently running sessions. Resident background tasks
# must clear the inherited context via ``clear_run_context`` so they never
# attribute their work to a user session.
# ---------------------------------------------------------------------------


@dataclass
class _RunStats:
    inference_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    input_known: bool = True
    output_known: bool = True


_iteration_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "nagent_iteration", default=None
)
_run_stats_var: contextvars.ContextVar[Optional[_RunStats]] = contextvars.ContextVar(
    "nagent_run_stats", default=None
)


def current_iteration() -> Optional[int]:
    return _iteration_var.get()


def current_run_stats() -> Optional[_RunStats]:
    return _run_stats_var.get()


@contextmanager
def iteration_scope(iteration: Optional[int], message_count=None) -> Iterator[None]:
    token = _iteration_var.set(iteration)
    count_token = _message_count.set(message_count)
    scene_token = _scene_set(
        {"iteration": iteration, "message_count": message_count}, mask=False
    )
    try:
        yield
    finally:
        _iteration_var.reset(token)
        _message_count.reset(count_token)
        _scene_var.reset(scene_token)


@contextmanager
def run_stats_scope(stats: _RunStats) -> Iterator[None]:
    token = _run_stats_var.set(stats)
    try:
        yield
    finally:
        _run_stats_var.reset(token)


def clear_run_context() -> None:
    """Detach the current task from any inherited run/iteration context."""
    _iteration_var.set(None)
    _run_stats_var.set(None)
    _execution_ids.set(None)
    _completion.set(None)
    _message_count.set(None)
    _scene_var.set(None)


# ---------------------------------------------------------------------------
# Chat attempt (one per provider round-trip; the caller owns entering it)
# ---------------------------------------------------------------------------


class NoopChatAttempt:
    """No-op replacement returned when telemetry is disabled."""

    def set_input(self, messages, tools=None, system=None):
        pass

    def set_output(self, message, incomplete=False):
        pass

    def append_output_text(self, text):
        pass

    def append_output_tool_delta(self, index, id=None, name=None, arguments=None):
        pass

    def set_error_detail(self, text):
        pass

    def mark_output_incomplete(self):
        pass

    def set_usage(self, input_tokens, output_tokens) -> None:  # noqa: D401
        pass

    def set_finish_reasons(self, reasons) -> None:
        pass

    def set_response_model(self, model) -> None:
        pass

    def set_error(self, error_type) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


_NOOP_CHAT_ATTEMPT = NoopChatAttempt()


class ChatAttempt:
    """One LLM round-trip attempt: one chat span + duration/token metrics.

    Usage (Infrastructure provider adapters)::

        with callback(model=..., stream=..., provider="openai") as attempt:
            ... call SDK ...
            attempt.set_usage(prompt_tokens, completion_tokens)

    Missing usage components are never fabricated as 0; each component's
    knownness is judged separately (``nagent.usage_known`` is true only
    when BOTH components are known).
    """

    def __init__(
        self,
        *,
        model: str,
        stream: Optional[bool],
        provider: Optional[str] = "openai",
        operation: str = "chat",
    ):
        self._model = model
        self._provider = provider
        self._operation = operation
        self._stream = stream
        self._span = None
        self._span_cm = None
        self._start = 0.0
        self._input_tokens: Optional[int] = None
        self._output_tokens: Optional[int] = None
        self._error_type: Optional[str] = None
        self._finish_reason = None
        self._details = {}
        self._output_accumulator = None
        self._output_incomplete = False

    def __enter__(self) -> "ChatAttempt":
        self._span_cm = chat_span(
            self._model,
            provider=self._provider,
            operation=self._operation,
            stream=self._stream,
            iteration=current_iteration(),
        )
        self._span = self._span_cm.__enter__()
        self._start = time.monotonic()
        self._scene_token = _scene_set(
            {"operation": self._operation, "model": map_model_name(self._model),
             "provider": map_provider_name(self._provider), "stream": self._stream},
            mask=True,
        )
        stats = current_run_stats()
        if stats is not None:
            stats.inference_calls += 1
        return self

    def set_input(self, messages, tools=None, system=None):
        try:
            ordinary = messages
            if system is None and isinstance(messages, (list, tuple)):
                system = [m for m in messages if isinstance(m, dict) and m.get('role') in ('system', 'developer')]
                ordinary = [m for m in messages if not (isinstance(m, dict) and m.get('role') in ('system', 'developer'))]
            fields = dict(input_messages=ordinary)
            if system:
                fields['system_prompt'] = system
            if tools is not None:
                fields['tool_definitions'] = tools
            self._details.update(project_details(**fields))
        except Exception:
            pass

    def set_output(self, message, incomplete=False):
        try:
            details = project_details(output_messages=message)
            if incomplete and details:
                details['output_messages']['incomplete'] = True
            self._details.update(details)
        except Exception:
            pass

    def mark_output_incomplete(self):
        self._output_incomplete = True

    def append_output_text(self, text):
        try:
            if self._output_accumulator is None:
                self._output_accumulator = StreamDetailsAccumulator()
            self._output_accumulator.append_text(text)
        except Exception:
            pass

    def append_output_tool_delta(self, index, id=None, name=None, arguments=None):
        try:
            if self._output_accumulator is None:
                self._output_accumulator = StreamDetailsAccumulator()
            self._output_accumulator.append_tool_delta(index, id=id, name=name, arguments=arguments)
        except Exception:
            pass

    def set_error_detail(self, text):
        try:
            self._details.update(project_details(error=text))
        except Exception:
            pass

    def set_usage(self, input_tokens, output_tokens) -> None:
        if isinstance(input_tokens, int) and input_tokens >= 0:
            self._input_tokens = input_tokens
        if isinstance(output_tokens, int) and output_tokens >= 0:
            self._output_tokens = output_tokens

    def set_finish_reasons(self, reasons) -> None:
        if reasons is None:
            return
        values = reasons if isinstance(reasons, (list, tuple)) else [reasons]
        known = frozenset({"stop", "length", "tool_calls", "function_call", "content_filter", "end_turn", "max_tokens", "tool_use", "stop_sequence", "pause_turn", "refusal"})
        self._finish_reason = ",".join(_safe_known(value, known) for value in values[:8])
        if self._span is not None:
            _set_attr(self._span, GEN_AI_RESPONSE_FINISH_REASONS, self._finish_reason)
        _scene_publish({"finish_reason": self._finish_reason})

    def set_response_model(self, model) -> None:
        if model and self._span is not None:
            _set_attr(self._span, GEN_AI_RESPONSE_MODEL, map_model_name(model))
        _scene_publish({"response_model": map_model_name(model) if model else None})

    def set_error(self, error_type) -> None:
        self._error_type = error_type
        _scene_publish({"error_type": map_error_type(error_type) if error_type else None})

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            elapsed = time.monotonic() - self._start
            if self._span is not None:
                if self._input_tokens is not None:
                    _set_attr(self._span, GEN_AI_USAGE_INPUT_TOKENS, self._input_tokens)
                if self._output_tokens is not None:
                    _set_attr(self._span, GEN_AI_USAGE_OUTPUT_TOKENS, self._output_tokens)
                _set_attr(
                    self._span,
                    NAGENT_USAGE_KNOWN,
                    self._input_tokens is not None and self._output_tokens is not None,
                )
                if self._error_type is not None:
                    _set_attr(self._span, "error.type", map_error_type(self._error_type))
            error_type = self._error_type
            if error_type is None and exc is not None:
                error_type = type(exc).__name__
            stats = current_run_stats()
            if stats is not None:
                stats.input_known &= self._input_tokens is not None
                stats.output_known &= self._output_tokens is not None
                stats.input_tokens += self._input_tokens or 0
                stats.output_tokens += self._output_tokens or 0
            try:
                if exc is not None:
                    self.set_error_detail(str(exc))
            except Exception:
                pass
            try:
                if self._stream and self._output_accumulator is None and 'output_messages' not in self._details:
                    self._output_accumulator = StreamDetailsAccumulator()
                if self._output_accumulator is not None:
                    self._details.update(self._output_accumulator.finish(incomplete=exc is not None or error_type is not None or self._output_incomplete))
            except Exception:
                pass
            finally:
                self._output_accumulator = None
            _complete(self._span, "chat", self._start, details=self._details,
                      model=map_model_name(self._model), provider=map_provider_name(self._provider),
                      iteration=current_iteration(), message_count=_message_count.get(), stream=self._stream,
                      finish_reason=self._finish_reason, input_tokens=self._input_tokens,
                      output_tokens=self._output_tokens,
                      usage_known=self._input_tokens is not None and self._output_tokens is not None,
                      outcome="cancelled" if isinstance(exc, asyncio.CancelledError) else "error" if error_type else "success",
                      error_type=map_error_type(error_type) if error_type else None)
            record_inference_duration(
                self._model, self._provider, self._operation, elapsed, error_type
            )
            record_inference_tokens(
                self._model,
                self._provider,
                self._operation,
                self._input_tokens,
                self._output_tokens,
                error_type,
            )
        except Exception:
            pass  # observability-only failure must never affect business
        finally:
            if self._span_cm is not None:
                try:
                    self._span_cm.__exit__(exc_type, exc, tb)
                except BaseException:
                    pass
            try:
                _scene_var.reset(getattr(self, "_scene_token", None))
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------------
# Telemetry facade (Application-facing; injected by the composition root)
# ---------------------------------------------------------------------------


class NoopTelemetry:
    """Default facade when observability is disabled: every hook is a no-op."""

    def agent_run(self, session_id=None, source=None, run_id=None, message_count=None):
        handle = SimpleNamespace(end_reason=None, outcome=None, details={})
        return _nullcontext_yielding(handle)

    def tool_execution(self, tool, call_id=None, risk_level=None):
        return _nullcontext_yielding(
            {"policy_outcome": None, "outcome": None, "executed": False}
        )

    def compression(self):
        return _nullcontext_yielding(
            {"outcome": None, "before_tokens": None, "after_tokens": None}
        )

    def record_tool_denial(self, tool, call_id=None, risk_level=None, reason=None, arguments=None, result=None):
        pass

    def record_budget_denied(self, kind):
        pass

    def iteration(self, iteration, message_count=None):
        return _nullcontext_yielding(None)

    def begin_chat_attempt(self, *, model, stream, provider="openai", operation="chat"):
        return _NOOP_CHAT_ATTEMPT


@contextmanager
def _nullcontext_yielding(value):
    yield value


class SemanticsTelemetry:
    """Semantic-conventions facade used by Application-layer call sites.

    All hooks isolate observability failures and never alter business
    behavior: business exceptions (including ``CancelledError``) propagate
    untouched, spans are closed exactly once in ``finally``, and every
    metric goes through the bounded safe mappings.
    """

    # -- agent run ---------------------------------------------------------

    @contextmanager
    def agent_run(self, session_id=None, source=None, run_id=None, message_count=None):
        stats = _RunStats()
        token = _run_stats_var.set(stats)
        ids_token = _execution_ids.set({k: v for k, v in {
            "session_id": _safe_id(session_id), "request_id": _safe_id(run_id),
            "source": map_source(source),
        }.items() if v is not None})
        iteration_token = _iteration_var.set(None)
        count_token = _message_count.set(None)
        scene_token = _scene_set(
            {"operation": "invoke_agent", "message_count": message_count}, mask=True
        )
        start = time.monotonic()
        handle = SimpleNamespace(end_reason=None, outcome=None, details={})
        exc = None
        try:
            with invoke_agent_span(session_id=session_id, source=source, run_id=run_id) as span:
                try:
                    yield handle
                except BaseException as caught:
                    exc = caught
                    raise
                finally:
                    end_reason = map_end_reason(handle.end_reason or (
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "error" if exc else "completed"))
                    error_type = _safe_error_type(exc) if exc else None
                    if isinstance(exc, asyncio.CancelledError) or end_reason == "cancelled":
                        outcome = "cancelled"
                    elif exc is not None:
                        outcome = "error"
                    elif handle.outcome is not None:
                        outcome = map_outcome(handle.outcome)
                    elif end_reason == "error":
                        outcome = "error"
                    else:
                        outcome = "success"
                    _complete(span, "invoke_agent", start, details={**handle.details, **(project_details(error=str(exc)) if exc else {})}, message_count=message_count,
                              inference_calls=stats.inference_calls, tool_calls=stats.tool_calls,
                              input_tokens=stats.input_tokens if stats.input_known else None,
                              output_tokens=stats.output_tokens if stats.output_known else None,
                              usage_known=stats.input_known and stats.output_known,
                              end_reason=end_reason,
                              outcome=outcome,
                              error_type=error_type)
                    try:
                        record_turn(source, end_reason)
                        record_invoke_agent_durations_and_calls(DEFAULT_AGENT_NAME, time.monotonic() - start,
                                                               error_type, stats.inference_calls, stats.tool_calls)
                    except Exception:
                        pass
        finally:
            _run_stats_var.reset(token)
            _execution_ids.reset(ids_token)
            _iteration_var.reset(iteration_token)
            _message_count.reset(count_token)
            _scene_var.reset(scene_token)

    # -- tool execution ----------------------------------------------------

    @contextmanager
    def tool_execution(self, tool, call_id=None, risk_level=None):
        start = time.monotonic()
        exc: Optional[BaseException] = None
        outcome = None
        scene_token = _scene_set(
            {"operation": "execute_tool", "tool": map_tool_name(tool),
             "call_id": _safe_id(call_id), "risk_level": (RiskLevel.from_value(risk_level).value if RiskLevel.from_value(risk_level) else "other")},
            mask=True,
        )
        try:
            with execute_tool_span(
                tool,
                call_id=call_id,
                risk_level=risk_level,
                iteration=current_iteration(),
            ) as span:
                holder = {"policy_outcome": None, "outcome": None, "executed": False, "details": {}}
                try:
                    yield holder
                except BaseException as caught:
                    exc = caught
                    raise
                finally:
                    if holder.get("outcome") is not None:
                        outcome = holder["outcome"]
                    elif exc is not None:
                        outcome = (
                            "cancelled"
                            if isinstance(exc, asyncio.CancelledError)
                            else "error"
                        )
                    elif holder.get("executed"):
                        outcome = "success"
                    else:
                        outcome = "denied"
                    _set_attr(
                        span, NAGENT_POLICY_OUTCOME, _safe_known(holder.get("policy_outcome") or "deny", frozenset({"allow", "deny", "approved", "require_approval"}))
                    )
                    _set_attr(span, NAGENT_OUTCOME, map_outcome(outcome))
                    holder_risk = holder.get("risk_level")
                    if holder_risk is not None:
                        level = RiskLevel.from_value(holder_risk)
                        _set_attr(span, NAGENT_RISK_LEVEL, level.value if level else "other")
                    stats = current_run_stats()
                    if stats is not None and holder.get("executed"):
                        stats.tool_calls += 1
                    level = RiskLevel.from_value(holder.get("risk_level") or risk_level)
                    _complete(span, "execute_tool", start, details={**holder.get("details", {}), **(project_details(error=str(exc)) if exc else {})}, tool=map_tool_name(tool),
                              call_id=_safe_id(call_id), iteration=current_iteration(),
                              risk_level=level.value if level else "other",
                              policy_outcome=_safe_known(holder.get("policy_outcome") or "deny", frozenset({"allow", "deny", "approved", "require_approval"})),
                              executed=bool(holder.get("executed")), outcome=map_outcome(outcome),
                              error_type=_safe_error_type(exc) if exc else "tool_error" if outcome == "error" else None)
                    try:
                        error_type = None
                        if exc is not None:
                            error_type = type(exc).__name__
                        elif outcome == "error":
                            # Error-status result without a real exception: use the
                            # safe sentinel, never a fabricated exception class name.
                            error_type = "tool_error"
                        record_execute_tool_duration(
                            tool,
                            "function",
                            DEFAULT_AGENT_NAME,
                            time.monotonic() - start,
                            error_type,
                        )
                    except Exception:
                        pass
        finally:
            _scene_var.reset(scene_token)

    def record_tool_denial(self, tool, call_id=None, risk_level=None, reason=None, arguments=None, result=None):
        """Short denied-execution span (approval/policy denial before ToolService)."""
        try:
            start = time.monotonic()
            with execute_tool_span(
                tool,
                call_id=call_id,
                risk_level=risk_level,
                policy_outcome="deny",
                iteration=current_iteration(),
            ) as span:
                _set_attr(span, NAGENT_OUTCOME, "denied")
                level = RiskLevel.from_value(risk_level)
                _complete(span, "execute_tool", start, details=project_details(tool_arguments=arguments, tool_result=result), tool=map_tool_name(tool), call_id=_safe_id(call_id),
                          iteration=current_iteration(), risk_level=level.value if level else "other",
                          policy_outcome="deny", executed=False, outcome="denied", error_type="denied")
            record_execute_tool_duration(
                tool,
                "function",
                DEFAULT_AGENT_NAME,
                time.monotonic() - start,
                # "denied" sentinel: denied durations stay separable from
                # successful ones in gen_ai.execute_tool.duration.
                "denied",
            )
        except Exception:
            pass

    # -- budget ------------------------------------------------------------

    def record_budget_denied(self, kind):
        record_budget_denied(kind)
        logger.warning("budget_denied kind=%s", map_kind(kind))

    # -- context compression -------------------------------------------------

    @contextmanager
    def compression(self):
        exc: Optional[BaseException] = None
        outcome = None
        start = time.monotonic()
        scene_token = _scene_set({"operation": "context_compression"}, mask=True)
        try:
            with compress_span() as span:
                comp = {"outcome": None, "before_tokens": None, "after_tokens": None}
                try:
                    yield comp
                except BaseException as caught:
                    exc = caught
                    raise
                finally:
                    if comp.get("outcome") is not None:
                        outcome = comp["outcome"]
                    elif exc is not None:
                        outcome = "failed"
                    else:
                        outcome = "skipped"
                    _set_attr(span, NAGENT_OUTCOME, map_outcome(outcome))
                    if comp.get("before_tokens") is not None:
                        _set_attr(span, NAGENT_BEFORE_TOKENS, comp["before_tokens"])
                    if comp.get("after_tokens") is not None:
                        _set_attr(span, NAGENT_AFTER_TOKENS, comp["after_tokens"])
                    _complete(span, "context_compression", start,
                              details=project_details(error=str(exc)) if exc else {},
                              outcome=outcome, before_tokens=comp.get("before_tokens"),
                              after_tokens=comp.get("after_tokens"))
        finally:
            _scene_var.reset(scene_token)
            try:
                record_context_compression(outcome)
            except Exception:
                pass

    # -- iteration / chat attempt --------------------------------------------

    def iteration(self, iteration, message_count=None):
        return iteration_scope(iteration, message_count)

    def begin_chat_attempt(
        self, *, model, stream, provider="openai", operation="chat"
    ) -> ChatAttempt:
        return ChatAttempt(
            model=model, stream=stream, provider=provider, operation=operation
        )
