"""Safe log projection and OTel log bridge (Infrastructure layer).

Implements the log contract of spec-261024-otel-observability (Components 4):

- Whitelist projection happens BEFORE any consumer: both the app's own JSON
  stderr formatter and the OTel bridge only ever emit a registered safe
  event code. Unknown records never have their message interpolated,
  args-formatted, or exception-formatted; they collapse to the fixed
  ``UNKNOWN_EVENT_CODE``.
- The OTel bridge forwards a per-call projection COPY (a fresh LogRecord
  whose body is the event code and whose args/exc_info/extra are dropped);
  the caller's original Record is never mutated, so other root handlers
  (external formats, the policy audit sink) keep the original fields.
- ``session_id`` comes only from trusted server-side context: an explicit
  trusted binding ContextVar or the current span's ``gen_ai.conversation.id``
  (set by Application from AgentState, never from client-controlled data).
- Self traffic (``opentelemetry.*`` / ``grpc.*`` loggers) never re-enters
  the bridge, so export/collector logs cannot cause recursion.
- Bridge failures are swallowed and counted; logging must never break the
  business path (same isolation pattern as the delivery-status wrapper in
  ``setup.py``).

Only ``opentelemetry-api`` is imported at module level; the SDK
``LoggingHandler`` is imported lazily so the disabled/local-only path never
loads ``opentelemetry.sdk``.
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from opentelemetry import trace

from app.infrastructure.observability.sanitizer import LogSanitizer

UNKNOWN_EVENT_CODE = "log_event_unknown"

# Trusted conversation attribute set by Application spans from AgentState
# (telemetry_semantics.GEN_AI_CONVERSATION_ID); kept as a literal here so
# Infrastructure does not import Application for a key string.
_CONVERSATION_ID_ATTR = "gen_ai.conversation.id"

# Logger-name prefixes whose records never re-enter the OTel bridge
# (exporter/collector/transport self traffic -- recursion guard).
_SELF_TRAFFIC_PREFIXES = ("opentelemetry", "grpc")

# Event registry: raw (developer-authored) record.msg template -> fixed,
# vetted event code. Exact-match on the un-interpolated template only, so
# dynamic message text can never match and never leak.
_event_registry: Dict[str, str] = {}
_registry_lock = threading.Lock()

# Trusted server-side session binding (explicit, e.g. from AgentState by a
# future composition-root hook); null when absent.
_session_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "nagent_log_session_id", default=None
)

# Composition-root injection avoids an Infrastructure -> Application dependency.
_execution_context = None
_details_validator = None
_details_flattener = None
_details_projector = None
EXECUTION_STRING_FIELDS = frozenset({
    "session_id", "request_id", "source", "operation", "summary", "outcome",
    "error_type", "end_reason", "model", "provider", "finish_reason", "tool",
    "call_id", "risk_level", "policy_outcome",
})
EXECUTION_COUNT_FIELDS = frozenset({
    "message_count", "inference_calls", "tool_calls", "input_tokens",
    "output_tokens", "iteration", "before_tokens", "after_tokens",
})
EXECUTION_BOOL_FIELDS = frozenset({"usage_known", "stream", "executed"})

HTTP_COMPLETED_EVENT = "http_request_completed"
HTTP_CLIENT_COMPLETED_EVENT = "http_client_completed"
_http_request_context = contextvars.ContextVar("nagent_http_request_log", default=None)
_policy_audit_context = contextvars.ContextVar("nagent_policy_audit_log", default=None)
_POLICY_AUDIT_FIELDS = frozenset({
    "policy", "version", "decision_kind", "reason", "outcome", "run_id",
    "session_id", "policy_scope", "occurred_at",
})


@contextmanager
def policy_audit_context(payload):
    """Bind sink-owned audit facts synchronously; never trust LogRecord extras."""
    token = _policy_audit_context.set(payload)
    try:
        yield
    finally:
        _policy_audit_context.reset(token)


def log_http_request_completed(method, route, status_code, duration_ms, outcome, *, details=None):
    """Emit bounded HTTP facts through both existing safe log outlets."""
    facts = {"operation": "http_request", "duration_ms": duration_ms,
             "outcome": outcome}
    facts["http.request.method"] = method if method in {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"
    } else "_OTHER"
    if isinstance(route, str) and route.startswith("/") and len(route) <= 512:
        facts["http.route"] = route
    if type(status_code) is int and 100 <= status_code <= 599:
        facts["http.response.status_code"] = status_code
    facts["summary"] = f"HTTP {facts['http.request.method']} {route}；状态 {status_code if status_code is not None else '无响应'}，{outcome}；耗时 {duration_ms:.1f} ms。"
    template = "http_request_completed %s"
    register_safe_event(template, HTTP_COMPLETED_EVENT)
    token = _http_request_context.set({**facts, "details": details or {}})
    try:
        logging.getLogger(__name__).info(template, facts["summary"])
    finally:
        _http_request_context.reset(token)


def log_http_client_completed(facts):
    """Publish already-projected request metadata while its client span is current."""
    template = "http_client_completed %s"
    register_safe_event(template, HTTP_CLIENT_COMPLETED_EVENT)
    token = _http_request_context.set({**_safe_execution_context(), **facts})
    try:
        logging.getLogger(__name__).info(template, facts["summary"])
    finally:
        _http_request_context.reset(token)


def configure_execution_context(snapshot=None, *, validate_details=None, flatten_details=None,
                                project_details=None) -> None:
    """Inject a trusted synchronous safe-facts snapshot, or clear it."""
    global _execution_context, _details_validator, _details_flattener, _details_projector
    _execution_context = snapshot if callable(snapshot) else None
    _details_validator = validate_details if callable(validate_details) else None
    _details_flattener = flatten_details if callable(flatten_details) else None
    _details_projector = project_details if callable(project_details) else None


def project_observation_details(**fields):
    try:
        return _details_projector(**fields) if _details_projector is not None else {}
    except Exception:
        return {}


def flatten_observation_details(details):
    try:
        return _details_flattener(details) if _details_flattener is not None else {}
    except Exception:
        return {}


def _execution_snapshot():
    try:
        return _execution_context() if _execution_context is not None else {}
    except Exception:
        return {}


def _safe_execution_context(snapshot=None) -> Dict[str, object]:
    try:
        snapshot = _execution_snapshot() if snapshot is None else snapshot
        if not isinstance(snapshot, Mapping):
            return {}
        out = {}
        for key, value in snapshot.items():
            if key in EXECUTION_STRING_FIELDS and isinstance(value, str):
                out[key] = value[:512 if key == "summary" else 256]
            elif key in EXECUTION_COUNT_FIELDS and type(value) is int and value >= 0:
                out[key] = value
            elif key in EXECUTION_BOOL_FIELDS and type(value) is bool:
                out[key] = value
            elif key == "duration_ms" and type(value) in (int, float):
                if math.isfinite(value) and value >= 0:
                    out[key] = value
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Safe event registry
# ---------------------------------------------------------------------------


def register_safe_event(template: str, event_code: str) -> None:
    """Register a vetted raw ``record.msg`` template under a fixed code.

    Idempotent: the first registration for a template wins."""
    if not isinstance(template, str) or not template:
        return
    if not isinstance(event_code, str) or not event_code:
        return
    with _registry_lock:
        _event_registry.setdefault(template, event_code)


def event_code_for(record: logging.LogRecord) -> str:
    """Fixed event code for a record; unknown events never leak text."""
    msg = record.msg if isinstance(record.msg, str) else None
    if msg is None:
        return UNKNOWN_EVENT_CODE
    with _registry_lock:
        code = _event_registry.get(msg)
    return code or UNKNOWN_EVENT_CODE


def _reset_event_registry_for_tests() -> None:
    with _registry_lock:
        _event_registry.clear()


# ---------------------------------------------------------------------------
# Trusted session binding
# ---------------------------------------------------------------------------


def bind_log_session(session_id: Optional[str]) -> contextvars.Token:
    """Bind a trusted server-side session id to the current context."""
    value = session_id if isinstance(session_id, str) and session_id else None
    return _session_var.set(value)


def reset_log_session(token: contextvars.Token) -> None:
    _session_var.reset(token)


@contextmanager
def bound_log_session(session_id: Optional[str]):
    """Context-managed trusted session binding (always reset on exit)."""
    token = bind_log_session(session_id)
    try:
        yield
    finally:
        reset_log_session(token)


# ---------------------------------------------------------------------------
# Shared content sanitizer (restores readable body/exception, fixed-pattern
# + known-secret redaction). Injected by the composition root: until
# ``prepare_sanitizer`` installs an instance, records stay in the safe
# event-only mode (no restored text) so a half-wired startup never leaks.
# ---------------------------------------------------------------------------

_sanitizer: Optional[LogSanitizer] = None
_sanitizer_lock = threading.Lock()
_content_max_bytes = 2048


def ensure_sanitizer():
    """Bootstrap fixed rules before service construction; secrets join later."""
    global _sanitizer
    with _sanitizer_lock:
        if _sanitizer is None:
            _sanitizer = LogSanitizer()


def prepare_sanitizer(sanitizer: Optional[LogSanitizer], *, max_bytes=None) -> None:
    """Atomically install (or clear, with ``None``) the shared sanitizer."""
    global _sanitizer, _content_max_bytes
    with _sanitizer_lock:
        _sanitizer = sanitizer
        _content_max_bytes = max_bytes if type(max_bytes) is int and max_bytes > 0 else 2048


def configure_secret_catalog(known_secrets: object) -> None:
    """Forward known-secret catalog to the shared sanitizer (no-op if not yet
    installed; wiring publishes atomically, see main.py ``_wire_log_sanitizer``)."""
    with _sanitizer_lock:
        exists = _sanitizer is not None
    if exists:
        _sanitizer.configure_secret_catalog(known_secrets)


def _sanitize(text: str) -> str:
    """Redact a string via the installed sanitizer, if present."""
    with _sanitizer_lock:
        sanitizer = _sanitizer
    if sanitizer is None:
        return text
    return sanitizer.sanitize(text)


def _sanitizer_installed() -> bool:
    with _sanitizer_lock:
        return _sanitizer is not None


def _sanitize_partial(text):
    with _sanitizer_lock:
        sanitizer = _sanitizer
    return sanitizer.sanitize_partial(text) if sanitizer is not None else "[content unavailable before sanitizer initialization]"


@dataclass
class Prepared:
    """Safe, sanitized presentation of one LogRecord for both outlets.

    Fields are only populated once a sanitizer is installed; otherwise the
    record stays collapsed to the fixed event code."""
    event: str
    scene: Dict[str, object] = field(default_factory=dict)
    details: Dict[str, object] = field(default_factory=dict)
    message: Optional[str] = None
    exception: Optional[Dict[str, str]] = None
    stack: Optional[str] = None


def _format_exception(exc_info: Optional[Tuple]) -> Optional[Dict[str, str]]:
    """Sanitized exception.type/message/stacktrace; None for no exc_info."""
    if not exc_info:
        return None
    etype, evalue, _etb = exc_info
    try:
        return {
            "type": _sanitize(
                getattr(etype, "__name__", None) or (str(etype) if etype else "?")
            ),
            "message": _bounded_text(str(evalue)) if evalue is not None else "",
            "stacktrace": _bounded_text(logging.Formatter().formatException(exc_info), multiplier=4),
        }
    except Exception:
        return {"type": getattr(etype, "__name__", "Exception"),
                "message": "[exception text unavailable]", "stacktrace": ""}


def _bounded_text(text, *, multiplier=1):
    safe = _sanitize(text)
    limit = multiplier * _content_max_bytes
    raw = safe.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return safe
    marker = " [truncated]"[:limit]
    return raw[:max(0, limit - len(marker))].decode("utf-8", errors="ignore") + marker


def _redundant_library_info(record):
    if record.levelno >= logging.WARNING:
        return False
    return (record.name.startswith(("httpx", "httpcore", "opentelemetry", "grpc"))
            or (record.name == "Lark" and record.funcName == "_connect")
            or (record.name == "n_agent_plugins.opa_client" and record.funcName == "register"))


def _prepare_record(record: logging.LogRecord) -> Prepared:
    """Build the sanitized presentation for ``record``.

    Never mutates the original record, never reads another handler's
    ``exc_text``. The body is ``getMessage()``-interpolated (registered
    templates only), the exception is the standard formatter output, and
    ``stack_info`` is kept as the standalone ``stack`` field."""
    event = event_code_for(record)
    readable = event != UNKNOWN_EVENT_CODE or not _redundant_library_info(record)
    snapshot = _execution_snapshot()
    if not isinstance(snapshot, Mapping):
        snapshot = {}
    scene = _safe_execution_context(snapshot)
    if event in (HTTP_COMPLETED_EVENT, HTTP_CLIENT_COMPLETED_EVENT) and _http_request_context.get() is not None:
        # HTTP completion owns its scene; do not inherit the last child operation.
        scene = dict(_http_request_context.get())
        details_snapshot = {"details": scene.pop("details", {})}
    else:
        details_snapshot = snapshot
    if event == "policy_decision" and _sanitizer_installed():
        audit = _policy_audit_context.get()
        if isinstance(audit, Mapping):
            for key in _POLICY_AUDIT_FIELDS:
                value = audit.get(key)
                if isinstance(value, str):
                    try:
                        name = "name" if key == "policy" else key
                        scene["policy." + name] = _sanitize(value)[:1024 if key == "reason" else 256]
                    except Exception:
                        pass  # Fail closed for a field whose sanitization fails.
    details = {}
    if _sanitizer_installed() and _details_validator is not None and isinstance(snapshot, Mapping):
        try:
            # Already sanitized by Application: validate only, never sanitize twice.
            details = _details_validator(details_snapshot.get("details"))
        except Exception:
            pass
    if "session_id" not in scene:
        session_id = _current_session_id(record)
        if session_id:
            scene["session_id"] = session_id
    if _sanitizer_installed() and readable:
        standard = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        extra = {k: v for k, v in vars(record).items()
                 if k not in standard and k not in {"message", "asctime"} and not k.startswith("_")
                 and type(v) in (str, int, float, bool, list, tuple, dict, type(None))}
        if extra:
            details.update(project_observation_details(log_fields=extra))
    if not _sanitizer_installed() or not readable:
        # Safe event-only mode: no restored body for unregistered/unknown
        # (ad-hoc, untrusted) templates, and none until a sanitizer exists.
        # Only vetted registered templates regain a readable, sanitized body.
        return Prepared(event=event, scene=scene, details=details)
    if event == UNKNOWN_EVENT_CODE:
        event = "application_log" if record.name.startswith("app.") else "diagnostic_log" if record.levelno >= logging.WARNING else "library_log"
    try:
        if type(record.msg) in (dict, list, tuple):
            structured = project_observation_details(log_fields=record.msg)
            details.update(structured)
            message = structured.get("log_fields", {}).get("content", "[structured log unavailable]")
        else:
            message = _bounded_text(record.getMessage())
    except Exception:
        message = None
    exception = _format_exception(record.exc_info)
    stack_info = record.stack_info
    stack = _bounded_text(stack_info, multiplier=4) if isinstance(stack_info, str) and stack_info else None
    return Prepared(event=event, scene=scene, details=details, message=message,
                    exception=exception, stack=stack)


def _current_session_id(record: logging.LogRecord) -> Optional[str]:
    """Trusted session id: explicit binding first, then the current span's
    conversation attribute (set from AgentState, not client data)."""
    bound = _session_var.get()
    if isinstance(bound, str) and bound:
        return bound
    try:
        span = trace.get_current_span()
        attributes = getattr(span, "attributes", None)
        if isinstance(attributes, Mapping):
            value = attributes.get(_CONVERSATION_ID_ATTR)
            if isinstance(value, str) and value:
                return value
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Correlation helpers
# ---------------------------------------------------------------------------


def _current_span_ids() -> Tuple[Optional[str], Optional[str]]:
    """Hex trace/span ids of the current span; nulls without a valid span."""
    try:
        span = trace.get_current_span()
        span_context = span.get_span_context() if span is not None else None
        if span_context is not None and getattr(span_context, "is_valid", False):
            return (
                format(span_context.trace_id, "032x"),
                format(span_context.span_id, "016x"),
            )
    except Exception:
        pass
    return None, None


_service_identity_cache: Optional[Tuple[str, Optional[str]]] = None
_service_identity_lock = threading.Lock()


def _service_identity() -> Tuple[str, Optional[str]]:
    """Service name/instance for JSON lines, cached once resolved.

    The identity is stable for the process, so the per-record cost drops to
    a guarded read once an instance id exists; until a handle appears the
    lookup retries (cheap) so late observability configuration still lands
    in the JSON lines. The per-record timestamp stays in the formatter."""
    global _service_identity_cache
    cached = _service_identity_cache
    if cached is not None and cached[1] is not None:
        return cached
    try:
        from app.infrastructure.observability.setup import (
            SERVICE_NAME,
            get_observability_handle,
        )

        handle = get_observability_handle()
        instance_id = getattr(handle, "instance_id", None) if handle else None
    except Exception:
        return cached if cached is not None else ("n-agent", None)
    identity = (SERVICE_NAME, instance_id if isinstance(instance_id, str) else None)
    with _service_identity_lock:
        _service_identity_cache = identity
    return identity


# ---------------------------------------------------------------------------
# JSON stderr formatter (app-owned handler; whitelist fields only)
# ---------------------------------------------------------------------------


class SafeEventJsonFormatter(logging.Formatter):
    """JSON line formatter with whitelisted fields only.

    Emits timestamp/severity/logger/event/trace_id/span_id/session_id and
    service info. The message body is never interpolated, formatted, or
    included: only the fixed event code survives projection."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = self._payload(record)
        except Exception:
            # Formatting itself must never raise into the logging machinery.
            payload = {
                "event": UNKNOWN_EVENT_CODE,
                "severity": getattr(record, "levelname", "ERROR"),
                "logger": getattr(record, "name", ""),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _payload(record: logging.LogRecord) -> Dict[str, object]:
        trace_id, span_id = _current_span_ids()
        service_name, instance_id = _service_identity()
        prepared = _prepare_record(record)
        scene = prepared.scene
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "event": prepared.event,
            "trace_id": trace_id,
            "span_id": span_id,
            "session_id": scene.get(
                "session_id", _current_session_id(record)
            ),
            "service.name": service_name,
            "service.instance.id": instance_id,
        }
        payload.update(scene)
        if prepared.details:
            payload["details"] = prepared.details
        if prepared.message is not None:
            payload["message"] = prepared.message
        if prepared.exception is not None:
            payload["exception"] = prepared.exception
        if prepared.stack is not None:
            payload["stack"] = prepared.stack
        return payload


# ---------------------------------------------------------------------------
# Projection copy
# ---------------------------------------------------------------------------


def project_record(record: logging.LogRecord, *, _prepared=None) -> logging.LogRecord:
    """Return a safe projection COPY of ``record``.

    The original record is never mutated (other handlers on the root logger
    keep the original message/args/exc_info). The copy carries the sanitized
    body as ``msg``; args, exception info and ``extra`` fields are dropped so
    the SDK handler's ``vars(record)`` attribute dump stays whitelisted. When
    no sanitizer is installed the body collapses to the fixed event code."""
    prepared = _prepared if _prepared is not None else _prepare_record(record)
    projected = logging.LogRecord(
        name=record.name,
        level=record.levelno,
        pathname=record.pathname,
        lineno=record.lineno,
        msg=prepared.message if prepared.message is not None else prepared.event,
        args=(),
        exc_info=None,
        func=record.funcName,
    )
    projected.created = record.created
    projected.msecs = record.msecs
    projected.event = prepared.event
    for key, value in _flatten_prepared_details(prepared).items():
        setattr(projected, key, value)
    for key, value in prepared.scene.items():
        setattr(projected, key, value)
    if prepared.message is not None:
        projected.message = prepared.message
    if prepared.exception is not None:
        # Flat dotted attribute keys: the OTel SDK dumps ``vars(record)`` as
        # log attributes, and nested dict values are not valid OTel attribute
        # values. The names line up with the collector's ``exception.*`` reads.
        setattr(projected, "exception.type", prepared.exception["type"])
        setattr(projected, "exception.message", prepared.exception["message"])
        setattr(projected, "exception.stacktrace", prepared.exception["stacktrace"])
    if prepared.stack is not None:
        setattr(projected, "stack", prepared.stack)
    return projected


# ---------------------------------------------------------------------------
# OTel bridge
# ---------------------------------------------------------------------------


def _is_self_traffic(record: logging.LogRecord) -> bool:
    return record.name.startswith(_SELF_TRAFFIC_PREFIXES)


def _is_empty_unknown(record, prepared):
    # An inherited execution scene is not useful content on its own.
    return (prepared.event == UNKNOWN_EVENT_CODE and record.levelno < logging.WARNING
            and not prepared.details and not prepared.exception and not prepared.stack
            and prepared.message in (None, "", UNKNOWN_EVENT_CODE))


class SafeLogFilter(logging.Filter):
    """Drop low-level placeholders from the application's own stderr handler."""

    def filter(self, record):
        try:
            return not _is_empty_unknown(record, _prepare_record(record))
        except Exception:
            return True  # Filtering faults must not hide diagnostics.


class SafeLogBridge(logging.Handler):
    """Root-logger bridge that forwards safe projections to OTel logs.

    ``otel_handler`` is a public injection point for tests (mirroring
    setup's ``sdk_loader``); by default the SDK ``LoggingHandler`` is
    created lazily, resolving the global LoggerProvider at construction."""

    MARKER = "_nagent_is_otel_log_bridge"

    def __init__(
        self,
        level: int = logging.NOTSET,
        otel_handler: Optional[logging.Handler] = None,
    ):
        super().__init__(level=level)
        if otel_handler is None:
            # Lazy SDK import: disabled/local-only paths never load it.
            from opentelemetry.sdk._logs import LoggingHandler

            otel_handler = LoggingHandler(level=level)
        self._otel_handler = otel_handler
        self.emit_failure_count = 0
        setattr(self, self.MARKER, True)
        self.addFilter(_reject_self_traffic)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Projection BEFORE bridging, on a per-handler copy. Missing the
            # bridge must not drop the independent OTel log record, and a
            # span-event failure must not drop the LogRecord either (each arm
            # is isolated below; failures are counted).
            prepared = _prepare_record(record)
            if _is_empty_unknown(record, prepared):
                return
            projected = project_record(record, _prepared=prepared)
            self._otel_handler.handle(projected)
            try:
                self.add_event_on_span(prepared, record)
            except Exception:
                self.emit_failure_count += 1
        except Exception:
            # Handler failures are swallowed and counted; logging must never
            # propagate into the business path.
            self.emit_failure_count += 1

    def add_event_on_span(
        self, prepared: "Prepared", record: logging.LogRecord
    ) -> None:
        """Attach the sanitized scene/body/exception to the current span.

        Only a recording span gets the event; attribute values are restricted
        to OTel scalar/string-array types (no nested dicts). Failures are
        observability-only and swallowed by the caller, and the independent
        LogRecord delivery never depends on this."""
        span = trace.get_current_span()
        if span is None or not getattr(span, "is_recording", lambda: False)():
            return
        attributes: Dict[str, object] = {
            "severity": record.levelname,
            "logger": record.name,
        }
        if prepared.message is not None:
            attributes["message"] = prepared.message
        if prepared.exception is not None:
            attributes["exception.type"] = prepared.exception["type"]
            attributes["exception.message"] = prepared.exception["message"]
            attributes["exception.stacktrace"] = prepared.exception["stacktrace"]
        if prepared.stack is not None:
            attributes["stack"] = prepared.stack
        for key, value in prepared.scene.items():
            if isinstance(value, (str, bool, int)):
                attributes[key] = value
            elif isinstance(value, float) and math.isfinite(value):
                attributes[key] = value
        attributes.update(_flatten_prepared_details(prepared))
        span.add_event(
            name=prepared.event,
            attributes=attributes,
            timestamp=int(record.created * 1_000_000_000),
        )


def _reject_self_traffic(record: logging.LogRecord) -> bool:
    return not _is_self_traffic(record)


# ---------------------------------------------------------------------------
# Root attachment (exactly one bridge)
# ---------------------------------------------------------------------------


def attach_observability_log_bridge(
    *, bridge: Optional[logging.Handler] = None, logger: Optional[logging.Logger] = None
) -> bool:
    """Attach at most one OTel log bridge to the root logger.

    Without an injected ``bridge``, a bridge is created only when
    observability is enabled AND a real (non-proxy) LoggerProvider is
    published. Local-only mode has no LoggerProvider (T3), hence no bridge.
    Returns True when a bridge is present after the call; idempotent.

    NOTE: the injected ``bridge`` is a public seam for tests only and
    bypasses the ``_bridge_allowed`` gating entirely (including the
    enabled/LoggerProvider conditions); production callers must not pass
    one. The MARKER check-then-set below is atomic under the registry lock
    so concurrent attaches cannot install two bridges."""
    with _registry_lock:
        root = logger if logger is not None else logging.getLogger()
        for handler in root.handlers:
            if getattr(handler, SafeLogBridge.MARKER, False):
                return True
        if bridge is None:
            if not _bridge_allowed():
                return False
            bridge = SafeLogBridge()
        root.addHandler(bridge)
        return True


def _bridge_allowed() -> bool:
    try:
        from app.infrastructure.observability.setup import get_observability_handle

        handle = get_observability_handle()
    except Exception:
        return False
    if handle is None or getattr(handle, "mode", "disabled") == "disabled":
        return False
    try:
        from opentelemetry._logs import get_logger_provider

        provider = get_logger_provider()
    except Exception:
        return False
    return provider is not None and type(provider).__name__ not in (
        "ProxyLoggerProvider",
        "_ProxyLoggerProvider",
    )


def _flatten_prepared_details(prepared):
    try:
        return _details_flattener(prepared.details) if _details_flattener is not None else {}
    except Exception:
        return {}
