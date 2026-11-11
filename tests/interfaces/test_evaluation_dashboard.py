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

    def test_topnav_evaluators_annotations_single_element_scopes(self) -> None:
        """新增两页顶导：单元素数组、concern 'evaluation'、scope 各自归属。"""
        content = _read("management-navigation.js")
        for view in ("evaluators", "annotations"):
            m = re.search(r"'evaluations-" + view + r"': \[(.*?)\n    \]", content, re.S)
            assert m is not None, f"topnavConfig 缺少 evaluations-{view}"
            block = m.group(1)
            assert block.count("{ tab:") == 1, f"evaluations-{view} 顶导必须是单元素数组"
            assert "concern: 'evaluation'" in block
            assert "scope: '" + view + "'" in block
            assert "topnavParent: 'evaluations-" + view + "'" in block

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
        assert labels == ["概览", "数据集", "评估器", "实验", "结果", "人工标注"], labels
        assert 'data-tab="evaluations-review"' not in content
        assert 'href="/evaluations/review"' not in content


# ---------------------------------------------------------------------------
# shell 路由（六菜单 + review 兼容页 = 七路径）
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
        "/evaluations/experiments", "/evaluations/results",
        "/evaluations/annotations", "/evaluations/review",
    )

    def test_shell_200_for_seven_evaluation_paths(self, tmp_path) -> None:
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
        ):
            assert cls in css, f"styles.css 缺少类: {cls}"
        # Phase 5 扫描修复（Warning）：详情/表单弹窗按钮容器统一标准类
        # providers-form__actions，自定义 evaluations-form-actions 已删除
        assert "providers-form__actions" in css
        assert "evaluations-form-actions" not in css

    def test_toggle_groups_use_aria_pressed_not_tablist(self) -> None:
        """spec-261112：筛选按钮组是查询过滤器，用 role=group +
        aria-pressed，禁止不完整的 tablist 语义（含行内注释）。"""
        js = _code_lines("evaluation.js")
        assert "aria-pressed" in js
        assert "role', 'group'" in js
        assert "tablist" not in js

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

    def test_human_evaluator_disabled_copy_present(self) -> None:
        """spec-261112 S9：human 行常驻说明文案锁定。"""
        assert "人工评估器不支持自动使用启停" in _read("evaluation.js")

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
        只引用 ui.LANGFUSE_SESSIONS_URL，不复制字面量。"""
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
        assert "LANGFUSE_SESSIONS_URL" in _read("chat.js")
        assert "LANGFUSE_SESSIONS_URL" in _read("observations-otel.js")

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
        # m6：Langfuse URL 单源引用
        assert "LANGFUSE_SESSIONS_URL" in section


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
        前提示自动使用默认关闭；同指标多版本启用提示不合并结果。"""
        content = _read("evaluation.js")
        for label in (
            "未配置 Judge",
            "执行已就绪",
            "自动使用默认关闭",
            "会增加评估作业，结果不合并",
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
