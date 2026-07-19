"""Static + behavior harness entry for host.js (本机 dashboard page)."""

from __future__ import annotations

import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "app" / "interfaces" / "http" / "static"
HARNESS = Path(__file__).resolve().parent / "host_terminal_frontend_harness.js"


def test_host_js_node_check():
    result = subprocess.run(
        ["node", "--check", str(STATIC / "host.js")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_host_js_harness():
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
