"""成本面板契约：实测名称、服务筛选窗口、零分母、零价/全未知与 total/components 分离。

名称依据 docker/observability/metric-name-mapping.md 与 nkb-metric-name-mapping.md
的 2026-11-09 实测登记（USD 单位产生字面 `_USD` 后缀），禁止凭规则猜测其它名称。
"""
import json
from pathlib import Path

DASHBOARDS = Path(__file__).resolve().parents[1] / "docker/observability/grafana/provisioning/dashboards"

# 两仓成本 Counter 的实测 Prometheus 名称与翻译后维度 label（. -> _）。
COST_CONTRACT = {
    "nagent-metrics": {
        "total": "nagent_model_cost_USD_total",
        "components": "nagent_model_cost_components_USD_total",
        "attempts": "nagent_model_cost_attempts_total",
        "status": "nagent_model_cost_status",
        "reason": "nagent_model_cost_reason",
        "component": "nagent_model_cost_component",
        "billing_provider": "nagent_model_billing_provider",
        "billing_model": "nagent_model_billing_model",
        # 禁止出现的规则猜测名称（缺 _USD 后缀）
        "forbidden": ["nagent_model_cost_total", "nagent_model_cost_components_total"],
    },
    "nkb-overview": {
        "total": "nkb_model_cost_USD_total",
        "components": "nkb_model_cost_components_USD_total",
        "attempts": "nkb_model_cost_attempts_total",
        "status": "nkb_model_cost_status",
        "reason": "nkb_model_cost_reason",
        "component": "nkb_model_cost_component",
        "billing_provider": "nkb_model_billing_provider",
        "billing_model": "nkb_model_billing_model",
        "forbidden": ["nkb_model_cost_total", "nkb_model_cost_components_total"],
    },
}


def load_dashboard(name):
    return json.loads((DASHBOARDS / f"{name}.json").read_text())


def cost_targets(dashboard, contract):
    """返回引用任一成本 Counter 的 (panel, target, expr) 三元组列表。"""
    markers = (contract["total"], contract["components"], contract["attempts"])
    result = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if any(marker in expr for marker in markers):
                result.append((panel, target, expr))
    return result


def test_cost_dashboards_are_valid_json_with_cost_panels():
    for name in COST_CONTRACT:
        dashboard = load_dashboard(name)
        contract = COST_CONTRACT[name]
        targets = cost_targets(dashboard, contract)
        assert targets, f"{name} 缺少成本面板"
        # 实测名称必须出现（含 _USD 后缀）
        joined = "\n".join(expr for _, _, expr in targets)
        assert contract["total"] in joined
        assert contract["components"] in joined
        assert contract["attempts"] in joined
        # 规则猜测名称禁止出现
        all_queries = "\n".join(
            t.get("expr", "") for p in dashboard["panels"] for t in p.get("targets", [])
        )
        for guessed in contract["forbidden"]:
            assert guessed not in all_queries, f"{name} 出现未实测名称 {guessed}"


def test_cost_queries_carry_service_filter_and_window():
    for name in COST_CONTRACT:
        dashboard = load_dashboard(name)
        contract = COST_CONTRACT[name]
        for panel, target, expr in cost_targets(dashboard, contract):
            assert 'service_name=~"$service"' in expr, (name, panel["title"], expr)
            assert "$__rate_interval" in expr or "[5m]" in expr, (
                name,
                panel["title"],
                expr,
            )


def test_amount_panels_guarded_by_estimated_attempts_show_zero_price():
    """金额查询以同窗口 estimated attempts > 0 作显示守卫。

    全未知或无调用 -> 守卫为空 -> 无数据；真实零价 estimated -> 序列存在
    且值为 0，守卫放行 -> 显示 0。守卫不得把成本值本身按 > 0 过滤。
    """
    for name in COST_CONTRACT:
        dashboard = load_dashboard(name)
        contract = COST_CONTRACT[name]
        amount_exprs = [
            expr
            for _, _, expr in cost_targets(dashboard, contract)
            if contract["total"] in expr
        ]
        assert amount_exprs, f"{name} 缺少已知成本金额查询"
        for expr in amount_exprs:
            assert "and on()" in expr, expr
            assert f'{contract["status"]}="estimated"' in expr, expr
            assert "> 0)" in expr, expr
            # 零价守卫：不能对成本金额本身做 > 0 过滤或 clamp 抹平零值
            assert "clamp_min" not in expr, expr
        amount_panel_titles = [
            panel["title"]
            for panel in dashboard["panels"]
            if any(contract["total"] in t.get("expr", "") for t in panel.get("targets", []))
        ]
        assert any("已知成本" in title for title in amount_panel_titles), amount_panel_titles
        for panel in dashboard["panels"]:
            exprs = [t.get("expr", "") for t in panel.get("targets", [])]
            if any(contract["total"] in expr for expr in exprs):
                assert "API 估算" in panel["title"] or "API 估算" in panel.get("description", ""), (
                    name,
                    panel["title"],
                )
        total_panel = next(
            panel
            for panel in dashboard["panels"]
            if panel.get("targets")
            and contract["total"] in panel["targets"][0].get("expr", "")
        )
        assert "不作为精确账单" in total_panel.get("description", ""), name


def test_components_ratio_is_queried_separately_from_total():
    """分项加总与 total 分开查询，禁止把两个 Counter 相加。"""
    for name in COST_CONTRACT:
        dashboard = load_dashboard(name)
        contract = COST_CONTRACT[name]
        for panel, target, expr in cost_targets(dashboard, contract):
            has_total = contract["total"] in expr
            has_components = contract["components"] in expr
            assert not (has_total and has_components), (name, panel["title"], expr)
        ratio_exprs = [
            expr
            for _, _, expr in cost_targets(dashboard, contract)
            if contract["components"] in expr and contract["component"] in expr
        ]
        assert ratio_exprs, f"{name} 缺少分项占比查询"
        for expr in ratio_exprs:
            # 占比在 cost_components Counter 内部计算（分项 / 分项合计），不引用 total
            assert contract["total"] not in expr, expr
            assert f"by ({contract['component']})" in expr, expr
            assert "/ on() group_left() (sum(" in expr, expr
            # 分项合计为 0（真实零价）时占比无意义，显示无数据而非 NaN
            assert expr.count("> 0)") >= 2, expr


def test_coverage_panel_zero_denominator_no_data_and_missing_numerator_zero():
    """计价覆盖率 = estimated attempts / 全部 cost_attempts。

    相同筛选先 rate 再 sum；分母为零显示无数据（不补 100%）；
    分子缺序列而分母>0 时经 `or 0 *` 兜底显示 0%（禁止 or vector(0)）。
    """
    for name in COST_CONTRACT:
        dashboard = load_dashboard(name)
        contract = COST_CONTRACT[name]
        coverage_exprs = [
            expr
            for _, _, expr in cost_targets(dashboard, contract)
            if " / " in expr and contract["attempts"] in expr and contract["total"] not in expr and contract["components"] not in expr
        ]
        assert coverage_exprs, f"{name} 缺少计价覆盖率查询"
        for expr in coverage_exprs:
            assert f'{contract["status"]}="estimated"' in expr, expr
            # 分母为零 -> (sum > 0) 为空 -> 无数据
            assert "/ (sum(rate(" in expr and expr.rstrip().endswith("> 0)"), expr
            # 分子缺序列而分母>0 -> 显示 0
            assert "or 0 * sum(rate(" in expr, expr
            assert "or vector(0)" not in expr, expr
            assert "or 1" not in expr and "clamp_min" not in expr, expr
            # 分子分母同窗口
            assert expr.count("$__rate_interval") >= 3, expr


def test_billing_identity_panel_groups_by_provider_and_model():
    for name in COST_CONTRACT:
        dashboard = load_dashboard(name)
        contract = COST_CONTRACT[name]
        identity_exprs = [
            expr
            for _, _, expr in cost_targets(dashboard, contract)
            if contract["billing_provider"] in expr or contract["billing_model"] in expr
        ]
        assert identity_exprs, f"{name} 缺少按计费身份分组的成本查询"
        for expr in identity_exprs:
            by_clauses = [seg for seg in expr.split("by (")[1:]]
            assert any(
                contract["billing_provider"] in seg
                and contract["billing_model"] in seg
                for seg in by_clauses
            ), expr


def test_unknown_reason_panel_counts_unknown_attempts_by_reason():
    for name in COST_CONTRACT:
        dashboard = load_dashboard(name)
        contract = COST_CONTRACT[name]
        unknown_exprs = [
            expr
            for _, _, expr in cost_targets(dashboard, contract)
            if f'{contract["status"]}="unknown"' in expr
        ]
        assert unknown_exprs, f"{name} 缺少未知计价原因面板"
        for expr in unknown_exprs:
            assert contract["total"] not in expr, expr
        assert any(f"by ({contract['reason']})" in expr for expr in unknown_exprs), unknown_exprs


def test_nkb_overview_lock_constraints_for_cost_panels():
    """满足 n-kb 仓锁定测试对 nkb-overview 的既有约束。"""
    dashboard = load_dashboard("nkb-overview")
    all_queries = "\n".join(
        t.get("expr", "") for p in dashboard["panels"] for t in p.get("targets", [])
    )
    assert "or vector(0)" not in all_queries
    for base in ("nkb_model_cost", "nkb_model_cost_components", "nkb_model_cost_attempts"):
        assert base in all_queries
    # 非吞吐面板必须为 timeseries（锁定测试契约）
    for panel in dashboard["panels"]:
        if "吞吐" not in panel["title"]:
            assert panel["type"] == "timeseries", panel["title"]
    variables = dashboard["templating"]["list"]
    assert len(variables) == 1 and variables[0]["name"] == "service"
