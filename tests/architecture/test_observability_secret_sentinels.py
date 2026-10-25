"""Secret sentinel tests for the observability signal outlets (T10).

Spec-261024 requirement (Acceptance Criteria 4 / plan T10 S2): construct
payloads containing sentinels (HTTP URLs/headers, SQL, log exceptions, tool
payloads) and assert NONE of the observation outlets contain the raw text:

- manual GenAI scalar attributes and the approved sanitized nagent.details channel
- auto-instrumentation filtering (no payload collection)
- log body (JSON stderr formatter + OTel projection copy + bridge)
- ring-buffer snapshot (and the self-check API that serves it)
- Profile projection files (folded-stack projection)
- GenAI sensitive Opt-in attributes (messages / arguments / results /
  system_instructions / tool.definitions) are never collected
- exporter self-traffic cannot recurse through the log bridge
- polling /health and the self-check route never fills the span buffer

Unit-level: real opentelemetry-sdk in-process with a list exporter; no
backend, no network. Trusted server-side identifiers (session_id, run_id,
tool.call.id) are recorded by design and are therefore NOT sentinel
carriers in these tests -- the sentinels model hostile payload content.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace.status import Status, StatusCode

from app.application import telemetry_semantics as ts
from app.config import Settings
from app.infrastructure.observability import instrumentors
from app.infrastructure.observability import logging as obs_logging
from app.infrastructure.observability import setup as obs_setup
from app.infrastructure.observability.profiling import project_folded_stacks

# ---------------------------------------------------------------------------
# Sentinels (unique, greppable, never legitimate content)
# ---------------------------------------------------------------------------

SENTINEL = "E2EOTELSENTINEL-7f3a-secret"
S_URL = f"http://alice:hunter2@sentinel.example.com/p?token={SENTINEL}#frag"
S_SQL = f"SELECT '{SENTINEL}' AS pw FROM users WHERE id=1"
S_HEADER = f"Bearer {SENTINEL}"
S_TOOL_PAYLOAD = json.dumps({"command": SENTINEL, "Authorization": S_HEADER})
S_MESSAGE = f"user prompt {SENTINEL}"
S_EXCEPTION = RuntimeError(f"boom {SENTINEL} TraceStack")

# GenAI sensitive Opt-in attributes (spec: never collected, default off).
GENAI_OPTIN_KEYS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "gen_ai.system_instructions",
    "gen_ai.tool.definitions",
)

# Metric label dimensions that must never appear on any instrument
# (spec: session_id/run_id/trace_id/user input are not label dimensions;
# nagent.source IS a legitimate nagent.turns dimension per the spec table).
FORBIDDEN_LABELS = (
    "session_id",
    "run_id",
    "trace_id",
    "user_input",
    "gen_ai.conversation.id",
    "nagent.run_id",
)


def _ring_allowed_keys() -> set[str]:
    """The exact attribute keys the span ring buffer may store (mirrors
    setup.py: GenAI + nagent.* constants + error.type)."""
    keys = {"error.type"}
    for name, value in vars(ts).items():
        if name.startswith(("GEN_AI_", "NAGENT_")) and isinstance(value, str):
            keys.add(value)
    return keys


# ---------------------------------------------------------------------------
# Shared real-SDK capture provider (module-local; the global provider is
# NEVER set in-process -- that global is irreversible and would leak into
# other test modules, so the manual-span tree runs in a subprocess)
# ---------------------------------------------------------------------------

_PROVIDER = TracerProvider(sampler=ALWAYS_ON)


class _ListSpanExporter:
    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self, timeout_millis=None):
        return True

    def force_flush(self, timeout_millis=None):
        return True


_PROVIDER.add_span_processor(SimpleSpanProcessor(_ListSpanExporter()))


def _no_sentinel(value: object) -> bool:
    return SENTINEL not in str(value)


class _Capture:
    """Test-local span processor + exporter (does not touch globals)."""

    def __init__(self, spans: list):
        self._spans = spans

    def on_start(self, span, parent_context=None):
        return None

    def _on_ending(self, span):
        return None

    def on_end(self, span):
        self._spans.append(span)

    def export(self, spans):
        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self, timeout_millis=None):
        return True

    def force_flush(self, timeout_millis=None):
        return True


# ---------------------------------------------------------------------------
# 1. Manual GenAI spans (telemetry_semantics) never carry payload sentinels
# ---------------------------------------------------------------------------

# Runs in a subprocess: the semantics helpers resolve the GLOBAL tracer
# provider, and publishing a real one is irreversible (spec: no global
# resets), so the pytest process must stay untouched.
_MANUAL_SPAN_SCRIPT = """
import json
import uuid

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from app.application import telemetry_semantics as ts

SENTINEL = "E2EOTELSENTINEL-7f3a-secret"

captured = []


class _E:
    def export(self, spans):
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self, timeout_millis=None):
        return True

    def force_flush(self, timeout_millis=None):
        return True


provider = TracerProvider(sampler=ALWAYS_ON)
provider.add_span_processor(SimpleSpanProcessor(_E()))
trace.set_tracer_provider(provider)

with ts.invoke_agent_span(
    session_id="conv-" + uuid.uuid4().hex[:8],
    source="dashboard",
    run_id="run-t10-1",
) as root:
    trace_id = root.get_span_context().trace_id
    with ts.chat_span(model="gpt-4o-mini", provider="openai", iteration=1):
        pass
    with ts.execute_tool_span(
        tool="calculator",
        call_id="call_t10_1",
        policy_outcome="allowed",
        risk_level="safe",
        iteration=1,
    ):
        pass
    with ts.compress_span(before_tokens=10, after_tokens=2, outcome="compressed"):
        pass

out = []
for span in captured:
    if span.context.trace_id != trace_id:
        continue
    status = span.status
    code = getattr(getattr(status, "status_code", None), "name", "")
    out.append({
        "name": span.name,
        "attrs": dict(span.attributes or {}),
        "events": len(span.events or []),
        "status": code,
    })
print("SPAN_TREE " + json.dumps({"trace_id": format(trace_id, "032x"), "spans": out}))
"""


def test_manual_genai_span_tree_attributes_are_whitelisted():
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", _MANUAL_SPAN_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(repo_root),
    )
    assert proc.returncode == 0, (
        f"manual span tree subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    marker = next(
        line for line in proc.stdout.splitlines() if line.startswith("SPAN_TREE")
    )
    payload = json.loads(marker[len("SPAN_TREE "):])
    spans = payload["spans"]
    names = sorted(span["name"] for span in spans)
    assert names == sorted(
        [
            ts.invoke_agent_span_name(),
            ts.chat_span_name("gpt-4o-mini"),
            ts.execute_tool_span_name("calculator"),
            ts.compress_span_name(),
        ]
    )
    allowed_keys = _ring_allowed_keys()
    for span in spans:
        attrs = span["attrs"]
        assert set(attrs) <= allowed_keys, (
            f"span {span['name']} carries non-whitelisted attribute keys: "
            f"{set(attrs) - allowed_keys}"
        )
        assert span["events"] == 0, (
            f"span {span['name']} recorded events (payload risk)"
        )
        for value in attrs.values():
            assert _no_sentinel(value), f"sentinel leaked via {span['name']}"
        assert _no_sentinel(span["name"])
    # source passes through the bounded mapping, never raw
    root = next(s for s in spans if s["name"] == ts.invoke_agent_span_name())
    assert root["attrs"][ts.NAGENT_SOURCE] == "dashboard"


def test_span_name_helpers_never_embed_raw_payloads():
    assert ts.chat_span_name(S_URL) == "chat other"
    assert ts.chat_span_name(S_TOOL_PAYLOAD) == "chat other"
    assert ts.execute_tool_span_name(S_SQL) == "execute_tool other"
    assert ts.execute_tool_span_name(S_HEADER) == "execute_tool other"
    assert ts.chat_span_name("x" * 4096) == "chat other"
    assert ts.map_model_name(S_URL) == "other"
    assert ts.map_tool_name(S_TOOL_PAYLOAD) == "other"
    assert ts.map_provider_name(SENTINEL) == "other"
    assert ts.map_source(S_SQL) == "other"
    assert ts.map_error_type(S_EXCEPTION) == "other"
    assert ts.map_end_reason(S_MESSAGE) == "other"
    assert ts.map_kind(S_URL) == "other"
    assert ts.map_outcome(SENTINEL) == "other"


def test_genai_optin_attributes_have_no_constants_or_helpers():
    module_values = {
        value for value in vars(ts).values() if isinstance(value, str)
    }
    for key in GENAI_OPTIN_KEYS:
        assert key not in module_values, (
            f"telemetry_semantics defines the sensitive Opt-in key {key}"
        )
    with open(ts.__file__, encoding="utf-8") as handle:
        text = handle.read()
    for key in GENAI_OPTIN_KEYS:
        assert key not in text, (
            f"telemetry_semantics mentions the Opt-in key {key}; sensitive "
            "GenAI opt-in attributes must have no collection path; the approved channel is nagent.details"
        )


def test_span_helpers_have_no_payload_parameter():
    """The span helpers are fixed-shape: no caller can funnel prompt,
    response or tool payloads through them."""
    for helper in (
        ts.invoke_agent_span,
        ts.chat_span,
        ts.execute_tool_span,
        ts.compress_span,
    ):
        params = set(inspect.signature(helper).parameters)
        for forbidden in (
            "messages",
            "prompt",
            "payload",
            "arguments",
            "result",
            "input",
            "output",
            "attributes",
            "instructions",
        ):
            assert forbidden not in params, (
                f"{helper.__name__} exposes a payload-shaped parameter "
                f"'{forbidden}'"
            )


def test_metric_dimensions_exclude_identity_labels():
    for instrument in ts.INSTRUMENTS.values():
        for dimension in instrument.allowed_dimensions:
            assert dimension not in FORBIDDEN_LABELS, (
                f"{instrument.name} allows forbidden label {dimension}"
            )


# ---------------------------------------------------------------------------
# 2. Auto-instrumentation filtering span (pre-write whitelist)
# ---------------------------------------------------------------------------


def test_filtering_span_blocks_payload_attributes_events_and_status():
    provider = TracerProvider(sampler=ALWAYS_ON)
    spans: list = []
    provider.add_span_processor(SimpleSpanProcessor(_Capture(spans)))
    tracer = instrumentors._FilteringTracerProvider(provider).get_tracer("t10")

    with tracer.start_as_current_span(
        "op",
        attributes={
            "http.url": S_URL,
            "db.statement": S_SQL,
            "http.method": "GET",  # whitelisted
        },
    ) as span:
        span.set_attribute("url.full", S_URL)
        span.set_attribute("gen_ai.input.messages", S_TOOL_PAYLOAD)
        span.set_attributes({"db.query.text": S_SQL, "http.route": "/p/{id}"})
        span.record_exception(S_EXCEPTION)
        span.add_event("exception", {"exception.message": str(S_EXCEPTION)})
        span.set_status(Status(StatusCode.ERROR, f"desc {SENTINEL}"))

    assert spans, "expected one exported span"
    exported = spans[0]
    attrs = dict(exported.attributes or {})
    assert set(attrs) <= set(instrumentors.ALLOWED_AUTO_ATTRIBUTES)
    assert attrs.get("http.method") == "GET"
    assert attrs.get("http.route") == "/p/{id}"
    for forbidden in ("http.url", "url.full", "db.statement", "db.query.text"):
        assert forbidden not in attrs
    assert list(exported.events or []) == []
    assert not getattr(exported.status, "description", None)
    assert all(_no_sentinel(v) for v in attrs.values())


def test_filtering_span_blocks_genai_optin_keys():
    provider = TracerProvider(sampler=ALWAYS_ON)
    spans: list = []
    provider.add_span_processor(SimpleSpanProcessor(_Capture(spans)))
    tracer = instrumentors._FilteringTracerProvider(provider).get_tracer("t10")
    with tracer.start_as_current_span("op") as span:
        for key in GENAI_OPTIN_KEYS:
            span.set_attribute(key, S_TOOL_PAYLOAD)
        span.set_attribute("gen_ai.system_instructions", S_MESSAGE)
    assert spans
    attrs = dict(spans[0].attributes or {})
    for key in GENAI_OPTIN_KEYS:
        assert key not in attrs, f"Opt-in attribute {key} was collected"


# ---------------------------------------------------------------------------
# 3. Ring-buffer snapshot (the data the self-check API serves)
# ---------------------------------------------------------------------------


def _sdk_span_with_sentinel_payloads():
    span = _PROVIDER.get_tracer("t10-rb").start_span("payload-span")
    span.set_attributes(
        {
            "http.url": S_URL,
            "http.request.header.authorization": S_HEADER,
            "db.statement": S_SQL,
            "db.statement.parameters": (SENTINEL,),
            "exception.stacktrace": str(S_EXCEPTION),
            "gen_ai.input.messages": S_MESSAGE,
            "gen_ai.output.messages": S_TOOL_PAYLOAD,
            "gen_ai.tool.call.arguments": S_TOOL_PAYLOAD,
            "gen_ai.tool.call.result": S_SQL,
            "gen_ai.system_instructions": S_MESSAGE,
            "gen_ai.tool.definitions": S_TOOL_PAYLOAD,
            "custom.freeform": SENTINEL,
        }
    )
    span.set_status(Status(StatusCode.ERROR, f"status {SENTINEL}"))
    return span


def test_ring_buffer_snapshot_drops_payload_and_optin_attributes():
    ring = obs_setup._RingBufferSpanProcessor()
    ring.on_end(_sdk_span_with_sentinel_payloads())
    snapshot = ring.snapshot_spans()
    assert len(snapshot) == 1
    entry = snapshot[0]
    # The snapshot is plain JSON: serialize and grep it for every sentinel
    # form (URL, header, SQL, payload, exception, status description).
    serialized = json.dumps(entry, ensure_ascii=False, default=str)
    assert SENTINEL not in serialized, (
        f"ring buffer leaked sentinel content: {serialized[:400]}"
    )
    assert "hunter2" not in serialized
    allowed_keys = _ring_allowed_keys()
    assert set(entry["attributes"]) <= allowed_keys
    for key in GENAI_OPTIN_KEYS:
        assert key not in entry["attributes"], (
            f"ring buffer stored sensitive Opt-in attribute {key}"
        )
    # status keeps only the code name, never the description
    assert entry["status"] == "ERROR"


def test_selfcheck_api_response_contains_no_sentinel():
    ring = obs_setup._RingBufferSpanProcessor()
    ring.on_end(_sdk_span_with_sentinel_payloads())
    snapshot = {
        "status": "healthy",
        "mode": "otlp",
        "instance_id": "inst",
        "sample_ratio": 1.0,
        "semconv_version": ts.SEMCONV_VERSION,
        "instruments": [],
        "signals": {
            "traces": {"status": "healthy", "delivery": "succeeded"},
            "metrics": {"status": "healthy", "delivery": "unknown"},
            "logs": {"status": "healthy", "delivery": "unknown"},
            "profile": {"status": "disabled", "delivery": "disabled"},
        },
        "spans": ring.snapshot_spans(),
        "dropped_after_deadline": 0,
    }
    from app.interfaces.http.dashboard import create_dashboard_router

    app = FastAPI()
    app.include_router(
        create_dashboard_router(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            lambda: {},
            observability_snapshot=lambda: snapshot,
            settings=Settings(_env_file=None),
        )
    )
    client = TestClient(app)
    response = client.get("/chat/observations/otel")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    body = response.text
    assert SENTINEL not in body, "self-check API leaked sentinel content"
    assert "hunter2" not in body
    assert response.json()["status"] == "healthy"


# ---------------------------------------------------------------------------
# 4. Log outlets: JSON stderr formatter, projection copy, OTel bridge
# ---------------------------------------------------------------------------


def _record(msg, args=(), level=logging.ERROR, exc_info=None, name="app.t10"):
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
        func="test",
    )


@pytest.fixture
def sentinel_sanitizer():
    """Install the shared sanitizer with the sentinel as a known secret.

    Spec-261027: registered/vetted templates regain a readable body that is
    redacted via the known-secret catalog + fixed patterns. The sentinel
    models a real API key that lives in the InformationFlowService catalog."""
    sanitizer = obs_logging.LogSanitizer()
    sanitizer.configure_secret_catalog({SENTINEL})
    obs_logging.prepare_sanitizer(sanitizer)
    yield sanitizer
    obs_logging.prepare_sanitizer(None)


def test_json_formatter_unknown_event_leaks_nothing(sentinel_sanitizer):
    formatter = obs_logging.SafeEventJsonFormatter()
    out = formatter.format(
        _record(
            "user said %s via %s",
            args=(S_MESSAGE, S_URL),
            exc_info=(RuntimeError, S_EXCEPTION, None),
        )
    )
    payload = json.loads(out)
    for field in (
        "timestamp",
        "severity",
        "logger",
        "event",
        "trace_id",
        "span_id",
        "session_id",
        "service.name",
        "service.instance.id",
    ):
        assert field in payload
    assert payload["event"] == "application_log"
    assert SENTINEL not in out
    assert "hunter2" not in out
    assert payload["exception"] is not None


def test_json_formatter_registered_event_restores_redacted_body(sentinel_sanitizer):
    template = "t10 sentinel probe %s -> %s"
    obs_logging.register_safe_event(template, "t10_sentinel_probe")
    formatter = obs_logging.SafeEventJsonFormatter()
    out = formatter.format(_record(template, args=(SENTINEL, S_SQL)))
    payload = json.loads(out)
    assert payload["event"] == "t10_sentinel_probe"
    assert "[REDACTED]" in payload["message"]
    assert SENTINEL not in out
    assert "hunter2" not in out


def test_projection_copy_is_safe_and_original_untouched(sentinel_sanitizer):
    template = "t10 projection probe %s"
    obs_logging.register_safe_event(template, "t10_projection_probe")
    original = _record(
        template,
        args=(S_EXCEPTION,),
        exc_info=(RuntimeError, S_EXCEPTION, None),
    )
    projected = obs_logging.project_record(original)
    # registered target -> the projected body is the RESTORED redacted body
    assert projected.msg == "t10 projection probe boom [REDACTED] TraceStack"
    assert projected.args == ()
    assert projected.exc_info is None
    assert getattr(projected, "exception.type") == "RuntimeError"
    assert "[REDACTED]" in getattr(projected, "exception.stacktrace")
    # caller-side handlers keep the original record untouched
    assert original.msg == template
    assert original.args == (S_EXCEPTION,)
    assert original.exc_info is not None
    # the projected body (what the SDK handler would emit) has no payload
    assert SENTINEL not in projected.getMessage()
    assert SENTINEL not in repr(projected)
    # an unknown record collapses to the fixed event code (no restored body)
    unknown = obs_logging.project_record(_record("free text " + SENTINEL))
    assert unknown.msg == "free text [REDACTED]"
    assert SENTINEL not in unknown.getMessage()
    assert unknown.message == "free text [REDACTED]"


def test_bridge_span_event_redacts_sentinel(sentinel_sanitizer):
    """Spec-261027: span events carry a REDACTED message, never raw payloads."""
    from opentelemetry.sdk import trace as sdk_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    template = "t10 span-event %s"
    obs_logging.register_safe_event(template, "t10_span_event")
    try:
        tracer = provider.get_tracer("t10-span-event")
        span = tracer.start_span("run")
        tok = trace.use_span(span, end_on_exit=False)
        tok.__enter__()
        try:
            rec = _record(template, args=(S_HEADER,))
            dummy = type("_DummyHandler", (), {"handle": lambda self, r: None})()
            obs_logging.SafeLogBridge(otel_handler=dummy).add_event_on_span(
                obs_logging._prepare_record(rec), rec
            )
        finally:
            tok.__exit__(None, None, None)
            span.end()
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        events = list(finished[0].events)
        assert len(events) == 1
        assert events[0].name == "t10_span_event"
        assert "[REDACTED]" in events[0].attributes["message"]
        assert SENTINEL not in str(events)
    finally:
        try:
            exporter.shutdown()
        except Exception:
            pass


def test_bridge_rejects_self_traffic_and_never_recurses():
    seen: list[logging.LogRecord] = []

    class _FakeOtelHandler(logging.Handler):
        def emit(self, record):
            seen.append(record)
            # simulate exporter/transport self-logging while inside the
            # bridge: the guard must break the cycle, not recurse forever
            logging.getLogger("opentelemetry.sdk.batch").error(
                "internal exporter noise %s", SENTINEL
            )

    bridge = obs_logging.SafeLogBridge(otel_handler=_FakeOtelHandler())
    root = logging.getLogger()
    root.addHandler(bridge)
    try:
        root.handle(_record("t10 bridge probe %s", args=(S_MESSAGE,)))
        # self-traffic records never reach the OTel handler at all
        root.handle(_record("exporter noise", name="opentelemetry.sdk.export"))
        root.handle(_record("grpc noise", name="grpc.framework"))
    finally:
        root.removeHandler(bridge)
    assert len(seen) == 1, "bridge must project exactly once (no recursion)"
    projected = seen[0]
    assert projected.msg == obs_logging.UNKNOWN_EVENT_CODE
    assert SENTINEL not in projected.getMessage()


def test_httpx_self_traffic_exclusion_covers_exporter_and_pyroscope():
    key = instrumentors._HTTPX_EXCLUDED_URLS_KEY
    generic = instrumentors._GENERIC_EXCLUDED_URLS_KEY
    saved, saved_generic = os.environ.get(key), os.environ.get(generic)
    try:
        os.environ.pop(key, None)
        os.environ.pop(generic, None)
        instrumentors._set_httpx_self_traffic_exclusion(
            SimpleNamespace(
                otel_exporter_otlp_endpoint="http://otel-collector:4317",
                profiling_pyroscope_endpoint="http://pyroscope:4040",
            )
        )
        patterns = os.environ[key]
        assert "otel-collector" in patterns
        assert "pyroscope" in patterns
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        if saved_generic is None:
            os.environ.pop(generic, None)
        else:
            os.environ[generic] = saved_generic


def test_excluded_http_paths_cover_health_and_selfcheck():
    assert "/health" in instrumentors.EXCLUDED_HTTP_PATHS
    assert "/chat/observations/otel" in instrumentors.EXCLUDED_HTTP_PATHS
    excluded = instrumentors._fastapi_excluded_urls()
    import re
    patterns = excluded.split(",")
    def excluded_url(url):
        return any(re.search(pattern, url) for pattern in patterns)
    assert excluded_url("http://localhost/health")
    assert excluded_url("http://localhost/health?probe=1")
    assert excluded_url("http://localhost/chat/observations/otel?refresh=1")
    assert excluded_url("http://localhost/health-records")
    assert not excluded_url("http://localhost/chat/completions?next=/health")


# ---------------------------------------------------------------------------
# 5. Profile projection (folded stacks)
# ---------------------------------------------------------------------------

_RAW_STACKS = "\n".join(
    [
        # thread name / non-frame noise carrying the sentinel
        f"thread-{SENTINEL};GIL 7",
        # legit frames
        "app.worker (app/application/agent_graph.py:100) 3",
        "calc (app/infrastructure/tools/builtin.py:71) 1",
        # absolute path under the (fake) workspace with sentinel content
        f"secret_func (/Users/niean/ws-{SENTINEL}/app/tool.py:42) 2",
        # dynamic-code filename (unfilterable -> dropped)
        f"evil (<string:{SENTINEL}>:1) 4",
        # exception-sampling segment with a secret
        f"boom (app/x.py:9);exception {SENTINEL} 1",
    ]
)


def test_profile_projection_keeps_safe_frames_only():
    out = project_folded_stacks(
        _RAW_STACKS, strip_prefixes=(f"/Users/niean/ws-{SENTINEL}",)
    )
    assert "app.worker" in out
    assert "calc" in out
    assert SENTINEL not in out, f"profile projection leaked sentinel: {out!r}"
    assert "<string" not in out
    assert "/Users/niean" not in out  # workspace prefix stripped
    assert out.endswith("\n")


def test_profile_projection_empty_when_no_safe_frame_survives():
    # only unfilterable frames: dynamic-code filenames and non-frame noise
    out = project_folded_stacks(
        f"evil (<string:{SENTINEL}>:1) 3\nthread-{SENTINEL} 9\n<generator> 2\n"
    )
    assert out == ""


# ---------------------------------------------------------------------------
# 6. Polling must not fill the span buffer (self-check exclusion)
# ---------------------------------------------------------------------------


def test_polling_health_and_selfcheck_never_fills_ring_buffer():
    provider = TracerProvider(sampler=ALWAYS_ON)
    spans: list = []
    provider.add_span_processor(_Capture(spans))
    ring = obs_setup._RingBufferSpanProcessor()
    provider.add_span_processor(ring)

    app = FastAPI()

    @app.get("/chat/completions")
    def get_item():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/chat/observations/otel")
    def otel_selfcheck():
        return {"status": "healthy"}

    # Same wiring as instrumentors._instrument_app for the FastAPI part
    # (without touching the process-global httpx/sqlite registration).
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=instrumentors._FilteringTracerProvider(provider),
        excluded_urls=instrumentors._fastapi_excluded_urls(),
    )
    with TestClient(app) as client:
        client.get("/chat/completions")
        for _ in range(120):
            client.get("/health")
            client.get("/chat/observations/otel")
    buffered = ring.snapshot_spans()
    names = [entry["name"] for entry in buffered]
    assert not any("health" in name for name in names), names
    assert not any("observations/otel" in name for name in names), names
    assert names, "conversation ingress must still be traced"
    assert all(name.startswith("GET /chat/completions") for name in names), names


def test_approved_details_channel_redacts_and_stays_out_of_ring_and_auto_spans(monkeypatch, sentinel_sanitizer):
    """Content has one approved sanitized channel; auto spans/ring stay scalar."""
    from app.application.telemetry_details import configure_details, validate_details, flatten_details
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor, InMemoryLogRecordExporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    configure_details(sentinel_sanitizer.sanitize)
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    ring = obs_setup._RingBufferSpanProcessor()
    provider.add_span_processor(ring)
    logs = InMemoryLogRecordExporter()
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
    # Exercise the same SDK bridge as production; acknowledge its known upstream deprecation.
    with pytest.warns(DeprecationWarning, match="LoggingHandler.*deprecated"):
        sdk_handler = LoggingHandler(logger_provider=log_provider)
    bridge = obs_logging.SafeLogBridge(otel_handler=sdk_handler)
    obs_logging.configure_execution_context(ts.execution_log_snapshot, validate_details=validate_details, flatten_details=flatten_details)
    monkeypatch.setattr(ts, 'get_tracer', lambda: provider.get_tracer('approved-details'))
    ts_logger = logging.getLogger(ts.__name__)
    previous = ts_logger.level
    ts_logger.setLevel(logging.INFO)
    ts_logger.addHandler(bridge)
    payloads = []
    class JsonSink(logging.Handler):
        def emit(self, record):
            payloads.append(json.loads(obs_logging.SafeEventJsonFormatter().format(record)))
    sink = JsonSink()
    ts_logger.addHandler(sink)
    obs_logging.register_safe_event(ts.EXECUTION_LOG_TEMPLATE, 'execution_completed')
    try:
        with ts.SemanticsTelemetry().begin_chat_attempt(model='gpt-4o-mini', stream=False) as attempt:
            attempt.set_input([{'role': 'user', 'content': S_MESSAGE}], tools=[{'password': 'structured-secret'}])
            attempt.set_output({'role': 'assistant', 'content': 'a' * 3000})
        span = spans.get_finished_spans()[0]
        payload = next(p for p in payloads if p.get('operation') == 'chat')
        details = payload['details']
        assert '[REDACTED]' in details['input_messages']['content']
        assert details['output_messages']['truncated'] is True
        assert len(details['output_messages']['content'].encode()) <= 2048
        assert details['output_messages']['original_bytes'] > 2048
        flat = flatten_details(details)
        log_record = next(r.log_record for r in logs.get_finished_logs() if r.log_record.attributes.get('operation') == 'chat')
        for key, value in flat.items():
            assert span.attributes[key] == log_record.attributes[key] == value
            assert any(e.attributes.get(key) == value for e in span.events)
        assert SENTINEL not in str(span.attributes) + str(log_record.attributes) + json.dumps(payload)
        assert 'structured-secret' not in str(flat)
        assert all(not key.startswith('nagent.details.') for entry in ring.snapshot_spans() for key in entry['attributes'])
        auto = instrumentors._FilteringTracerProvider(provider).get_tracer('auto').start_span('auto')
        for key, value in flat.items():
            auto.set_attribute(key, value)
        auto.end()
        assert not any(k.startswith('nagent.details.') for k in spans.get_finished_spans()[-1].attributes)
    finally:
        ts_logger.removeHandler(sink)
        ts_logger.removeHandler(bridge)
        ts_logger.setLevel(previous)
        obs_logging.configure_execution_context(None)
        configure_details(None)
        log_provider.shutdown()
        provider.shutdown()
