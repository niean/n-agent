from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_execution_ui_harness():
    result = subprocess.run(["node", "tests/interfaces/evaluation_executions_harness.js"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_execution_ui_reuses_existing_components():
    path = ROOT / "app/interfaces/http/static/evaluation-executions.js"
    assert path.exists(), "execution UI missing"
    content = path.read_text()
    for forbidden in ("innerHTML", ".style.", "window.alert", "window.confirm"):
        assert forbidden not in content
    for helper in ("buildFrame", "formActions", "filterItem", "actionButton", "mountDetailModal", "blockTitle"):
        assert helper in content


def test_execution_list_follows_workbench_fe_contract():
    """实验页执行记录区块对齐评估工作台 FE 规范：无手动刷新按钮，
    分页复用工作台共享 pagerBar（evaluations-pager + 上一页/下一页），
    每页数量走共享 LIST_PAGE_SIZE，列表页不渲染区块标题行，
    异步加载具备 loading/error/重试 反馈。"""
    path = ROOT / "app/interfaces/http/static/evaluation-executions.js"
    content = path.read_text()
    assert "刷新执行记录" not in content
    assert "下一页执行记录" not in content
    assert "返回最新执行记录" not in content
    # 分页组件与每页数量由 evaluation.js 共享 helper 注入，本模块不重复实现
    assert "pagerBar" in content
    assert "LIST_PAGE_SIZE" in content
    assert "limit: 50" not in content
    # 执行任务入口名已表达语义，列表页不再挂载 blockTitle('实验执行') 行
    assert "blockTitle('实验执行')" not in content
    assert "loading-state" in content
    assert "error-state" in content
    assert "actionButton('重试'" in content
    # pagerBar 始终占位：边界页通过 hasPrev/hasNext 禁用而非隐藏按钮
    assert "hasPrev:" in content
    assert "hasNext:" in content
