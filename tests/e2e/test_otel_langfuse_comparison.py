"""使用固定后端证据验证对账断言，不运行模型或宿主应用。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
    rows[1]["metadata"]["nagent.model.duration_ms"] = 1000
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


def test_comparison_is_manual_only_and_probe_still_runs_in_docker():
    text = Path(__file__).with_name("otel_smoke.sh").read_text()
    assert "otel_langfuse_comparison.sh" not in text
    assert "langfuse-comparison" not in text
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
    fixtures = scripts / "fixtures"
    fixtures.mkdir()
    shutil.copy2(Path(__file__).parent / "fixtures" / "model-pricing-e2e.json",
                 fixtures / "model-pricing-e2e.json")
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
    # 配置齐全时 Phase C 部署先于探针容器创建；沙箱内 n-kb compose 缺失 =>
    # 成本探测部署失败（exit 1），同样必须产出失败工件；未配置仍为显式 SKIP。
    assert result.returncode == (1 if configured else 0), result.stdout + result.stderr
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
    # Span 墙钟口径的耗时核对（Agent 组）保留边界预算；模型组已改为
    # duration_ms 同口径核对（见 metadata 投影用例）。
    rows[0]["endTime"] = "2026-11-08T00:00:02.508Z"
    prefix = "gen_ai_invoke_agent_duration_seconds"
    next(r for r in after if r["metric"]["__name__"] == prefix + "_sum")["value"][1] = "2.499"
    after.append({"metric": {"__name__": prefix + "_bucket", "le": "2.5"}, "value": [1, "1"]})
    assert m.verdict(m.compare_metrics(before, after, rows, 3)) == "PASS"


def test_model_duration_compares_histogram_with_metadata_projection_not_span_wall():
    m = module()
    before, after, rows = evidence()
    # Span 墙钟（endTime）与钩子测量不同口径：把 Span 拉长到必然越界，
    # duration_ms 投影（1000ms = 直方图总和）仍须严格对齐。
    rows[1]["endTime"] = "2026-11-08T00:00:03.000Z"
    checks = m.compare_metrics(before, after, rows, 3)
    assert next(c for c in checks if c["metric"] == "模型耗时总和(s)")["result"] == "PASS"
    assert m.verdict(checks) == "PASS"


def test_missing_model_duration_metadata_skips_and_cannot_pass():
    m = module()
    before, after, rows = evidence()
    del rows[1]["metadata"]["nagent.model.duration_ms"]
    checks = m.compare_metrics(before, after, rows, 3)
    skipped = {c["metric"] for c in checks if c["result"] == "SKIP"}
    assert {"模型耗时样本数", "模型耗时总和(s)", "模型平均耗时(s)", "模型 p95(s)"} <= skipped
    assert m.verdict(checks) != "PASS"


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


# ---------------------------------------------------------------------------
# T8 成本对比扩展（spec AC10/AC11/AC12/AC14/AC16）
# ---------------------------------------------------------------------------

from decimal import Decimal


E2E_PRICES_CHAT = {"input": Decimal("2"), "output": Decimal("10"),
                   "cache_read": Decimal("0.2"), "cache_write": Decimal("2.5")}
E2E_PRICES_EMBED = {"input": Decimal("0.1"), "output": None,
                    "cache_read": None, "cache_write": None}
E2E_PRICES_ZERO = {"input": Decimal("0"), "output": None,
                   "cache_read": None, "cache_write": None}
E2E_CATALOG = {"content_version": "e2e-test-261109", "content_sha256": "x",
               "prices": {("openai", "e2e-cost-model", "chat"): E2E_PRICES_CHAT,
                          ("ollama", "bge-m3", "embeddings"): E2E_PRICES_EMBED,
                          ("ollama", "e2e-zero-model", "embeddings"): E2E_PRICES_ZERO}}


def estimated_leaf(leaf_id, decimal="0.00041", amount=0.00041, details=None,
                   usage=None, **values):
    details = details or {"input": 0.0002, "output": 0.0002, "cache_read": 0.00001,
                          "total": 0.00041}
    base = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
            "nagent.model.cost_status": "estimated", "nagent.model.cost_currency": "USD",
            "nagent.model.cost_decimal": decimal, "nagent.model.cost_usd": amount,
            "nagent.model.pricing_version": "e2e-test-261109",
            "nagent.model.price_id": "e2e-chat-vector-ab",
            "nagent.model.billing_provider": "openai",
            "nagent.model.billing_model": "e2e-cost-model",
            "nagent.model.cost_input_usd": 0.0002, "nagent.model.cost_output_usd": 0.0002,
            "nagent.model.cost_cache_read_usd": 0.00001}
    base.update(values)
    row = {"type": "GENERATION", "id": leaf_id, "model": "e2e-cost-model",
           "usageDetails": usage or {"input": 100, "output": 20, "cache_read": 50,
                                     "total": 170},
           "metadata": {**base, "nagent.model.cost_decimal": decimal},
           "costDetails": details}
    return base, row


def unknown_leaf(leaf_id, reason="missing_usage", **overrides):
    values = {"gen_ai.operation.name": "chat", "nagent.model.cost_status": "unknown",
              "nagent.model.cost_reason": reason}
    values.update(overrides)
    row = {"type": "GENERATION", "id": leaf_id, "model": None, "usageDetails": {},
           "metadata": {**values}, "costDetails": None}
    return values, row


def test_expected_cost_reproduces_plan_vectors_exactly():
    m = module()
    # 向量 B（OpenAI 无 write）：Ti=100、To=20、Tic=50 -> 0.00041。
    total, components = m.expected_cost(
        {"input": 100, "output": 20, "cache_read": 50}, E2E_PRICES_CHAT, "chat")
    assert total == Decimal("0.00041")
    assert components == {"input": Decimal("0.0002"), "output": Decimal("0.0002"),
                          "cache_read": Decimal("0.00001")}
    # 向量 A（四桶）：Ti=100、To=20、Tic=50、Ticw=10 -> 0.000435。
    total_a, components_a = m.expected_cost(
        {"input": 100, "output": 20, "cache_read": 50, "cache_write": 10},
        E2E_PRICES_CHAT, "chat")
    assert total_a == Decimal("0.000435")
    assert components_a["cache_write"] == Decimal("0.000025")
    # 向量 C（N-KB）：input=200 @0.1 -> 0.00002；显式零价 -> estimated/0。
    assert m.expected_cost({"input": 200}, E2E_PRICES_EMBED, "embeddings")[0] == Decimal("0.00002")
    assert m.expected_cost({"input": 0}, E2E_PRICES_ZERO, "embeddings")[0] == Decimal("0")
    assert m.expected_cost({"input": 0, "output": 0, "cache_read": 0},
                           E2E_PRICES_CHAT, "chat")[0] == Decimal("0")
    # 缺桶/缺单价不伪造为零。
    assert m.expected_cost({"input": 100, "output": 20}, E2E_PRICES_CHAT, "chat")[0] is None
    assert m.expected_cost({"input": 10}, {"input": None, "output": None,
                                           "cache_read": None, "cache_write": None},
                           "embeddings")[0] is None


def test_expected_cost_cache_write_zero_with_null_price_is_skipped():
    m = module()
    # openai 适配器声明 cache_write not_applicable（目录价为 null），但采集器
    # 会把已知零值投影进 usageDetails（cache_write=0 通过 IsInt>=0）。0 桶对
    # 总额贡献恒为 0，应跳过且不进入分项（与 costDetails 键集一致）。
    openai_prices = {"input": Decimal("2"), "output": Decimal("10"),
                     "cache_read": Decimal("0.2"), "cache_write": None}
    total, components = m.expected_cost(
        {"input": 100, "output": 20, "cache_read": 50, "cache_write": 0},
        openai_prices, "chat")
    assert total == Decimal("0.00041")
    assert components == {"input": Decimal("0.0002"), "output": Decimal("0.0002"),
                          "cache_read": Decimal("0.00001")}
    assert "cache_write" not in components
    # 非零 cache_write 无单价仍是 pricing_unavailable，不得伪造为零。
    reason = m.expected_cost(
        {"input": 100, "output": 20, "cache_read": 50, "cache_write": 10},
        openai_prices, "chat")
    assert reason == (None, "pricing_unavailable")


def test_cost_decimal_must_be_byte_identical_across_three_backends():
    m = module()
    values, row = estimated_leaf("s1")
    assert m.verify_cost_leaf(values, row, E2E_CATALOG) == []
    # 任一后端字符串不一致都失败（含尾零等非逐字节差异）。
    row["metadata"]["nagent.model.cost_decimal"] = "0.000410"
    issues = m.verify_cost_leaf(values, row, E2E_CATALOG)
    assert any("Langfuse" in item for item in issues)
    row["metadata"]["nagent.model.cost_decimal"] = "0.00041"
    # 完成日志逐字节核对（Trace 级，第三个后端）。
    assert m.verify_completion_logs(["0.00041"], ['{"nagent.model.cost_decimal":"0.00041"}']) == []
    assert m.verify_completion_logs(["0.00041"],
                                    ['{"nagent.model.cost_decimal":"0.00042"}']) != []
    assert m.verify_completion_logs(["0.00041"], ["no cost"]) != []
    # Span decimal 与本地 Decimal 预期不符。
    values2, row2 = estimated_leaf("s1", decimal="0.00042")
    issues = m.verify_cost_leaf(values2, row2, E2E_CATALOG)
    assert any("cost_decimal" in item for item in issues)
    # double 超容差。
    values3, row3 = estimated_leaf("s1", amount=0.00052)
    assert m.verify_cost_leaf(values3, row3, E2E_CATALOG) != []
    # costDetails 分项和与 total 超出 double 容差。
    values4, row4 = estimated_leaf(
        "s1", details={"input": 0.0003, "output": 0.0002, "cache_read": 0.00001,
                       "total": 0.00041})
    assert m.verify_cost_leaf(values4, row4, E2E_CATALOG) != []
    # 目录缺少该计价身份时显式失败，不静默跳过。
    values5, row5 = estimated_leaf("s1")
    assert m.verify_cost_leaf(values5, row5, {"prices": {}}) != []


def test_langfuse_metadata_numeric_decimal_is_compared_canonically():
    m = module()
    # Langfuse exporter 重组 metadata JSON 时把数字形态的字符串解析为 number
    # （'0' -> 0、'0.00041' -> 0.00041、'0.00002' -> 2e-05）；metadata 侧按
    # Decimal 规范化等值比对，Tempo 属性与完成日志仍是逐字节权威。
    values, row = estimated_leaf("s1")
    row["metadata"]["nagent.model.cost_decimal"] = 0.00041
    assert m.verify_cost_leaf(values, row, E2E_CATALOG) == []
    values2, row2 = estimated_leaf(
        "s1", decimal="0.00002", amount=0.00002,
        details={"input": 0.00002, "total": 0.00002},
        usage={"input": 200, "total": 200},
        **{"gen_ai.operation.name": "embeddings",
           "nagent.model.billing_provider": "ollama",
           "nagent.model.billing_model": "bge-m3",
           "nagent.model.price_id": "e2e-embed-vector-c",
           "nagent.model.cost_input_usd": 0.00002,
           "nagent.model.cost_output_usd": 0,
           "nagent.model.cost_cache_read_usd": 0})
    row2["model"] = "bge-m3"
    row2["metadata"]["nagent.model.cost_decimal"] = 2e-05
    assert m.verify_cost_leaf(values2, row2, E2E_CATALOG) == []
    values3, row3 = estimated_leaf("s1", decimal="0", amount=0,
                                   details={"input": 0, "output": 0,
                                            "cache_read": 0, "total": 0},
                                   usage={"input": 0, "output": 0, "cache_read": 0,
                                          "total": 0})
    row3["metadata"]["nagent.model.cost_decimal"] = 0
    assert m.verify_cost_leaf(values3, row3, E2E_CATALOG) == []
    # 数值不一致仍失败。
    row["metadata"]["nagent.model.cost_decimal"] = 0.00042
    assert m.verify_cost_leaf(values, row, E2E_CATALOG) != []
    # 非有限数值/布尔不接受。
    row["metadata"]["nagent.model.cost_decimal"] = True
    assert m.verify_cost_leaf(values, row, E2E_CATALOG) != []
    # float 形态的 metadata decimal 也进入完成日志逐字节核对（不再静默跳过）。
    row["metadata"]["nagent.model.cost_decimal"] = 0.00041
    issues = m.verify_call_costs(
        {row["id"]: {"values": values}}, [row], ['{"nagent.model.cost_decimal":"0.00042"}'],
        E2E_CATALOG)
    assert any("完成日志" in item or "cost_decimal" in item for item in issues)
    assert m.verify_call_costs(
        {row["id"]: {"values": values}}, [row], ['{"nagent.model.cost_decimal":"0.00041"}'],
        E2E_CATALOG) == []


def test_kb_search_retries_only_cold_load_502(monkeypatch):
    m = module()
    calls = []

    def flaky(url, payload, timeout=120):
        calls.append(url)
        if len(calls) == 1:
            raise HTTPError(url, 502, "Bad Gateway", None, None)
        return {"ok": True}

    monkeypatch.setattr(m, "_post", flaky)
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    assert m.kb_search("http://n-kb:8212/", {"query": "x", "top_k": 1}) == {"ok": True}
    assert calls == ["http://n-kb:8212/retrieval/search"] * 2

    def always(url, payload, timeout=120):
        calls.append(url)
        raise HTTPError(url, 502, "Bad Gateway", None, None)

    monkeypatch.setattr(m, "_post", always)
    with pytest.raises(HTTPError):
        m.kb_search("http://n-kb:8212", {"query": "x"})
    assert len(calls) == 2 + 3

    def forbidden(url, payload, timeout=120):
        raise HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(m, "_post", forbidden)
    with pytest.raises(HTTPError):
        m.kb_search("http://n-kb:8212", {"query": "x"})
    # 404 未被 forbidden 计数；重试仅针对 502，非 502 首次即抛。
    assert len(calls) == 5


def test_unknown_leaf_blocks_auto_pricing_and_reports_coverage():
    m = module()
    values, row = unknown_leaf("u1")
    assert m.verify_cost_leaf(values, row, E2E_CATALOG) == []
    # amount/reason 冲突各自失败。
    values2, row2 = unknown_leaf("u1", reason="not-a-reason")
    assert m.verify_cost_leaf(values2, row2, E2E_CATALOG) != []
    values3, row3 = unknown_leaf("u1", **{"nagent.model.cost_usd": 0.0})
    assert any("金额" in item for item in m.verify_cost_leaf(values3, row3, E2E_CATALOG))
    # 未阻断推价（model 或 costDetails 存在）必须失败。
    values4, row4 = unknown_leaf("u1")
    row4["model"] = "e2e-cost-model"
    assert m.verify_cost_leaf(values4, row4, E2E_CATALOG) != []
    values5, row5 = unknown_leaf("u1")
    row5["costDetails"] = {"total": 0.0}
    assert m.verify_cost_leaf(values5, row5, E2E_CATALOG) != []

    _, good = estimated_leaf("g1")
    bad = {"type": "GENERATION", "id": "g2", "metadata": {}}
    issues, coverage = m.verify_no_auto_pricing([good, unknown_leaf("u1")[1], bad])
    assert any("cost_status" in item for item in issues)
    assert coverage == {"estimated": 1, "unknown": 1, "total": 2, "ratio": 0.5}
    issues_all, coverage_all = m.verify_no_auto_pricing([good, good.copy()])
    assert issues_all == [] and coverage_all["ratio"] == 1.0


def cost_metric_rows():
    _, chat = estimated_leaf("g1")
    _, embed = estimated_leaf(
        "g2", decimal="0.00002", amount=0.00002, details={"input": 0.00002, "total": 0.00002})
    embed["type"] = "EMBEDDING"
    embed["metadata"]["gen_ai.operation.name"] = "embeddings"
    return [chat, embed, unknown_leaf("u1")[1]]


def cost_snapshot(cost="0.00043", components=None, attempts="3"):
    components = components or {"input": "0.00022", "output": "0.0002",
                                "cache_read": "0.00001"}
    rows = [{"metric": {"__name__": "nagent_model_cost_USD_total"}, "value": [1, cost]},
            {"metric": {"__name__": "nagent_model_cost_attempts_total"}, "value": [1, attempts]}]
    for component, value in components.items():
        rows.append({"metric": {"__name__": "nagent_model_cost_components_USD_total",
                                "nagent_model_cost_component": component},
                     "value": [1, value]})
    return rows


def test_cost_metric_reconciliation_separates_total_components_and_exact_attempts():
    m = module()
    counters = ("nagent_model_cost_USD_total", "nagent_model_cost_components_USD_total",
                "nagent_model_cost_attempts_total", "nagent_model_cost_component")
    checks = m.compare_cost_metrics([], cost_snapshot(), cost_metric_rows(), counters)
    assert m.verdict(checks) == "PASS"
    assert len(checks) == 5
    # total 与 components 分开，禁止相加：篡改任一分项即失败。
    tampered = cost_snapshot(components={"input": "0.00023", "output": "0.0002",
                                        "cache_read": "0.00001"})
    assert m.verdict(m.compare_cost_metrics([], tampered, cost_metric_rows(), counters)) == "FAIL"
    # attempts 必须精确相等（unknown 也计入分母）。
    assert m.verdict(m.compare_cost_metrics([], cost_snapshot(attempts="2"),
                                            cost_metric_rows(), counters)) == "FAIL"
    # 缺序列不补零：无 estimated 时金额对账跳过，attempts 仍精确。
    empty_rows = [unknown_leaf("u1")[1]]
    checks = m.compare_cost_metrics([], cost_snapshot(cost="0", components={}, attempts="1"),
                                    empty_rows, counters)
    assert any(c["result"] == "SKIP" for c in checks)
    assert m.verdict(checks) == "SKIP"
    assert next(c for c in checks if "尝试数" in c["metric"])["result"] == "PASS"
    # 有 estimated 但 Counter 缺失 -> FAIL，不得补零。
    missing = cost_snapshot()
    missing = [row for row in missing
               if row["metric"]["__name__"] != "nagent_model_cost_USD_total"]
    assert m.verdict(m.compare_cost_metrics([], missing, cost_metric_rows(), counters)) == "FAIL"


def test_cross_service_chain_total_and_independent_index_exclusion():
    m = module()
    _, chat = estimated_leaf("g1")
    _, retrieval = estimated_leaf("g2", decimal="0.00002", amount=0.00002,
                                  details={"input": 0.00002, "total": 0.00002})
    retrieval["type"] = "EMBEDDING"
    issues = m.verify_cross_service_chain([chat, retrieval])
    assert issues == []
    # 聊天 Trace 总额精确 0.00041 + 0.00002 = 0.00043。
    assert m.verify_expected_trace_total([chat, retrieval], Decimal("0.00043")) == []
    assert m.verify_expected_trace_total([chat, retrieval], Decimal("0.00045")) != []
    # 独立索引 Trace 只含自身 0.00002，不计入聊天 Trace。
    assert m.verify_expected_trace_total([retrieval],
                                         Decimal("0.00002")) == []
    _, index_leaf = estimated_leaf("k1", decimal="0.00002", amount=0.00002,
                                   details={"input": 0.00002, "total": 0.00002})
    index_leaf["type"] = "EMBEDDING"
    assert m.verify_expected_trace_total([index_leaf], Decimal("0.00002")) == []
    assert m.trace_cost_total([chat, index_leaf]) == Decimal("0.00043")
    # unknown 或缺叶只能称已观测下界。
    mixed = [chat, unknown_leaf("u1")[1]]
    assert m.trace_cost_total(mixed) is None
    assert m.verify_expected_trace_total(mixed, Decimal("0.00041")) != []
    assert m.verify_cross_service_chain([chat]) != []
    # 父级（AGENT/TOOL/RETRIEVER）不参与叶子总额。
    assert m.trace_cost_total([chat, {"type": "AGENT", "id": "a", "metadata": {}}]) == Decimal("0.00041")


def test_nkb_rows_use_nkb_semantic_keys_in_totals_and_reconciliation():
    m = module()
    # 真实 N-KB 叶以 nkb.model.* 键投影（attempt-5 真机缺陷：nagent 硬编码把
    # nkb 叶判为"未知叶子"）；总额、覆盖率与 Metric 对账都按行前缀识别。
    def nkb_leaf(leaf_id, status="estimated", decimal="0.00002"):
        return {"type": "EMBEDDING", "id": leaf_id,
                "metadata": {"nkb.model.cost_status": status,
                             "nkb.model.cost_decimal": decimal},
                "costDetails": {"input": 0.00002, "total": 0.00002}}
    assert m.trace_cost_total([nkb_leaf("k1")]) == Decimal("0.00002")
    assert m.trace_cost_total([nkb_leaf("k1", status="unknown")]) is None
    # 跨服务链：nagent chat 叶 + nkb embedding 叶按各自前缀求和。
    _, chat = estimated_leaf("g1")
    assert m.trace_cost_total([chat, nkb_leaf("k1")]) == Decimal("0.00043")
    issues, coverage = m.verify_no_auto_pricing([nkb_leaf("k1")])
    assert issues == [] and coverage["estimated"] == 1
    # Metric 对账：nkb 行配 nkb Counter 集合；float 形态 metadata decimal 同样接受。
    counters = ("nkb_model_cost_USD_total", "nkb_model_cost_components_USD_total",
                "nkb_model_cost_attempts_total", "nkb_model_cost_component")
    rows = [nkb_leaf("k1")]
    rows[0]["metadata"]["nkb.model.cost_decimal"] = 2e-05
    snapshot = [{"metric": {"__name__": "nkb_model_cost_USD_total"}, "value": [1, "0.00002"]},
                {"metric": {"__name__": "nkb_model_cost_attempts_total"}, "value": [1, "1"]},
                {"metric": {"__name__": "nkb_model_cost_components_USD_total",
                            "nkb_model_cost_component": "input"},
                 "value": [1, "0.00002"]}]
    checks = m.compare_cost_metrics([], snapshot, rows, counters)
    assert m.verdict(checks) == "PASS"


def test_window_rows_split_by_service_prefix_for_counter_reconciliation():
    m = module()
    # 窗口内 N-KB 探针叶不得混入 N-Agent 侧对账（attempt-9 真机缺陷：
    # 混入导致 OTel attempts 差值 7 vs Langfuse 侧 8 的伪失败）。
    _, chat = estimated_leaf("g1")
    kb_leaf = {"type": "EMBEDDING", "id": "k1",
               "metadata": {"nkb.model.cost_status": "estimated",
                            "nkb.model.cost_decimal": "0.00002"}}
    agent_rows, kb_rows = m.split_service_rows([chat, kb_leaf])
    assert [row["id"] for row in agent_rows] == ["g1"]
    assert [row["id"] for row in kb_rows] == ["k1"]
    # N-Agent 侧自己的 EMBEDDING 叶归 N-Agent 对账，不进 kb 集合。
    _, agent_embed = estimated_leaf("g2")
    agent_embed["type"] = "EMBEDDING"
    agent_rows, kb_rows = m.split_service_rows([chat, agent_embed, kb_leaf])
    assert [row["id"] for row in agent_rows] == ["g1", "g2"]
    assert [row["id"] for row in kb_rows] == ["k1"]


def test_completion_log_decimal_extraction_is_strict():
    m = module()
    assert m.log_cost_decimal('{"nagent.model.cost_decimal":"0.00041"}') == "0.00041"
    assert m.log_cost_decimal('{"nkb.model.cost_decimal":"0.00002"}') == "0.00002"
    assert m.log_cost_decimal('x nagent_model_cost_decimal="0.00002" y') == "0.00002"
    assert m.log_cost_decimal('{"nested":{"nagent.model.cost_decimal":"0.000435"}}') == "0.000435"
    assert m.log_cost_decimal("no cost here") is None
    issues = m.verify_completion_logs(["0.00041", "0.00002"],
                                      ['{"nagent.model.cost_decimal":"0.00041"}',
                                       'nagent_model_cost_decimal="0.00002"'])
    assert issues == []
    assert m.verify_completion_logs(["0.00041", "0.00002"],
                                    ['{"nagent.model.cost_decimal":"0.00041"}']) != []


def test_report_carries_repo_versions_pricing_identity_and_boundary(tmp_path):
    m = module()
    data = {"result": "PASS", "checks": [], "calls": [], "cleanup": "PASS",
            "versions": {"n-agent": "ver-agent", "n-kb": "ver-kb"},
            "pricing": {"content_version": "e2e-test-261109",
                        "content_sha256": "a" * 64,
                        "source": "tests/e2e/fixtures/model-pricing-e2e.json"},
            "cost": {"coverage": {"estimated": 3, "unknown": 1, "total": 4, "ratio": 0.75},
                     "issues": [], "trace_totals": {"chat": "0.00043"}}}
    m.write_comparison(tmp_path, data)
    text = (tmp_path / "comparison.md").read_text()
    assert "ver-agent" in text and "ver-kb" in text
    assert "e2e-test-261109" in text and "a" * 64 in text
    assert "75.00%" in text and "0.00043" in text
    assert "已观测" in text and "采样" in text


def test_pricing_fixture_identity_matches_module_constants():
    m = module()
    fixture = Path(__file__).with_name("fixtures").joinpath("model-pricing-e2e.json")
    identity = m.load_pricing(fixture)
    assert identity["content_version"] == m.E2E_PRICING_CONTENT_VERSION
    assert identity["content_sha256"] == m.E2E_PRICING_SHA256
    assert identity["prices"][("openai", "e2e-cost-model", "chat")]["input"] == Decimal("2")
    assert identity["prices"][("ollama", "bge-m3", "embeddings")]["input"] == Decimal("0.1")
    assert identity["prices"][("ollama", "e2e-zero-model", "embeddings")]["input"] == Decimal("0")


def test_comparison_shell_wires_probe_catalog_and_kb_context():
    shell = Path(__file__).with_name("otel_langfuse_comparison.sh").read_text()
    assert "model-pricing-e2e.json" in shell
    assert "E2E_PRICING_CATALOG" in shell
    assert "N_KB_APP_URL" in shell
    assert "n-kb_default" in shell
    # Phase C 部署编排：两服务测试目录注入、stub Provider 身份映射、容器内验证、
    # 以及退出时的完整还原路径必须全部存在。
    assert "nkb-pricing-override.yml" in shell
    assert "cost-override.json" in shell
    assert "N_AGENT_PRICING_CATALOG_PATH" in shell
    assert "N_AGENT_MODEL_BILLING_IDENTITY_MAP" in shell
    assert "fake_llm_cost.py" in shell
    assert "cost-probe-query.txt" in shell
    assert "restore_cost_environment" in shell
    assert "--force-recreate" in shell
    assert 'COST_CATALOG_JCS_SHA256="1536ca56d137af4f903d3ca43b589e986cfddfb3395a2e0a357d537bdb40c758"' in shell
    # Ollama bge-m3 keep_alive 预载（attempt-10：冷加载叠加单核推理可超
    # N-KB 30s 嵌入超时，502；部署窗口内驻留，探针全程温热）。
    assert "keep_alive" in shell and "bge-m3" in shell
    # n-kb 还原必须显式 -f：shell 继承 environment.sh 的 COMPOSE_FILE（n-agent
    # override 链），裸 compose 会得到 "no such service: n-kb"。
    assert 'docker compose -f docker-compose.yml \\\n        up -d --no-deps --force-recreate n-kb' in shell
    # 评审修复（第三轮）：还原责任必须在首次环境改动之前登记——两服务测试
    # 计价标志先于对应 compose up 置位；否则 compose 重建容器后 start/--wait
    # 失败时函数提前返回，cleanup 跳过还原，遗留测试目录挂载、身份映射或
    # 半启动容器。
    deploy = shell[shell.index("deploy_cost_environment() {"):]
    deploy = deploy[:deploy.index("if ! deploy_cost_environment")]
    assert deploy.index("nkb_test_pricing=1") < deploy.index("up -d --build --no-deps n-kb")
    assert deploy.index("nagent_test_pricing=1") < deploy.index(
        "up -d --force-recreate --no-deps --wait n-agent")


def test_write_comparison_serializes_tuple_keyed_catalog_and_decimals(tmp_path):
    m = module()
    data = {"result": "RUNNING", "checks": [], "calls": [], "cleanup": "未执行",
            "catalog": {"prices": {("openai", "e2e-cost-model", "chat"):
                                   {"input": __import__("decimal").Decimal("2")}}}}
    m.write_comparison(tmp_path, data)
    document = json.loads((tmp_path / "comparison.json").read_text())
    assert document["catalog"]["prices"]["('openai', 'e2e-cost-model', 'chat')"]["input"] == "2"


def test_cost_probe_query_fixture_is_exactly_200_tokens():
    query = (Path(__file__).with_name("fixtures") / "cost-probe-query.txt").read_text(
        encoding="utf-8").strip()
    # bge-m3 实测："成"*N -> prompt_eval_count = N+3；N=197 => 恰 200 token（向量 C）。
    assert len(query) == 197
    assert set(query) == {"成"}
    source = Path(__file__).with_name("otel_langfuse_comparison.py").read_text()
    assert "/tests/fixtures/cost-probe-query.txt" in source
    assert "UNKNOWN_PROBE_MODEL" in source


def load_fake_llm_stub():
    source = (Path(__file__).with_name("fixtures") / "fake_llm_cost.py").read_text()
    head = source.split("ThreadingHTTPServer(")[0]
    query = Path(__file__).with_name("fixtures") / "cost-probe-query.txt"
    argv = sys.argv
    sys.argv = ["fake_llm_cost.py", "9999", str(query)]
    try:
        namespace = {"__name__": "fake_llm_cost_test"}
        exec(compile(head, "fake_llm_cost.py", "exec"), namespace)
    finally:
        sys.argv = argv
    return namespace


def test_fake_llm_stub_dispatch_is_deterministic():
    fake = load_fake_llm_stub()
    kb_tools = [{"type": "function", "function": {"name": "search_knowledge", "description":
                 "搜索知识库。Enabled knowledge bases: legacy-n-kb (Legacy N-KB, n_kb): 搜索"}}]
    calc_tools = [{"type": "function", "function": {"name": "calculator", "description": "计算器"}}]
    handler = fake["Handler"].__new__(fake["Handler"])
    handler.messages = []
    handler.tools = []
    # 工具结果在先 => 显式全零用量最终回答（显式零保留）。
    handler.messages = [{"role": "tool", "content": "{}"}]
    completion = fake["Handler"]._completion(handler, "e2e-cost-model")
    assert completion["usage"] == fake["USAGE_ZERO"]
    assert completion["choices"][0]["message"]["content"] == "kb answer"
    # calculator 工具结果 => 最终回答必须是 323（business() 输出断言），全零用量。
    handler.messages = [{"role": "tool", "name": "calculator", "tool_call_id": "call-cost-probe",
                         "content": "{\"result\": 323}"}]
    completion = fake["Handler"]._completion(handler, "e2e-cost-model")
    assert completion["usage"] == fake["USAGE_ZERO"]
    assert completion["choices"][0]["message"]["content"] == "323"
    # 知识库请求 => search_knowledge 工具调用，query 为 200-token 检索串，向量 B。
    handler.messages = [{"role": "user", "content": "请使用知识库检索成本"}]
    handler.tools = kb_tools
    completion = fake["Handler"]._completion(handler, "e2e-cost-model")
    call = completion["choices"][0]["message"]["tool_calls"][0]["function"]
    arguments = json.loads(call["arguments"])
    assert call["name"] == "search_knowledge"
    assert arguments["kb_id"] == "legacy-n-kb"
    assert arguments["query"] == "成" * 197
    assert completion["usage"] == fake["USAGE_VECTOR_B"]
    # calculator 请求 => calculator 工具调用（business() 的 execute_tool 断言兼容）。
    handler.messages = [{"role": "user",
                         "content": "请必须调用 calculator 工具计算 17*19，然后仅回答计算结果。"}]
    handler.tools = calc_tools
    completion = fake["Handler"]._completion(handler, "e2e-cost-model")
    call = completion["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "calculator"
    assert json.loads(call["arguments"]) == {"expression": "17*19"}
    assert completion["usage"] == fake["USAGE_VECTOR_B"]
    # 缺省路径 => 直接回答 323，向量 B（unknown 样例单叶）。
    handler.messages = [{"role": "user", "content": "请仅回答数字 42。"}]
    handler.tools = []
    completion = fake["Handler"]._completion(handler, "cost-unknown-model")
    assert completion["choices"][0]["message"]["content"] == "323"
    assert completion["usage"] == fake["USAGE_VECTOR_B"]
    assert completion["model"] == "cost-unknown-model"
