"""Trace 报告必须绑定真实消息，缺失或后端异常不得伪造成功。"""
import importlib.util
import functools
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


@functools.cache
def support():
    path = Path(__file__).with_name("trace_report.py")
    assert path.exists(), "会话 E2E 缺少 Trace 采集模块"
    spec = importlib.util.spec_from_file_location("e2e_trace_report_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRACE = "ab" * 16
BASE = "https://grafana.example/grafana/explore?orgId=2"


def span(trace=TRACE, session="s1", start=10_000_000_000):
    return {"traceId": trace, "spanId": "cd" * 8,
            "name": "invoke_agent n-agent", "startTimeUnixNano": str(start),
            "attributes": [{"key": "gen_ai.conversation.id",
                            "value": {"stringValue": session}}]}


def payload(*spans):
    return {"batches": [{"scopeSpans": [{"spans": list(spans)}]}]}


def collector(get, **kw):
    return support().TraceCollector(
        "http://tempo:3200", BASE, get=get, timeout=0.05, quiet_seconds=0, **kw)


def test_query_verifies_detail_and_filters_other_sessions_and_messages():
    requests = []
    def get(url, timeout):
        requests.append(url)
        if "/api/search?" in url:
            return {"traces": [{"traceID": TRACE}]}
        return payload(span(), span(session="other"), span(start=1))
    result = collector(get).collect("s1", 9_000_000_000, 11_000_000_000)
    assert [r["trace_id"] for r in result] == [TRACE]
    assert requests[-1].endswith("/api/traces/" + TRACE)
    query = parse_qs(urlsplit(requests[0]).query)["q"][0]
    assert 'span.gen_ai.conversation.id="s1"' in query
    params = parse_qs(urlsplit(result[0]["trace_url"]).query)
    assert params["orgId"] == ["2"]
    assert json.loads(params["panes"][0])["trace"]["queries"][0]["query"] == TRACE


@pytest.mark.parametrize("missing", ["http", "request", "response", "event", None])
def test_chat_requires_downstream_http_payload_and_completion(missing):
    agent = span()
    chat = {**span(), "spanId": "11" * 8, "parentSpanId": agent["spanId"], "name": "chat model"}
    http = {**span(), "spanId": "22" * 8, "parentSpanId": chat["spanId"], "name": "POST llm.example /v1/chat/completions",
            "attributes": [{"key": "http.request.method", "value": {"stringValue": "POST"}},
                           {"key": "nagent.details.http_request.content", "value": {"stringValue": "useful request"}},
                           {"key": "nagent.details.http_response.content", "value": {"stringValue": "useful response"}}],
            "events": [{"name": "http_client_completed"}]}
    if missing in {"request", "response"}:
        http["attributes"] = [a for a in http["attributes"] if a["key"] != f"nagent.details.http_{missing}.content"]
    if missing == "event":
        http["events"] = []
    def get(url, timeout):
        return ({"traces": [{"traceID": TRACE}]} if "/api/search?" in url else
                payload(agent, chat, *([] if missing == "http" else [http])))
    if missing is None:
        assert collector(get).collect("s1", 9_000_000_000, 11_000_000_000)[0]["trace_id"] == TRACE
    else:
        with pytest.raises(support().TraceCollectionError, match="HTTP") as error:
            collector(get).collect("s1", 9_000_000_000, 11_000_000_000)
        assert error.value.traces[0]["trace_id"] == TRACE


def test_incomplete_http_trace_cannot_be_hidden_by_later_valid_search_hit():
    second = "ef" * 16
    polls = [0]
    def get(url, timeout):
        if "/api/search?" in url:
            polls[0] += 1
            return {"traces": [{"traceID": TRACE if polls[0] == 1 else second}]}
        if url.endswith(TRACE):
            agent = span()
            return payload(agent, {**span(), "spanId": "11" * 8,
                                   "parentSpanId": agent["spanId"], "name": "chat model"})
        return payload(span(trace=second))
    with pytest.raises(support().TraceCollectionError) as error:
        collector(get, sleep=lambda _: None).collect("s1", 9_000_000_000, 11_000_000_000)
    assert {t["trace_id"] for t in error.value.traces} == {TRACE, second}


@pytest.mark.parametrize("detail", [payload(span(session="other")),
                                     payload(span(start=1)), {"batches": []}])
def test_unrelated_or_empty_detail_cannot_satisfy_message(detail):
    def get(url, timeout):
        return {"traces": [{"traceID": TRACE}]} if "/api/search?" in url else detail
    with pytest.raises(support().TraceCollectionError):
        collector(get).collect("s1", 9_000_000_000, 11_000_000_000)


def test_backend_failure_is_a_trace_failure_with_diagnostic():
    def get(url, timeout):
        raise OSError("backend unavailable")
    with pytest.raises(support().TraceCollectionError, match="OSError"):
        collector(get).collect("s1", 9_000_000_000, 11_000_000_000)


def test_missing_trace_polls_until_deadline_then_fails():
    clock = [0]
    calls = []
    def get(url, timeout):
        calls.append(url)
        return {"traces": []}
    module = support()
    c = module.TraceCollector("http://tempo:3200", BASE, get=get, timeout=2,
                              monotonic=lambda: clock[0],
                              sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(module.TraceCollectionError, match="未取得"):
        c.collect("s1", 9_000_000_000, 11_000_000_000)
    assert 1 < len(calls) <= 3


def test_delayed_export_and_multiple_traces_are_retained():
    calls = [0]
    second = "ef" * 16
    def get(url, timeout):
        if "/api/search?" in url:
            calls[0] += 1
            return {"traces": [] if calls[0] == 1 else
                    [{"traceID": TRACE}, {"traceID": second}]}
        return payload(span(trace=url.rsplit("/", 1)[-1]))
    c = support().TraceCollector("http://tempo:3200", BASE, get=get,
                                  timeout=2, quiet_seconds=0, sleep=lambda _: None)
    assert {r["trace_id"] for r in c.collect("s1", 9_000_000_000, 11_000_000_000)} == {TRACE, second}


def test_report_renders_failed_attempt_and_each_trace(tmp_path):
    module = support()
    report = {"run_id": "r1", "cases": [{"id": "c1", "title": "case",
        "verdict": "PASS", "attempts": [
            {"attempt": 1, "verdict": "FAIL", "reason": "bad response", "messages": [
                {"id": "m1", "verdict": "FAIL", "traces": [{"trace_id": TRACE,
                    "trace_url": BASE}], "trace_reason": ""}]},
            {"attempt": 2, "verdict": "PASS", "messages": [
                {"id": "m1", "verdict": "PASS", "traces": [],
                 "trace_reason": "OTel 未启用"}]}]}]}
    markdown = module.render_report(report)
    assert "| 分类 | 用例 | 结果 | TraceID | 链接 |" in markdown
    assert "| 对话 |" in markdown
    assert "c1 / 尝试1 / m1" in markdown
    assert "FAIL" in markdown and TRACE in markdown
    assert "c1 / 尝试2 / m1" in markdown and "OTel 未启用" in markdown
    assert "查看Trace" in markdown


def message_result(task=False, replay="channel"):
    context = {"session_id": "s1", "start_ns": 9_000_000_000,
               "end_ns": 11_000_000_000, "replay_via": replay}
    if task:
        context["task_id"] = "t1"
    return {"verdict": "PASS", "messages": [{"id": "m1", "verdict": "PASS",
            "reason": "", "trace_context": context}]}


class Driver:
    def __init__(self, status="healthy", task_status=200):
        self.status, self.task_status = status, task_status

    def get(self, path, timeout):
        return {"status": self.status, "links": {"Trace": BASE}}

    def get_raw(self, path, timeout):
        return self.task_status, {"task": {"execution_session_id": "worker"}}


def test_case_requires_both_origin_and_independent_task_trace(monkeypatch):
    module = support()
    seen = []
    class Collector:
        def __init__(self, *args):
            pass
        def collect(self, session, start, end, **kwargs):
            seen.append(session)
            return [{"trace_id": TRACE if session == "s1" else "ef" * 16,
                     "trace_url": BASE}]
    monkeypatch.setattr(module, "TraceCollector", Collector)
    result = message_result(task=True)
    module.collect_case(result, Driver(), {})
    assert seen == ["s1", "worker"]
    assert result["verdict"] == "PASS"
    assert [t["kind"] for t in result["messages"][0]["traces"]] == ["message", "task"]


def test_task_resolution_failure_keeps_already_collected_origin(monkeypatch):
    module = support()
    class Collector:
        def __init__(self, *args):
            pass
        def collect(self, *args, **kwargs):
            return [{"trace_id": TRACE, "trace_url": BASE}]
    monkeypatch.setattr(module, "TraceCollector", Collector)
    result = message_result(task=True)
    module.collect_case(result, Driver(task_status=500), {})
    assert result["verdict"] == "FAIL"
    assert result["messages"][0]["traces"][0]["trace_id"] == TRACE


def test_case_missing_trace_fails_functionally_successful_message(monkeypatch):
    module = support()
    class Collector:
        def __init__(self, *args):
            pass
        def collect(self, *args, **kwargs):
            raise module.TraceCollectionError("未取得 Trace")
    monkeypatch.setattr(module, "TraceCollector", Collector)
    result = message_result()
    module.collect_case(result, Driver(), {})
    assert result["verdict"] == "FAIL"
    assert result["messages"][0]["verdict"] == "FAIL"
    assert "未取得" in result["messages"][0]["trace_reason"]


def test_disabled_otel_is_explicit_not_a_fake_link():
    result = message_result()
    support().collect_case(result, Driver(status="disabled"), {})
    assert result["verdict"] == "PASS"
    assert result["messages"][0]["traces"] == []
    assert result["messages"][0]["trace_reason"] == "OTel 未启用"


@pytest.mark.parametrize("task, expected", [
    ({"execution_session_id": "explicit", "origin_session_id": "origin"}, "explicit"),
    ({"execution_session_id": None, "origin_session_id": "origin"}, "origin"),
    ({"execution_session_id": None, "origin_session_id": None},
     "task-6603a47b-10a2-52b5-bb91-c88a1a99c126"),
])
def test_worker_session_matches_production_selection(task, expected):
    assert support().worker_session_id(task, "t1") == expected


def test_shared_session_worker_cannot_substitute_for_origin_message():
    worker = span()
    worker["attributes"].append({"key": "nagent.source", "value": {"stringValue": "task"}})
    def get(url, timeout):
        return {"traces": [{"traceID": TRACE}]} if "/api/search?" in url else payload(worker)
    with pytest.raises(support().TraceCollectionError):
        collector(get).collect("s1", 9_000_000_000, 11_000_000_000, source="dashboard")


def test_task_trace_requires_matching_task_root():
    worker = span()
    worker["attributes"].append({"key": "nagent.source", "value": {"stringValue": "task"}})
    root = span()
    root["name"] = "task.run"
    root["attributes"].append({"key": "nagent.task.id", "value": {"stringValue": "other-task"}})
    def get(url, timeout):
        return {"traces": [{"traceID": TRACE}]} if "/api/search?" in url else payload(worker, root)
    with pytest.raises(support().TraceCollectionError):
        collector(get).collect("s1", 9_000_000_000, 11_000_000_000, source="task", task_id="t1")


def test_task_trace_accepts_completed_matching_run_and_rejects_origin():
    worker_id = "ef" * 16
    worker = span(trace=worker_id)
    worker["attributes"].append({"key": "nagent.source", "value": {"stringValue": "task"}})
    root = span(trace=worker_id)
    root.update(name="task.run", endTimeUnixNano="10500000000")
    root["attributes"].append({"key": "nagent.task.id", "value": {"stringValue": "t1"}})
    origin = span()
    origin["attributes"].append({"key": "nagent.source", "value": {"stringValue": "dashboard"}})
    def get(url, timeout):
        if "/api/search?" in url:
            return {"traces": [{"traceID": TRACE}, {"traceID": worker_id}]}
        return payload(origin) if url.endswith(TRACE) else payload(worker, root)
    result = collector(get).collect("s1", 9_000_000_000, 11_000_000_000, source="task", task_id="t1")
    assert [t["trace_id"] for t in result] == [worker_id]


def test_deadline_stops_trace_detail_requests_after_slow_search():
    clock = [0]
    calls = []
    def get(url, timeout):
        calls.append(url)
        clock[0] += 2
        return {"traces": [{"traceID": TRACE}]}
    module = support()
    c = module.TraceCollector("http://tempo:3200", BASE, get=get, timeout=1,
                              monotonic=lambda: clock[0])
    with pytest.raises(module.TraceCollectionError):
        c.collect("s1", 9_000_000_000, 11_000_000_000)
    assert len(calls) == 1


def test_waits_for_later_traces_in_same_message_window():
    clock = [0]
    polls = [0]
    second = "ef" * 16
    def get(url, timeout):
        if "/api/search?" in url:
            polls[0] += 1
            ids = [TRACE] if polls[0] == 1 else [TRACE, second]
            return {"traces": [{"traceID": t} for t in ids]}
        return payload(span(trace=url.rsplit("/", 1)[-1]))
    c = support().TraceCollector("http://tempo:3200", BASE, get=get, timeout=5,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert {t["trace_id"] for t in c.collect("s1", 9_000_000_000, 11_000_000_000)} == {TRACE, second}
    assert polls[0] >= 3


def test_report_displays_attempt_failure_when_messages_pass():
    report = {"run_id": "r1", "cases": [{"id": "c1", "title": "case", "verdict": "FAIL",
        "attempts": [{"attempt": 1, "verdict": "FAIL", "reason": "artifact count mismatch",
                      "messages": [{"id": "m1", "verdict": "PASS", "traces": []}]}]}]}
    rendered = support().render_report(report)
    assert "FAIL" in rendered and "artifact count mismatch" in rendered


def test_task_waits_for_every_expected_run_not_just_first_completed_root():
    clock = [0]
    polls = [0]
    second = "ef" * 16
    def get(url, timeout):
        if "/api/search?" in url:
            polls[0] += 1
            ids = [TRACE] if polls[0] < 4 else [TRACE, second]
            return {"traces": [{"traceID": t} for t in ids]}
        tid = url.rsplit("/", 1)[-1]
        worker = span(trace=tid)
        worker["attributes"].append({"key": "nagent.source", "value": {"stringValue": "task"}})
        root = span(trace=tid)
        root.update(name="task.run", endTimeUnixNano="10500000000")
        root["attributes"].extend([
            {"key": "nagent.task.id", "value": {"stringValue": "t1"}},
            {"key": "nagent.task.run_id", "value": {"stringValue": "1" if tid == TRACE else "2"}},
        ])
        return payload(worker, root)
    c = support().TraceCollector("http://tempo:3200", BASE, get=get, timeout=8,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    result = c.collect("s1", 9_000_000_000, 11_000_000_000,
                       source="task", task_id="t1", run_ids={"1", "2"})
    assert {t["trace_id"] for t in result} == {TRACE, second}
    assert polls[0] >= 4


def test_summary_counts_flattened_trace_rows_and_cleanup_failure():
    module = support()
    report = {"run_id": "r1", "cases": [{"id": "c1", "verdict": "PASS",
        "messages": [{"id": "m1", "verdict": "PASS", "traces": [
            {"trace_id": TRACE, "trace_url": BASE},
            {"trace_id": "cd" * 16, "trace_url": BASE}]}]}],
        "cleanup": {"failed": [{"reason": "delete failed"}]}}
    assert module.render_summary(report) == "Summary（结果行）: PASS 2 | FAIL 1 | SKIP 0"
    assert "套件清理 | FAIL" in module.render_report(report)
