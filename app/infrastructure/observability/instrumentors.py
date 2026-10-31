"""Automatic OTel instrumentation: FastAPI/httpx/sqlite3 (Infrastructure).

``instrument_app(app)`` installs FastAPI middleware instrumentation while
the middleware stack is still unbuilt (it must be called from
``create_app`` before returning, never from lifespan). ``instrument_clients``
registers global httpx/sqlite3 instrumentors once per process from shared
service assembly, including independent CLI/ACP processes.

Safety rules (spec-261024-otel-observability, Components 1 & 4):

- All auto-instrumentation spans are produced through a filtering tracer
  provider: attributes that are not on the explicit whitelist never reach
  the SDK (pre-write filtering; ended spans are immutable, so filtering
  afterwards is not an option).
- Attributes that cannot be reliably filtered are not collected at all:
  exception recording (message/stacktrace events) is disabled and status
  descriptions (which may embed exception messages) are stripped. Outbound
  HTTP completion is the sole fixed event built from projected metadata;
  arbitrary event payloads remain disabled.
- Excluded from span creation entirely: the exporter self-traffic URLs
  (the OTLP/Pyroscope endpoints, via the httpx excluded-URLs mechanism)
  and incoming HTTP outside the two conversation completion routes.
  Automatic HTTPX/SQLite clients only record under a valid parent operation.
  ASGI receive/send spans are disabled; the business server span remains.
- Instrumentation failures are isolated: ``instrument_app`` never raises
  into the application; on any failure it returns False.

Only imports OTel packages, ``app.config``, sibling safe logging helpers and
stdlib -- never interfaces/domain/business modules.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional
from urllib.parse import unquote, urlparse
from types import SimpleNamespace

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import SpanKind, Status, StatusCode

# Exact routes excluded from automatic spans; /static is excluded as a subtree.
EXCLUDED_HTTP_PATHS = ("/health", "/chat/observations/otel", "/favicon.ico")
TRACED_HTTP_PATHS = ("/chat/completions", "/v1/chat/completions")

# ---------------------------------------------------------------------------
# Attribute whitelist (the ONLY attributes auto-instrumentation may collect)
# ---------------------------------------------------------------------------

ALLOWED_AUTO_ATTRIBUTES = frozenset(
    {
        # HTTP: method / status / templated route / scheme / safe host
        "http.method",
        "http.request.method",
        "http.status_code",
        "http.response.status_code",
        "http.route",
        "http.scheme",
        "server.address",
        "server.port",
        "net.peer.name",
        "url.template",
        "nagent.http.caller",
        "error.type",
        # DB: system + operation type only (span name carries the
        # operation); statement/query text and parameters are dropped.
        "db.system",
        "db.system.name",
        "db.operation",
        "db.operation.name",
        "db.collection.name",
    }
)

# httpx excluded-URLs env keys (read once by HTTPXClientInstrumentor at
# instrument() time; self-traffic to the observability backends must not
# create spans).
_HTTPX_EXCLUDED_URLS_KEY = "OTEL_PYTHON_HTTPX_EXCLUDED_URLS"
_GENERIC_EXCLUDED_URLS_KEY = "OTEL_PYTHON_EXCLUDED_URLS"

_logger = logging.getLogger(__name__)

# Preserve endpoint structure while replacing identifiable resource values.
_HTTP_RESOURCE_SEGMENTS = frozenset({
    "users", "user", "people", "persons", "accounts", "customers", "clients",
    "tenants", "projects", "sessions", "conversations", "tasks", "runs",
    "messages", "memories", "files", "documents", "artifacts", "agents",
    "threads", "assistants", "models", "search", "downloads", "uploads",
    "workspaces", "peers", "conclusions",
})
_HTTP_ACTION_SEGMENTS = frozenset({
    "search", "list", "create", "batch", "count", "status", "health", "me",
    "metadata", "config", "schema", "query", "import", "export", "upload",
    "download", "content", "versions", "revisions", "completions", "add",
})
_HTTP_CREDENTIAL_SEGMENTS = frozenset({
    "token", "tokens", "key", "keys", "api-key", "api_key", "apikey", "secret",
    "secrets", "password", "credentials", "authorization",
})


def _http_route_template(path):
    """Keep route words; template recognized identifiers and sensitive segments."""
    from app.infrastructure.observability.logging import _sanitize

    path = path.split("?", 1)[0].split("#", 1)[0][:8192]
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    parts, previous = [], ""
    for segment in path.split("/"):
        if not segment:
            continue
        clean = _sanitize(segment)
        if clean != segment or previous in _HTTP_CREDENTIAL_SEGMENTS:
            part = "{redacted}"
        elif previous in _HTTP_RESOURCE_SEGMENTS and segment not in _HTTP_ACTION_SEGMENTS:
            part = "{id}"
        elif re.fullmatch(r"v\d+(?:\.\d+)*", segment):
            part = segment
        elif segment.isdecimal() or re.fullmatch(r"[0-9a-fA-F-]{16,}", segment):
            part = "{id}"
        elif re.fullmatch(r"[a-z][a-z_.-]{0,63}", segment):
            part = segment
        else:
            part = "{redacted}"
        if len("/".join(parts)) + len(part) > 495:
            parts.append("{redacted}")
            break
        parts.append(part)
        previous = segment.lower()
    return "/" + "/".join(parts)


def _http_caller():
    """Read code identity only, never arguments, locals or request content."""
    frame = sys._getframe(1)
    try:
        for _ in range(40):
            if frame is None:
                break
            module = frame.f_globals.get("__name__", "")
            if module.startswith("app.") and not module.startswith("app.infrastructure.observability."):
                return (module + ":" + frame.f_code.co_name)[:256]
            frame = frame.f_back
        return "external"
    finally:
        del frame


def _httpx_request_hook(span, request):
    try:
        if not isinstance(span, _FilteringSpan) or not span.get_span_context().is_valid:
            return
        from app.infrastructure.observability.logging import _sanitize

        url = request.url
        if isinstance(url, tuple):
            scheme, host, port, raw_path = url
            host = host.decode("ascii")
            path = raw_path.decode("utf-8", errors="replace").split("?", 1)[0].split("#", 1)[0]
        else:
            host, port, path = url.host, url.port, url.path
        method = request.method.decode("ascii") if isinstance(request.method, bytes) else request.method
        method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"} else "_OTHER"
        host = _sanitize(host)[:255] if re.fullmatch(r"[A-Za-z0-9.:-]{1,255}", host) else "other"
        route = _http_route_template(path)
        facts = {"http.request.method": method, "server.address": host,
                 "url.template": route, "nagent.http.caller": _sanitize(_http_caller())[:256]}
        if type(port) is int and 0 < port < 65536:
            facts["server.port"] = port
        span._http_attributes = {}
        span._http_client = True
        span.set_attributes(facts)
        span.update_name(f"{method} {host} {route}")
    except Exception:
        pass


def _httpx_response_hook(span, request, response):
    try:
        if isinstance(span, _FilteringSpan) and span._http_client:
            span.set_attribute("http.response.status_code", response.status_code)
    except Exception:
        pass


async def _async_httpx_request_hook(span, request):
    _httpx_request_hook(span, request)


async def _async_httpx_response_hook(span, request, response):
    _httpx_response_hook(span, request, response)


def _filter_attributes(attributes: Any) -> Optional[Dict[str, Any]]:
    """Project an attributes mapping onto the whitelist (pre-write)."""
    if not attributes:
        return None
    if isinstance(attributes, Mapping):
        items = attributes.items()
    else:
        items = attributes  # sequence of (key, value) pairs
    filtered: Dict[str, Any] = {}
    for key, value in items:
        if key in ALLOWED_AUTO_ATTRIBUTES:
            filtered[key] = value
    if isinstance(attributes, Mapping):
        statement = attributes.get("db.statement")
        if isinstance(statement, str):
            match = re.search(r'\b(?:FROM|INTO|UPDATE|TABLE)\s+["`\[]?([A-Za-z_][A-Za-z0-9_]{0,63})', statement, re.I)
            if match:
                filtered["db.collection.name"] = match.group(1)
    return filtered or None


# ---------------------------------------------------------------------------
# Filtering span / tracer / provider adapters (pre-write whitelist)
# ---------------------------------------------------------------------------


class _FilteringSpan(trace_api.Span):
    """Proxy span: unsafe writes never reach the backing SDK span.

    The proxy is also what stays current in the context, so any later
    instrumentation write (e.g. the FastAPI exception-handler middleware
    calling ``record_exception`` on the current span) goes through this
    filter too.
    """

    def __init__(self, span: Any, *, http_attributes=None):
        self._span = span
        self._http_attributes = dict(http_attributes) if http_attributes is not None else None
        self._http_started = time.monotonic()
        self._http_logged = False
        self._http_cancelled = False
        self._http_client = False
        self._http_error = None
        self._http_capture = None
        self._http_details = {}
        self._http_context = {}
        self._http_incomplete = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._span, name)

    def set_attribute(self, key: str, value: Any) -> None:
        if key in ALLOWED_AUTO_ATTRIBUTES:
            if self._http_attributes is not None:
                self._http_attributes[key] = value
            self._span.set_attribute(key, value)

    def set_attributes(self, attributes: Any) -> None:
        filtered = _filter_attributes(attributes)
        if filtered:
            if self._http_attributes is not None:
                self._http_attributes.update(filtered)
            self._span.set_attributes(filtered)

    def record_exception(
        self,
        exception: BaseException,
        attributes: Any = None,
        timestamp: Optional[int] = None,
        escaped: bool = False,
    ) -> None:
        # Cannot be reliably filtered (message/stacktrace) -> collection is
        # turned off entirely.
        return None

    def add_event(
        self,
        name: str,
        attributes: Any = None,
        timestamp: Optional[int] = None,
    ) -> None:
        # Event payloads are unfilterable -> collection is turned off
        # entirely. add_link / update_name are left delegating: links only
        # carry span contexts (ids), not payloads, and the span name is
        # bounded operation text produced by the instrumentation itself.
        return None

    def set_status(self, status: Any, description: Optional[str] = None) -> None:
        # Descriptions may embed exception messages; keep only the code.
        try:
            code = getattr(status, "status_code", status)
            self._span.set_status(Status(status_code=code))
        except Exception:
            pass

    def end(self, end_time: Optional[int] = None) -> None:
        if self._http_capture is not None:
            capture = self._http_capture
            capture.end_requested = True
            if not capture.ready:
                return
            try:
                capture.project()
            except Exception:
                self._http_capture = None
        if self._http_client:
            self._complete_http_client()
            self._span.end(end_time)
            return
        if self._http_attributes is not None and not self._http_logged:
            self._http_logged = True
            try:
                from app.infrastructure.observability.logging import log_http_request_completed, flatten_observation_details

                attrs = self._http_attributes
                status = attrs.get("http.response.status_code", attrs.get("http.status_code"))
                failed = getattr(getattr(self._span, "status", None), "status_code", None) == StatusCode.ERROR
                outcome = "cancelled" if self._http_cancelled else "error" if failed or (isinstance(status, int) and status >= 500) else (
                    "completed" if status is not None else "incomplete")
                # end() can run while a child is current (e.g. streaming send).
                # Bind the server span explicitly, including unsampled contexts.
                with trace_api.use_span(self, end_on_exit=False, record_exception=False,
                                        set_status_on_exception=False):
                    log_http_request_completed(
                        attrs.get("http.request.method", attrs.get("http.method")),
                        attrs.get("http.route"), status,
                        max(0.0, (time.monotonic() - self._http_started) * 1000), outcome,
                        **({"details": self._http_details} if self._http_details else {}),
                    )
                    flat = flatten_observation_details(self._http_details)
                    self._span.set_attributes(flat)
                    self._span.add_event("http_request_completed", attributes=flat)
            except Exception:
                pass  # Logging failure must never prevent ending the span.
        self._span.end(end_time)

    def _complete_http_client(self):
        if self._http_logged:
            return
        self._http_logged = True
        try:
            from app.infrastructure.observability.logging import log_http_client_completed, flatten_observation_details

            facts = dict(self._http_attributes)
            status = facts.get("http.response.status_code")
            outcome = "cancelled" if self._http_cancelled else "error" if self._http_error or (
                type(status) is int and status >= 400) else "incomplete" if self._http_incomplete else "completed" if status else "incomplete"
            facts.update(operation="http_client", outcome=outcome,
                         duration_ms=max(0.0, (time.monotonic() - self._http_started) * 1000))
            if self._http_error:
                facts["error.type"] = self._http_error
                self._span.set_status(Status(StatusCode.ERROR))
            facts["summary"] = (f"出站请求 {facts['http.request.method']} {facts['server.address']} "
                                f"{facts['url.template']}；调用位置 {facts['nagent.http.caller']}；"
                                f"状态 {status if status is not None else '无响应'}，{outcome}；"
                                f"耗时 {facts['duration_ms']:.1f} ms。")
            # This is the sole allowlisted event on an auto client span. The
            # generic proxy add_event/record_exception remain closed.
            try:
                flat = flatten_observation_details(self._http_details)
                self._span.set_attributes(flat)
                self._span.add_event("http_client_completed", attributes={**facts, **flat})
            except Exception:
                pass
            with trace_api.use_span(self, end_on_exit=False, record_exception=False,
                                    set_status_on_exception=False):
                log_http_client_completed({**self._http_context, **facts, "details": self._http_details})
        except Exception:
            pass

    def get_span_context(self) -> Any:
        return self._span.get_span_context()

    def is_recording(self) -> bool:
        return self._span.is_recording()

    def update_name(self, name: str) -> None:
        self._span.update_name(name)


class _FilteringTracer:
    """Tracer wrapper whose spans are filtering proxies."""

    def __init__(self, tracer: Any):
        self._tracer = tracer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tracer, name)

    def start_span(
        self,
        name: str,
        context: Optional[context_api.Context] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Any = None,
        links: Any = (),
        start_time: Optional[int] = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Any:
        # Auto client/DB work without an active operation is background noise.
        # Keep client spans inside traced ingress, Agent and other manual spans.
        if kind == SpanKind.CLIENT and not trace_api.get_current_span(context).get_span_context().is_valid:
            return _FilteringSpan(trace_api.INVALID_SPAN)
        try:
            span = self._tracer.start_span(
                name,
                context=context,
                kind=kind,
                attributes=_filter_attributes(attributes),
                links=links,
                start_time=start_time,
                record_exception=False,
                set_status_on_exception=False,
            )
        except TypeError:
            # Providers with narrower signatures (e.g. proxies).
            span = self._tracer.start_span(
                name, context=context, kind=kind, attributes=_filter_attributes(attributes)
            )
        return _FilteringSpan(
            span, http_attributes=_filter_attributes(attributes) or {}
            if kind == SpanKind.SERVER else None,
        )

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Optional[context_api.Context] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Any = None,
        links: Any = (),
        start_time: Optional[int] = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[Any]:
        # Exception/status recording is disabled at creation: unfilterable
        # data must not be collected in the first place.
        proxy = self.start_span(
            name,
            context=context,
            kind=kind,
            attributes=_filter_attributes(attributes),
            links=links,
            start_time=start_time,
            record_exception=False,
            set_status_on_exception=False,
        )
        # It must be an API Span, not just a duck-typed wrapper: get_current_span
        # otherwise returns INVALID_SPAN and silently starts unrelated traces.
        with trace_api.use_span(proxy, end_on_exit=False, record_exception=False,
                                set_status_on_exception=False):
            try:
                yield proxy
            except BaseException as exc:
                if proxy._http_client:
                    proxy._http_error = type(exc).__name__[:64]
                    proxy._http_cancelled = isinstance(exc, asyncio.CancelledError)
                    proxy.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                if end_on_exit:
                    proxy.end()


class _FilteringTracerProvider:
    """Wraps a tracer provider so every instrumentation tracer filters."""

    def __init__(self, provider: Any):
        self._provider = provider

    def get_tracer(self, instrumenting_module_name: str, *args: Any, **kwargs: Any):
        return _FilteringTracer(
            self._provider.get_tracer(instrumenting_module_name, *args, **kwargs)
        )


# ---------------------------------------------------------------------------
# Global registration state (exactly-once)
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_HTTPX_REGISTERED = False
_SQLITE3_REGISTERED = False
_SYSTEM_METRICS_REGISTERED = False
_HTTP_CAPTURE_REGISTERED = False


def global_registration_state() -> Dict[str, bool]:
    """Public introspection of the process-wide instrumentor registration."""
    with _LOCK:
        return {
            "httpx": _HTTPX_REGISTERED,
            "sqlite3": _SQLITE3_REGISTERED,
            "system_metrics": _SYSTEM_METRICS_REGISTERED,
        }


def _reset_for_tests() -> None:
    global _HTTPX_REGISTERED, _SQLITE3_REGISTERED, _SYSTEM_METRICS_REGISTERED
    with _LOCK:
        _HTTPX_REGISTERED = False
        _SQLITE3_REGISTERED = False
        _SYSTEM_METRICS_REGISTERED = False


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def _fastapi_excluded_urls() -> str:
    # Match the URL's actual path, never a path appearing in query values.
    # Include query strings and trailing slashes in exact-route exclusions.
    origin = r"^https?://[^/?#]+"
    paths = [re.escape(path) + r"/?(?:\?[^#]*)?$" for path in EXCLUDED_HTTP_PATHS]
    paths.append(r"/static(?:/[^?#]*)?/?(?:\?[^#]*)?$")
    allowed = "|".join(re.escape(path.lstrip("/")) for path in TRACED_HTTP_PATHS)
    paths.append(r"/(?!(?:" + allowed + r")/?(?:\?|$))[^?#]*(?:\?[^#]*)?$")
    return ",".join(origin + path for path in paths)


def _set_httpx_self_traffic_exclusion(settings: Any) -> None:
    """Exclude exporter/ingest self-traffic URLs from httpx spans.

    HTTPXClientInstrumentor reads ``OTEL_PYTHON_HTTPX_EXCLUDED_URLS`` once
    at instrument() time, so the patterns must be in the environment
    before global registration. Any pre-existing user patterns (specific
    or generic key) are preserved.
    """
    patterns = []
    for attr in ("otel_exporter_otlp_endpoint", "profiling_pyroscope_endpoint"):
        url = getattr(settings, attr, "") or ""
        host = urlparse(url).netloc.split("@")[-1] if url else ""
        if host:
            patterns.append("https?://" + host.replace(".", r"\."))
    if not patterns:
        return
    existing = os.environ.get(_HTTPX_EXCLUDED_URLS_KEY) or os.environ.get(
        _GENERIC_EXCLUDED_URLS_KEY, ""
    )
    merged = [p for p in patterns if p not in existing]
    if not merged:
        return
    combined = ",".join(([existing] if existing else []) + merged)
    os.environ[_HTTPX_EXCLUDED_URLS_KEY] = combined


# ---------------------------------------------------------------------------
# instrument_app
# ---------------------------------------------------------------------------


class _HttpAccessFactsMiddleware:
    """Keep response facts even when the server span is not sampled.

    Installed inside OTel: no new span; details use the shared projection.
    OTel normally writes response attributes only to recording spans.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        span = trace_api.get_current_span()
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        untraced = not isinstance(span, _FilteringSpan) or span._http_attributes is None or span._http_client
        started = time.monotonic()
        if untraced:
            path = scope.get("path", "")
            if (scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}
                    or path in EXCLUDED_HTTP_PATHS or path == "/static" or path.startswith("/static/")):
                return await self.app(scope, receive, send)
            # Management writes need a record, not another root trace.
            span = SimpleNamespace(_http_attributes={"http.request.method": scope["method"],
                                   "http.route": _http_route_template(path)},
                                   _http_details={}, _http_context={}, _http_cancelled=False,
                                   _http_incomplete=False, _http_capture=None)

        from .http_capture import Exchange
        from . import logging as observation_logging
        exchange = Exchange(span, scope=scope) if observation_logging._details_projector is not None else None
        if exchange:
            span._http_capture = exchange

        async def request_receive():
            message = await receive()
            if exchange and message["type"] == "http.request":
                exchange.request.append(message.get("body", b""))
                exchange.request.complete = not message.get("more_body", False)
            return message

        async def response_send(message):
            if message["type"] == "http.response.start":
                span._http_attributes["http.response.status_code"] = message["status"]
                if exchange:
                    from .http_capture import Body
                    exchange.response = Body({k.decode("latin1"): v.decode("latin1") for k, v in message.get("headers", [])})
            if exchange and message["type"] == "http.response.body":
                exchange.response.append(message.get("body", b""))
                if not message.get("more_body", False):
                    exchange.finish(complete=True)
            await send(message)

        try:
            await self.app(scope, request_receive, response_send)
        except asyncio.CancelledError as exc:
            span._http_cancelled = True
            if exchange:
                exchange.error = exc
            raise
        except Exception as exc:
            span._http_attributes.setdefault("http.response.status_code", 500)
            if exchange:
                exchange.error = exc
            raise
        finally:
            if exchange and not exchange.ready:
                exchange.finish(error=exchange.error)
            if untraced:
                try:
                    if exchange:
                        exchange.project()
                    status = span._http_attributes.get("http.response.status_code")
                    outcome = "cancelled" if span._http_cancelled else "error" if status is None or status >= 500 else "completed"
                    with trace_api.use_span(trace_api.INVALID_SPAN, end_on_exit=False):
                        observation_logging.log_http_request_completed(scope["method"], span._http_attributes["http.route"],
                            status, max(0, (time.monotonic() - started) * 1000), outcome, details=span._http_details)
                except Exception:
                    pass
            # ASGI instrumentation skips its final end() for non-recording spans
            # when a request aborts before sending its final response body.
            elif not span.is_recording() and not span._http_logged:
                span.end()


def instrument_app(
    app: Any,
    *,
    settings: Any = None,
    tracer_provider: Any = None,
) -> bool:
    """Install automatic instrumentation for ``app``.

    Must be called after the FastAPI app is constructed and before its
    middleware stack is built (i.e. from ``create_app``; never from
    lifespan). Registers the global httpx/sqlite3 instrumentors exactly
    once per process. Returns True when instrumentation is (or already
    was) installed on this app, False when skipped or on isolated
    failure. Never raises into the application.
    """
    try:
        return _instrument_app(app, settings, tracer_provider)
    except Exception as exc:
        # Observability failures never propagate into the app; report once
        # via a safe warning (exception type only, no raw payload).
        try:
            _logger.warning(
                "instrument_app: automatic instrumentation skipped (%s)",
                type(exc).__name__,
            )
        except Exception:
            pass
        return False


def _instrument_app(app: Any, settings: Any, tracer_provider: Any) -> bool:
    if settings is None:
        try:
            from app.config import Settings

            settings = Settings()
        except Exception:
            settings = None

    # FastAPI middleware instrumentation is only possible before the
    # middleware stack exists; a stack built without it can no longer be
    # wrapped (installing later would silently record nothing).
    # Already instrumented wins over the middleware-stack check: an
    # instrumented app whose stack was built (first request served) is
    # still correctly instrumented, and repeat calls must stay idempotent.
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        return True
    if getattr(app, "middleware_stack", None) is not None:
        return False

    provider = tracer_provider
    if provider is None:
        provider = trace_api.get_tracer_provider()
    filtering_provider = _FilteringTracerProvider(provider)

    clients_ready = instrument_clients(settings=settings, tracer_provider=provider)

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    app.add_middleware(_HttpAccessFactsMiddleware)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=filtering_provider,
        excluded_urls=_fastapi_excluded_urls(),
        exclude_spans=["receive", "send"],
    )

    return clients_ready


def instrument_clients(*, settings: Any = None, tracer_provider: Any = None) -> bool:
    """Register HTTPX capture and SQLite once for HTTP, CLI and ACP alike.

    Call after SDK setup and before constructing services/clients. Transport
    capture stays inside the HTTPX span and observes only business-consumed
    bytes, using the existing sanitized, bounded details projection.
    """
    try:
        return _instrument_clients(settings, tracer_provider)
    except Exception as exc:
        try:
            _logger.warning("instrument_clients: automatic instrumentation skipped (%s)",
                            type(exc).__name__)
        except Exception:
            pass
        return False


def _instrument_clients(settings: Any, tracer_provider: Any) -> bool:
    global _HTTPX_REGISTERED, _SQLITE3_REGISTERED, _HTTP_CAPTURE_REGISTERED

    provider = tracer_provider if tracer_provider is not None else trace_api.get_tracer_provider()
    filtering_provider = _FilteringTracerProvider(provider)

    # Global instrumentors: exactly once per process. The registration flag
    # is committed only AFTER the instrumentor call succeeds -- if
    # instrument() raises, the flag stays unset so a later call can retry
    # (a half-registered global would silently disable that instrumentation
    # for the whole process).
    if not _HTTPX_REGISTERED:
        # env must be set before the httpx instrumentor reads it.
        _set_httpx_self_traffic_exclusion(settings)
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        with _LOCK:
            if not _HTTPX_REGISTERED:
                if not _HTTP_CAPTURE_REGISTERED:
                    import httpx
                    import wrapt
                    from .http_capture import wrap_sync_transport, wrap_async_transport
                    wrapt.wrap_function_wrapper(httpx.HTTPTransport, "handle_request", wrap_sync_transport)
                    wrapt.wrap_function_wrapper(httpx.AsyncHTTPTransport, "handle_async_request", wrap_async_transport)
                    _HTTP_CAPTURE_REGISTERED = True
                HTTPXClientInstrumentor().instrument(
                    tracer_provider=filtering_provider,
                    request_hook=_httpx_request_hook,
                    response_hook=_httpx_response_hook,
                    async_request_hook=_async_httpx_request_hook,
                    async_response_hook=_async_httpx_response_hook,
                )
                _HTTPX_REGISTERED = True

    if not _SQLITE3_REGISTERED:
        from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor

        with _LOCK:
            if not _SQLITE3_REGISTERED:
                SQLite3Instrumentor().instrument(tracer_provider=filtering_provider)
                _SQLITE3_REGISTERED = True

    return True


def instrument_system_metrics(meter_provider: Any = None) -> bool:
    """Start process/system metrics (cpu/mem/disk/network) collection.

    These system.* / process.* instruments give dashboards a stable,
    always-present time series independent of business traffic. Idempotent
    per process (exactly-once registration, flag committed only after
    success, same contract as the httpx/sqlite3 globals). Never raises into
    the caller; returns True when collection is (or already was) started.
    """
    global _SYSTEM_METRICS_REGISTERED
    if _SYSTEM_METRICS_REGISTERED:
        return True
    try:
        from opentelemetry.instrumentation.system_metrics import (
            SystemMetricsInstrumentor,
        )

        with _LOCK:
            if _SYSTEM_METRICS_REGISTERED:
                return True
            SystemMetricsInstrumentor().instrument(
                meter_provider=meter_provider
            )
            _SYSTEM_METRICS_REGISTERED = True
        return True
    except Exception as exc:
        # Observability failures never propagate; report the exception type
        # only (no raw payload) and let the caller decide how to surface it.
        try:
            _logger.warning(
                "system metrics instrumentation skipped (%s)",
                type(exc).__name__,
            )
        except Exception:
            pass
        return False
