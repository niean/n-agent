"""T4: automatic instrumentation (FastAPI/httpx/sqlite3) safety contract.

WARNING (test isolation): this module registers the process-global httpx
and sqlite3 instrumentors (that registration is itself under test). They
stay installed for the whole pytest session afterwards -- every httpx
transport call and sqlite3 cursor execution in later tests will produce
(auto-filtered, no-op-provider) spans. Do not rely on sqlite3.connect /
httpx transports being unwrapped in other test modules.


Covers (plan-261024-otel-observability T4 / review item 15):
- FastAPI instrumentation installed before the middleware stack is built;
  repeat calls are idempotent (no double middleware / double globals).
- httpx/sqlite3 global registration happens exactly once.
- Span attributes are whitelisted BEFORE collection (pre-write filtering):
  httpx spans carry no URL query/fragment/userinfo, sqlite spans carry no
  statement/query text, FastAPI spans carry no raw path/URL and no
  exception events; status descriptions (which may carry exception
  messages) are stripped.
- Exclusions: exporter self-traffic (the OTLP endpoint URL), /health and
  the self-check route /chat/observations/otel produce no span.
- Instrumentation failures never propagate into the app.
"""
from __future__ import annotations

import sqlite3
import json
import logging
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from app.infrastructure.observability import instrumentors
from app.infrastructure.observability import logging as obs_logging

# Auto-instrumentation attribute whitelist (the module's public contract).
ALLOWED_ATTRS = instrumentors.ALLOWED_AUTO_ATTRIBUTES


@pytest.mark.parametrize("mode", ["normal", "stream", "error", "unsampled", "unsampled_error"])
def test_http_completion_log_matches_server_span(mode, monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
    from starlette.responses import StreamingResponse

    spans, logs, contexts = [], [], []
    provider = TracerProvider(sampler=ALWAYS_OFF if mode.startswith("unsampled") else ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    app = FastAPI()

    @app.post("/chat/completions")
    async def complete():
        contexts.append(trace.get_current_span().get_span_context())
        if mode.endswith("error"):
            raise RuntimeError("secret-error-body")
        if mode == "stream":
            async def chunks():
                with provider.get_tracer("test").start_as_current_span("stream-child"):
                    yield b"first"
                    yield b"last"
            return StreamingResponse(chunks())
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    instrumentors.instrument_app(app, settings=_FakeSettings(), tracer_provider=provider)

    class Capture(logging.Handler):
        def emit(self, record):
            if obs_logging.event_code_for(record) == obs_logging.HTTP_COMPLETED_EVENT:
                logs.append((json.loads(obs_logging.SafeEventJsonFormatter().format(record)),
                             obs_logging.project_record(record)))

    logger = logging.getLogger(obs_logging.__name__)
    monkeypatch.setattr(logger, "level", logging.INFO)
    handler = Capture()
    logger.addHandler(handler)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/chat/completions?secret=hidden", json={"secret": "body"})
            client.get("/health")
        assert response.status_code == (500 if mode.endswith("error") else 200)
        assert len(logs) == 1  # no duplicate end or health-check access event
        payload, projected = logs[0]
        assert payload["trace_id"] == format(contexts[0].trace_id, "032x")
        assert payload["span_id"] == format(contexts[0].span_id, "016x")
        assert payload["http.request.method"] == "POST"
        assert payload["http.route"] == "/chat/completions"
        assert payload["http.response.status_code"] == response.status_code
        assert payload["duration_ms"] >= 0
        assert payload["outcome"] == ("error" if mode.endswith("error") else "completed")
        for key in ("http.request.method", "http.route", "http.response.status_code", "duration_ms"):
            assert getattr(projected, key) == payload[key]
        assert "secret" not in json.dumps(payload)
        if mode == "stream":
            assert response.content == b"firstlast"
        if mode.startswith("unsampled"):
            assert not spans
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


def test_http_completion_log_failure_does_not_break_response_or_span(instrumented, monkeypatch):
    def broken(*args):
        raise RuntimeError("log unavailable")
    monkeypatch.setattr(obs_logging, "log_http_request_completed", broken)
    before = len(instrumented.spans)
    with TestClient(instrumented.app) as client:
        assert client.get("/v1/chat/completions").status_code == 200
    assert any(s.name == "GET /v1/chat/completions" for s in instrumented.spans[before:])


@pytest.mark.parametrize("sampled", [True, False])
def test_http_cancelled_request_logs_once_and_propagates(sampled, monkeypatch):
    import asyncio
    from opentelemetry import trace
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    calls = []
    monkeypatch.setattr(obs_logging, "log_http_request_completed", lambda *args, **kwargs: calls.append(args))
    provider = TracerProvider(sampler=ALWAYS_ON if sampled else ALWAYS_OFF)
    tracer = instrumentors._FilteringTracer(provider.get_tracer("cancel-test"))
    span = tracer.start_span("POST /chat/completions", kind=trace.SpanKind.SERVER,
                             attributes={"http.request.method": "POST", "http.route": "/chat/completions"})

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError()

    async def unused(*args):
        pass

    try:
        with trace.use_span(span, end_on_exit=False):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(instrumentors._HttpAccessFactsMiddleware(cancelled)(
                    {"type": "http"}, unused, unused))
            span.end()
            span.end()
        assert len(calls) == 1
        assert calls[0][-1] == "cancelled"
        assert calls[0][2] is None  # no fabricated status before a response
    finally:
        provider.shutdown()


class _ListSpanExporter:
    """Collects ended ReadableSpans in a thread-safe list."""

    def __init__(self, spans: list):
        self._spans = spans
        self._lock = threading.Lock()

    def export(self, spans):
        with self._lock:
            self._spans.extend(spans)
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self, timeout_millis=None):
        return True

    def force_flush(self, timeout_millis=None):
        return True


class _FakeSettings:
    def __init__(self) -> None:
        self.otel_exporter_otlp_endpoint = "http://127.0.0.1:4317"
        self.profiling_pyroscope_endpoint = ""


def _attrs(span) -> dict:
    return dict(span.attributes or {})


@pytest.fixture(scope="module")
def instrumented():
    """One instrumented FastAPI app + recording tracer provider.

    Global httpx/sqlite3 registration binds to this provider (exactly-once
    semantics are asserted separately against the module state).
    """
    spans: list = []
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))

    app = FastAPI()

    @app.get("/v1/chat/completions")
    def get_item(item_id: int = 7):
        return {"item_id": item_id}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/chat/observations/otel")
    def otel_selfcheck():
        return {"status": "healthy"}

    @app.post("/chat/completions")
    def boom():
        raise RuntimeError("BOOM secret-message-with-sentinel")

    first = instrumentors.instrument_app(
        app, settings=_FakeSettings(), tracer_provider=provider
    )
    sqlite_connect_after_first = sqlite3.connect
    second = instrumentors.instrument_app(
        app, settings=_FakeSettings(), tracer_provider=provider
    )
    return SimpleNamespace(
        spans=spans,
        app=app,
        provider=provider,
        first_call_installed=first,
        second_call_installed=second,
        sqlite_connect_after_first=sqlite_connect_after_first,
    )


@pytest.fixture(autouse=True)
def _clear_spans(instrumented):
    instrumented.spans.clear()
    yield
    instrumented.spans.clear()


def _server_spans(spans):
    # Server spans are the FastAPI request spans (kind SERVER).
    return [s for s in spans if str(s.kind) == "SpanKind.SERVER"]


def _client_spans(spans):
    return [s for s in spans if str(s.kind) == "SpanKind.CLIENT"]


# ---------------------------------------------------------------------------
# Installation, ordering and idempotency
# ---------------------------------------------------------------------------


def test_fastapi_installed_before_middleware_stack(instrumented):
    # The fixture instrumented the app before any request built the stack.
    assert instrumented.first_call_installed is True
    # instrumentation is flagged on the app itself
    assert getattr(instrumented.app, "_is_instrumented_by_opentelemetry", False)


def test_repeat_calls_idempotent(instrumented):
    # Second instrument_app call on the same app: no exception, reports
    # already-instrumented, no duplicate middleware installation.
    assert instrumented.second_call_installed is True
    state = instrumentors.global_registration_state()
    assert state["httpx"] is True
    assert state["sqlite3"] is True


def test_globals_registered_exactly_once(instrumented):
    # sqlite3.connect wrapper is not re-wrapped by the second call.
    assert sqlite3.connect is instrumented.sqlite_connect_after_first
    # httpx transports are wrapped by the global registration (0.66b1 wraps
    # HTTPTransport.handle_request, not the Client constructor).
    import httpx as _httpx

    assert hasattr(_httpx.HTTPTransport.handle_request, "__wrapped__")
    assert hasattr(_httpx.AsyncHTTPTransport.handle_async_request, "__wrapped__")


def test_install_after_middleware_stack_built_is_skipped(instrumented):
    app = FastAPI()
    # Build the middleware stack first (what happens when instrumentation
    # is attempted after the first request / at lifespan time).
    app.middleware_stack = app.build_middleware_stack()
    installed = instrumentors.instrument_app(
        app, settings=_FakeSettings(), tracer_provider=instrumented.provider
    )
    assert installed is False
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)


def test_already_instrumented_app_with_built_stack_returns_true(instrumented):
    # An instrumented app whose middleware stack was built (e.g. the first
    # request already served) is still correctly instrumented: the
    # already-instrumented check must win over the built-stack check, and
    # the repeat call must stay idempotent.
    app = FastAPI()
    assert (
        instrumentors.instrument_app(
            app, settings=_FakeSettings(), tracer_provider=instrumented.provider
        )
        is True
    )
    app.middleware_stack = app.build_middleware_stack()
    assert (
        instrumentors.instrument_app(
            app, settings=_FakeSettings(), tracer_provider=instrumented.provider
        )
        is True
    )


def test_instrumentation_failure_never_propagates():
    # A broken app object must not raise out of instrument_app.
    class Broken:
        pass

    assert instrumentors.instrument_app(Broken(), settings=_FakeSettings()) is False


# ---------------------------------------------------------------------------
# Span attribute whitelist (pre-collection filtering)
# ---------------------------------------------------------------------------


def test_fastapi_span_attributes_whitelisted(instrumented):
    with TestClient(instrumented.app) as client:
        client.get("/v1/chat/completions?item_id=7")
    server = _server_spans(instrumented.spans)
    assert server, "expected a server span for conversation ingress"
    attrs = _attrs(server[0])
    assert set(attrs) <= set(ALLOWED_ATTRS)
    # templated route is kept, raw path/URL/query are not
    assert attrs.get("http.route") == "/v1/chat/completions"
    for forbidden in ("http.url", "url.full", "http.target", "url.path", "http.path"):
        assert forbidden not in attrs


def test_fastapi_exception_events_and_status_description_blocked(instrumented):
    with TestClient(instrumented.app, raise_server_exceptions=True) as client:
        with pytest.raises(RuntimeError):
            client.post("/chat/completions")
    server = _server_spans(instrumented.spans)
    assert server
    span = server[0]
    # exception events (message/stacktrace) are never collected
    assert all(e.name == "http_request_completed" for e in span.events)
    # status description may carry exception messages -> stripped
    if span.status is not None:
        assert not getattr(span.status, "description", None)


def test_httpx_span_attributes_whitelisted_no_url_leak(instrumented):
    # Request URL carries query + fragment + userinfo; the request fails
    # but the span must still exist, without any of that content.
    with instrumented.provider.get_tracer("application").start_as_current_span("invoke_agent n-agent"), httpx.Client(timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError):
            client.get(
                "http://user:secret-pass@127.0.0.1:1/leak?token=SECRET&pw=hunter2#frag"
            )
    client_spans = _client_spans(instrumented.spans)
    assert client_spans, "expected an httpx client span"
    for span in client_spans:
        attrs = _attrs(span)
        assert set(attrs) <= set(ALLOWED_ATTRS)
        for forbidden in ("http.url", "url.full", "url.query", "url.fragment"):
            assert forbidden not in attrs
        for value in attrs.values():
            assert "SECRET" not in str(value)
            assert "hunter2" not in str(value)
            assert "secret-pass" not in str(value)


def test_sqlite_span_no_statement_text(instrumented):
    conn = sqlite3.connect(":memory:")
    with instrumented.provider.get_tracer("application").start_as_current_span("invoke_agent n-agent"):
        conn.cursor().execute(
            "SELECT 'hunter2' AS secret_password FROM sqlite_master WHERE name='tbl-secret'"
        ).fetchall()
    conn.close()
    db_spans = [
        s
        for s in instrumented.spans
        if str(s.kind) == "SpanKind.CLIENT" or s.name in ("SELECT", "SELECT ")
    ]
    assert db_spans, "expected an sqlite span"
    for span in db_spans:
        attrs = _attrs(span)
        assert set(attrs) <= set(ALLOWED_ATTRS)
        for forbidden in (
            "db.statement",
            "db.query.text",
            "db.statement.parameters",
        ):
            assert forbidden not in attrs
        assert "hunter2" not in span.name
        assert "tbl-secret" not in span.name


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_health_and_selfcheck_routes_excluded(instrumented):
    with TestClient(instrumented.app) as client:
        client.get("/health")
        client.get("/chat/observations/otel")
    for span in instrumented.spans:
        assert span.name not in ("GET /health", "GET /chat/observations/otel")
    for span in _server_spans(instrumented.spans):
        assert _attrs(span).get("http.route") not in ("/health", "/chat/observations/otel")


@pytest.mark.parametrize("path", [
    "/static/app.js", "/static/styles.css?v=2", "/static/missing.js",
    "/favicon.ico", "/health?probe=1", "/chat/observations/otel?refresh=1",
])
def test_ui_noise_does_not_create_server_spans(instrumented, path):
    with TestClient(instrumented.app) as client:
        client.get(path)
    assert not _server_spans(instrumented.spans)


def test_business_request_keeps_server_span_without_asgi_events(instrumented):
    with TestClient(instrumented.app) as client:
        response = client.get("/v1/chat/completions?item_id=42&next=/static/app.js")
    assert response.status_code == 200
    assert len(_server_spans(instrumented.spans)) == 1
    assert not any("http send" in s.name or "http receive" in s.name
                   for s in instrumented.spans)


def test_exporter_self_traffic_excluded(instrumented):
    # The OTLP endpoint host:port configured in settings must not produce
    # an httpx span (no recursive observability traffic).
    with httpx.Client(timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError):
            client.post("http://127.0.0.1:4317/v1/traces", content=b"would-be-export")
    for span in _client_spans(instrumented.spans):
        assert "4317" not in span.name
        assert "4317" not in str(_attrs(span))


class TestSystemMetrics:
    """T-refine: process/system metrics instrumentor (stable dashboards)."""

    def setup_method(self):
        instrumentors._reset_for_tests()

    def teardown_method(self):
        instrumentors._reset_for_tests()

    def test_registers_exactly_once_and_reports_started(self, monkeypatch):
        calls = []

        class _FakeInstrumentor:
            def instrument(self, *, meter_provider=None):
                calls.append(meter_provider)

        import opentelemetry.instrumentation.system_metrics as sm

        monkeypatch.setattr(sm, "SystemMetricsInstrumentor", _FakeInstrumentor)
        provider = object()
        assert instrumentors.instrument_system_metrics(provider) is True
        assert instrumentors.instrument_system_metrics(object()) is True
        assert calls == [provider], "second call must not re-register"
        assert instrumentors.global_registration_state()["system_metrics"] is True

    def test_failure_never_raises_and_flag_stays_unset(self, monkeypatch):
        class _Boom:
            def instrument(self, *, meter_provider=None):
                raise RuntimeError("no psutil")

        import opentelemetry.instrumentation.system_metrics as sm

        monkeypatch.setattr(sm, "SystemMetricsInstrumentor", _Boom)
        assert instrumentors.instrument_system_metrics(object()) is False
        # Flag committed only after success: a later working call can retry.
        monkeypatch.setattr(
            sm,
            "SystemMetricsInstrumentor",
            lambda: SimpleNamespace(instrument=lambda *, meter_provider: None),
        )
        assert instrumentors.instrument_system_metrics(object()) is True

    def test_missing_package_degrades_to_false(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _missing(name, *args, **kwargs):
            if "system_metrics" in name:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _missing)
        assert instrumentors.instrument_system_metrics(object()) is False
