import json
import logging
import threading
import asyncio
from urllib.error import HTTPError
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

from app.application import telemetry_semantics as ts
from app.application import telemetry_details as td
from app.application.external_memory_manager import ExternalMemoryManager
from app.infrastructure.memory.external.http_client import ExternalMemoryHttpClient
from app.infrastructure.memory.external.mem0 import Mem0Adapter
from app.infrastructure.memory.external.honcho import HonchoAdapter
from app.infrastructure.memory.external.holographic import HolographicAdapter
from app.infrastructure.observability import logging as logs
from app.infrastructure.observability.sanitizer import LogSanitizer


@pytest.fixture
def sdk(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("memory-test")
    monkeypatch.setattr(ts, "get_tracer", lambda: tracer)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    sanitizer = LogSanitizer()
    sanitizer.configure_secret_catalog({"PRIVATE"})
    monkeypatch.setattr(td, "_config", (sanitizer.sanitize, 2048))
    monkeypatch.setattr(logs, "_sanitizer", sanitizer)
    monkeypatch.setattr(logs, "_details_projector", td.project_details)
    monkeypatch.setattr(logs, "_details_flattener", td.flatten_details)
    monkeypatch.setattr(logs, "_details_validator", td.validate_details)
    monkeypatch.setattr(logs, "_event_registry", dict(logs._event_registry))
    yield tracer, exporter
    provider.shutdown()
    # Logging caches isEnabledFor results; clear them for suites patching level.
    logger = logging.getLogger(logs.__name__)
    logger.setLevel(logger.level)


def http_client(monkeypatch, payload=None, failure=None):
    client = ExternalMemoryHttpClient()
    requests = []

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self):
            self.body = json.dumps(payload or {"results": [{"memory": "likes tea"}]}).encode()

        def read(self, size):
            body, self.body = self.body[:size], self.body[size:]
            return body

        def close(self):
            pass

    def open_request(request, **kwargs):
        requests.append(request)
        if failure:
            raise failure
        return Response()

    monkeypatch.setattr(client, "_check_url", lambda url: None)
    monkeypatch.setattr(client, "_opener", lambda: SimpleNamespace(open=open_request))
    return client, requests


def test_external_memory_http_has_client_span_and_propagation(sdk, monkeypatch):
    tracer, exporter = sdk
    client, requests = http_client(monkeypatch)
    with tracer.start_as_current_span("agent") as parent:
        result = client.post("https://memory.example/v3/memories/search/?token=PRIVATE",
                             json={"query": "tea", "api_key": "PRIVATE"},
                             headers={"Authorization": "Bearer PRIVATE"})
    spans = [s for s in exporter.get_finished_spans() if s.kind is trace.SpanKind.CLIENT]
    assert len(spans) == 1
    child = spans[0]
    assert child.parent.span_id == parent.get_span_context().span_id
    assert requests[0].get_header("Traceparent").split("-")[2] == format(child.context.span_id, "016x")
    assert child.attributes["http.response.status_code"] == 200
    assert "tea" in child.attributes["nagent.details.http_request.content"]
    assert "likes tea" in child.attributes["nagent.details.http_response.content"]
    assert "PRIVATE" not in str(child.attributes) + str(child.events)
    assert result["results"][0]["memory"] == "likes tea"


def test_mem0_prefetch_worker_keeps_request_trace(sdk, monkeypatch):
    tracer, exporter = sdk
    client, _ = http_client(monkeypatch)
    adapter = Mem0Adapter(http_client=client, config={"api_key": "test"})
    with tracer.start_as_current_span("agent") as parent:
        assert "likes tea" in adapter.prefetch("tea", session_id="session")
    spans = [s for s in exporter.get_finished_spans() if s.kind is trace.SpanKind.CLIENT]
    assert len(spans) == 1
    assert spans[0].context.trace_id == parent.get_span_context().trace_id


@pytest.mark.parametrize("provider", ["mem0", "honcho", "holographic", "multi-project", "builtin"])
def test_manager_observes_automatic_memory_calls(sdk, provider):
    tracer, exporter = sdk
    adapter = SimpleNamespace(
        name=provider, is_available=lambda: True, get_tool_schemas=lambda: [],
        initialize=lambda **kwargs: None,
        prefetch=lambda *args, **kwargs: "recalled tea",
        sync_turn=lambda *args, **kwargs: None,
    )
    manager = ExternalMemoryManager()
    manager.add_provider(adapter)
    with tracer.start_as_current_span("agent") as parent:
        manager.prefetch_all("tea", session_id="session", enabled_override=[provider])
        manager.sync_all("tea", "ok", session_id="session", agent_context="primary", enabled_override=[provider])
    spans = exporter.get_finished_spans()
    for operation in ("prefetch", "sync"):
        span = next(s for s in spans if s.name == f"memory.{operation} {provider}")
        assert span.parent.span_id == parent.get_span_context().span_id
        assert "tea" in span.attributes["nagent.details.input_messages.content"]


@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
@pytest.mark.parametrize("mode", ["success", "http_error", "network_error", "oversize", "observation_failure", "cancel"])
def test_shared_http_lifecycle(sdk, monkeypatch, method, mode):
    tracer, exporter = sdk
    failure = {
        "http_error": HTTPError("https://memory.example/", 401, "PRIVATE", {}, None),
        "network_error": OSError("token=PRIVATE"),
        "cancel": asyncio.CancelledError(),
    }.get(mode)
    client, requests = http_client(monkeypatch, failure=failure)
    if mode == "oversize":
        client._max_bytes = 1
    if mode == "observation_failure":
        monkeypatch.setattr(logs, "_details_projector", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")))
    with tracer.start_as_current_span("agent") as parent:
        if failure is not None or mode == "oversize":
            with pytest.raises(type(failure) if failure is not None else ValueError):
                getattr(client, method)("https://memory.example/v3/workspaces/private-workspace/peers/private-peer/card")
        else:
            assert getattr(client, method)("https://memory.example/v3/workspaces/private-workspace/peers/private-peer/card")
        assert trace.get_current_span() is parent
    spans = [s for s in exporter.get_finished_spans() if s.kind is trace.SpanKind.CLIENT]
    assert len(spans) == 1 and len(requests) == 1
    span = spans[0]
    assert span.attributes["url.template"] == "/v3/workspaces/{id}/peers/{id}/card"
    assert span.status.status_code.name == ("ERROR" if failure is not None or mode == "oversize" else "UNSET")
    assert "PRIVATE" not in str(span.attributes) + str(span.events)
    assert len([e for e in span.events if e.name == "http_client_completed"]) == 1
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("provider", ["mem0", "honcho"])
def test_handled_sync_failure_is_not_reported_as_success(sdk, monkeypatch, provider):
    tracer, exporter = sdk
    client, _ = http_client(monkeypatch, failure=OSError("token=PRIVATE"))
    adapter_type = Mem0Adapter if provider == "mem0" else HonchoAdapter
    adapter = adapter_type(http_client=client, config={"api_key": "test", "workspace_id": "private"})
    manager = ExternalMemoryManager()
    manager.add_provider(adapter)
    with tracer.start_as_current_span("agent"):
        manager.sync_all("tea", "ok", session_id="session", agent_context="primary", enabled_override=[provider])
    span = next(s for s in exporter.get_finished_spans() if s.name == f"memory.sync {provider}")
    assert span.attributes["nagent.outcome"] == "failed"
    assert span.status.status_code.name == "ERROR"
    assert "PRIVATE" not in str(span.attributes) + str(span.events)


def test_mem0_late_worker_retains_original_trace(sdk, monkeypatch):
    from app.infrastructure.memory.external import mem0

    tracer, exporter = sdk
    release, entered = threading.Event(), threading.Event()
    client, _ = http_client(monkeypatch)
    adapter = Mem0Adapter(http_client=client, config={"api_key": "test"})
    original_search = adapter._search_backend

    def delayed(query):
        entered.set()
        assert release.wait(2)
        return original_search(query)

    monkeypatch.setattr(adapter, "_search_backend", delayed)
    monkeypatch.setattr(mem0, "_PREFETCH_WAIT_SECS", 0)
    manager = ExternalMemoryManager()
    manager.add_provider(adapter)
    try:
        with tracer.start_as_current_span("first") as first:
            assert manager.prefetch_all("tea", session_id="first", enabled_override=["mem0"]) == ""
            assert entered.wait(2)
        with tracer.start_as_current_span("second") as second:
            release.set()
            adapter._prefetch_thread.join(2)
            assert not adapter._prefetch_thread.is_alive()
            assert trace.get_current_span() is second
        spans = exporter.get_finished_spans()
        worker = next(s for s in spans if s.name == "memory.search mem0")
        request = next(s for s in spans if s.kind is trace.SpanKind.CLIENT)
        entry = next(s for s in spans if s.name == "memory.prefetch mem0")
        assert entry.attributes["nagent.outcome"] == "timed_out"
        assert worker.context.trace_id == first.get_span_context().trace_id
        assert worker.context.trace_id != second.get_span_context().trace_id
        assert request.parent.span_id == worker.context.span_id
        assert worker.parent.span_id == entry.context.span_id
    finally:
        release.set()
        if adapter._prefetch_thread:
            adapter._prefetch_thread.join(2)


def test_holographic_real_store_recall_and_tool_failure(sdk, tmp_path):
    tracer, exporter = sdk
    adapter = HolographicAdapter(config={"db_path": str(tmp_path / "facts.db")})
    adapter.initialize("session")
    manager = ExternalMemoryManager()
    manager.add_provider(adapter)
    try:
        with tracer.start_as_current_span("agent"):
            result = manager.handle_tool_call("fact_store", {"action": "add", "content": "tea preference"},
                                             agent_context="primary", session_id="session", enabled_override=["holographic"])
            assert json.loads(result)["success"]
            assert "tea preference" in manager.prefetch_all("tea preference", session_id="session", enabled_override=["holographic"])
            result = manager.handle_tool_call("fact_search", {"action": "invalid"},
                                             agent_context="primary", session_id="session", enabled_override=["holographic"])
            assert json.loads(result)["success"] is False
        spans = exporter.get_finished_spans()
        tools = [s for s in spans if s.name == "memory.tool holographic"]
        assert [s.attributes["nagent.outcome"] for s in tools] == ["success", "failed"]
        recall = next(s for s in spans if s.name == "memory.prefetch holographic")
        assert "tea preference" in recall.attributes["nagent.details.output_messages.content"]
    finally:
        adapter.shutdown()


def test_http_without_parent_has_no_orphan_span(sdk, monkeypatch):
    _, exporter = sdk
    client, requests = http_client(monkeypatch)
    assert client.get("https://memory.example/v3/memories")
    assert not exporter.get_finished_spans()
    assert requests[0].get_header("Traceparent") is None


def test_unsampled_parent_still_propagates_and_logs(monkeypatch):
    provider = TracerProvider(sampler=ALWAYS_OFF)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    records = []
    monkeypatch.setattr(logs, "log_http_client_completed", lambda facts: records.append(dict(facts)))
    client, requests = http_client(monkeypatch)
    try:
        with provider.get_tracer("unsampled").start_as_current_span("agent") as parent:
            client.get("https://memory.example/v3/memories")
        carrier = requests[0].get_header("Traceparent")
        assert carrier.split("-")[1] == format(parent.get_span_context().trace_id, "032x")
        assert int(carrier.split("-")[3], 16) & 1 == 0
        assert len(records) == 1
        assert records[0]["outcome"] == "completed"
    finally:
        provider.shutdown()

