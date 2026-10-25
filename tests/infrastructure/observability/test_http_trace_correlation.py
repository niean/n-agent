"""Real SDK regression: ingress HTTP and Agent share a trace; polling is silent."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.application import telemetry_semantics as ts
from app.infrastructure.observability import instrumentors
from tests.infrastructure.observability.test_instrumentors import _ListSpanExporter, _FakeSettings


def instrument(app, provider):
    # Do not register process-global HTTPX/SQLite instrumentors against a
    # short-lived provider: other test modules own those global registrations.
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=instrumentors._FilteringTracerProvider(provider),
        excluded_urls=instrumentors._fastapi_excluded_urls(),
        exclude_spans=["receive", "send"],
    )


@pytest.mark.parametrize("path", ["/chat/completions", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_http_agent_parentage_with_real_sdk(monkeypatch, path, stream):
    spans = []
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    monkeypatch.setattr(ts, "get_tracer", lambda: provider.get_tracer("application"))
    app = FastAPI()

    async def run():
        await asyncio.sleep(0)
        with ts.SemanticsTelemetry().agent_run("session", "dashboard", "request"):
            with ts.chat_span("gpt-4o"):
                pass

    @app.post(path)
    async def completion():
        if stream:
            async def events():
                # The real runner likewise creates its task during SSE iteration.
                await asyncio.create_task(run())
                yield "data: [DONE]\n\n"
            return StreamingResponse(events(), media_type="text/event-stream")
        await run()
        return {"ok": True}

    instrument(app, provider)
    with TestClient(app) as client:
        assert client.post(path).status_code == 200
    server = next(s for s in spans if s.kind == trace.SpanKind.SERVER)
    agent = next(s for s in spans if s.name == "invoke_agent n-agent")
    chat = next(s for s in spans if s.name == "chat gpt-4o")
    assert agent.context.trace_id == server.context.trace_id
    assert agent.parent is not None and agent.parent.span_id == server.context.span_id
    assert chat.parent.span_id == agent.context.span_id
    assert not any("http send" in s.name or "http receive" in s.name for s in spans)
    provider.shutdown()


@pytest.mark.parametrize("path", ["/chat/sessions", "/chat/observations", "/chat/providers", "/v1/models", "/chat", "/static/app.js"])
def test_non_agent_http_requests_create_no_spans(path):
    spans = []
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    app = FastAPI()
    @app.get(path)
    async def poll():
        return {"ok": True}
    instrument(app, provider)
    with TestClient(app) as client:
        assert client.get(path).status_code == 200
    assert not spans
    provider.shutdown()


def test_auto_client_without_parent_is_not_recorded():
    spans = []
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    tracer = instrumentors._FilteringTracerProvider(provider).get_tracer("auto")
    with tracer.start_as_current_span("GET", kind=trace.SpanKind.CLIENT):
        pass
    assert not spans
    provider.shutdown()
