"""E2E 报告中的 Trace 链接必须直达对应现场。"""
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

spec = importlib.util.spec_from_file_location("result_report", Path(__file__).with_name("result_report.py"))
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_trace_link_keeps_configured_origin_subpath_and_org():
    trace_id = "60dd333cd9bcbe72451017f4bd3d639d"
    url = report.trace_url("https://grafana.example/grafana/explore?orgId=2&panes=old", trace_id)
    parsed = urlsplit(url)
    assert parsed.netloc == "grafana.example"
    assert parsed.path == "/grafana/explore"
    params = parse_qs(parsed.query)
    assert params["orgId"] == ["2"]
    pane = json.loads(params["panes"][0])["trace"]
    assert pane["datasource"] == "nagent-tempo"
    assert pane["queries"][0]["query"] == trace_id
    assert pane["queries"][0]["queryType"] == "traceql"
    assert pane["queries"][0]["datasource"]["uid"] == "nagent-tempo"


@pytest.mark.parametrize("value", ["", "bad-id", "0" * 32])
def test_invalid_trace_id_has_no_link(value):
    assert report.trace_url("http://grafana.localhost/explore", value) is None


def test_failed_case_keeps_trace_and_unstarted_case_has_no_fake_link(tmp_path):
    state = report.new_report("run-1")
    state["cases"][0].update(result="RUNNING", trace_id="a" * 32)
    report.finish(state, "http://grafana.localhost/explore", 1)
    report.write_report(tmp_path, state)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["cases"][0]["result"] == "FAIL"
    assert saved["cases"][0]["trace_url"]
    assert saved["cases"][1]["result"] == "SKIP"
    assert saved["cases"][1]["trace_url"] is None
    markdown = (tmp_path / "report.md").read_text()
    assert markdown.count("[查看Trace](") == 1
    assert "未执行" in markdown
    assert "exemplar" not in markdown


def test_management_checks_have_persistent_results_and_explicit_trace_reason(tmp_path):
    state = report.new_checks_report("run-2", ["artifacts"])
    state["cases"][0].update(result="PASS")
    report.finish(state, "", 0)
    report.write_report(tmp_path, state)
    markdown = (tmp_path / "report.md").read_text()
    assert "Artifact" in markdown and "PASS" in markdown
    assert "| 分类 | 用例 | 结果 | TraceID | 链接 |" in markdown
    assert "| 制品 |" in markdown
    assert "管理接口" in markdown
    assert "查看Trace" not in markdown


def test_aggregate_flattens_all_categories_and_retains_trace(tmp_path):
    otel = report.new_report("otel-1")
    otel["cases"][0].update(result="FAIL", trace_id="a" * 32)
    report.finish(otel, "http://grafana.localhost/explore", 1)
    report.write_report(tmp_path / "otel-1", otel)
    artifacts = report.new_checks_report("art-1", ["artifacts"])
    report.write_report(tmp_path / "art-1", artifacts)
    conversations = {"run_id": "conv-1", "cases": [{"id": "c1", "verdict": "PASS",
        "messages": [{"id": "m1", "verdict": "PASS", "trace_reason": "OTel 未启用"}]}]}
    (tmp_path / "conv-1").mkdir()
    (tmp_path / "conv-1/report.json").write_text(json.dumps(conversations))
    merged = report.aggregate_report("all-1", [tmp_path / name for name in ("conv-1", "art-1", "otel-1")], 1)
    report.write_report(tmp_path / "all-1", merged)
    markdown = (tmp_path / "all-1/report.md").read_text()
    assert markdown.count("| 分类 | 用例 | 结果 | TraceID | 链接 |") == 1
    assert all(f"| {category} |" in markdown for category in ("对话", "制品", "OTel"))
    assert "a" * 32 in markdown and "[查看Trace](" in markdown
    assert len(merged["cases"]) == 5


def test_e2e_runner_excludes_config_bundle_and_aggregates_reports():
    source = Path(__file__).with_name("run.sh").read_text()
    assert "config-bundle" not in source
    assert ' aggregate ' in source
    migration = Path(__file__).parents[1] / "migrate/config-bundle.sh"
    assert migration.is_file()


@pytest.mark.parametrize("failed_suite", ["", "conversations", "missing"])
def test_runner_aggregates_only_current_children_and_keeps_failure(tmp_path, failed_suite):
    import os
    import shutil
    import subprocess
    repo = tmp_path / "repo with spaces"
    scripts = repo / "tests/e2e"
    scripts.mkdir(parents=True)
    for name in ("run.sh", "result_report.py", "trace_report.py", "tempo_query.py"):
        shutil.copy2(Path(__file__).with_name(name), scripts)
    (scripts / "environment.sh").write_text('e2e_environment() { :; }\n')
    docker = repo / "docker"
    docker.mkdir()
    restart = docker / "restart.sh"
    restart.write_text('#!/bin/sh\nexit 0\n')
    restart.chmod(0o755)
    (scripts / "artifacts.sh").write_text('#!/bin/sh\nexit 0\n')
    for name, state in (("conversations", {"run_id": "conv", "cases": [{"id": "c1", "verdict": "PASS", "messages": [{"id": "m1", "verdict": "PASS"}]}]}),
                        ("otel_smoke", report.new_report("otel"))):
        child = repo / "locals/e2e-reports" / name
        child.mkdir(parents=True)
        (child / "report.json").write_text(json.dumps(state))
        if failed_suite == "missing" and name == "conversations":
            (scripts / f"{name}.sh").write_text("#!/bin/sh\nexit 0\n")
            continue
        (scripts / f"{name}.sh").write_text(
            f'#!/bin/sh\necho "REPORT_DIR /app/locals/e2e-reports/{name}"\nexit {7 if failed_suite == name else 0}\n')
    for name in ("artifacts", "conversations", "otel_smoke"):
        (scripts / f"{name}.sh").chmod(0o755)
    completed = subprocess.run(["bash", str(scripts / "run.sh")], text=True, capture_output=True, env=os.environ)
    assert completed.returncode == (1 if failed_suite == "missing" else 7 if failed_suite else 0), completed.stdout + completed.stderr
    summaries = list((repo / "locals/e2e-reports").glob("e2e-*/report.json"))
    assert len(summaries) == 1
    merged = json.loads(summaries[0].read_text())
    assert {c["category"] for c in merged["cases"]} == ({"对话", "制品"} if failed_suite else {"对话", "制品", "OTel"})
    assert merged["exit_code"] == completed.returncode
    if failed_suite:
        assert any(case["result"] == "FAIL" for case in merged["cases"])


def test_cli_writes_result_file_and_prints_only_summary_and_location(tmp_path):
    import subprocess
    tool = str(Path(__file__).with_name("result_report.py"))
    subprocess.run(["python3", tool, str(tmp_path), "init", "run-1"], check=True)
    completed = subprocess.run(["python3", tool, str(tmp_path), "finish", "", "0"],
                               check=True, text=True, capture_output=True)
    assert (tmp_path / "result.md").read_text() == (tmp_path / "report.md").read_text()
    assert "Summary（结果行）: PASS 0 | FAIL 0 | SKIP 3" in completed.stdout
    assert f"RESULT_FILE {tmp_path / 'result.md'}" in completed.stdout
    assert "| 分类 |" not in completed.stdout
