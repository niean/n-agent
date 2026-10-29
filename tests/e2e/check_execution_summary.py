"""Compare delivered completion records with Tempo without printing log bodies."""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def execution_fields(record):
    context = record.get("context", record)
    return context.get("execution", context)


def check(trace, records, series=None):
    expected = {"invoke_agent": 1, "chat": 2, "execute_tool": 1}
    roots = [s for s in trace["spans"] if s["name"].split()[0] == "invoke_agent"]
    assert len(roots) == 1, "expected one Agent run"
    subtree = {roots[0]["spanId"]}
    while True:
        expanded = subtree | {s["spanId"] for s in trace["spans"] if s.get("parentSpanId") in subtree}
        if expanded == subtree:
            break
        subtree = expanded
    spans = [s for s in trace["spans"] if s["spanId"] in subtree and s["name"].split()[0] in expected]
    for operation, count in expected.items():
        assert sum(s["name"].split()[0] == operation for s in spans) == count, "unexpected span count"
    expected_ids = {s["spanId"] for s in spans}
    delivered = [r for r in records if r.get("event") == "execution_completed" and r.get("trace_id") == trace["traceId"] and r.get("span_id") in expected_ids]
    assert len(delivered) == 4, "expected four completion records"
    for span in spans:
        matches = [r for r in delivered if r.get("span_id") == span["spanId"]]
        assert len(matches) == 1, "missing or duplicate completion for span"
        record = matches[0]
        fields = execution_fields(record)
        attrs = span["attrs"]
        assert fields.get("operation") == span["name"].split()[0], "operation mismatch"
        for field, attr in (("summary", "nagent.summary"), ("session_id", "gen_ai.conversation.id"), ("request_id", "nagent.run_id")):
            assert fields.get(field) and fields[field] == attrs.get(attr), "completion field differs from span"
        assert len(fields["summary"]) <= 512, "summary exceeds bound"
        assert isinstance(fields.get("duration_ms"), (int, float)) and fields["duration_ms"] >= 0, "duration missing"
        assert fields.get("outcome"), "outcome missing"
    # /series returns real indexed labels; query_range stream can also contain
    # structured metadata and therefore cannot establish index cardinality.
    forbidden = {"session_id", "request_id", "summary", "trace_id", "span_id", "nagent_run_id", "nagent_summary", "gen_ai_conversation_id"}
    for labels in series or []:
        assert not forbidden.intersection(labels), "execution data became Loki index labels"


def read_stderr(path):
    records = []
    for line in open(path, encoding="utf-8"):
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            records.append(json.loads(line[brace:]))
        except json.JSONDecodeError:
            continue
    return records


def fetch(trace_id, start, compose_script=None):
    deadline = time.monotonic() + 60
    selector = '{service_name="n-agent"}'
    last_error = None
    while time.monotonic() < deadline:
        try:
            def query(endpoint, params):
                url = "http://loki:3100/loki/api/v1/" + endpoint + "?" + urllib.parse.urlencode(params)
                timeout = max(.1, min(5, deadline - time.monotonic()))
                if compose_script:
                    raw = subprocess.check_output(
                        ["sh", compose_script, "compose", "exec", "-T", "prometheus", "wget", "-q", "-T", "5", "-O", "-", url],
                        timeout=timeout + 2)
                    payload = json.loads(raw)
                else:
                    with urllib.request.urlopen(url, timeout=timeout) as response:
                        payload = json.load(response)
                assert payload.get("status") == "success", "Loki query failed"
                return payload["data"]
            records = []
            data = query("query_range", {"query": selector + ' |= "execution_completed" |= ' + json.dumps(trace_id), "start": start, "end": str(time.time_ns()), "limit": "1000"})
            for stream in data["result"]:
                for value in stream["values"]:
                    records.append(json.loads(value[1]))
            if len({r.get("span_id") for r in records if r.get("event") == "execution_completed" and r.get("trace_id") == trace_id and execution_fields(r).get("request_id")}) == 4:
                series = query("series", {"match[]": selector, "start": start, "end": str(time.time_ns())})
                return {"records": records, "series": series}
        except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as error:
            last_error = type(error).__name__
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise SystemExit("Loki completion delivery timed out (60s; error class: %s)" % last_error)


if __name__ == "__main__":
    if sys.argv[1] == "fetch":
        print(json.dumps(fetch(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None), ensure_ascii=False))
    else:
        trace = json.load(open(sys.argv[2]))
        if sys.argv[1] == "stderr":
            check(trace, read_stderr(sys.argv[3]))
        else:
            payload = json.load(open(sys.argv[3]))
            assert payload["series"], "Loki indexed series missing"
            check(trace, payload["records"], payload["series"])
