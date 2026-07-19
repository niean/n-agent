import shutil
import subprocess
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

TASKS_JS = STATIC_DIR / "tasks.js"
HARNESS_JS = Path(__file__).parent / "tasks_frontend_harness.js"


def test_tasks_js_node_syntax_check():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run([node, "--check", str(TASKS_JS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_tasks_frontend_harness():
    """Regression: /chat/tasks/board returns `columns` as an array of
    {status, cards, total}; tasks.js must render cards by indexing that array
    by status (not treat it as a dict keyed by status, which rendered zero
    cards). Also covers the archived toggle re-fetching with ?archived=true."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run([node, str(HARNESS_JS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all tests passed" in result.stdout
