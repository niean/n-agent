"""Docker E2E probe plus stdlib reconciliation of fixed backend evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode


COUNTERS = (
    "nagent_agent_runs_total", "gen_ai_invoke_agent_inference_calls_sum",
    "gen_ai_invoke_agent_tool_calls_sum", "gen_ai_client_inference_usage_input_tokens_total",
    "gen_ai_client_inference_usage_output_tokens_total", "nagent_model_cache_read_tokens_total",
    "nagent_model_cache_write_tokens_total", "nagent_model_normalized_tokens_total",
    "nagent_model_cache_eligible_prompt_tokens_total", "nagent_model_cache_eligible_read_tokens_total",
)
HISTOGRAMS = (
    "gen_ai_invoke_agent_duration_seconds", "gen_ai_client_inference_duration_seconds",
    "gen_ai_execute_tool_duration_seconds",
)
CASE_ID = "langfuse-comparison"


def load_support(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scalar(rows, name):
    values = [float(row["value"][1]) for row in rows if row["metric"]["__name__"] == name]
    return sum(values) if values else None


def buckets(rows, name):
    values = {}
    for row in rows:
        if row["metric"]["__name__"] == name + "_bucket":
            le = float(row["metric"]["le"])
            values[le] = values.get(le, 0) + float(row["value"][1])
    return values


def seconds(row):
    start, end = (datetime.fromisoformat(row[key].replace("Z", "+00:00"))
                  for key in ("startTime", "endTime"))
    return (end - start).total_seconds()


def verdict(checks):
    results = {row["result"] for row in checks}
    return "FAIL" if "FAIL" in results else "SKIP" if not checks or "SKIP" in results else "PASS"


def compare_metrics(before, after, observations, tempo_span_count):
    """No extrapolation: compare raw counter differences for exactly one instance."""
    checks = []

    def delta(name):
        end = scalar(after, name)
        return None if end is None else end - (scalar(before, name) or 0)

    def check(name, actual, expected, tolerance=0, note=""):
        result = ("SKIP" if expected is None else "FAIL" if actual is None or
                  not math.isfinite(actual) or abs(actual - expected) > tolerance else "PASS")
        checks.append({"metric": name, "otel": actual, "langfuse": expected, "result": result,
                       "absolute_error": None if actual is None or expected is None else abs(actual - expected),
                       "tolerance": tolerance, "note": note or ("缺失指标，不能补零" if actual is None else
                                                                "未知用量，不能补零" if expected is None else
                                                                f"绝对容差 {tolerance:g}")})

    agents = [r for r in observations if r["type"] == "AGENT"]
    generations = [r for r in observations if r["type"] == "GENERATION"]
    tools = [r for r in observations if r["type"] == "TOOL"]
    usage = [r.get("usageDetails") or {} for r in generations]
    check("Agent 运行数", delta(COUNTERS[0]), len(agents))
    check("模型调用数", delta(COUNTERS[1]), len(generations))
    check("工具调用数", delta(COUNTERS[2]), len(tools))
    for title, field in (("输入", "input"), ("输出", "output")):
        name = "gen_ai_client_inference_operation_" + field + "_tokens"
        known = [u[field] for u in usage if field in u]
        check(title + "Token直方图样本数", delta(name + "_count"), len(known))
        check(title + "Token直方图总和", delta(name + "_sum"), sum(known))
    for field, name in zip(("input", "output", "cache_read", "cache_write"), COUNTERS[3:7]):
        expected = sum(u[field] for u in usage) if usage and all(field in u for u in usage) else None
        check(field + " tokens", delta(name), expected)
    deltas = [delta(name) for name in COUNTERS[3:7]]
    check("total tokens（input+output+cache read+cache write）",
          sum(deltas) if all(n is not None for n in deltas) else None,
          sum(u["total"] for u in usage) if usage and all("total" in u for u in usage) else None)
    known = [u for u in usage if all(k in u for k in ("input", "cache_read", "output"))]
    check("normalized tokens（逐调用取整）", delta(COUNTERS[7]),
          sum(round(u["input"] + .2 * u["cache_read"] + 5 * u["output"]) for u in known)
          if known and len(known) == len(usage) else None)
    eligible = []
    for row, u in zip(generations, usage):
        provider = (row.get("metadata") or {}).get("gen_ai.provider.name")
        if provider in ("openai", "openai-compatible"):
            if all(k in u for k in ("input", "cache_read")) and u.get("cache_write", 0) == 0:
                eligible.append((u["input"] + u["cache_read"], u["cache_read"]))
        elif all(k in u for k in ("input", "cache_read", "cache_write")):
            eligible.append((u["input"] + u["cache_read"] + u["cache_write"], u["cache_read"]))
    prompt = sum(u[0] for u in eligible) if eligible and len(eligible) == len(usage) else None
    read = sum(u[1] for u in eligible) if prompt is not None else None
    check("cache eligible prompt tokens", delta(COUNTERS[8]), prompt)
    check("cache eligible read tokens", delta(COUNTERS[9]), read)
    denominator, numerator = delta(COUNTERS[8]), delta(COUNTERS[9])
    rate = numerator / denominator if denominator and numerator is not None else None
    check("缓存命中率", rate, read / prompt if prompt else None, 1e-10,
          "已知缓存读 / 可判定prompt；缺失计数器不按零流量处理")
    for title, name, group in zip(("Agent", "模型", "工具"), HISTOGRAMS, (agents, generations, tools)):
        durations = [seconds(row) for row in group]
        count, total = delta(name + "_count"), delta(name + "_sum")
        check(title + "耗时样本数", count, len(durations))
        check(title + "耗时总和(s)", total, sum(durations), max(.005 * len(durations), sum(durations) * .01),
              "容忍每样本5ms或总和1%，取较大值；Span时间与测量边界及时间戳精度不同")
        check(title + "平均耗时(s)", total / count if count and total is not None else None,
              sum(durations) / len(durations) if durations else None,
              max(.005, sum(durations) / len(durations) * .01) if durations else 0)
        old = buckets(before, name)
        bs = sorted((le, n - old.get(le, 0)) for le, n in buckets(after, name).items())
        # Entire CDF must agree, allowing timing measurements near a bucket edge.
        cdf_ok = bool(bs) and all(sum(x < le - max(.005, x * .01) for x in durations) <= n <=
                                  sum(x <= le + max(.005, x * .01) for x in durations) for le, n in bs)
        rank = .95 * len(durations)
        previous_le, previous_n, quantile, bounds = 0., 0., None, None
        for le, n in bs:
            if n >= rank and rank and n > previous_n:
                quantile = (previous_le + (le - previous_le) * (rank - previous_n) / (n - previous_n)
                            if math.isfinite(le) else previous_le)
                bounds = (previous_le, le)
                break
            previous_le, previous_n = le, n
        raw = sorted(durations)[math.ceil(rank) - 1] if durations else None
        margin = max(.005, raw * .01) if raw is not None else 0
        good = cdf_ok and bounds is not None and raw is not None and bounds[0] - margin <= raw <= bounds[1] + margin
        checks.append({"metric": title + " p95(s)", "otel": quantile, "langfuse": raw,
                       "result": "PASS" if good else "FAIL",
                       "absolute_error": abs(quantile - raw) if quantile is not None and raw is not None else None,
                       "note": f"OTel桶内线性插值；Langfuse样本最近秩；CDF及对应桶{bounds}须一致，边界容忍每样本5ms或1%"})
    check("Tempo/Langfuse Span 数", tempo_span_count, len(observations))
    return checks


def write_comparison(directory, data):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "comparison.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def cell(value):
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.9g}"
        return str(value).replace("|", "\\|").replace("\n", " ")

    counts = {key: sum(c["result"] == key for c in data["checks"]) for key in ("PASS", "FAIL", "SKIP")}
    lines = ["# 对比 OTel 与 Langfuse 指标", "",
             f'结果：{data["result"]}；通过{counts["PASS"]}，失败{counts["FAIL"]}，跳过{counts["SKIP"]}。', "",
             "同一服务实例、同批Trace；3次真实calculator流式请求（17×19）。计数使用原始样本差值，不使用increase外推。", "",
             "实例：" + str(data.get("instance_id") or "未取得"), "",
             "检查说明：" + data.get("reason", ""), "",
             "## 指标数值对比", "", "| 指标 | OTel | Langfuse | 结果 | 误差与口径 |",
             "|---|---:|---:|---|---|"]
    for row in data["checks"]:
        lines.append("| " + " | ".join(cell(row.get(k)) for k in ("metric", "otel", "langfuse", "result", "note")) + " |")
    lines += ["", "## Agent 调用 Trace", "", "| 调用 | 结果 | 会话 | TraceID | 链接 |", "|---|---|---|---|---|"]
    for i, call in enumerate(data["calls"], 1):
        link = f'[查看Trace]({call["trace_url"]})' if call.get("trace_url") else "未确认Tempo交付，无法生成Trace链接"
        lines.append("| " + " | ".join(cell(v) for v in (i, call["result"], call.get("session_id"), call.get("trace_id"), link)) + " |")
    if not data["calls"]:
        lines.append("| — | 未执行 | — | — | 前置检查失败，未调用Agent |")
    snapshots = [f'[{title}]({name})' for title, name in (("调用前快照", "prometheus-before.json"),
                 ("调用后快照", "prometheus-after.json")) if (directory / name).exists()]
    lines += ["", "## 证据与边界", "",
              "- Prometheus：" + (" / ".join(snapshots) if snapshots else "未取得快照"),
              "- 每次调用的trace-<TraceID>.json保留双端原始响应；用量、Span身份、父子关系和时间逐项校验。",
              "- p95按直方图桶精度核验，不表示两端p95数值相等；未知用量不补零且不能判为全部通过。",
              "- Prometheus无费用/TTFT对应instrument；有明确定价版本的估算费用及completionStartTime由双端逐Span检查。",
              "- 会话清理：" + data.get("cleanup", "未执行"), ""]
    (directory / "comparison.md").write_text("\n".join(lines))


def cleanup_owned_sessions(directory, run_id, app, *, request_fn=None, timeout_seconds=30):
    """Manifest is the sole deletion authority; cancel, delete, then read back."""
    path = Path(directory) / "owned-sessions.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    owned = {}
    for entry in entries:
        if entry.get("run_id") != run_id:
            continue
        sid, source = entry.get("session_id"), entry.get("source")
        if not isinstance(sid, str) or not re.fullmatch(r"e2e-langfuse-[0-9a-f]{32}", sid) or source not in ("api", "dashboard"):
            raise ValueError("invalid owned session manifest")
        if sid in owned and owned[sid] != source:
            raise ValueError("conflicting session ownership")
        owned[sid] = source
    request_fn = request_fn or load_support("langfuse_smoke").request
    evidence = {"removed": [], "already_absent": [], "failed": []}

    def detail(url):
        try:
            value = request_fn(url, timeout=5)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise
        if not isinstance(value, dict) or "session" not in value:
            raise ValueError("invalid session readback")
        return value["session"]

    for sid, source in owned.items():
        url = app.rstrip("/") + "/chat/sessions/" + sid
        value = detail(url)
        if value is None:
            evidence["already_absent"].append(sid)
            continue
        if (not isinstance(value, dict) or value.get("source") != source or
                value.get("session_id", sid) != sid):
            raise ValueError("session ownership mismatch")
        deadline = time.monotonic() + timeout_seconds
        cancelled = set()
        while True:
            try:
                request_fn(url, method="DELETE", timeout=5)
            except HTTPError as error:
                if error.code not in (404, 409):
                    raise
                if error.code == 409:
                    runs = request_fn(url + "/runs", timeout=5)
                    if not isinstance(runs, list):
                        raise ValueError("invalid session runs")
                    for run in runs:
                        if run.get("session_id") != sid:
                            raise ValueError("run ownership mismatch")
                        if run.get("status") not in ("pending", "running"):
                            continue
                        ident = run.get("id")
                        if not isinstance(ident, str) or not ident:
                            raise ValueError("missing active run identity")
                        if ident not in cancelled:
                            try:
                                request_fn(url + "/runs/" + quote(ident, safe="") + "/cancel", method="POST", timeout=5)
                            except HTTPError as cancel_error:
                                if cancel_error.code not in (404, 409):
                                    raise
                            cancelled.add(ident)
            value = detail(url)
            if value is None:
                evidence["removed"].append(sid)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("owned session remains after cleanup")
            time.sleep(.25)
    return evidence


def collect_call(runner, directory, run_id, data, support, reporting, save):
    call = {"result": "RUNNING", "trace_id": None, "trace_url": None}
    data["calls"].append(call)
    runner.trace = runner.confirmed_trace = None
    try:
        evidence = runner.business()
        trace = evidence["trace_id"]
        tempo = runner.tempo(trace)
        observations = support.observations(runner.get, trace)
        (directory / ("trace-" + trace + ".json")).write_text(
            json.dumps({"tempo": tempo, "langfuse": observations}, ensure_ascii=False) + "\n")
        support.compare_backends(tempo, observations)
        call.update(result="PASS", trace_id=trace, session_id=evidence["session"],
                    trace_url=reporting.trace_url(runner.report.base_url, trace))
        return observations, len(support.original_spans(tempo))
    except Exception:
        trace = runner.trace
        call.update(result="FAIL", trace_id=trace, session_id="e2e-langfuse-" + trace if trace else None)
        if trace:
            try:
                tempo = runner.tempo(trace)
                path = directory / ("trace-" + trace + ".json")
                if not path.exists():
                    path.write_text(json.dumps({"tempo": tempo}) + "\n")
                call["trace_url"] = reporting.trace_url(runner.report.base_url, trace)
            except Exception:
                pass
        raise
    finally:
        try:
            call["cleanup_evidence"] = cleanup_owned_sessions(directory, run_id, runner.app)
            call["cleanup"] = "PASS"
        except Exception:
            call["cleanup"] = "FAIL"
            raise
        finally:
            save()


def replay_cleanup(directory, run_id):
    """Recovery requires the manifest and app only, never Langfuse credentials."""
    directory = Path(directory)
    path = directory / "comparison.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {"result": "FAIL", "checks": [], "calls": [], "reason": "对账执行中断或报告未完整写入"}
    if data["result"] == "RUNNING":
        data.update(result="FAIL", reason="对账执行中断；已重放会话清理")
    try:
        data["cleanup_evidence"] = cleanup_owned_sessions(
            directory, run_id, os.environ.get("LANGFUSE_APP_URL", "http://n-agent-n-agent-1:8201"))
        data["cleanup"] = "PASS"
    except Exception as error:
        data.update(result="FAIL", cleanup="FAIL", reason="清单会话清理失败：" + type(error).__name__)
    write_comparison(directory, data)
    return 0 if data["cleanup"] == "PASS" else 1


def run(directory, run_id):
    """All business and backend traffic is restricted to this Docker entry point."""
    if not Path("/.dockerenv").exists():
        raise RuntimeError("E2E must run inside Docker")
    support = load_support("langfuse_smoke")
    reporting = load_support("result_report")
    directory = Path(directory)
    data = {"result": "RUNNING", "reason": "执行中", "checks": [], "calls": [], "cleanup": "未执行",
            "started_at": datetime.now(timezone.utc).isoformat()}
    runner = None

    def save():
        write_comparison(directory, data)

    def snapshot():
        if support.request(runner.app + "/chat/observations/otel")["instance_id"] != runner.instance_id:
            raise AssertionError("service instance changed during comparison")
        query = ('{service_name="n-agent",service_instance_id=' + json.dumps(runner.instance_id) +
                 ',__name__=~"(nagent_agent_runs_total|nagent_model_.*|gen_ai_.*)"}')
        value = support.request("http://prometheus:9090/api/v1/query?" + urlencode({"query": query}))
        if value.get("status") != "success":
            raise AssertionError("Prometheus query failed")
        return value["data"]["result"]

    save()
    try:
        runner = support.Runner(str(directory), run_id, plan=False)
        data["instance_id"] = runner.instance_id
        # Settle metrics from preceding OTel/failure/cancel or conversation cases.
        time.sleep(12)
        before = snapshot()
        (directory / "prometheus-before.json").write_text(json.dumps(before, indent=2) + "\n")
        rows, span_count = [], 0
        for _ in range(3):
            observations, count = collect_call(runner, directory, run_id, data, support, reporting, save)
            rows.extend(observations)
            span_count += count
        time.sleep(12)
        after = snapshot()
        (directory / "prometheus-after.json").write_text(json.dumps(after, indent=2) + "\n")
        data["checks"] = compare_metrics(before, after, rows, span_count)
        data.update(result=verdict(data["checks"]), reason="计数/Token严格对齐；耗时与p95按报告所列容差核验")
    except Exception as error:
        data.update(result="FAIL", reason=support.safe_error(error) + "; " + getattr(runner, "stage", "preflight"))
    finally:
        if runner:
            try:
                data["cleanup_evidence"] = cleanup_owned_sessions(directory, run_id, runner.app)
                data["cleanup"] = "PASS"
            except Exception as error:
                data.update(result="FAIL", cleanup="FAIL", reason=data["reason"] + "; cleanup " + support.safe_error(error))
        data["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return 1 if data["result"] == "FAIL" else 0


if __name__ == "__main__":
    if not Path("/.dockerenv").exists():
        raise SystemExit("E2E must run inside Docker")
    if sys.argv[1] == "--cleanup-only":
        sys.exit(replay_cleanup(sys.argv[2], sys.argv[3]))
    sys.exit(run(sys.argv[1], sys.argv[2]))
