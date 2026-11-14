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
    分页统一 evaluations-pager + 上一页/下一页，区块标题走 blockTitle，
    异步加载具备 loading/error/重试 反馈。"""
    path = ROOT / "app/interfaces/http/static/evaluation-executions.js"
    content = path.read_text()
    assert "刷新执行记录" not in content
    assert "下一页执行记录" not in content
    assert "返回最新执行记录" not in content
    assert "evaluations-pager" in content
    assert "blockTitle('实验执行')" in content
    assert "loading-state" in content
    assert "error-state" in content
    assert "actionButton('重试'" in content
    # 工作台其余 Tab 的分页短文案保持一致
    assert "actionButton('上一页'" in content
    assert "actionButton('下一页'" in content
