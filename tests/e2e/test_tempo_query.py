"""Tempo API compatibility and failure handling without a running stack."""
import base64
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("tempo_query", Path(__file__).with_name("tempo_query.py"))
tempo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tempo)


@pytest.mark.parametrize("size", [8, 16])
def test_normalize_id_accepts_unpadded_base64_but_rejects_garbage(size):
    raw = bytes.fromhex("ab" * size)
    encoded = base64.b64encode(raw).decode().rstrip("=")
    assert tempo.normalize_id(encoded, size) == raw.hex()
    with pytest.raises(ValueError):
        tempo.normalize_id("!" + encoded, size)


def test_trace_fetch_failure_is_not_mistaken_for_zero_traces(monkeypatch):
    def get(url, timeout=5):
        if "/api/search?" in url:
            return {"traces": [{"traceID": "ab" * 16}]}
        raise OSError("trace fetch unavailable")
    monkeypatch.setattr(tempo, "_json_get", get)
    with pytest.raises(OSError):
        tempo.search("http://tempo:3200", "2026-10-27T00:00:00Z", "2026-10-27T00:00:01Z")


def test_search_normalizes_otlp_ids_and_fetches_complete_trace(monkeypatch):
    trace_id, span_id = "ab" * 16, "cd" * 8
    payload = {"batches": [{"scopeSpans": [{"spans": [{
        "traceId": base64.b64encode(bytes.fromhex(trace_id)).decode(),
        "spanId": base64.b64encode(bytes.fromhex(span_id)).decode(),
        "name": "invoke_agent n-agent",
    }]}]}]}
    requests = []
    def get(url, timeout=5):
        requests.append(url)
        return {"traces": [{"traceID": trace_id}]} if "/api/search?" in url else payload
    monkeypatch.setattr(tempo, "_json_get", get)
    result = tempo.search("http://tempo:3200", "2026-10-27T00:00:00Z", "2026-10-27T00:00:01Z")
    span = result["result"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == trace_id
    assert span["spanId"] == span_id
    assert span["parentSpanId"] == ""
    assert requests[1] == "http://tempo:3200/api/traces/" + trace_id


def test_backend_failure_is_not_mistaken_for_zero_traces(monkeypatch):
    def fail(url, timeout=5):
        raise OSError("unavailable")
    monkeypatch.setattr(tempo, "_json_get", fail)
    with pytest.raises(OSError):
        tempo.search("http://tempo:3200", "2026-10-27T00:00:00Z", "2026-10-27T00:00:01Z")


@pytest.mark.parametrize("payload", [{}, {"trace": {}}, {"batches": [{"scopeSpans": [{"spans": [{"traceId": "bad"}]}]}]}])
def test_malformed_trace_response_fails_closed(payload):
    with pytest.raises((ValueError, base64.binascii.Error)):
        tempo.resource_spans(payload)


@pytest.mark.parametrize("detail", [{"batches": []}, {"resourceSpans": [{"scopeSpans": []}]}])
def test_search_hit_with_missing_detail_is_not_zero_traces(monkeypatch, detail):
    def get(url, timeout=5):
        return {"traces": [{"traceID": "ab" * 16}]} if "/api/search?" in url else detail
    monkeypatch.setattr(tempo, "_json_get", get)
    with pytest.raises(ValueError, match="incomplete"):
        tempo.search("http://tempo:3200", "2026-10-27T00:00:00Z", "2026-10-27T00:00:01Z")


def test_search_total_deadline_includes_trace_fetches(monkeypatch):
    clock = iter([0, 0, 6])
    monkeypatch.setattr(tempo.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(tempo, "_json_get", lambda url, timeout: {"traces": [{"traceID": "ab" * 16}]})
    with pytest.raises(TimeoutError):
        tempo.search("http://tempo:3200", "2026-10-27T00:00:00Z", "2026-10-27T00:00:01Z")


def test_search_accepts_tempo_hex_trace_id_without_leading_zeroes(monkeypatch):
    short_id = "70673dc11c18c725dfcab3bcc5a6ef5"
    full_id = short_id.zfill(32)
    requested = []
    def get(url, timeout=5):
        requested.append(url)
        if "/api/search?" in url:
            return {"traces": [{"traceID": short_id}]}
        return {"batches": [{"scopeSpans": [{"spans": [{
            "traceId": full_id, "spanId": "ab" * 8,
        }]}]}]}
    monkeypatch.setattr(tempo, "_json_get", get)
    tempo.search("http://tempo:3200", "2026-10-27T00:00:00Z", "2026-10-27T00:00:01Z")
    assert requested[1].endswith("/api/traces/" + full_id)
