"""Black-box E2E Trace collection; persists evidence before session cleanup."""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def _support(name):
    spec = importlib.util.spec_from_file_location(
        "e2e_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_tempo = _support("tempo_query")
_report = _support("result_report")


def worker_session_id(task, task_id):
    # Black-box equivalent of the documented task execution session contract.
    return (task.get("execution_session_id") or task.get("origin_session_id")
            or f"task-{uuid.uuid5(uuid.NAMESPACE_URL, str(task_id))}")


class TraceCollectionError(Exception):
    def __init__(self, reason, traces=()):
        super().__init__(reason)
        self.traces = list(traces)


def _get(url, timeout):
    # Tempo is deployment infrastructure; do not route through a web proxy.
    with build_opener(ProxyHandler({})).open(
            Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
        return json.load(response)


def _verify_chat_http(spans, roots):
    """An Agent trace is insufficient when its real model transport is absent."""
    by_id = {s["spanId"]: s for s in spans}
    root_ids = {s["spanId"] for s in roots}

    def belongs(span):
        seen = set()
        while span and span["spanId"] not in seen:
            ident = span["spanId"]
            if ident in root_ids:
                return True
            seen.add(ident)
            span = by_id.get(span.get("parentSpanId"))
        return False

    for chat in spans:
        if not chat.get("name", "").startswith("chat ") or not belongs(chat):
            continue
        clients = [s for s in spans if s.get("parentSpanId") == chat["spanId"]
                   and any(a["key"] in {"http.request.method", "http.method"}
                           for a in s.get("attributes", []))]
        if not clients:
            raise ValueError("模型调用缺少下游 HTTP Span")
        for client in clients:
            attrs = {a["key"]: a.get("value", {}).get("stringValue")
                     for a in client.get("attributes", [])}
            if any(not attrs.get("nagent.details.http_" + field + ".content")
                   for field in ("request", "response")):
                raise ValueError("HTTP Span 缺少请求或结果 payload")
            if not any(e.get("name") == "http_client_completed" for e in client.get("events", [])):
                raise ValueError("HTTP Span 缺少完成事件")


class TraceCollector:
    def __init__(self, tempo_url, trace_page_url, *, get=_get, timeout=30,
                 monotonic=time.monotonic, sleep=time.sleep, quiet_seconds=2):
        self.tempo_url = tempo_url.rstrip("/")
        self.trace_page_url = trace_page_url
        self.get = get
        self.timeout = timeout
        self.monotonic = monotonic
        self.sleep = sleep
        self.quiet_seconds = quiet_seconds

    def collect(self, session_id, start_ns, end_ns, *, source=None, task_id=None, run_ids=()):
        deadline = self.monotonic() + self.timeout
        found = {}
        pending = set()
        reason = "未取得匹配消息的 Trace"
        signature = None
        stable_since = None
        expected_runs = {str(r) for r in run_ids}
        observed_runs = set()
        while self.monotonic() < deadline:
            matched_this_poll = False
            def get(url):
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Tempo query deadline exceeded")
                return self.get(url, timeout=min(5, remaining))
            try:
                query = ('{resource.service.name="n-agent" && '
                         'span.gen_ai.conversation.id=' + json.dumps(session_id))
                if source:
                    query += ' && span.nagent.source=' + json.dumps(source)
                query += '}'
                params = urlencode({"q": query, "start": start_ns // 1_000_000_000,
                                    "end": math.ceil(end_ns / 1_000_000_000), "limit": 1000})
                search = get(self.tempo_url + "/api/search?" + params)
                hits = search.get("traces", [])
                if "traces" not in search and "metrics" not in search:
                    raise ValueError("Tempo search response invalid")
                if len(hits) >= 1000:
                    raise ValueError("Tempo search truncated")
                # Search results can change while a batch is still exporting.
                # Re-fetch incomplete traces even if a later search omits them.
                hit_ids = {h.get("traceID", "").lower().zfill(32) for h in hits}
                hits = hits + [{"traceID": ident} for ident in sorted(pending - hit_ids)]
                for hit in hits:
                    raw = hit.get("traceID", "")
                    if not re.fullmatch(r"[0-9a-fA-F]{1,32}", raw) or int(raw, 16) == 0:
                        raise ValueError("invalid trace ID")
                    trace_id = raw.lower().zfill(32)
                    batches = _tempo.resource_spans(get(self.tempo_url + "/api/traces/" + trace_id))
                    spans = [s for b in batches for scope in b.get("scopeSpans", [])
                             for s in scope.get("spans", [])]
                    if not spans or any(s["traceId"] != trace_id or not s["spanId"] for s in spans):
                        raise ValueError("incomplete trace detail")
                    def attributes(s):
                        return {a["key"]: a.get("value", {}).get("stringValue",
                                                       a.get("value", {}).get("intValue"))
                                for a in s.get("attributes", [])}
                    roots = [s for s in spans if s.get("name") == "task.run"
                             and attributes(s).get("nagent.task.id") == task_id
                             and attributes(s).get("gen_ai.conversation.id") == session_id
                             and start_ns <= int(s.get("startTimeUnixNano", 0)) < end_ns
                             and int(s.get("endTimeUnixNano", 0)) > 0] if task_id else []
                    if task_id and not roots:
                        continue
                    matching = []
                    for s in spans:
                        attrs = attributes(s)
                        started = int(s.get("startTimeUnixNano", 0))
                        if (attrs.get("gen_ai.conversation.id") == session_id
                                and start_ns <= started < end_ns
                                and (not source or attrs.get("nagent.source") == source)
                                and s.get("name", "").startswith("invoke_agent")):
                            matching.append(s)
                    if task_id:
                        # A failed Task run may end before invoking the Agent.
                        matching = roots
                        observed_runs.update(str(attributes(s).get("nagent.task.run_id")) for s in roots)
                    if matching:
                        matched_this_poll = True
                        url = _report.trace_url(self.trace_page_url, trace_id)
                        found[trace_id] = {"trace_id": trace_id, "trace_url": url,
                                           "session_id": session_id,
                                           "span_ids": [s["spanId"] for s in matching]}
                        pending.add(trace_id)
                        _verify_chat_http(spans, matching)
                        pending.discard(trace_id)
                        if not url:
                            raise ValueError("Trace 页面地址未配置或无效")
                if matched_this_poll:
                    current = tuple(sorted((t, tuple(v["span_ids"])) for t, v in found.items()))
                    if current != signature:
                        signature, stable_since = current, self.monotonic()
                    if (not pending and expected_runs <= observed_runs
                            and self.monotonic() - stable_since >= self.quiet_seconds):
                        return list(found.values())
                    reason = ("Task run Trace 未到齐: " + ",".join(sorted(expected_runs - observed_runs))
                              if expected_runs - observed_runs else "Trace 已取得，等待全部运行上报完成超时")
            except Exception as exc:
                reason = (f"Trace 采集失败: {exc}" if isinstance(exc, ValueError)
                          else f"Trace 采集失败: {type(exc).__name__}")
            self.sleep(min(1, max(0, deadline - self.monotonic())))
        raise TraceCollectionError(reason, found.values())


def collect_case(result, driver, environ):
    """Collect each sent message and its independent Task run before cleanup."""
    sent = [m for m in result.get("messages", []) if m.get("trace_context")]
    if not sent:
        return
    try:
        snapshot = driver.get("/chat/observations/otel", timeout=5)
        if snapshot.get("status") == "disabled":
            for message in sent:
                message.update(traces=[], trace_reason="OTel 未启用")
            return
        if snapshot.get("status") != "healthy":
            raise ValueError("OTel 自检非 healthy")
        collector = TraceCollector(
            environ.get("N_AGENT_E2E_TEMPO_URL", "http://host.docker.internal:3200"),
            snapshot.get("links", {}).get("Trace", ""))
    except Exception as exc:
        for message in sent:
            _trace_failure(message, f"Trace 采集初始化失败: {type(exc).__name__}")
        result.update(verdict="FAIL", reason="trace_incomplete")
        return
    for message in sent:
        context = message["trace_context"]
        message.update(traces=[], trace_reason="")
        targets = []
        if context.get("replay_via") != "task_api":
            targets.append((context["session_id"], "message"))
        def collect_target(session_id, kind, run_ids=()):
            try:
                traces = collector.collect(
                    session_id, context["start_ns"], context["end_ns"],
                    source="task" if kind == "task" else context.get("source"),
                    task_id=context.get("task_id") if kind == "task" else None,
                    run_ids=run_ids)
            except TraceCollectionError as exc:
                traces = exc.traces
                _trace_failure(message, f"{kind}: {exc}")
            for trace in traces:
                message["traces"].append({**trace, "kind": kind})
        for session_id, kind in targets:
            collect_target(session_id, kind)
        try:
            if context.get("task_id"):
                status, payload = driver.get_raw("/chat/tasks/" + context["task_id"], timeout=5)
                if status != 200 or not isinstance(payload, dict):
                    raise ValueError("Task 关联读取失败")
                task = payload.get("task", payload)
                worker = worker_session_id(task, context["task_id"])
                run_ids = [str(r["id"]) for r in payload.get("runs", []) if r.get("started_at")]
                collect_target(worker, "task", run_ids)
        except Exception as exc:
            _trace_failure(message, f"Task Trace 关联失败: {type(exc).__name__}")
        if message.get("trace_reason"):
            result.update(verdict="FAIL", reason="trace_incomplete")


def _trace_failure(message, reason):
    message.setdefault("traces", [])
    message["trace_reason"] = "; ".join(filter(None, [message.get("trace_reason"), reason]))
    message["verdict"] = "FAIL"
    message["reason"] = "; ".join(filter(None, [message.get("reason"), reason]))


def report_cases(report):
    rows = []
    def add(name, result, trace=None, reason=""):
        trace = trace or {}
        rows.append({"category": "对话", "id": name, "name": name, "result": result,
                     "trace_id": trace.get("trace_id"), "trace_url": trace.get("trace_url"),
                     "reason": reason})
    for case in report["cases"]:
        attempts = case.get("attempts") or [{"attempt": 1, **case}]
        for attempt in attempts:
            if attempt.get("verdict") != "PASS":
                add(f'{case["id"]} / 尝试{attempt["attempt"]} / 验收汇总', "FAIL",
                    reason=attempt.get("reason") or "用例未通过，见消息现场")
            for message in attempt.get("messages", []):
                name = f'{case["id"]} / 尝试{attempt["attempt"]} / {message["id"]}'
                for trace in message.get("traces") or [{}]:
                    label = name + (" / Task" if trace.get("kind") == "task" else "")
                    add(label, message["verdict"], trace,
                        message.get("trace_reason") or message.get("reason") or "未执行，未产生 Trace")
                if message.get("traces") and message.get("trace_reason"):
                    add(name + " / Trace 验收", "FAIL", reason=message["trace_reason"])
    if report.get("cleanup", {}).get("failed"):
        add("套件清理", "FAIL", reason="清理失败，详见 report.json 和运行日志")
    return rows


def render_summary(report):
    return _report.render_summary(report_cases(report))


def render_report(report):
    return (f'E2E：conversations；运行：{report["run_id"]}\n\n' +
            _report.render_table(report_cases(report)))
