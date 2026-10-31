import json
import logging

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from app.infrastructure.observability import instrumentors as ins
from app.infrastructure.observability import logging as obs
from tests.infrastructure.observability.test_instrumentors import _ListSpanExporter


def test_caller_identifies_application_function_without_reading_arguments():
    namespace = {"__name__": "app.infrastructure.llm.catalog", "observe": ins._http_caller}
    exec("def list_models(secret):\n    return observe()\n", namespace)
    assert namespace["list_models"]("PRIVATE") == "app.infrastructure.llm.catalog:list_models"


@pytest.mark.parametrize(("path", "template"), [
    ("/internal/catalog/status", "/internal/catalog/status"),
    ("/api/v2/provider-models/metadata", "/api/v2/provider-models/metadata"),
    ("/users/alice/messages/123", "/users/{id}/messages/{id}"),
    ("/v1/sessions/550e8400-e29b-41d4-a716-446655440000/history", "/v1/sessions/{id}/history"),
    ("/api/tasks/abc123/actions/retry", "/api/tasks/{id}/actions/retry"),
    ("/files/private-report.pdf/download", "/files/{id}/download"),
    ("/users/%E5%BC%A0%E4%B8%89/profile", "/users/{id}/profile"),
    ("/users/%2561lice/profile", "/users/{id}/profile"),
    ("/token/private-credential/status", "/token/{redacted}/status"),
    ("/search/alice@example.com/results", "/search/{id}/results"),
    ("/private/PRIVATE", "/private/{redacted}"),
    ("/v1/memories/search", "/v1/memories/search"),
    ("/v3/memories/add/", "/v3/memories/add"),
    ("/v3/workspaces/alice/peers/bob/card", "/v3/workspaces/{id}/peers/{id}/card"),
    ("/v3/workspaces/alice/conclusions/secret-fact", "/v3/workspaces/{id}/conclusions/{id}"),
    ("/v1/models?token=PRIVATE#PRIVATE", "/v1/models"),
    ("/api/12345/health", "/api/{id}/health"),
])
def test_http_route_keeps_structure_and_templates_dynamic_segments(path, template, monkeypatch):
    monkeypatch.setattr(obs, "_sanitizer", obs.LogSanitizer())
    assert ins._http_route_template(path) == template


def test_http_route_uses_secret_catalog_and_bounds_output(monkeypatch):
    sanitizer = obs.LogSanitizer()
    sanitizer.configure_secret_catalog({"private-credential"})
    monkeypatch.setattr(obs, "_sanitizer", sanitizer)
    assert ins._http_route_template("/api/private-credential/status") == "/api/{redacted}/status"
    assert len(ins._http_route_template("/catalog" * 1000)) <= 512


@pytest.mark.parametrize("sampled", [True, False])
@pytest.mark.parametrize("status", [200, 503, None])
def test_outbound_request_has_target_event_and_correlated_log(sampled, status, monkeypatch):
    spans, logs = [], []
    provider = TracerProvider(sampler=ALWAYS_ON if sampled else ALWAYS_OFF)
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    monkeypatch.setattr(obs, "_sanitizer", obs.LogSanitizer())
    monkeypatch.setattr(ins, "_http_caller", lambda: "app.infrastructure.llm.model_catalog:list_models", raising=False)

    class Capture(logging.Handler):
        def emit(self, record):
            if obs.event_code_for(record) == "http_client_completed":
                logs.append(json.loads(obs.SafeEventJsonFormatter().format(record)))

    logger = logging.getLogger(obs.__name__)
    monkeypatch.setattr(logger, "level", logging.INFO)
    handler = Capture()
    logger.addHandler(handler)

    def respond(request):
        if status is None:
            raise httpx.ConnectError("sensitive exception token=PRIVATE", request=request)
        return httpx.Response(status)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    HTTPXClientInstrumentor.instrument_client(
        client, tracer_provider=ins._FilteringTracerProvider(provider),
        request_hook=ins._httpx_request_hook, response_hook=ins._httpx_response_hook)
    try:
        with provider.get_tracer("test").start_as_current_span("agent") as parent:
            if status is None:
                with pytest.raises(httpx.ConnectError):
                    client.get("https://user:PRIVATE@models.example/v1/models?token=PRIVATE#PRIVATE")
            else:
                assert client.get("https://user:PRIVATE@models.example/v1/models?token=PRIVATE#PRIVATE").status_code == status
        assert len(logs) == 1
        log = logs[0]
        assert log["server.address"] == "models.example"
        assert log["url.template"] == "/v1/models"
        assert log["nagent.http.caller"].endswith(":list_models")
        assert log["span_id"] != format(parent.get_span_context().span_id, "016x")
        assert "models.example" in log["message"] and "list_models" in log["message"]
        assert log["outcome"] == ("completed" if status == 200 else "error")
        assert "PRIVATE" not in json.dumps(log)
        if sampled:
            span = next(s for s in spans if s.context.span_id == int(log["span_id"], 16))
            assert span.parent.span_id == parent.get_span_context().span_id
            assert span.attributes["server.address"] == "models.example"
            assert len([e for e in span.events if e.name == "http_client_completed"]) == 1
            assert "PRIVATE" not in str(span.attributes) + str(span.events)
        else:
            assert not spans
    finally:
        client.close()
        logger.removeHandler(handler)
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "failure", "cancelled", "orphan", "log_failure"])
async def test_async_outbound_lifecycle_and_redacted_unknown_path(mode, monkeypatch):
    import asyncio
    from opentelemetry import trace

    spans, logs = [], []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    monkeypatch.setattr(obs, "_sanitizer", obs.LogSanitizer())

    def capture(facts):
        logs.append((dict(facts), trace.get_current_span().get_span_context().span_id))
        if mode == "log_failure":
            raise RuntimeError("log unavailable")
    monkeypatch.setattr(obs, "log_http_client_completed", capture)

    async def respond(request):
        if mode == "cancelled":
            raise asyncio.CancelledError()
        if mode == "failure":
            raise httpx.ConnectError("PRIVATE", request=request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    HTTPXClientInstrumentor.instrument_client(
        client, tracer_provider=ins._FilteringTracerProvider(provider),
        request_hook=ins._async_httpx_request_hook, response_hook=ins._async_httpx_response_hook)

    async def run():
        if mode in ("failure", "cancelled"):
            with pytest.raises(asyncio.CancelledError if mode == "cancelled" else httpx.ConnectError):
                await client.get("https://api.example/private/PRIVATE?token=PRIVATE")
        else:
            assert (await client.get("https://api.example/private/PRIVATE?token=PRIVATE")).status_code == 200
    try:
        if mode == "orphan":
            await run()
            assert logs == [] and spans == []
            return
        with provider.get_tracer("test").start_as_current_span("agent"):
            await run()
        assert len(logs) == 1
        facts, span_id = logs[0]
        assert facts["url.template"] == "/private/{redacted}"
        assert facts["outcome"] == {"failure": "error", "cancelled": "cancelled"}.get(mode, "completed")
        child = next(s for s in spans if s.context.span_id == span_id)
        assert len([e for e in child.events if e.name == "http_client_completed"]) == 1
        assert "PRIVATE" not in str(child.attributes) + str(child.events) + str(facts)
    finally:
        await client.aclose()
        provider.shutdown()
