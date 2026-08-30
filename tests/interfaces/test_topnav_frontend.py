import shutil
import subprocess
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

NAV_JS = STATIC_DIR / "management-navigation.js"
TOPNAV_JS = STATIC_DIR / "topnav.js"
TASKS_OBSERVATIONS_JS = STATIC_DIR / "tasks-observations.js"
HARNESS_JS = Path(__file__).parent / "topnav_frontend_harness.js"

TARGET_FILES = (NAV_JS, TOPNAV_JS, TASKS_OBSERVATIONS_JS, STATIC_DIR / "observations.js")


def test_topnav_js_node_syntax_check():
    """T8: 对 management-navigation.js、topnav.js、tasks-observations.js 逐文件 node --check。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    for js_file in TARGET_FILES:
        result = subprocess.run(
            [node, "--check", str(js_file)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, (
            f"{js_file.name} syntax check failed:\n{result.stdout}{result.stderr}"
        )


def test_topnav_frontend_harness():
    """T8: 调用 topnav_frontend_harness.js（T2-T7 已覆盖路由表/双入口/顶导/精确高亮/
    溢出/destroy/reduced-motion/scope/API 过滤/错误/过期响应/安全渲染）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, str(HARNESS_JS)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all tests passed" in result.stdout
