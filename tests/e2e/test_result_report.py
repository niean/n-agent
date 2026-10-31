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
