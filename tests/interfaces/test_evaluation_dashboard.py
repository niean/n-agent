"""评估 Dashboard 前端静态契约测试（T21）。

只做代码级契约（路由注册/容器/脚本/安全渲染/时间规范/反馈入口/导出
闭环/概览聚合），不做浏览器视觉验收（人工完成）。规范来源：
.harness/knowledge/03-conventions.md "Dashboard 前端"。

多入口模式：评估页必须用独立 routeConfig entry 与独立左导/顶导菜单，
禁止复用观测其他入口的路由。
"""

from __future__ import annotations

import re

from app.interfaces.http.dashboard import STATIC_DIR


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _code_lines(name: str) -> str:
    """去注释后的代码文本（整行 // 注释剔除），避免把注释里的禁用词
    （如"禁 innerHTML"）误判为代码使用。"""
    return re.sub(r"^\s*//.*$", "", _read(name), flags=re.M)


def _strip_section_comments(section: str) -> str:
    return re.sub(r"^\s*//.*$", "", section, flags=re.M)


# ---------------------------------------------------------------------------
# 路由与导航（独立 entry + 独立菜单归属）
# ---------------------------------------------------------------------------


class TestNavigationRegistration:
    def test_route_config_has_evaluations_entry(self) -> None:
        content = _read("management-navigation.js")
        assert "/evaluations" in content

    def test_route_entries_have_independent_sidebar_and_shared_renderer(self) -> None:
        content = _read("management-navigation.js")
        for view, path in (("overview", "/evaluations"), ("datasets", "/evaluations/datasets"),
                           ("evaluators", "/evaluations/evaluators"),
                           ("experiments", "/evaluations/experiments"),
                           ("results", "/evaluations/results"),
                           ("annotations", "/evaluations/annotations"),
                           ("review", "/evaluations/review")):
            match = re.search(r"\{ paths: \['" + re.escape(path) + r"'\][^}]*}", content)
            assert match is not None
            entry = match.group(0)
            assert "tab: 'evaluations-" + view + "'" in entry
            assert "renderTab: 'evaluations'" in entry
            assert "sidebarTab: 'evaluations-" + view + "'" in entry
            assert "topnavParent: 'evaluations-" + view + "'" in entry
            assert "scope: '" + view + "'" in entry

    def test_executions_route_shares_experiments_subdomain(self) -> None:
        """执行任务是实验子域的独立 routeConfig entry：独立 path/tab/scope，
        但 sidebarTab/topnavParent 归属 experiments，与能力目录同范式。"""
        content = _read("management-navigation.js")
        match = re.search(
            r"\{ paths: \['/evaluations/executions'\][^}]*}", content
        )
        assert match is not None
        entry = match.group(0)
        assert "tab: 'evaluations-executions'" in entry
        assert "renderTab: 'evaluations'" in entry
        assert "sidebarTab: 'evaluations-experiments'" in entry
        assert "topnavParent: 'evaluations-experiments'" in entry
        assert "scope: 'executions'" in entry

    def test_evaluations_is_independent_parent(self) -> None:
        content = _read("management-navigation.js")
        observations = re.search(r"tab: 'observations'.*?children: \[([^\]]*)\]", content)
        assert "evaluations" not in observations.group(1)
        parent = re.search(r"tab: 'evaluations', label: '评估', parent: true, children: \[([^\]]*)\]", content)
        assert parent is not None
        for view in ("overview", "datasets", "evaluators", "experiments", "results", "annotations"):
            assert "evaluations-" + view in parent.group(1)
        # review 兼容页保留 routeConfig，但不再是左导 children
        assert "evaluations-review" not in parent.group(1)

    def test_topnav_has_evaluations_entry(self) -> None:
        content = _read("management-navigation.js")
        topnav_m = re.search(r"const topnavConfig = \{(.*?)\n  \};", content, re.S)
        assert topnav_m is not None, "缺少 topnavConfig"
        assert "evaluations" in topnav_m.group(1)

    def test_topnav_annotations_single_element_scope(self) -> None:
        """人工标注页顶导：单元素数组、concern 'evaluation'、scope 各自归属。"""
        content = _read("management-navigation.js")
        m = re.search(r"'evaluations-annotations': \[(.*?)\n    \]", content, re.S)
        assert m is not None, "topnavConfig 缺少 evaluations-annotations"
        block = m.group(1)
        assert block.count("{ tab:") == 1, "evaluations-annotations 顶导必须是单元素数组"
        assert "concern: 'evaluation'" in block
        assert "scope: 'annotations'" in block
        assert "topnavParent: 'evaluations-annotations'" in block

    def test_topnav_evaluators_has_three_entries(self) -> None:
        """评估器左导子域顶导三入口：能力目录 + 评估器 + 模型，顺序固定，
        共享 topnavParent，各自独立 path/scope（同 /tasks/observations 范式）。"""
        content = _read("management-navigation.js")
        m = re.search(r"'evaluations-evaluators': \[(.*?)\n    \]", content, re.S)
        assert m is not None, "topnavConfig 缺少 evaluations-evaluators"
        block = m.group(1)
        assert block.count("{ tab:") == 3, "评估器子域顶导必须是三元素数组"
        cap_m = re.search(
            r"\{ tab: 'evaluations-capabilities', path: '/evaluations/capabilities', "
            r"label: '能力目录', concern: 'evaluation', scope: 'capabilities', "
            r"topnavParent: 'evaluations-evaluators' \}",
            block,
        )
        eva_m = re.search(
            r"\{ tab: 'evaluations-evaluators', path: '/evaluations/evaluators', "
            r"label: '评估器', concern: 'evaluation', scope: 'evaluators', "
            r"topnavParent: 'evaluations-evaluators' \}",
            block,
        )
        jm_m = re.search(
            r"\{ tab: 'evaluations-judge-model', path: '/evaluations/judge-model', "
            r"label: '模型', concern: 'evaluation', scope: 'judge-model', "
            r"topnavParent: 'evaluations-evaluators' \}",
            block,
        )
        assert cap_m and eva_m and jm_m, "顶导三入口条目字段不符"
        assert cap_m.start() < eva_m.start() < jm_m.start(), \
            "能力目录必须排在评估器之前，模型排在评估器之后"

    def test_capabilities_route_shares_evaluators_sidebar_subdomain(self) -> None:
        """能力目录独立 routeConfig entry：独立 path/tab/scope，
        但 sidebarTab/topnavParent 与评估器页相同（顶导是左导的子域）。"""
        content = _read("management-navigation.js")
        match = re.search(
            r"\{ paths: \['/evaluations/capabilities'\][^}]*}", content
        )
        assert match is not None
        entry = match.group(0)
        assert "tab: 'evaluations-capabilities'" in entry
        assert "renderTab: 'evaluations'" in entry
        assert "sidebarTab: 'evaluations-evaluators'" in entry
        assert "topnavParent: 'evaluations-evaluators'" in entry
        assert "scope: 'capabilities'" in entry
        # 能力目录不进左导 children（与 review 兼容页同范式）
        parent = re.search(
            r"tab: 'evaluations', label: '评估', parent: true, children: \[([^\]]*)\]",
            content,
        )
        assert parent is not None
        assert "evaluations-capabilities" not in parent.group(1)

    def test_judge_model_route_shares_evaluators_sidebar_subdomain(self) -> None:
        """模型页独立 routeConfig entry：独立 path/tab/scope，但
        sidebarTab/topnavParent 与评估器页相同（顶导是左导的子域）。"""
        content = _read("management-navigation.js")
        match = re.search(
            r"\{ paths: \['/evaluations/judge-model'\][^}]*}", content
        )
        assert match is not None
        entry = match.group(0)
        assert "tab: 'evaluations-judge-model'" in entry
        assert "renderTab: 'evaluations'" in entry
        assert "sidebarTab: 'evaluations-evaluators'" in entry
        assert "topnavParent: 'evaluations-evaluators'" in entry
        assert "scope: 'judge-model'" in entry
        # 模型页不进左导 children（与能力目录同范式）
        parent = re.search(
            r"tab: 'evaluations', label: '评估', parent: true, children: \[([^\]]*)\]",
            content,
        )
        assert parent is not None
        assert "evaluations-judge-model" not in parent.group(1)

    def test_sidebar_landing_uses_first_topnav_item(self) -> None:
        """左导落地规范（无例外）：navigateTo 经 sidebarLandingPath 收敛到
        子域顶导首项 path，评估器子域首项为能力目录；无顶导 tab 回落
        pathByTab。禁止例外映射机制（SIDEBAR_LANDING_PATH_OVERRIDES 已删除）。"""
        content = _read("management-navigation.js")
        assert "function sidebarLandingPath(name)" in content
        assert "SIDEBAR_LANDING_PATH_OVERRIDES" not in content, (
            "左导落地规则为无例外 FE 规范，禁止引入落点特判/例外映射"
        )
        landing_m = re.search(
            r"function sidebarLandingPath\(name\) \{(.*?)\n  \}", content, re.S
        )
        assert landing_m is not None
        block = landing_m.group(1)
        assert "topnavConfig[name]" in block
        assert "items[0].path" in block
        assert "pathByTab[name]" in block
        navigate_m = re.search(r"function navigateTo\(name\) \{(.*?)\n  \}", content, re.S)
        assert navigate_m is not None
        assert "sidebarLandingPath(name)" in navigate_m.group(1)
        assert "pathByTab[name]" not in navigate_m.group(1)

    def test_app_js_resolve_module_maps_evaluations(self) -> None:
        content = _read("app.js")
        assert "evaluations" in content


class TestIndexHtml:
    def test_tab_container_declared_once(self) -> None:
        content = _read("index.html")
        assert content.count('id="tab-evaluations"') == 1

    def test_evaluation_script_included_once(self) -> None:
        content = _read("index.html")
        assert content.count("evaluation.js") == 1

    def test_sidebar_has_evaluation_children(self) -> None:
        content = _read("index.html")
        assert 'data-tab-group="evaluations"' in content
        assert 'data-submenu-of="evaluations"' in content
        for view in ("overview", "datasets", "evaluators", "experiments", "results", "annotations"):
            assert content.count('data-tab="evaluations-' + view + '"') == 1

    def test_sidebar_evaluators_lands_on_capabilities(self) -> None:
        """左导"评估器"href 与 SPA 落地规则一致：指向顶导首项能力目录，
        data-tab 保持 evaluations-evaluators（侧栏高亮仍归评估器子域）。"""
        content = _read("index.html")
        item_m = re.search(
            r'<a class="sidebar__item sidebar__item--child"[^>]*data-tab="evaluations-evaluators"[^>]*>.*?</a>',
            content, re.S,
        )
        assert item_m is not None
        assert 'href="/evaluations/capabilities"' in item_m.group(0)
        assert '/evaluations/evaluators"' not in item_m.group(0)

    def test_sidebar_experiments_lands_on_executions(self) -> None:
        """左导"实验"href 与 SPA 落地规则一致：指向顶导首项执行任务，
        data-tab 保持 evaluations-experiments（侧栏高亮仍归实验子域）。"""
        content = _read("index.html")
        item_m = re.search(
            r'<a class="sidebar__item sidebar__item--child"[^>]*data-tab="evaluations-experiments"[^>]*>.*?</a>',
            content, re.S,
        )
        assert item_m is not None
        assert 'href="/evaluations/executions"' in item_m.group(0)
        assert '/evaluations/experiments"' not in item_m.group(0)

    def test_sidebar_evaluation_children_order_and_review_removed(self) -> None:
        """六子链接顺序 = 概览、数据集、评估器、实验、结果、人工标注；
        旧"待复核"链接从左导移除（routeConfig 兼容保留）。"""
        content = _read("index.html")
        submenu_m = re.search(
            r'<div class="sidebar__submenu" data-submenu-of="evaluations">(.*?)</div>',
            content, re.S,
        )
        assert submenu_m is not None, "评估左导子菜单不存在"
        submenu = submenu_m.group(1)
        tabs = re.findall(r'data-tab="(evaluations-[^"]*)"', submenu)
        assert tabs == [
            "evaluations-overview", "evaluations-datasets", "evaluations-evaluators",
            "evaluations-experiments", "evaluations-results", "evaluations-annotations",
        ], f"评估左导子链接顺序不符: {tabs}"
        labels = re.findall(r'sidebar__item-label">([^<]*)</span>', submenu)
        assert labels == ["概览", "数据集", "评估器", "实验评估", "线上评估", "人工标注"], labels
        assert 'data-tab="evaluations-review"' not in content
        assert 'href="/evaluations/review"' not in content


# ---------------------------------------------------------------------------
# shell 路由（六菜单 + 执行任务/模型独立入口 + review 兼容页 = 十路径）
# ---------------------------------------------------------------------------


class _StubToolExecutor:
    async def execute(self, request):
        from app.domain.tool import ToolResult, ToolCallRequest, ToolResultStatus
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


class _StubProvider:
    async def list_models(self):
        from app.domain.provider import ModelInfo
        return [ModelInfo("real-1", "Real 1", "openai-compatible", True, True)]

    async def supports_tools(self, model):
        return True

    async def chat(self, *args, **kwargs):
        raise NotImplementedError


def _shell_client(tmp_path):
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient
    from app.application.model_service import ModelService
    from app.application.session_service import SessionService
    from app.application.tool_service import ToolService, builtin_tool_definitions
    from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
    from app.interfaces.http.dashboard import create_dashboard_router

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_dashboard_router(
        SessionService(store),
        ToolService(_StubToolExecutor(), builtin_tool_definitions()),
        ModelService(_StubProvider(), "real-1"),
        lambda: {
            "provider": {"status": "ok"},
            "memory": {"status": "ok"},
            "knowledge": {"status": "disabled", "enabled": False},
        },
    ))
    return TestClient(app)


class TestShellRoutes:
    EVALUATION_SHELL_PATHS = (
        "/evaluations", "/evaluations/datasets", "/evaluations/evaluators",
        "/evaluations/capabilities", "/evaluations/judge-model",
        "/evaluations/executions", "/evaluations/experiments", "/evaluations/results",
        "/evaluations/session-scores", "/evaluations/run-scores",
        "/evaluations/annotations", "/evaluations/review",
    )

    def test_shell_200_for_twelve_evaluation_paths(self, tmp_path) -> None:
        client = _shell_client(tmp_path)
        for path in self.EVALUATION_SHELL_PATHS:
            response = client.get(path)
            assert response.status_code == 200, f"missing shell at {path}"
            assert 'id="tab-evaluations"' in response.text, f"shell incomplete at {path}"


# ---------------------------------------------------------------------------
# evaluation.js 安全渲染与规范
# ---------------------------------------------------------------------------


class TestEvaluationJsContract:
    def test_no_inner_html(self) -> None:
        content = _code_lines("evaluation.js")
        assert "innerHTML" not in content
        assert "outerHTML" not in content
        assert "document.write" not in content

    def test_uses_text_content(self) -> None:
        content = _read("evaluation.js")
        assert "textContent" in content

    def test_no_native_alert_confirm(self) -> None:
        content = _read("evaluation.js")
        assert not re.search(r"(?<![.\w])alert\s*\(", content)
        assert not re.search(r"(?<![.\w])confirm\s*\(", content)

    def test_uses_module_modal(self) -> None:
        content = _read("evaluation.js")
        assert "NAGENT.modal" in content

    def test_modal_frame_uses_project_standard_classes(self) -> None:
        """M2：弹窗帧用项目标准类（modal-backdrop/modal-dialog/
        providers-form/modal-header/modal-close），不用自制帧。"""
        content = _code_lines("evaluation.js")
        for cls in ("modal-backdrop", "modal-dialog", "providers-form",
                    "modal-header", "modal-close"):
            assert cls in content, f"缺少标准 modal 类: {cls}"
        # 自制帧类不得出现（对照 management-ui.js / artifacts.js 基准）
        assert "className = 'modal'" not in content
        assert "'modal-title'" not in content
        assert "'modal-actions'" not in content

    def test_styles_css_covers_evaluation_classes(self) -> None:
        """M2：evaluation.js 与 chat.js 反馈区用到的自定义类必须在
        styles.css 定义。"""
        css = _read("styles.css")
        for cls in (
            "evaluations-section-title",
            "evaluations-subtitle",
            "evaluations-note",
            "evaluations-hint",
            "evaluations-review__tabs",
            "evaluations-review__list",
            "evaluations-export-cases",
            "evaluations-export-case",
            "run-detail__feedback",
            "run-detail__feedback-actions",
            "run-detail__feedback-status",
            # spec-261112 FE 规范对齐：评测作用域布局类
            "evaluations-panel",
            "evaluations-panel__heading",
            "evaluations-filter",
            "evaluations-filter__item",
            "evaluations-filter__label",
            "evaluations-filter__help",
            "evaluations-filter-group",
            "evaluations-pager",
            "evaluations-table-scroll",
            "evaluations-delivery-counts",
            "evaluations-result-detail",
        ):
            assert cls in css, f"styles.css 缺少类: {cls}"
        # Phase 5 扫描修复（Warning）：详情/表单弹窗按钮容器统一标准类
        # providers-form__actions，自定义 evaluations-form-actions 已删除
        assert "providers-form__actions" in css
        assert "evaluations-form-actions" not in css

    def test_result_detail_box_contract(self) -> None:
        """spec-261123：评分详情"结果"文字框与原因标签映射。"""
        js = _code_lines("evaluation.js")
        assert "buildResultDetailText" in js
        assert "evaluations-result-detail" in js
        for label in ("缺少参考答案", "指标不适用", "参考相关性标注中无相关项",
                      "缺少可核实证据", "证据已脱敏", "证据超出大小上限",
                      "评估预算耗尽", "一致性检查未通过",
                      "网络错误", "作业领取次数", "评估 Trace", "无门禁判定"):
            assert label in js, f"缺少标签: {label}"
        css = _read("styles.css")
        rule = re.search(r"\.evaluations-result-detail\s*\{[^}]*\}", css)
        assert rule, "styles.css 缺少 .evaluations-result-detail 规则"
        # P094 同类约束：字号/换行/滚动声明必须在规则块内显式声明
        for declaration in (
            r"font-size\s*:\s*var\(\s*--font-size-sm\s*,\s*12px\s*\)",
            r"white-space\s*:\s*pre-wrap", r"overflow-wrap\s*:\s*anywhere",
            r"max-height\s*:\s*320px", r"overflow-y\s*:\s*auto",
        ):
            assert re.search(declaration + r"\s*(?:;|})", rule.group(0)), declaration

    def test_review_pressed_group_kept_annotation_filters_use_select(self) -> None:
        """spec-261112：筛选组是查询过滤器，用 role=group，禁止不完整的
        tablist 语义。人工标注两组筛选已改为原生 select 下拉同一筛选行，
        aria-pressed 按钮组仅保留在 review 待复核视图。"""
        js = _code_lines("evaluation.js")
        # review 保留 focusableChoiceGroup + aria-pressed
        assert "aria-pressed" in js
        assert "role', 'group'" in js
        assert "tablist" not in js
        # 人工标注走 selectControl 下拉，不再出现按钮组/checkbox 组
        assert "focusableChoiceGroup('annotation-state'" not in js
        assert "focusableChoiceGroup('annotation-reason'" not in js
        assert "checkboxGroup(" not in js
        assert "selectControl('annotation_state'" in js
        assert "selectControl('annotation_reason'" in js

    def test_no_native_title_tooltip(self) -> None:
        """spec-261112：标题补充说明统一走 panel-title-group 三件套，
        禁止 title 属性异构提示（含行内注释）。"""
        js = _code_lines("evaluation.js")
        assert "setAttribute('title'" not in js
        assert ".title =" not in js

    def test_uses_shared_loading_helper_with_fallback(self) -> None:
        """spec-261112：Loading 优先共享 ui.renderLoading，无 ui 环境
        降级 muted loading-state，两条路径都必须存在。"""
        js = _code_lines("evaluation.js")
        assert "ui.renderLoading" in js
        assert "muted loading-state" in js

    def test_pager_class_in_js_and_css(self) -> None:
        """spec-261112 S9：evaluations-pager 同时存在于 JS 与 CSS。"""
        assert "evaluations-pager" in _code_lines("evaluation.js")
        assert "evaluations-pager" in _read("styles.css")

    def test_legacy_review_tabs_css_contract_kept(self) -> None:
        """spec-261112 S9：JS 切换组改 evaluations-filter-group 后，
        旧类 evaluations-review__tabs 的 CSS 定义不删除（静态合同不回归）。"""
        assert "evaluations-review__tabs" in _read("styles.css")

    def test_human_evaluator_operation_note_absent(self) -> None:
        """human 行操作列不展示启停备注。"""
        assert "人工评估器不支持自动使用启停" not in _read("evaluation.js")

    def test_enabled_field_uses_select_control(self) -> None:
        """spec-261112 S9：评估开关为 select 控件而非自由文本输入。"""
        assert "selectControl('evaluation_enabled'" in _code_lines("evaluation.js")

    def test_time_rendered_as_utc8(self) -> None:
        content = _read("evaluation.js")
        # Asia/Shanghai 固定偏移渲染（与 observations-otel.js 同模式）；
        # Mi12：正则放宽空白，锁语义不锁具体排版
        assert re.search(r"8\s*\*\s*3600\s*\*\s*1000", content)

    def test_resolve_module_routes_evaluations_tab(self) -> None:
        """Mi12：锁定 app.js resolveModule 内 evaluations 映射行（此前
        只锁模块暴露面，映射行变更会让 evaluations 入口静默失联）。"""
        content = _read("app.js")
        assert re.search(
            r"if \(tab === 'evaluations'\) return namespace\.evaluations;",
            content,
        )

    def test_uses_management_api_module(self) -> None:
        content = _read("evaluation.js")
        # management-api.js 暴露 NAGENT.api（评估面为 api.evaluations）
        assert "NAGENT.api" in content
        assert "api.evaluations" in content

    def test_langfuse_url_single_source(self) -> None:
        """m6：Langfuse 会话 URL 常量单源（management-ui.js），其余文件
        只引用单源常量/helper，不复制字面量。"""
        literal = (
            "http://langfuse.localhost/project/n-agent-local-project/sessions"
        )
        assert literal in _read("management-ui.js")
        for name in ("evaluation.js", "chat.js", "observations-otel.js"):
            assert literal not in _read(name), (
                f"{name} 不得重复定义 Langfuse URL 字面量（单源在 "
                "management-ui.js）"
            )
        assert "LANGFUSE_SESSIONS_URL" in _read("evaluation.js")
        assert "LANGFUSE_SESSIONS_URL" in _read("observations-otel.js")
        # 对话-执行详情与评分详情走会话详情直达 helper，不再引用 List 常量
        assert "langfuseSessionUrl" in _read("chat.js")
        assert "LANGFUSE_SESSIONS_URL" not in _read("chat.js")
        assert "langfuseSessionUrl" in _read("evaluation.js")

    def test_export_modal_per_case_truth_and_inline_content(self) -> None:
        """M1：导出闭环——UI 内联 fixture/rubric 内容、逐 case 金标。"""
        content = _code_lines("evaluation.js")
        assert "fixture_content" in content
        assert "rubric_content" in content
        assert "expected_output" in content
        # 金标输入框逐 case 生成（不是单值套全批）
        assert "expected_output_" in content
        # query 来自待复核条目（样本真实 query）
        assert "item.query" in content

    def test_overview_and_results_sections(self) -> None:
        """M3：概览聚合端点 + 结果四态浏览。"""
        content = _read("management-api.js")
        assert "'/chat/evaluations/overview'" in content
        js = _code_lines("evaluation.js")
        assert "api.evaluations.overview()" in js
        # 四态文案（质量不通过/评估失败/跳过/通过）
        for label in ("质量不通过", "评估失败", "跳过", "通过"):
            assert label in js
        # 投递等待/未关联口径展示
        assert "unlinked" in js
        # Langfuse 不可变历史 vs 本地 latest 口径说明（M3c）
        assert "supersedes" in js
        # 证据保留边界（M3e）
        assert "evidence_retention_days" in js
        # 结果详情与投递重试入口
        assert "getResult(" in js
        assert "retryDelivery(" in js


# ---------------------------------------------------------------------------
# chat.js Run 详情反馈入口
# ---------------------------------------------------------------------------


class TestRunDetailFeedback:
    def test_feedback_buttons_in_run_detail(self) -> None:
        content = _read("chat.js")
        # Run 详情有用/无用反馈按钮：走 evaluations 反馈入口，禁止原生 confirm
        assert "feedback.helpful" in content
        assert "/chat/evaluations/feedback" in content
        assert not re.search(r"(?<![.\w])confirm\s*\(", content)
        # 反馈区段代码不使用 innerHTML（整行注释剔除后检查）
        start = content.index("function renderRunDetail(")
        end = content.index("未选中执行时展示整个会话账本", start)
        section = _strip_section_comments(content[start:end])
        assert "innerHTML" not in section
        # m4：幂等键防双击重复 revision
        assert "request_key" in section
        # m6：Langfuse 会话详情经单源 helper 直达具体会话
        assert "langfuseSessionUrl" in section
        assert "buildLangfuseSessionUrl(sessionId)" in section
        assert "LANGFUSE_SESSIONS_URL" not in section

    def test_session_feedback_entry_in_run_detail(self) -> None:
        """会话人评入口（feedback.session_helpful）：与运行人评同区段，
        目标 kind=session_feedback，subject 取当前会话 ID。"""
        content = _read("chat.js")
        assert "feedback.session_helpful" in content
        start = content.index("function renderRunDetail(")
        end = content.index("未选中执行时展示整个会话账本", start)
        section = _strip_section_comments(content[start:end])
        assert "session_feedback" in section
        assert "currentSessionId" in section


# ---------------------------------------------------------------------------
# management-api.js 评估端点
# ---------------------------------------------------------------------------


class TestManagementApi:
    def test_evaluation_endpoints_declared(self) -> None:
        content = _read("management-api.js")
        for path in (
            "/chat/evaluations/overview",
            "/chat/evaluations/experiments",
            "/chat/evaluations/results",
            "/chat/evaluations/feedback",
            "/chat/evaluations/config",
            "/chat/evaluations/deliveries/retry",
            "/chat/evaluations/review-queue",
        ):
            assert path in content, f"缺少评估端点: {path}"

    def test_evaluator_admin_endpoints_declared(self) -> None:
        """Task 11：评估器管理端点（能力目录 + 列表/详情/创建/启停）。"""
        content = _read("management-api.js")
        for path in (
            "/chat/evaluations/evaluators/capabilities",
            "/chat/evaluations/evaluators",
        ):
            assert path in content, f"缺少评估器端点: {path}"

    def test_judge_model_endpoint_declared(self) -> None:
        """plan-261115 T6：Judge 模型配置读取端点。"""
        content = _read("management-api.js")
        assert "'/chat/evaluations/judge-model'" in content


class TestJudgeModelProviderPage:
    """plan-261116 T6：评估-模型页重构为 Provider 列表形态（设为评估默认
    + 编辑/新增/删除 modal + 供给精简区）。行为契约见
    evaluation_workbench_harness.js Part I；不渲染"模型列表（当前Provider）"。
    """

    def test_provider_list_wired_to_registry_api(self) -> None:
        """Provider 列表与 CRUD 复用主注册表 API，无新增端点。"""
        js = _code_lines("evaluation.js")
        for call in (
            "api.listProviders(",
            "api.createProvider(",
            "api.updateProvider(",
            "api.deleteProvider(",
        ):
            assert call in js, f"evaluation.js 缺少 Provider 注册表调用: {call}"

    def test_eval_default_ops_never_activate_agent_provider(self) -> None:
        """is_active 只读展示：评估页所有操作不得调用 Agent activate API。"""
        js = _code_lines("evaluation.js")
        assert "activateProvider" not in js, "评估页禁止调用 Agent activate API"

    def test_provider_table_columns_and_default_column(self) -> None:
        """表格列对齐左导模型页；"启用"列替换为"默认"（是/否）。"""
        content = _read("evaluation.js")
        for label in ("名称", "类型", "Base URL", "Model", "Key", "Vision",
                      "默认", "操作"):
            assert label in content, f"Provider 表格缺少列: {label}"

    def test_eval_default_entry_in_edit_modal(self) -> None:
        """设为评估默认入口在编辑 modal（勾选项），行内不再提供。"""
        content = _read("evaluation.js")
        assert "eval-provider-default" in content
        assert "设为评估默认" in content
        assert "judgeDefaultButton" not in content, "行内设默认按钮已移除"

    def test_model_list_section_absent(self) -> None:
        """评估-模型页不渲染"模型列表（当前Provider）"区。"""
        assert "模型列表（当前Provider）" not in _read("evaluation.js")

    def test_shared_provider_modal_note_and_ids(self) -> None:
        """modal 明示与 Agent 共用；页内独立 DOM id（eval-provider-* 前缀）。"""
        content = _read("evaluation.js")
        assert ("Provider 配置与 Agent 共用，修改会影响共用该 Provider 的调用"
                in content)
        for dom_id in ("eval-provider-name", "eval-provider-type",
                       "eval-provider-base-url", "eval-provider-model",
                       "eval-provider-api-key", "eval-provider-vision",
                       "eval-provider-default"):
            assert dom_id in content, f"Provider modal 缺少独立 DOM id: {dom_id}"

    def test_secret_never_persists_to_browser_storage(self) -> None:
        """密钥不写浏览器存储；输入随 modal 关闭/成功清空。"""
        js = _code_lines("evaluation.js")
        assert "localStorage" not in js
        assert "sessionStorage" not in js

    def test_eval_default_patch_payload_uses_provider_id(self) -> None:
        """设默认走既有 config CAS 通道：evaluation_judge_provider_id。"""
        js = _code_lines("evaluation.js")
        assert "evaluation_judge_provider_id" in js
        assert "evaluation_judge_model" not in js, "旧默认模型字段已移除"

    def test_supply_compact_sector_fields(self) -> None:
        """供给精简区：就绪/来源/有效模型/env 基线/host/missing 警示/回落提示。"""
        content = _read("evaluation.js")
        for label in ("未设置默认 Provider，回落 env 基线", "supply_source",
                      "effective_model", "env_baseline_model",
                      "provider_base_url_host", "default_provider_missing"):
            assert label in content, f"供给精简区缺少字段/文案: {label}"

    def test_create_form_prefers_effective_model(self) -> None:
        """创建表单预填优先有效供给模型 effective_model；无供给留空。"""
        js = _code_lines("evaluation.js")
        assert "effective_model" in js
        assert "default_model" not in js, "旧投影字段 default_model 不得再消费"


class TestSessionEvalConfigModal:
    """plan-261124 T4：会话评分视图「评估配置」弹窗 + 概览页会话类型
    白名单复选框。两入口共用 SESSION_EVAL_SOURCE_OPTIONS 选项数组，
    有序值序列与 Python SessionSource 枚举一致。行为契约见
    evaluation_workbench_harness.js Part K。"""

    def _js_option_values(self, content: str, const_name: str) -> list[str]:
        """解析 JS 顶部 [value, label] 二元组常量的有序值序列。"""
        match = re.search(r"const " + const_name + r" = \[(.*?)\];", content, re.S)
        assert match is not None, f"缺少常量 {const_name}"
        values = re.findall(r"\[\s*'([^']+)',\s*'[^']*'\s*\]", match.group(1))
        assert values, f"{const_name} 未解析出选项"
        return values

    def test_modal_function_button_and_shared_constant_exist(self) -> None:
        js = _code_lines("evaluation.js")
        assert "openSessionEvalConfigModal" in js, "缺少会话评估配置弹窗函数"
        assert "SESSION_EVAL_SOURCE_OPTIONS" in js, "缺少复选框选项常量"
        # 选项数组被两个入口共用（会话评分弹窗 + 概览修订配置弹窗）
        assert js.count("SESSION_EVAL_SOURCE_OPTIONS") >= 2, (
            "SESSION_EVAL_SOURCE_OPTIONS 必须被两个入口共用"
        )
        content = _read("evaluation.js")
        assert "actionButton('评估配置'" in content, "缺少「评估配置」入口按钮"

    def test_button_only_rendered_on_session_scores_view(self) -> None:
        """renderResults 是多视图共享 renderer，按钮必须只在
        session-scores 视图出现在头部操作区。"""
        js = _code_lines("evaluation.js")
        assert "viewAtStart === 'session-scores' && body.evaluationsHeaderActions" in js

    def test_options_ordered_values_match_enum_and_chat(self) -> None:
        """选项合同比较有序值序列与 Python SessionSource（不能只比集合
        或固定数量）；值集合与 chat.js SESSION_SOURCE_OPTIONS 一致。"""
        from app.domain.session import SessionSource

        eval_values = self._js_option_values(
            _read("evaluation.js"), "SESSION_EVAL_SOURCE_OPTIONS")
        chat_values = self._js_option_values(
            _read("chat.js"), "SESSION_SOURCE_OPTIONS")
        enum_values = [item.value for item in SessionSource]
        assert eval_values == enum_values, (
            f"SESSION_EVAL_SOURCE_OPTIONS 有序值序列须与 SessionSource 一致: "
            f"{eval_values} != {enum_values}"
        )
        assert sorted(eval_values) == sorted(chat_values), (
            "SESSION_EVAL_SOURCE_OPTIONS 值集合须与 chat.js "
            "SESSION_SOURCE_OPTIONS 一致"
        )

    def test_checkbox_group_payload_and_copy_contract(self) -> None:
        js = _code_lines("evaluation.js")
        assert "input.type = 'checkbox'" in js, "白名单须为复选框组"
        assert "evaluation_session_source_allowlist" in js
        assert "expected_version" in js
        content = _read("evaluation.js")
        assert "空名单 = 关闭会话评估" in content, "缺少空名单警示文案"
        assert "配置已被他人修改，请关闭后重新打开核对" in content, (
            "缺少 409 冲突文案"
        )
        assert "评估配置已保存" in content, "缺少保存成功提示"

    def test_get_failure_and_invalid_shape_block_save(self) -> None:
        js = _code_lines("evaluation.js")
        assert "api.evaluations.getConfig(" in js, "弹窗打开须先 GET 最新配置"
        content = _read("evaluation.js")
        assert "评估配置加载失败" in content, "缺少 GET 失败提示文案"
        assert "配置数据形状非法，禁止保存" in content, "缺少非法形状禁止保存文案"


def test_evaluation_navigation_lifecycle():
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    if not shutil.which('node'):
        pytest.skip('node unavailable')
    subprocess.run(
        ['node', 'tests/interfaces/evaluation_navigation_harness.js'],
        cwd=Path(__file__).resolve().parents[2], check=True,
    )


def test_evaluation_workbench_lifecycle():
    """Task 11：评估器管理视图 harness（api 方法/视图生命周期/安全渲染）。"""
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    if not shutil.which('node'):
        pytest.skip('node unavailable')
    subprocess.run(
        ['node', 'tests/interfaces/evaluation_workbench_harness.js'],
        cwd=Path(__file__).resolve().parents[2], check=True,
    )


class TestEvaluationWorkbenchView:
    """Task 11：评估器视图静态契约（api 方法接线 + spec 4.2 状态文案）。"""

    def test_evaluator_view_uses_admin_api(self) -> None:
        js = _code_lines("evaluation.js")
        for call in (
            "api.evaluations.listEvaluators(",
            "api.evaluations.getEvaluator(",
            "api.evaluations.createEvaluator(",
            "api.evaluations.setEvaluatorUsage(",
            "api.evaluations.getCapabilities(",
        ):
            assert call in js, f"evaluation.js 缺少评估器管理调用: {call}"

    def test_evaluator_status_copy_distinctions(self) -> None:
        """spec 4.2：可配置/执行已就绪/未配置 Judge 状态区分；新建注册
        前提示自动使用默认关闭。"""
        content = _read("evaluation.js")
        for label in (
            "未配置 Judge",
            "执行已就绪",
            "自动使用默认关闭",
        ):
            assert label in content, f"evaluation.js 缺少状态文案: {label}"

    def test_evaluator_view_in_views_table(self) -> None:
        """evaluators 子视图挂入 views 表（renderTab evaluations 共享）。"""
        js = _code_lines("evaluation.js")
        assert re.search(r"evaluators:\s*\{\s*title:\s*'评估器'", js)


class TestAnnotationView:
    """Task 12：人工标注视图静态契约（api 方法接线 + views 表挂载 +
    拒绝原因/证据边界文案）。行为契约见 evaluation_workbench_harness.js
    Part C（spec 4.4）。"""

    def test_annotation_view_uses_annotation_api(self) -> None:
        js = _code_lines("evaluation.js")
        for call in (
            "api.evaluations.listAnnotations(",
            "api.evaluations.getAnnotation(",
            "api.evaluations.submitAnnotation(",
        ):
            assert call in js, f"evaluation.js 缺少人工标注调用: {call}"

    def test_annotation_api_endpoints_registered(self) -> None:
        js = _code_lines("management-api.js")
        assert "/chat/evaluations/annotations" in js

    def test_annotation_view_in_views_table(self) -> None:
        """annotations 子视图挂入 views 表（renderTab evaluations 共享）。"""
        js = _code_lines("evaluation.js")
        assert re.search(r"annotations:\s*\{\s*title:\s*'人工标注'", js)

    def test_annotation_guardrail_copy(self) -> None:
        """spec 4.4：不支持类型提示 + 证据不可用不补造正文。"""
        content = _read("evaluation.js")
        for label in ("暂不支持在此标注", "不补造正文"):
            assert label in content, f"evaluation.js 缺少边界文案: {label}"


class TestWorkbenchAnalyticsView:
    """Task 13：概览趋势/结果筛选分页/实验对比 UI 静态契约。
    行为契约见 evaluation_workbench_harness.js Part E/F（spec 4.5/4.6）。"""

    def test_analytics_and_compare_calls_wired(self) -> None:
        js = _code_lines("evaluation.js")
        assert "api.evaluations.getAnalytics(" in js, "概览趋势卡须调用 getAnalytics"
        assert "api.evaluations.compareExperiments(" in js, "实验对比须调用 compareExperiments"

    def test_analytics_compare_endpoints_declared(self) -> None:
        content = _read("management-api.js")
        assert "'/chat/evaluations/analytics'" in content
        assert "'/chat/evaluations/experiments/compare'" in content

    def test_no_value_copy_no_zero_percent(self) -> None:
        """spec 4.5：无值分组显示暂无有效评分，不显示 0% 成功/100% 质量。"""
        content = _read("evaluation.js")
        assert "暂无有效评分" in content

    def test_overview_lists_paged_and_remarks_in_sectors(self) -> None:
        """概览：评分趋势/Producer 心跳接入标准 slice pager，备注从列表
        上方移到下方并按 Sector 分块（对齐安全-概览 sector 结构）。"""
        content = _read("evaluation.js")
        # 备注 Sector 标题
        for label in ("运行配置", "容量与预算"):
            assert label in content
        # 两个概览列表各自持有独立 slice 分页状态
        for state_name in ("trendPageIndex", "producerPageIndex"):
            assert state_name in content
        # 概览 Sector 复用 sectionPanel 壳，不再用 blockTitle 堆 kvRow
        assert "overviewSector" in content

    def test_review_view_links_annotations(self) -> None:
        content = _read("evaluation.js")
        assert "前往人工标注" in content
        assert "'/evaluations/annotations'" in content

    def test_results_legacy_note_removed(self) -> None:
        js = _code_lines("evaluation.js")
        assert "暂无翻页" not in js, "固定 50 条暂无翻页提示须随筛选分页移除"

    def test_results_filter_params_wired(self) -> None:
        """spec 4.5：结果筛选维度（时间/目标类型/DefinitionKey/来源/状态）
        与游标分页控件须接线。"""
        js = _code_lines("evaluation.js")
        for name in ("results_created_from", "results_created_to",
                     "results_target_kind", "results_definition_id",
                     "results_provenance", "results_status"):
            assert f"'{name}'" in js, f"缺少结果筛选控件: {name}"
        assert "'上一页'" in js
        assert "'下一页'" in js
        assert "resultsCursorStack" in js, "游标栈状态须存在"
