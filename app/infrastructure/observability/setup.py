"""OTel SDK assembly and runtime handle (Infrastructure layer).

This module is the single assembly point for OTel providers/exporters.
Rules implemented here (spec-261024-otel-observability, Components 1):

- Unified Resource: ``service.name=n-agent``, build ``service.version``,
  per-process unique ``service.instance.id``.
- ``configure_observability(settings)`` returns a runtime handle exposing
  ``snapshot()`` (pure JSON) and ``shutdown()``; the SDK is imported
  lazily so the disabled state never loads ``opentelemetry.sdk`` or any
  exporter module.
- Assembly is idempotent; global providers are set only after all
  fallible local construction has succeeded (globals are irreversible,
  rollback never resets them). An externally-owned provider is never
  replaced; that signal degrades instead.
- Trace sampling uses ``ParentBased(TraceIdRatioBased(ratio))``; the ring
  buffer only accepts ended, sampled spans and stores a capped, safe
  projection.
- Bounded delivery: span/log queues 2048, batches 512, OTLP timeout 3s,
  metric export period 15s. Local-only mode (empty endpoint) creates no
  network exporter and uses a bounded local metric aggregation reader.
- Shutdown: one shared budget (default 5s) for all signals; producers are
  stopped first, remaining budget is given to flush/shutdown, blocking
  SDK cleanup runs off the event loop in a worker thread and data after
  the deadline is dropped.

Only imports ``app.config`` and ``app.application.telemetry_semantics``
plus the OTel SDK/exporters -- never interfaces/domain/business modules.
"""
from __future__ import annotations

import threading
import time
import uuid
import logging
from collections import deque
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from app.config import Settings
from app.application import telemetry_semantics as ts

SERVICE_NAME = "n-agent"
UNKNOWN_VERSION = "unknown"

# Project supplement attribute prefix (single centralization point for
# template extraction; keep in sync with telemetry_semantics nagent.* keys).
# NOTE: the ring-buffer attribute whitelist uses the exact enumerated key
# set (_RING_BUFFER_ALLOWED_ATTR_KEYS), not this prefix.
SHUTDOWN_THREAD_NAME = "nagent-observability-shutdown"

# Bounded delivery budgets (spec: span/log queues 2048, batches 512,
# OTLP timeout 3s, metric period 15s).
SPAN_QUEUE_MAX = 2048
LOG_QUEUE_MAX = 2048
EXPORT_BATCH_MAX = 512
OTLP_TIMEOUT_SECONDS = 3.0
METRIC_EXPORT_PERIOD_SECONDS = 15.0

# Total shared shutdown budget for all observability signals (profiler's
# up-to-2s recovery is included in this budget, not added on top).
SHUTDOWN_BUDGET_SECONDS = 5.0

# Snapshot ring buffer / projection caps.
RING_BUFFER_MAX = 100
SNAPSHOT_FIELD_CHAR_CAP = 256
SNAPSHOT_MAX_ATTRIBUTES = 20
LOCAL_METRIC_NAMES_MAX = 1000

_STATUS_DISABLED = "disabled"
_STATUS_HEALTHY = "healthy"
_STATUS_DEGRADED = "degraded"

_DELIVERY_DISABLED = "disabled"
_DELIVERY_UNKNOWN = "unknown"
_DELIVERY_SUCCEEDED = "succeeded"
_DELIVERY_FAILED = "failed"


def _service_version() -> str:
    try:
        from importlib.metadata import version

        return version(SERVICE_NAME)
    except Exception:
        return UNKNOWN_VERSION


def _now() -> float:
    return time.monotonic()


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _cap(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value[:SNAPSHOT_FIELD_CHAR_CAP]


# ---------------------------------------------------------------------------
# Delivery status tracking (public exporter result, no SDK private fields)
# ---------------------------------------------------------------------------


class _SignalState:
    """Per-signal runtime state (traces / metrics / logs / profile)."""

    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self.status: str = _STATUS_HEALTHY if enabled else _STATUS_DISABLED
        self.delivery: Optional[str] = None  # None -> unknown once enabled
        self.last_success: Optional[str] = None
        self.last_failure: Optional[str] = None
        self.failure_count: int = 0
        self.current_error: Optional[str] = None
        self.extra: Dict[str, Any] = {}

    def degrade(self, error_code: str) -> None:
        if not self.enabled:
            return
        self.status = _STATUS_DEGRADED
        self.current_error = error_code

    def record_export_success(self) -> None:
        self.delivery = _DELIVERY_SUCCEEDED
        self.last_success = _utc_now_iso()
        # Success clears only this signal's current transport fault; the
        # cumulative failure count is never reset.
        if self.current_error and self.current_error.startswith("export_"):
            self.current_error = None
            self.status = _STATUS_HEALTHY

    def record_export_failure(self, error_code: str = "export_failed") -> None:
        self.delivery = _DELIVERY_FAILED
        self.last_failure = _utc_now_iso()
        self.failure_count += 1
        self.current_error = error_code
        self.status = _STATUS_DEGRADED

    def as_json(self) -> Dict[str, Any]:
        delivery = _DELIVERY_DISABLED
        if self.enabled:
            delivery = self.delivery or _DELIVERY_UNKNOWN
        payload = {
            "status": self.status,
            "delivery": delivery,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "failure_count": self.failure_count,
            "current_error": self.current_error,
        }
        payload.update(self.extra)
        return payload


def _export_failed(result: Any) -> bool:
    """Interpret an exporter's public return value (no private fields)."""
    if isinstance(result, BaseException):
        return True
    name = getattr(result, "name", None)
    if name is not None:
        return name != "SUCCESS"
    return not result


class _DeliveryStatusWrapper:
    """Wraps a real exporter and maintains the delivery status of one
    signal from the exporter's public export() result only."""

    def __init__(self, exporter: Any, signal: _SignalState):
        self.exporter = exporter
        self.signal = signal

    def __getattr__(self, name: str) -> Any:
        # Delegate SDK-facing attributes (e.g. _preferred_temporality read
        # by PeriodicExportingMetricReader) to the wrapped exporter.
        # NOTE: copy/pickle/deepcopy of this wrapper would reconstruct the
        # inner exporter graph; do not deepcopy handle graphs containing
        # wrappers.
        return getattr(self.exporter, name)

    def export(self, data: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            result = self.exporter.export(data, *args, **kwargs)
        except Exception as exc:
            # Exporter exceptions never propagate into the business path;
            # record the delivery failure and return a failure-ish result
            # (processors treat any non-SUCCESS value as failure).
            if self.signal.delivery != _DELIVERY_FAILED:
                try:
                    # Excluded from SafeLogBridge: export errors must not feed
                    # the failing exporter recursively. Still visible in stderr.
                    from .logging import _bounded_text
                    logging.getLogger("opentelemetry.nagent.delivery").warning("telemetry export failed: %s", _bounded_text(str(exc)))
                except Exception:
                    pass
            self.signal.record_export_failure()
            return None
        if _export_failed(result):
            self.signal.record_export_failure()
        else:
            self.signal.record_export_success()
        return result

    def force_flush(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.exporter.force_flush(*args, **kwargs)
        except Exception:
            # Delivery-side failure is recorded, never propagated into the
            # business/SDK shutdown path.
            self.signal.record_export_failure("export_flush_failed")
            return False

    def shutdown(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.exporter.shutdown(*args, **kwargs)
        except Exception:
            self.signal.record_export_failure("export_shutdown_failed")
            return None


# ---------------------------------------------------------------------------
# Ring buffer span processor (ended + sampled spans only, safe projection)
# ---------------------------------------------------------------------------

# Ring-buffer attribute whitelist: the EXACT enumerated key set (GenAI +
# nagent.* semantics constants + error.type). A prefix rule would be too
# wide -- e.g. the sensitive GenAI Opt-in attributes (gen_ai.input.messages
# and friends) share the gen_ai. prefix and must never be stored, and any
# future nagent.* key would silently enter the self-check payload without
# review. Keys outside this set never reach the snapshot.
_RING_BUFFER_ALLOWED_ATTR_KEYS = frozenset(
    {
        value
        for name, value in vars(ts).items()
        if name.startswith(("GEN_AI_", "NAGENT_")) and isinstance(value, str)
    }
    | {"error.type"}
)
_SCALAR_ATTR_TYPES = (str, bool, int, float)


class _RingBufferSpanProcessor:
    """Keeps the last N ended, sampled spans as a safe capped projection.

    No full Span/events/exceptions/Resource are stored; strings are capped
    and only whitelisted low-cardinality attribute keys survive."""

    def __init__(self, maxlen: int = RING_BUFFER_MAX):
        self._lock = threading.Lock()
        self._buffer: deque = deque(maxlen=maxlen)

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        # Only ended spans are buffered; on_start never records.
        return None

    def _on_ending(self, span: Any) -> None:
        # SDK 1.45 SpanProcessor contract: called on span.end() before
        # on_end; must never raise into the business path.
        return None

    def on_end(self, span: Any) -> None:
        try:
            context = getattr(span, "context", None)
            if context is None:
                return
            if not context.trace_flags.sampled:
                return
            trace_id = getattr(context, "trace_id", None)
            span_id = getattr(context, "span_id", None)
            if not trace_id or not span_id:
                return
            parent = getattr(span, "parent", None)
            parent_span_id = getattr(parent, "span_id", None) if parent else None
            status_obj = getattr(span, "status", None)
            status_code = getattr(status_obj, "status_code", status_obj)
            entry = {
                "name": _cap(getattr(span, "name", "")) or "",
                "trace_id": format(trace_id, "032x"),
                "span_id": format(span_id, "016x"),
                "parent_span_id": (
                    format(parent_span_id, "016x") if parent_span_id else None
                ),
                "start_time": getattr(span, "start_time", None),
                "end_time": getattr(span, "end_time", None),
                "duration_ms": self._duration_ms(span),
                "status": _cap(
                    getattr(status_code, "name", str(status_code))
                    if status_code is not None
                    else ""
                ),
                "attributes": self._safe_attributes(span),
            }
        except Exception:
            return
        with self._lock:
            self._buffer.append(entry)

    @staticmethod
    def _duration_ms(span: Any) -> Optional[float]:
        try:
            start = span.start_time
            end = span.end_time
            if start is None or end is None:
                return None
            return round((end - start) / 1_000_000, 3)
        except Exception:
            return None

    @staticmethod
    def _safe_attributes(span: Any) -> Dict[str, Any]:
        attributes = getattr(span, "attributes", None) or {}
        projected: Dict[str, Any] = {}
        for key, value in attributes.items():
            if len(projected) >= SNAPSHOT_MAX_ATTRIBUTES:
                break
            if not isinstance(key, str) or not isinstance(value, _SCALAR_ATTR_TYPES):
                continue
            if key not in _RING_BUFFER_ALLOWED_ATTR_KEYS:
                continue
            if isinstance(value, str):
                value = value[:SNAPSHOT_FIELD_CHAR_CAP]
            projected[key] = value
        return projected

    def snapshot_spans(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buffer)

    def force_flush(self, timeout_millis: Optional[float] = None) -> bool:
        return True

    def shutdown(self, timeout_millis: Optional[float] = None) -> bool:
        return True


# ---------------------------------------------------------------------------
# Local-only bounded metric aggregation
# ---------------------------------------------------------------------------


class _BoundedLocalMetricExporter:
    """Keeps only the latest bounded aggregation snapshot (instrument names
    and counts); individual measurements are never cached."""

    def __init__(self):
        # MetricExporter.__init__ sets _preferred_temporality/_preferred_
        # aggregation, which PeriodicExportingMetricReader reads. Imported
        # here so the disabled path still never loads the SDK.
        from opentelemetry.sdk.metrics.export import MetricExporter

        MetricExporter.__init__(self)
        self._lock = threading.Lock()
        self._latest_names: List[str] = []
        self.collect_count = 0

    def export(self, metrics_data: Any, *args: Any, **kwargs: Any) -> Any:
        names: List[str] = []
        try:
            for resource_metrics in getattr(metrics_data, "resource_metrics", ()):
                for scope_metrics in getattr(resource_metrics, "scope_metrics", ()):
                    for metric in getattr(scope_metrics, "metrics", ()):
                        name = getattr(metric, "name", None)
                        if isinstance(name, str):
                            names.append(name[:SNAPSHOT_FIELD_CHAR_CAP])
        except Exception:
            names = []
        with self._lock:
            self._latest_names = names[:LOCAL_METRIC_NAMES_MAX]
            self.collect_count += 1
        # MetricExportResult.SUCCESS == 0 (falsy); resolve lazily to avoid
        # importing the SDK when unused.
        from opentelemetry.sdk.metrics.export import MetricExportResult

        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: Optional[float] = None, **kwargs: Any) -> Any:
        from opentelemetry.sdk.metrics.export import MetricExportResult

        return MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis: Optional[float] = None, **kwargs: Any) -> Any:
        from opentelemetry.sdk.metrics.export import MetricExportResult

        return MetricExportResult.SUCCESS

    def latest(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "local_instruments": list(self._latest_names),
                "local_collections": self.collect_count,
            }


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------


def _build_sampler(ratio: float):
    """ParentBased(TraceIdRatioBased(ratio)): the ratio only constrains
    parentless local roots; parent decisions (incl. remote) are inherited."""
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    return ParentBased(TraceIdRatioBased(ratio))


# ---------------------------------------------------------------------------
# Real SDK loader (lazy; the disabled path never calls it)
# ---------------------------------------------------------------------------


def _default_sdk_loader() -> SimpleNamespace:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    import opentelemetry.trace as otel_trace
    import opentelemetry.metrics as otel_metrics
    import opentelemetry._logs as otel_logs

    _proxy_names = {
        "ProxyTracerProvider",
        "_ProxyMeterProvider",
        "ProxyLoggerProvider",
    }

    def _is_unclaimed(provider: Any) -> bool:
        return provider is None or type(provider).__name__ in _proxy_names

    return SimpleNamespace(
        Resource=Resource,
        TracerProvider=TracerProvider,
        MeterProvider=MeterProvider,
        LoggerProvider=LoggerProvider,
        BatchSpanProcessor=BatchSpanProcessor,
        BatchLogRecordProcessor=BatchLogRecordProcessor,
        PeriodicExportingMetricReader=PeriodicExportingMetricReader,
        OTLPSpanExporter=OTLPSpanExporter,
        OTLPMetricExporter=OTLPMetricExporter,
        OTLPLogExporter=OTLPLogExporter,
        set_tracer_provider=otel_trace.set_tracer_provider,
        get_tracer_provider=otel_trace.get_tracer_provider,
        set_meter_provider=otel_metrics.set_meter_provider,
        get_meter_provider=otel_metrics.get_meter_provider,
        set_logger_provider=otel_logs.set_logger_provider,
        get_logger_provider=otel_logs.get_logger_provider,
        is_unclaimed=_is_unclaimed,
    )


# ---------------------------------------------------------------------------
# Runtime handle
# ---------------------------------------------------------------------------


class ObservabilityHandle:
    """Runtime handle: pure-JSON ``snapshot()`` and budgeted
    ``shutdown()``. Interfaces consume the injected snapshot callable and
    never touch the SDK."""

    def __init__(
        self,
        *,
        mode: str,
        instance_id: str,
        sample_ratio: float,
        signals: Dict[str, _SignalState],
        ring_buffer: Optional[_RingBufferSpanProcessor],
        providers: List[Any],
        local_metric_exporter: Optional[_BoundedLocalMetricExporter],
        semantics: Any = None,
    ):
        self._mode = mode
        self._instance_id = instance_id
        self._sample_ratio = sample_ratio
        self._signals = signals
        self._ring_buffer = ring_buffer
        self._providers = providers
        self._local_metric_exporter = local_metric_exporter
        # Semantics registry (module-like object providing SEMCONV_VERSION
        # and INSTRUMENTS); parameterized for template extraction.
        self._semantics = semantics if semantics is not None else ts
        self._stop_callbacks: List[Callable[[], None]] = []
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self._dropped_after_deadline = 0
        # True once global providers were published (irreversible); such
        # handles must be cached even if degraded, so a retry never
        # re-assembles (and leaks) another set of providers.
        self._published = False
        self.status = self._compute_status()

    # -- lifecycle ---------------------------------------------------------

    @property
    def mode(self) -> str:
        """Lifecycle mode: ``disabled`` / ``otlp`` / ``local_only``."""
        return self._mode

    @property
    def instance_id(self) -> str:
        """Per-process unique service.instance.id."""
        return self._instance_id

    def register_stop_callback(self, callback: Callable[[], None]) -> bool:
        """Register a business producer to stop before flush/shutdown.

        Returns False (and registers nothing) when shutdown already ran:
        a late producer cannot be stopped by this handle anymore, and the
        caller must not assume registration succeeded."""
        with self._shutdown_lock:
            if self._shutdown_done:
                return False
            self._stop_callbacks.append(callback)
            return True

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        budget = SHUTDOWN_BUDGET_SECONDS
        deadline = _now() + budget
        # 1. stop business producers first (ours, quick, synchronous)
        for callback in list(self._stop_callbacks):
            try:
                callback()
            except Exception:
                pass
        # 2. flush/shutdown providers with the remaining budget, off the
        # caller's thread so a blocking SDK cleanup never occupies the
        # event loop; anything left after the deadline is dropped.
        dropped = 0
        if self._providers:

            def _drain() -> None:
                nonlocal dropped
                for provider in self._providers:
                    remaining = deadline - _now()
                    if remaining <= 0:
                        dropped += 1
                        continue
                    try:
                        provider.force_flush(timeout_millis=int(remaining * 1000))
                        provider.shutdown()
                    except Exception:
                        dropped += 1

            worker = threading.Thread(
                target=_drain, name=SHUTDOWN_THREAD_NAME, daemon=True
            )
            worker.start()
            worker.join(max(0.0, deadline - _now()))
            if worker.is_alive():
                # Deadline hit: remaining data is dropped, safe diagnostic
                # counters only. The daemon thread finishes on its own.
                dropped += 1
        self._dropped_after_deadline = dropped
        self.status = self._compute_status()

    # -- profiler status bridge ---------------------------------------------

    def mark_profiler_attached(self) -> bool:
        """Clear the assembly-time ``profiler_not_configured`` fault once
        the composition root successfully started the sampler. Delivery
        stays ``unknown`` until the first window is actually ingested."""
        state = self._signals.get("profile")
        if state is None or not state.enabled:
            return False
        if state.current_error == "profiler_not_configured":
            state.current_error = None
            state.status = _STATUS_HEALTHY
        self.status = self._compute_status()
        return True

    def profiler_on_status(self) -> Callable[[str, Optional[str]], None]:
        """Bridge ``ProfilingSampler.on_status(status, code)`` (public
        per-window results, safe codes only) into the profile signal
        state: healthy clears the current transport fault, degraded
        records the failure count and the sampler's error code (incl.
        the 3-strikes ``sampler_disabled``)."""
        def _bridge(status: str, code: Optional[str]) -> None:
            state = self._signals.get("profile")
            if state is None or not state.enabled:
                return
            if status == "healthy":
                state.delivery = _DELIVERY_SUCCEEDED
                state.last_success = _utc_now_iso()
                state.current_error = None
                state.status = _STATUS_HEALTHY
            elif status == "degraded":
                state.delivery = _DELIVERY_FAILED
                state.last_failure = _utc_now_iso()
                state.failure_count += 1
                state.current_error = code or "profiler_failed"
                state.status = _STATUS_DEGRADED
            self.status = self._compute_status()

        return _bridge

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        partial_failure = False
        spans: List[Dict[str, Any]] = []
        if self._ring_buffer is not None:
            try:
                spans = self._ring_buffer.snapshot_spans()
            except Exception:
                # Read failure keeps the readable components and reports
                # degraded; never a fake healthy.
                partial_failure = True
        signals = {
            name: state.as_json() for name, state in self._signals.items()
        }
        if self._local_metric_exporter is not None:
            try:
                signals["metrics"].update(self._local_metric_exporter.latest())
            except Exception:
                partial_failure = True
        status = self._compute_status()
        if partial_failure or (self._shutdown_done and self._mode != "disabled"):
            if status != _STATUS_DISABLED:
                status = _STATUS_DEGRADED
        return {
            "status": status,
            "mode": self._mode,
            "instance_id": self._instance_id,
            "sample_ratio": self._sample_ratio,
            "semconv_version": self._semantics.SEMCONV_VERSION,
            "instruments": [
                {
                    "name": inst.name,
                    "kind": inst.kind,
                    "unit": inst.unit,
                    "allowed_dimensions": list(inst.allowed_dimensions),
                }
                for inst in self._semantics.INSTRUMENTS.values()
            ],
            "spans": spans,
            "signals": signals,
            "dropped_after_deadline": self._dropped_after_deadline,
        }

    def _compute_status(self) -> str:
        if self._mode == "disabled":
            return _STATUS_DISABLED
        for state in self._signals.values():
            if state.enabled and state.status == _STATUS_DEGRADED:
                return _STATUS_DEGRADED
        if self._dropped_after_deadline:
            return _STATUS_DEGRADED
        return _STATUS_HEALTHY


# ---------------------------------------------------------------------------
# configure / shutdown
# ---------------------------------------------------------------------------

_HANDLE: Optional[ObservabilityHandle] = None
_HANDLE_LOCK = threading.Lock()


def configure_observability(
    settings: Settings, *, sdk_loader: Any = None, semantics: Any = None
) -> ObservabilityHandle:
    """Assemble the OTel runtime (idempotent) and return its handle.

    ``sdk_loader`` is a public injection point for tests: it supplies the
    SDK classes and the global provider setters/getters, so tests isolate
    all global state without touching SDK private fields. ``semantics``
    overrides the telemetry semantics registry (module-like object with
    SEMCONV_VERSION and INSTRUMENTS).

    Caching rule: any handle that completed assembly (i.e. published its
    global providers, or is disabled) is cached -- even when degraded --
    because OTel globals are irreversible and a retry would re-assemble
    and leak another set of providers/threads. A handle that failed
    before publishing anything may be retried."""
    global _HANDLE
    with _HANDLE_LOCK:
        if _HANDLE is not None:
            return _HANDLE
        handle = _assemble(settings, sdk_loader, semantics)
        if handle._published or handle._mode == "disabled":
            _HANDLE = handle
        return handle


def get_observability_handle() -> Optional[ObservabilityHandle]:
    return _HANDLE


def _reset_for_tests() -> None:
    global _HANDLE
    with _HANDLE_LOCK:
        _HANDLE = None


def _assemble(
    settings: Settings, sdk_loader: Any, semantics: Any = None
) -> ObservabilityHandle:
    instance_id = uuid.uuid4().hex
    ratio = settings.otel_traces_sample_ratio

    signals = {
        "traces": _SignalState(enabled=settings.otel_enabled),
        "metrics": _SignalState(enabled=settings.otel_enabled),
        "logs": _SignalState(enabled=settings.otel_enabled),
        "profile": _SignalState(enabled=settings.profiling_enabled),
    }
    if not settings.profiling_enabled:
        signals["profile"].status = _STATUS_DISABLED
        signals["profile"].delivery = _DELIVERY_DISABLED
    else:
        # Profiler assembly happens in profiling.py (T7); until it is
        # attached the enabled profile signal reports degraded.
        signals["profile"].degrade("profiler_not_configured")

    if not settings.otel_enabled:
        signals["traces"].status = _STATUS_DISABLED
        signals["traces"].delivery = _DELIVERY_DISABLED
        signals["metrics"].status = _STATUS_DISABLED
        signals["metrics"].delivery = _DELIVERY_DISABLED
        signals["logs"].status = _STATUS_DISABLED
        signals["logs"].delivery = _DELIVERY_DISABLED
        return ObservabilityHandle(
            mode="disabled",
            instance_id=instance_id,
            sample_ratio=ratio,
            signals=signals,
            ring_buffer=None,
            providers=[],
            local_metric_exporter=None,
            semantics=semantics,
        )

    loader = sdk_loader if sdk_loader is not None else _default_sdk_loader()
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    insecure: Optional[bool] = None
    if endpoint:
        from urllib.parse import urlparse

        insecure = urlparse(endpoint).scheme == "http"

    resource = loader.Resource(
        attributes={
            "service.name": SERVICE_NAME,
            "service.version": _service_version(),
            "service.instance.id": instance_id,
        }
    )
    owners: List[Any] = []  # providers to shut down on rollback
    exporters: List[_DeliveryStatusWrapper] = []
    publishes: List[tuple] = []  # (setter, provider), applied only on success
    ring_buffer = _RingBufferSpanProcessor()
    local_metric_exporter: Optional[_BoundedLocalMetricExporter] = None
    published = False

    try:
        # -- traces -----------------------------------------------------
        if loader.is_unclaimed(loader.get_tracer_provider()):
            if endpoint:
                trace_exporter = _DeliveryStatusWrapper(
                    loader.OTLPSpanExporter(
                        endpoint=endpoint,
                        insecure=insecure,
                        timeout=OTLP_TIMEOUT_SECONDS,
                    ),
                    signals["traces"],
                )
                exporters.append(trace_exporter)
                batch_processor = loader.BatchSpanProcessor(
                    trace_exporter,
                    max_queue_size=SPAN_QUEUE_MAX,
                    max_export_batch_size=EXPORT_BATCH_MAX,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
            else:
                batch_processor = None
                signals["traces"].delivery = _DELIVERY_DISABLED
            tracer_provider = loader.TracerProvider(
                resource=resource, sampler=_build_sampler(ratio)
            )
            owners.append(tracer_provider)
            tracer_provider.add_span_processor(ring_buffer)
            if batch_processor is not None:
                tracer_provider.add_span_processor(batch_processor)
            publishes.append((loader.set_tracer_provider, tracer_provider))
        else:
            # Externally-owned global provider: degrade this signal only;
            # never replace or shut down the external instance.
            signals["traces"].degrade("provider_unavailable")

        # -- metrics ----------------------------------------------------
        if loader.is_unclaimed(loader.get_meter_provider()):
            if endpoint:
                metric_exporter = _DeliveryStatusWrapper(
                    loader.OTLPMetricExporter(
                        endpoint=endpoint,
                        insecure=insecure,
                        timeout=OTLP_TIMEOUT_SECONDS,
                    ),
                    signals["metrics"],
                )
                exporters.append(metric_exporter)
                metric_reader = loader.PeriodicExportingMetricReader(
                    metric_exporter,
                    export_interval_millis=METRIC_EXPORT_PERIOD_SECONDS * 1000,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
            else:
                # local-only: bounded aggregation reader, no network.
                local_metric_exporter = _BoundedLocalMetricExporter()
                metric_reader = loader.PeriodicExportingMetricReader(
                    local_metric_exporter,
                    export_interval_millis=METRIC_EXPORT_PERIOD_SECONDS * 1000,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
                signals["metrics"].delivery = _DELIVERY_DISABLED
            meter_provider = loader.MeterProvider(
                metric_readers=[metric_reader], resource=resource
            )
            owners.append(meter_provider)
            publishes.append((loader.set_meter_provider, meter_provider))
        else:
            signals["metrics"].degrade("provider_unavailable")

        # -- logs -------------------------------------------------------
        if endpoint:
            if loader.is_unclaimed(loader.get_logger_provider()):
                log_exporter = _DeliveryStatusWrapper(
                    loader.OTLPLogExporter(
                        endpoint=endpoint,
                        insecure=insecure,
                        timeout=OTLP_TIMEOUT_SECONDS,
                    ),
                    signals["logs"],
                )
                exporters.append(log_exporter)
                log_processor = loader.BatchLogRecordProcessor(
                    log_exporter,
                    max_queue_size=LOG_QUEUE_MAX,
                    max_export_batch_size=EXPORT_BATCH_MAX,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
                logger_provider = loader.LoggerProvider(resource=resource)
                owners.append(logger_provider)
                logger_provider.add_log_record_processor(log_processor)
                publishes.append((loader.set_logger_provider, logger_provider))
            else:
                signals["logs"].degrade("provider_unavailable")
        else:
            # local-only: no network log exporter; application logs stay
            # on the app's own stderr pipeline.
            signals["logs"].status = _STATUS_DISABLED
            signals["logs"].delivery = _DELIVERY_DISABLED

        # -- publish (only after all fallible construction succeeded) ---
        for setter, provider in publishes:
            setter(provider)
        published = True
    except Exception as exc:
        # Partial failure rollback: close everything this component built.
        # Globals were never set (publish happens last), so no reset of
        # global state is needed or attempted.
        for owner in owners:
            try:
                owner.shutdown()
            except Exception:
                pass
        for wrapper in exporters:
            try:
                wrapper.shutdown()
            except Exception:
                pass
        for state in signals.values():
            if state.enabled:
                state.degrade("setup_failed")
        try:
            from .logging import _bounded_text
            logging.getLogger("opentelemetry.nagent.setup").error("observability setup failed: %s", _bounded_text(str(exc)))
        except Exception:
            pass
        return ObservabilityHandle(
            mode="otlp" if endpoint else "local_only",
            instance_id=instance_id,
            sample_ratio=ratio,
            signals=signals,
            ring_buffer=ring_buffer,
            providers=[],
            local_metric_exporter=None,
            semantics=semantics,
        )

    handle = ObservabilityHandle(
        mode="otlp" if endpoint else "local_only",
        instance_id=instance_id,
        sample_ratio=ratio,
        signals=signals,
        ring_buffer=ring_buffer,
        providers=owners,
        local_metric_exporter=local_metric_exporter,
        semantics=semantics,
    )
    handle._published = published
    # Stable process/system metrics (process.* cpu/mem, system.* cpu/mem) ride
    # the same meter pipeline, giving dashboards an always-present time
    # series. Started after publish so failures can never trigger the
    # rollback path; the helper itself never raises (business metrics keep
    # flowing without them). setup.py deliberately has no logger -- the
    # metrics signal's ``extra`` annotation is the visible failure channel
    # (non-degrading: system metrics are auxiliary, not the signal's health).
    if published:
        try:
            meter_provider  # noqa: B018 -- assigned only in the metrics branch
        except NameError:
            meter_provider = None
        if meter_provider is not None:
            metrics_extra = signals["metrics"].extra
            try:
                from app.infrastructure.observability.instrumentors import (
                    instrument_system_metrics,
                )

                if instrument_system_metrics(meter_provider):
                    metrics_extra["system_metrics"] = "started"
            except Exception as exc:
                metrics_extra["system_metrics"] = f"failed:{type(exc).__name__}"
    return handle
