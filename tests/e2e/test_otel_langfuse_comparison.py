"""使用固定后端证据验证对账断言，不运行模型或宿主应用。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest


def module():
    path = Path(__file__).with_name("otel_langfuse_comparison.py")
    assert path.is_file(), "缺少可复用的 OTel/Langfuse 对账器"
    spec = importlib.util.spec_from_file_location("otel_langfuse_comparison", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def evidence():
    # Real pre-fix snapshots are intentionally not test dependencies.
    def observation(kind, ident, duration, usage=None):
        return {"type": kind, "id": ident, "startTime": "2026-11-08T00:00:00.000Z",
                "endTime": f"2026-11-08T00:00:{duration:06.3f}Z", "usageDetails": usage or {},
                "metadata": {"gen_ai.provider.name": "openai"}}
    rows = [observation("AGENT", "a", 2), observation("GENERATION", "b", 1,
            {"input": 100, "output": 10, "cache_read": 50, "cache_write": 0, "total": 160}),
            observation("TOOL", "c", .005)]
    values = {"nagent_agent_runs_total": 1, "gen_ai_invoke_agent_inference_calls_sum": 1,
              "gen_ai_invoke_agent_tool_calls_sum": 1,
              "gen_ai_client_inference_usage_input_tokens_total": 100,
              "gen_ai_client_inference_usage_output_tokens_total": 10,
              "nagent_model_cache_read_tokens_total": 50, "nagent_model_cache_write_tokens_total": 0,
              "nagent_model_normalized_tokens_total": 160,
              "nagent_model_cache_eligible_prompt_tokens_total": 150,
              "nagent_model_cache_eligible_read_tokens_total": 50}
    for field, n in (("input", 100), ("output", 10)):
        values[f"gen_ai_client_inference_operation_{field}_tokens_count"] = 1
        values[f"gen_ai_client_inference_operation_{field}_tokens_sum"] = n
    after = [{"metric": {"__name__": name}, "value": [1, str(n)]} for name, n in values.items()]
    for name, duration in (("invoke_agent", 2), ("client_inference", 1), ("execute_tool", .005)):
        prefix = f"gen_ai_{name}_duration_seconds"
        for suffix, n in (("count", 1), ("sum", duration)):
            after.append({"metric": {"__name__": prefix + "_" + suffix}, "value": [1, str(n)]})
        for le, n in (("0", 0), ("5", 1), ("+Inf", 1)):
            after.append({"metric": {"__name__": prefix + "_bucket", "le": le}, "value": [1, str(n)]})
    return [], after, rows


def test_comparison_is_strict_for_counts_but_accepts_bucket_quantile_error():
    m = module()
    before, after, rows = evidence()
    checks = m.compare_metrics(before, after, rows, tempo_span_count=3)
    assert len(checks) == 29
    assert m.verdict(checks) == "PASS"
    assert any(c["metric"] == "模型 p95(s)" and c["otel"] != c["langfuse"] for c in checks)
    after[0]["value"][1] = "2"
    assert m.verdict(m.compare_metrics(before, after, rows, 3)) == "FAIL"


def test_missing_cache_eligibility_never_becomes_zero_or_divides_by_zero():
    m = module()
    before, after, rows = evidence()
    after = [x for x in after if "cache_eligible" not in x["metric"]["__name__"]]
    checks = m.compare_metrics(before, after, rows, 3)
    assert m.verdict(checks) == "FAIL"
    rate = next(c for c in checks if c["metric"] == "缓存命中率")
    assert rate["result"] == "FAIL" and rate["otel"] is None


def test_unknown_usage_is_reported_as_incomplete_and_cannot_pass():
    m = module()
    before, after, rows = evidence()
    rows[1]["usageDetails"].pop("cache_read")
    checks = m.compare_metrics(before, after, rows, 3)
    assert m.verdict(checks) != "PASS"
    assert any(c["result"] == "SKIP" for c in checks)


def test_comparison_artifact_contains_numbers_traces_and_failure_reason(tmp_path):
    m = module()
    trace = "a" * 32
    data = {"result": "FAIL", "reason": "后端未交付", "instance_id": "instance", "checks": [],
            "calls": [{"trace_id": trace, "trace_url": "https://grafana.example/explore?trace=" + trace,
                       "session_id": "e2e-langfuse-" + trace, "result": "FAIL"}], "cleanup": "PASS"}
    m.write_comparison(tmp_path, data)
    text = (tmp_path / "comparison.md").read_text()
    assert "指标数值对比" in text and "Agent 调用 Trace" in text
    assert trace in text and "[查看Trace](https://grafana.example/" in text
    assert "后端未交付" in text
    assert json.loads((tmp_path / "comparison.json").read_text())["result"] == "FAIL"


def test_otel_shell_runs_probe_in_docker_after_restoring_real_provider():
    text = Path(__file__).with_name("otel_smoke.sh").read_text()
    assert "otel_langfuse_comparison.sh" in text
    tail = text[text.index("run_otel_checks\n"):]
    assert tail.index("restore_provider") < tail.index("otel_langfuse_comparison.sh")
    shell = Path(__file__).with_name("otel_langfuse_comparison.sh").read_text()
    assert 'docker exec "$name" python /tests/otel_langfuse_comparison.py' in shell
    assert "trap cleanup EXIT" in shell


@pytest.mark.parametrize("configured", [False, True])
def test_shell_missing_configuration_skips_but_probe_failure_has_failure_artifact(tmp_path, configured):
    root = tmp_path / "repo with spaces"
    scripts = root / "tests/e2e"
    scripts.mkdir(parents=True)
    for name in ("result_report.py", "otel_langfuse_comparison.py", "otel_langfuse_comparison.sh"):
        shutil.copy2(Path(__file__).with_name(name), scripts / name)
    if configured:
        credentials = root / "docker/langfuse/.env"
        credentials.parent.mkdir(parents=True)
        credentials.write_text("# no real credentials\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text('#!/bin/sh\ncase "$1" in\ninspect) echo \'["--config=/etc/otelcol-contrib/langfuse.json"]\';;\ncreate) exit 7;;\n*) exit 90;;\nesac\n')
    docker.chmod(0o755)
    report_dir = root / "reports/run"
    tool = str(scripts / "result_report.py")
    subprocess.run(["python3", tool, str(report_dir), "init", "test-run"], check=True)
    result = subprocess.run(["bash", str(scripts / "otel_langfuse_comparison.sh"), str(report_dir), "test-run", "app"],
                            env={**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]},
                            text=True, capture_output=True)
    assert result.returncode == (7 if configured else 0), result.stdout + result.stderr
    case = next(c for c in json.loads((report_dir / "report.json").read_text())["cases"] if c["id"] == "langfuse-comparison")
    assert case["result"] == ("FAIL" if configured else "SKIP")
    assert case["trace_ids"] == []
    artifact = report_dir / case["comparison_path"]
    assert artifact.exists()
    assert "[查看对比结果](comparison/comparison.md)" in (report_dir / "result.md").read_text()
    assert "[调用前快照]" not in artifact.read_text()


def test_incorrect_histogram_distribution_fails_even_when_sum_and_count_match():
    m = module()
    before, after, rows = evidence()
    next(r for r in after if r["metric"] == {"__name__": "gen_ai_client_inference_duration_seconds_bucket", "le": "0"})["value"][1] = "1"
    checks = m.compare_metrics(before, after, rows, 3)
    assert next(c for c in checks if c["metric"] == "模型 p95(s)")["result"] == "FAIL"


def owned_manifest(directory, session="e2e-langfuse-" + "a" * 32, source="api"):
    (directory / "owned-sessions.jsonl").write_text(json.dumps({
        "run_id": "run", "session_id": session, "source": source}) + "\n")
    return session


def test_cleanup_cancels_owned_active_run_deletes_and_confirms_absence(tmp_path):
    m = module()
    sid = owned_manifest(tmp_path)
    events = []
    state = {"running": True, "exists": True}
    def request(url, method=None, **kwargs):
        events.append((method or "GET", url))
        if url.endswith("/runs/r1/cancel"):
            state["running"] = False
            return {}
        if url.endswith("/runs"):
            return [{"id": "r1", "session_id": sid, "status": "running"}]
        if method == "DELETE":
            if state["running"]:
                raise HTTPError(url, 409, "busy", {}, None)
            state["exists"] = False
            return {}
        return {"session": {"session_id": sid, "source": "api"} if state["exists"] else None}
    result = m.cleanup_owned_sessions(tmp_path, "run", "http://app", request_fn=request)
    assert result["removed"] == [sid] and result["failed"] == []
    assert any(method == "POST" and url.endswith("/cancel") for method, url in events)
    assert events[-1] == ("GET", "http://app/chat/sessions/" + sid)
    assert not state["exists"]


@pytest.mark.parametrize("sid,source,actual_source", [
    ("user-session", "api", "api"), ("e2e-langfuse-" + "a" * 32, "api", "cli"),
])
def test_cleanup_refuses_unowned_or_wrong_source_sessions(tmp_path, sid, source, actual_source):
    m = module()
    owned_manifest(tmp_path, sid, source)
    mutations = []
    def request(url, method=None, **kwargs):
        if method: mutations.append(method)
        return {"session": {"session_id": sid, "source": actual_source}}
    with pytest.raises(ValueError):
        m.cleanup_owned_sessions(tmp_path, "run", "http://app", request_fn=request)
    assert mutations == []


def test_cleanup_requires_readback_and_is_idempotent(tmp_path):
    m = module()
    sid = owned_manifest(tmp_path)
    def survives(url, method=None, **kwargs):
        return {} if method == "DELETE" else {"session": {"session_id": sid, "source": "api"}}
    with pytest.raises(TimeoutError):
        m.cleanup_owned_sessions(tmp_path, "run", "http://app", request_fn=survives, timeout_seconds=0)
    calls = []
    def absent(url, method=None, **kwargs):
        calls.append(method)
        return {"session": None}
    assert m.cleanup_owned_sessions(tmp_path, "run", "http://app", request_fn=absent)["already_absent"] == [sid]
    assert calls == [None]


def test_failed_business_call_cleans_before_returning_and_retains_trace(tmp_path, monkeypatch):
    m = module()
    events = []
    trace = "a" * 32
    def business():
        events.append("business")
        raise RuntimeError("business failed")
    runner = SimpleNamespace(business=business, trace=None, confirmed_trace=None, app="http://app",
                             tempo=lambda trace: {}, report=SimpleNamespace(base_url="http://grafana/explore"))
    def business_with_trace():
        runner.trace = trace
        return business()
    runner.business = business_with_trace
    def cleanup(*args, **kwargs):
        events.append("cleanup")
        return {"removed": ["e2e-langfuse-" + trace], "already_absent": [], "failed": []}
    monkeypatch.setattr(m, "cleanup_owned_sessions", cleanup, raising=False)
    data = {"calls": []}
    with pytest.raises(RuntimeError, match="business failed"):
        m.collect_call(runner, tmp_path, "run", data, SimpleNamespace(),
                       SimpleNamespace(trace_url=lambda *args: "http://grafana/trace"), lambda: events.append("save"))
    assert events == ["business", "cleanup", "save"]
    assert data["calls"][0]["cleanup"] == "PASS"
    assert data["calls"][0]["trace_id"] == trace


def test_shell_replays_session_cleanup_before_removing_probe():
    shell = Path(__file__).with_name("otel_langfuse_comparison.sh").read_text()
    cleanup = shell.split("cleanup() {", 1)[1].split("trap cleanup EXIT", 1)[0]
    assert "--cleanup-only" in cleanup
    assert cleanup.index('docker stop') < cleanup.index("--cleanup-only") < cleanup.index('docker rm -f')


def test_histogram_boundary_uses_same_one_percent_timing_budget():
    m = module()
    before, after, rows = evidence()
    rows[1]["endTime"] = "2026-11-08T00:00:02.508Z"
    prefix = "gen_ai_client_inference_duration_seconds"
    next(r for r in after if r["metric"]["__name__"] == prefix + "_sum")["value"][1] = "2.499"
    after.append({"metric": {"__name__": prefix + "_bucket", "le": "2.5"}, "value": [1, "1"]})
    assert m.verdict(m.compare_metrics(before, after, rows, 3)) == "PASS"


def test_interrupted_report_write_cannot_block_manifest_cleanup(tmp_path, monkeypatch):
    m = module()
    (tmp_path / "comparison.json").write_text('{"result":')
    calls = []
    def cleanup(*args, **kwargs):
        calls.append(args[1])
        return {"removed": ["e2e-langfuse-" + "a" * 32], "already_absent": [], "failed": []}
    monkeypatch.setattr(m, "cleanup_owned_sessions", cleanup)
    assert m.replay_cleanup(tmp_path, "run") == 0
    data = json.loads((tmp_path / "comparison.json").read_text())
    assert calls == ["run"] and data["cleanup"] == "PASS" and data["result"] == "FAIL"
