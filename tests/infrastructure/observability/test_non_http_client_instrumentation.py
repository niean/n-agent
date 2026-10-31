"""CLI/ACP runtime must observe real SDK HTTP transfers without a FastAPI app."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry import trace

from app.infrastructure.observability import instrumentors as ins
from app.infrastructure.observability import logging as obs
from tests.infrastructure.observability.test_nkb_trace_propagation import wire
from tests.infrastructure.observability.test_information_completeness import content_wiring
from tests.test_main_observability_wiring import _settings


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json", "sse", "error", "cancelled", "long"])
async def test_runtime_sdk_http_parent_payload_and_completion(
    wire, content_wiring, tmp_path, monkeypatch, mode
):
    import app.main as main
    from app.infrastructure.observability import setup
    from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: wire.provider)
    monkeypatch.setattr(setup, "configure_observability", lambda settings: SimpleNamespace(
        mode="local", shutdown=lambda: None))
    settings = _settings(tmp_path, otel_enabled=True)
    settings.profiling_enabled = False
    logs = []
    monkeypatch.setattr(obs, "log_http_client_completed", lambda facts: logs.append(dict(facts)))
    reply = "useful output" if mode != "long" else "visible-prefix " + "中" * 5000
    message = {"id": "chatcmpl-test", "object": "chat.completion", "model": "test-model",
               "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop"}]}

    async def send(transport, request):
        assert json.loads(request.content)["messages"][0]["content"] == "useful input"
        if mode == "cancelled":
            raise asyncio.CancelledError()
        if mode == "error":
            response = httpx.Response(500, json={"error": {"message": "backend failed", "api_key": "PRIVATE-CREDENTIAL"}})
        elif mode == "sse":
            chunk = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "model": "test-model",
                     "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}]}
            response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        else:
            response = httpx.Response(200, json=message)

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                assert not [s for s in wire.exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT]
                data = response.content
                for offset in range(0, len(data), 37):
                    yield data[offset:offset + 37]

        return httpx.Response(response.status_code, headers=response.headers, stream=Chunks())

    # Substitute only the wire: global registration and capture wrappers are production code.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with main.application_runtime(settings):
        provider = OpenAICompatibleProvider("https://llm.example/v1", "PRIVATE-CREDENTIAL", "test-model")
        try:
            with wire.provider.get_tracer("test").start_as_current_span("chat test-model") as chat:
                if mode in {"error", "cancelled"}:
                    from openai import InternalServerError
                    with pytest.raises(asyncio.CancelledError if mode == "cancelled" else InternalServerError):
                        await provider.chat([{"role": "user", "content": "useful input"}], [], False, "test-model", {})
                else:
                    result = await provider.chat([{"role": "user", "content": "useful input"}], [], mode == "sse", "test-model", {})
                    if mode == "sse":
                        assert reply in "".join([event.content or "" async for event in result])
                    else:
                        assert result.message["content"] == reply
            clients = [s for s in wire.exporter.get_finished_spans()
                       if s.kind == trace.SpanKind.CLIENT]
            assert len(clients) == 1, "CLI/ACP runtime must register HTTPX instrumentation"
            child = clients[0]
            assert child.parent.span_id == chat.get_span_context().span_id
            assert child.context.trace_id == chat.get_span_context().trace_id
            assert len(logs) == 1
            assert "useful input" in child.attributes["nagent.details.http_request.content"]
            expected = "backend failed" if mode == "error" else "visible-prefix" if mode == "long" else reply
            if mode != "cancelled":
                assert expected in child.attributes["nagent.details.http_response.content"]
            # OpenAI stops at [DONE] and closes without reading transport EOF.
            assert logs[0]["outcome"] == {"error": "error", "cancelled": "cancelled", "sse": "incomplete"}.get(mode, "completed")
            if mode == "sse":
                assert logs[0]["details"]["http_response"]["incomplete"]
            for value in logs[0]["details"].values():
                assert len(value["content"].encode()) <= 2048
            if mode == "long":
                assert logs[0]["details"]["http_response"]["truncated"]
            assert "PRIVATE-CREDENTIAL" not in str(child.attributes) + str(child.events) + str(logs)
            assert len([e for e in child.events if e.name == "http_client_completed"]) == 1
        finally:
            await provider.client.close()


@pytest.mark.asyncio
async def test_client_registration_without_fastapi_is_idempotent(wire):
    assert ins.instrument_clients(settings=SimpleNamespace(), tracer_provider=wire.provider)
    assert ins.instrument_clients(settings=SimpleNamespace(), tracer_provider=wire.provider)
    async with httpx.AsyncClient() as client:
        with wire.provider.get_tracer("test").start_as_current_span("operation"):
            await client.get("https://backend.example/status")
    clients = [s for s in wire.exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT]
    assert len(clients) == 1


def test_client_registration_failure_does_not_disable_server_spans(wire, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(ins, "instrument_clients", lambda **kwargs: False)
    app = FastAPI()
    app.get("/v1/chat/completions")(lambda: {"ok": True})
    assert ins.instrument_app(app, settings=SimpleNamespace(), tracer_provider=wire.provider) is False
    with TestClient(app) as client:
        assert client.get("/v1/chat/completions").json() == {"ok": True}
    assert len([s for s in wire.exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER]) == 1
