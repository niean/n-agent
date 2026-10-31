"""Source filters and exemplar navigation must survive provisioning updates."""
import json
from pathlib import Path

import pytest


DASHBOARDS = Path(__file__).resolve().parents[1] / "docker/observability/grafana/provisioning/dashboards"
MATCHER = 'nagent_source=~"${source:regex}"'


@pytest.fixture(params=["nagent-overview", "nagent-metrics"])
def dashboard(request):
    return json.loads((DASHBOARDS / f"{request.param}.json").read_text())


def test_source_variable_keeps_historical_unlabelled_series_in_all(dashboard):
    source = next(v for v in dashboard["templating"]["list"] if v["name"] == "source")
    assert source["multi"] and source["includeAll"]
    assert source["allValue"] == ".*"
    assert source["current"]["value"] == "$__all"
    assert 'nagent_turns_total{service_name=~"$service"}' in source["query"]["query"]
    assert "nagent_source" in source["query"]["query"]
    assert source["refresh"] == 2


def test_filters_follow_metric_capabilities_and_agent_duration_has_exemplars(dashboard):
    durations = []
    rates = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if "gen_ai_invoke_agent_" in expr or "nagent_turns_total" in expr:
                assert MATCHER in expr, (panel["title"], expr)
            elif "gen_ai_client_" in expr or "gen_ai_execute_tool_" in expr:
                assert MATCHER not in expr, (panel["title"], expr)
                assert "服务级" in panel["description"]
            if "histogram_quantile" in expr and "gen_ai_invoke_agent_duration" in expr:
                durations.append(target)
                assert "le, nagent_source" in expr
                assert "{{nagent_source}}" in target["legendFormat"]
                assert target["exemplar"] and target["range"] and not target["instant"]
                link = panel["links"][0]["url"]
                assert link.startswith("?viewPanel=")  # retains Grafana reverse-proxy subpath
                assert "${source:queryparam}" in link
                panel_id = int(link.split("viewPanel=", 1)[1].split("&", 1)[0])
                raw_panel = next(p for p in dashboard["panels"] if p["id"] == panel_id)
                assert raw_panel["targets"][0]["expr"].startswith("gen_ai_invoke_agent_duration_seconds_bucket{")
                assert raw_panel["targets"][0]["exemplar"]
            if "nagent_turns_total" in expr and "sum by (nagent_source)" in expr:
                rates.append(target)
    assert durations and rates


def test_average_calls_uses_observations_sum_and_run_count(dashboard):
    averages = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if " / " in expr and "gen_ai_invoke_agent_" in expr:
                averages.append(expr)
                numerator, denominator = expr.split(" / ", 1)
                assert "_calls_sum{" in numerator
                assert "_calls_count{" in denominator
                assert "sum by (nagent_source)" in numerator
                assert "sum by (nagent_source)" in denominator
                assert "clamp_min" in denominator
    assert len(averages) == 2


def test_source_panels_have_explicit_historical_display_and_unique_positions(dashboard):
    panels = dashboard["panels"]
    assert len({p["id"] for p in panels}) == len(panels)
    for i, panel in enumerate(panels):
        if any("{{nagent_source}}" in t.get("legendFormat", "") for t in panel.get("targets", [])):
            assert any(prop.get("value", "").startswith("历史未知") for override in panel["fieldConfig"]["overrides"]
                       for prop in override["properties"])
            for target in panel["targets"]:
                if "{{nagent_source}}" not in target.get("legendFormat", ""):
                    continue
                suffix = target["legendFormat"].split("{{nagent_source}}", 1)[1]
                if suffix and "{{" not in suffix:
                    assert any(prop.get("value") == "历史未知" + suffix
                               for override in panel["fieldConfig"]["overrides"]
                               for prop in override["properties"])
        a = panel["gridPos"]
        for other in panels[i + 1:]:
            b = other["gridPos"]
            assert (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                    or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])
