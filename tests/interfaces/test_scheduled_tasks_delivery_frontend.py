import shutil
import subprocess
from pathlib import Path

import pytest


def test_scheduled_tasks_delivery_frontend_harness():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    harness = Path(__file__).with_name("scheduled_tasks_delivery_frontend_harness.js")
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK scheduled tasks delivery frontend harness passed" in result.stdout
