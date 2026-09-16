import shutil
import subprocess
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

OBSERVATIONS_JS = STATIC_DIR / "scheduled-tasks-observations.js"
HARNESS_JS = Path(__file__).parent / "scheduled_observations_frontend_harness.js"


def test_scheduled_observations_js_node_syntax_check():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run([node, "--check", str(OBSERVATIONS_JS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_scheduled_observations_frontend_harness():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run([node, str(HARNESS_JS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK scheduled observations frontend harness passed" in result.stdout
