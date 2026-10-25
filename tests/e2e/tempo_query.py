"""Query Tempo search/trace APIs and normalize their OTLP JSON for smoke checks."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _json_get(url, timeout=5):
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
        return json.load(response)


def normalize_id(value, size):
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{%d}" % (size * 2), value):
        return value.lower()
    raw = base64.b64decode(value, validate=True)
    if len(raw) != size:
        raise ValueError("invalid OTLP identifier size")
    return raw.hex()


def resource_spans(data):
    # Tempo 2.x uses batches; newer OTLP JSON uses resourceSpans.
    batches = data.get("batches", data.get("resourceSpans"))
    if batches is None:
        raise ValueError("Tempo trace response missing resource spans")
    for batch in batches:
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                for field, size in (("traceId", 16), ("spanId", 8), ("parentSpanId", 8)):
                    span[field] = normalize_id(span.get(field, ""), size)
        # Normalize the pre-OTLP-1.0 scope field too.
        if "scopeSpans" not in batch:
            batch["scopeSpans"] = batch.pop("instrumentationLibrarySpans", [])
    return batches


def search(base_url, start, end, timeout=5):
    deadline = time.monotonic() + float(timeout)
    def get(url):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Tempo search deadline exceeded")
        return _json_get(url, timeout=min(5, remaining))
    params = urlencode({"q": '{resource.service.name="n-agent"}',
                        "start": int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()),
                        "end": int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()) + 1,
                        "limit": 1000})
    result = get(base_url + "/api/search?" + params)
    if "traces" not in result and "metrics" not in result:
        raise ValueError("Tempo search response invalid")
    traces = result.get("traces", [])
    if len(traces) >= 1000:
        raise ValueError("Tempo smoke search truncated; narrow the time range")
    batches = []
    for trace in traces:
        trace_id = normalize_id(trace["traceID"], 16)
        detail = resource_spans(get(base_url + "/api/traces/" + trace_id))
        spans = [s for b in detail for scope in b["scopeSpans"] for s in scope.get("spans", [])]
        if not spans or any(s["traceId"] != trace_id or not s["spanId"] for s in spans):
            raise ValueError("Tempo search hit has incomplete trace detail")
        batches.extend(detail)
    return {"result": {"resourceSpans": batches}}


if __name__ == "__main__":
    print(json.dumps(search(*sys.argv[1:5])))
