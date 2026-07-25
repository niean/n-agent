import shutil
import subprocess
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

TASKS_SECURITY_JS = STATIC_DIR / "tasks-security.js"
SECURITY_JS = STATIC_DIR / "security.js"
HARNESS_JS = Path(__file__).parent / "task_security_frontend_harness.js"

TARGET_FILES = (TASKS_SECURITY_JS, SECURITY_JS)


def test_tasks_security_js_node_syntax_check():
    """T5: tasks-security.js 与 security.js 逐文件 node --check。"""
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


def test_tasks_security_frontend_harness():
    """T5: 调用 task_security_frontend_harness.js（renderer 暴露、payload 合同校验、
    并发生命周期、错误脱敏、XSS 安全）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, str(HARNESS_JS)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all tests passed" in result.stdout
