"""End-to-end information must survive projection without changing business IO."""
import json
import logging

import httpx
import pytest
import wrapt
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from app.application import telemetry_details as details
from app.infrastructure.observability import instrumentors as ins
from app.infrastructure.observability import logging as obs
from tests.infrastructure.observability.test_instrumentors import _ListSpanExporter


@pytest.fixture
def content_wiring(monkeypatch):
    sanitizer = obs.LogSanitizer()
    sanitizer.configure_secret_catalog({"PRIVATE-CREDENTIAL"})
    monkeypatch.setattr(obs, "_sanitizer", sanitizer)
    details.configure_details(sanitizer.sanitize)
    obs.configure_execution_context(None, validate_details=details.validate_details,
                                    flatten_details=details.flatten_details,
                                    project_details=details.project_details)
    yield
    obs.configure_execution_context(None)
    details.configure_details(None)


def test_application_logs_and_unregistered_exceptions_are_readable(content_wiring):
    record = logging.LogRecord("app.work", logging.INFO, __file__, 1,
                               "task %s finished", ("task-123",), None)
    record.task_id = "task-123"
    record.password = "PRIVATE-CREDENTIAL"
    payload = json.loads(obs.SafeEventJsonFormatter().format(record))
    assert payload["message"] == "task task-123 finished"
    assert "task-123" in payload["details"]["log_fields"]["content"]
    assert "PRIVATE-CREDENTIAL" not in json.dumps(payload)
    try:
        raise RuntimeError("backend failed token=PRIVATE-CREDENTIAL")
    except RuntimeError:
        import sys
        record = logging.LogRecord("some.library", logging.ERROR, __file__, 2,
                                   "request failed", (), sys.exc_info())
        payload = json.loads(obs.SafeEventJsonFormatter().format(record))
    assert "backend failed" in payload["exception"]["message"]
    assert "PRIVATE-CREDENTIAL" not in json.dumps(payload)


def test_http_request_response_details_share_span_event_and_log(content_wiring, monkeypatch):
    from app.infrastructure.observability import http_capture
    spans, logs = [], []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))

    def respond(request):
        assert json.loads(request.content)["prompt"] == "useful input"
        return httpx.Response(200, json={"answer": "useful output", "api_key": "PRIVATE-CREDENTIAL"})

    transport = httpx.MockTransport(respond)
    wrapt.wrap_function_wrapper(transport, "handle_request", http_capture.wrap_sync_transport)
    client = httpx.Client(transport=transport)
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=ins._FilteringTracerProvider(provider),
        request_hook=ins._httpx_request_hook, response_hook=ins._httpx_response_hook)

    class Capture(logging.Handler):
        def emit(self, record):
            if obs.event_code_for(record) == "http_client_completed":
                logs.append(json.loads(obs.SafeEventJsonFormatter().format(record)))
    logger = logging.getLogger(obs.__name__)
    monkeypatch.setattr(logger, "level", logging.INFO)
    handler = Capture()
    logger.addHandler(handler)
    try:
        with provider.get_tracer("test").start_as_current_span("agent"):
            response = client.post("https://backend.example/api/infer?q=visible&token=PRIVATE-CREDENTIAL",
                headers={"Authorization": "Bearer PRIVATE-CREDENTIAL", "x-request-purpose": "inference"},
                json={"prompt": "useful input", "max_tokens": 123, "password": "PRIVATE-CREDENTIAL"})
            assert response.json()["answer"] == "useful output"
        assert len(logs) == 1
        log = logs[0]
        assert "useful input" in log["details"]["http_request"]["content"]
        assert "visible" in log["details"]["http_request"]["content"]
        assert "inference" in log["details"]["http_request"]["content"]
        assert "useful output" in log["details"]["http_response"]["content"]
        assert "PRIVATE-CREDENTIAL" not in json.dumps(log)
        child = next(s for s in spans if s.context.span_id == int(log["span_id"], 16))
        assert "useful input" in child.attributes["nagent.details.http_request.content"]
        assert "useful output" in child.events[-1].attributes["nagent.details.http_response.content"]
        assert "PRIVATE-CREDENTIAL" not in str(child.attributes) + str(child.events)
    finally:
        logger.removeHandler(handler)
        client.close()
        provider.shutdown()


def test_numeric_token_configuration_is_not_a_credential(content_wiring):
    content = details.project_details(input_messages={"max_tokens": 123, "input_tokens": 42,
                                                     "access_token": "PRIVATE-CREDENTIAL"})
    value = json.loads(content["input_messages"]["content"])
    assert value["max_tokens"] == 123 and value["input_tokens"] == 42
    assert value["access_token"] == "[REDACTED]"


def test_structured_logs_and_library_diagnostics_keep_useful_fields(content_wiring):
    record = logging.LogRecord("app.work", logging.INFO, __file__, 1,
                               {"message": "useful", "password": "not-in-catalog", "result": 12}, (), None)
    payload = json.loads(obs.SafeEventJsonFormatter().format(record))
    assert "useful" in payload["message"] and "not-in-catalog" not in str(payload)
    record = logging.LogRecord("some.library", logging.INFO, __file__, 1, "retry attempt %d", (2,), None)
    assert json.loads(obs.SafeEventJsonFormatter().format(record))["message"] == "retry attempt 2"


def test_management_write_is_logged_without_creating_an_orphan_span(content_wiring, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    app, emitted = FastAPI(), []
    app.add_middleware(ins._HttpAccessFactsMiddleware)
    @app.patch("/settings")
    async def settings(request: Request):
        return {"updated": (await request.json())["name"]}
    original = obs.log_http_request_completed
    def capture(*args, **kwargs):
        emitted.append((obs._current_span_ids(), kwargs.get("details")))
        return original(*args, **kwargs)
    monkeypatch.setattr(obs, "log_http_request_completed", capture)
    with TestClient(app) as client:
        assert client.patch("/settings", json={"name": "model", "api_key": "PRIVATE-CREDENTIAL"}).status_code == 200
        client.get("/static/ignored")
        client.get("/health")
    assert len(emitted) == 1
    assert emitted[0][0] == (None, None)
    assert "model" in emitted[0][1]["http_request"]["content"]
    assert "PRIVATE-CREDENTIAL" not in str(emitted)


def test_partial_gzip_and_multipart_information_is_readable_and_bounded(content_wiring):
    import gzip
    from app.infrastructure.observability.http_capture import Body
    body = Body({"content-type": "application/json", "content-encoding": "gzip"})
    encoded = gzip.compress(json.dumps({"message": "visible", "password": "PRIVATE-CREDENTIAL"}).encode())
    body.append(encoded[:10])
    body.append(encoded[10:])
    body.complete = True
    safe = details.project_details(http_response=body.value())
    assert "visible" in str(safe) and "PRIVATE-CREDENTIAL" not in str(safe)
    multipart = Body({"content-type": "multipart/form-data; boundary=audit"})
    multipart.append(b'--audit\r\nContent-Disposition: form-data; name="message"\r\n\r\nvisible form\r\n--audit\r\nContent-Disposition: form-data; name="file"; filename="sample.png"\r\nContent-Type: image/png\r\n\r\nRAW-IMAGE-BYTES\r\n--audit--\r\n')
    multipart.complete = True
    safe = details.project_details(http_request=multipart.value())
    assert "visible form" in str(safe) and "sample.png" in str(safe)
    assert "RAW-IMAGE-BYTES" not in str(safe)
    partial = Body({"content-type": "application/json"})
    partial.append(b'{"message":"visible","access_token":"PRIVATE-')
    safe = details.project_details(http_response=partial.value())
    assert "visible" in str(safe) and "PRIVATE-" not in str(safe)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "close", "error", "cancel"])
async def test_streaming_io_is_unchanged_and_partial_details_are_preserved(content_wiring, monkeypatch, mode):
    import asyncio
    from app.infrastructure.observability import http_capture
    spans, logs = [], []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"answer":"useful partial output"}\n\n'
            if mode == "error":
                raise httpx.ReadError("read failed token=PRIVATE-CREDENTIAL")
            if mode == "cancel":
                raise asyncio.CancelledError()
            yield b'data: {"access_token":"PRIVATE-'
            yield b'CREDENTIAL"}\n\n'

    async def respond(request):
        return httpx.Response(200, stream=ResponseStream(), headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(respond)
    wrapt.wrap_function_wrapper(transport, "handle_async_request", http_capture.wrap_async_transport)
    client = httpx.AsyncClient(transport=transport)
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=ins._FilteringTracerProvider(provider),
        request_hook=ins._async_httpx_request_hook, response_hook=ins._async_httpx_response_hook)
    original = obs.log_http_client_completed
    def capture(facts):
        logs.append(facts)
        original(facts)
    monkeypatch.setattr(obs, "log_http_client_completed", capture)
    try:
        with provider.get_tracer("test").start_as_current_span("agent"):
            async with client.stream("GET", "https://backend.example/events") as response:
                assert logs == []  # do not declare completion at response headers
                iterator = response.aiter_bytes()
                assert "useful partial output" in (await anext(iterator)).decode()
                if mode == "full":
                    remainder = b"".join([chunk async for chunk in iterator])
                    assert b"PRIVATE-CREDENTIAL" in remainder  # business bytes never sanitized
                elif mode in {"error", "cancel"}:
                    with pytest.raises(httpx.ReadError if mode == "error" else asyncio.CancelledError):
                        await anext(iterator)
        assert len(logs) == 1
        item = logs[0]["details"]["http_response"]
        assert "useful partial output" in item["content"]
        assert "PRIVATE-CREDENTIAL" not in str(logs) + str(spans)
        assert bool(item.get("incomplete")) == (mode != "full")
    finally:
        await client.aclose()
        provider.shutdown()


def test_inbound_http_details_and_concurrent_identity_are_not_discarded(content_wiring, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    spans, logs = [], []
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    app = FastAPI()
    @app.post("/chat/completions")
    async def respond(request: Request):
        body = await request.json()
        return {"answer": body["prompt"], "password": "PRIVATE-CREDENTIAL"}
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    app.add_middleware(ins._HttpAccessFactsMiddleware)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=ins._FilteringTracerProvider(provider),
                                     exclude_spans=["receive", "send"])
    original = obs.log_http_request_completed
    def capture(*args, **kwargs):
        logs.append((obs._current_span_ids(), kwargs.get("details")))
        return original(*args, **kwargs)
    monkeypatch.setattr(obs, "log_http_request_completed", capture)
    from concurrent.futures import ThreadPoolExecutor
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: client.post("/chat/completions?q=visible&token=PRIVATE-CREDENTIAL", json={"prompt": p}), ("first", "second")))
    assert all(r.status_code == 200 for r in results)
    assert len(logs) == 2 and logs[0][0][0] != logs[1][0][0]
    for (_, span_id), detail in logs:
        request = json.loads(detail["http_request"]["content"])
        response = json.loads(detail["http_response"]["content"])
        assert request["body"]["prompt"] == response["body"]["answer"]
        assert "PRIVATE-CREDENTIAL" not in str(detail)
        assert any(s.context.span_id == int(span_id, 16) for s in spans)
    provider.shutdown()
