"""N-KB outbound contract through the production global HTTPX registration.

Only the network send is replaced. The receiver deliberately extracts from
an empty context: sharing the pytest process must not fake remote propagation.
"""
import asyncio
import inspect
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import context, trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.domain.knowledge import KnowledgeBackendSearchRequest, KnowledgeSearchError
from app.infrastructure.knowledge.http_adapters import NkbKnowledgeRetriever
from app.infrastructure.observability import instrumentors as ins
from tests.infrastructure.test_knowledge_adapters import make_base


@pytest.fixture
def wire(monkeypatch):
    # Existing log tests temporarily assign logger.level directly. Do not let
    # our default-level INFO checks populate their inherited enabled cache.
    monkeypatch.setattr(logging.getLogger("app.infrastructure.observability.logging"), "_cache", {})
    # Save exact previous wrappers/provider bindings, including wrappers left by
    # test_instrumentors. Undoing our local patches restores them, not new ones.
    for transport, method in ((httpx.HTTPTransport, "handle_request"),
                              (httpx.AsyncHTTPTransport, "handle_async_request")):
        monkeypatch.setattr(transport, method, inspect.unwrap(getattr(transport, method)))
    monkeypatch.setattr(HTTPXClientInstrumentor(), "_is_instrumented_by_opentelemetry", False)
    monkeypatch.setattr(ins, "_HTTPX_REGISTERED", False)
    monkeypatch.setattr(ins, "_HTTP_CAPTURE_REGISTERED", False)
    # SQLite is unrelated to this boundary; preserve its process registration.
    monkeypatch.setattr(ins, "_SQLITE3_REGISTERED", True)
    monkeypatch.delenv("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", raising=False)
    monkeypatch.delenv("OTEL_PYTHON_EXCLUDED_URLS", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    receiver = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    receiver_exporter = InMemorySpanExporter()
    receiver.add_span_processor(SimpleSpanProcessor(receiver_exporter))
    received = []
    state = SimpleNamespace(status=200, received=received, provider=provider,
                            exporter=exporter, receiver_exporter=receiver_exporter)
    propagator = TraceContextTextMapPropagator()

    async def send(_transport, request):
        client = trace.get_current_span().get_span_context()
        extracted = propagator.extract(dict(request.headers), context=context.Context())
        remote = trace.get_current_span(extracted).get_span_context()
        with receiver.get_tracer("n-kb-test-receiver").start_as_current_span(
            "POST /retrieval/search", context=extracted, kind=trace.SpanKind.SERVER,
        ) as server:
            await asyncio.sleep(0)  # Force interleaving in the concurrency case.
            received.append(SimpleNamespace(request=request, client=client, remote=remote,
                                            server=server.get_span_context()))
        assert trace.get_current_span().get_span_context() == client
        return httpx.Response(state.status, json={"results": []})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)

    def install():
        app = FastAPI()
        assert ins.instrument_app(app, settings=SimpleNamespace(), tracer_provider=provider)

    state.install = install
    try:
        yield state
    finally:
        # No global uninstrument(): it would also unwrap pre-existing capture
        # layers. monkeypatch restores the exact saved methods and flags.
        provider.shutdown()
        receiver.shutdown()


async def search():
    return await NkbKnowledgeRetriever().search(
        make_base(), KnowledgeBackendSearchRequest(query="python", top_k=3, min_score=0.7),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sampled", [True, False])
@pytest.mark.parametrize("status", [200, 503])
async def test_search_propagates_client_context_and_preserves_protocol(wire, sampled, status):
    wire.install()
    wire.status = status
    incoming = TraceContextTextMapPropagator().extract({
        "traceparent": f"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-{int(sampled):02x}",
        "tracestate": "vendor=value",
    }, context=context.Context())
    tracer = wire.provider.get_tracer("n-agent-test")
    before = trace.get_current_span().get_span_context()
    with tracer.start_as_current_span("invoke_agent", context=incoming) as agent:
        with tracer.start_as_current_span("execute_tool search_knowledge") as tool:
            if status == 503:
                with pytest.raises(KnowledgeSearchError, match="N-KB search failed: HTTP 503"):
                    await search()
            else:
                assert (await search()).results == []
            assert trace.get_current_span().get_span_context() == tool.get_span_context()
    assert trace.get_current_span().get_span_context() == before
    item, = wire.received
    parent = tool.get_span_context()
    assert item.remote.is_valid and item.remote.is_remote
    assert item.remote.trace_id == parent.trace_id == agent.get_span_context().trace_id
    assert item.remote.span_id == item.client.span_id != parent.span_id
    assert item.server.trace_id == item.remote.trace_id
    assert item.server.span_id != item.remote.span_id
    assert bool(item.remote.trace_flags.sampled) == sampled
    assert bool(item.server.trace_flags.sampled) == sampled
    assert item.request.headers["tracestate"] == "vendor=value"
    assert item.request.headers["traceparent"] == (
        f"00-{parent.trace_id:032x}-{item.client.span_id:016x}-{int(sampled):02x}")
    assert item.request.method == "POST" and item.request.url.path == "/retrieval/search"
    assert json.loads(item.request.content) == {
        "query": "python", "top_k": 3, "min_score": 0.7, "filters": {"dataset_id": "dataset-1"},
    }
    assert "baggage" not in item.request.headers and "x-trace-id" not in item.request.headers
    clients = [s for s in wire.exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT]
    if sampled:
        client, = clients
        assert client.context.span_id == item.remote.span_id
        assert client.parent.span_id == parent.span_id
        server, = wire.receiver_exporter.get_finished_spans()
        assert server.context.span_id == item.server.span_id
        assert server.parent.is_remote
        assert server.parent.span_id == client.context.span_id
        assert server.parent.trace_id == client.context.trace_id
    else:
        assert wire.exporter.get_finished_spans() == ()
        assert wire.receiver_exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_concurrent_searches_keep_independent_traces(wire):
    wire.install()
    tracer = wire.provider.get_tracer("n-agent-test")

    async def run():
        with tracer.start_as_current_span("execute_tool search_knowledge", context=context.Context()) as tool:
            await search()
            assert trace.get_current_span().get_span_context() == tool.get_span_context()
            return tool.get_span_context()

    parents = await asyncio.gather(run(), run())
    assert len({p.trace_id for p in parents}) == 2
    assert {r.remote.trace_id for r in wire.received} == {p.trace_id for p in parents}
    clients = [s for s in wire.exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT]
    assert len(clients) == 2
    assert {(s.context.trace_id, s.parent.span_id) for s in clients} == {
        (p.trace_id, p.span_id) for p in parents}


@pytest.mark.asyncio
@pytest.mark.parametrize("instrumented", [True, False])
async def test_independent_probe_or_disabled_instrumentation_preserves_search(wire, instrumented):
    if instrumented:
        wire.install()
    token = context.attach(context.Context())
    try:
        await NkbKnowledgeRetriever().probe(make_base())
    finally:
        context.detach(token)
    item, = wire.received
    assert "traceparent" not in item.request.headers
    assert not item.remote.is_valid
    assert wire.exporter.get_finished_spans() == ()
