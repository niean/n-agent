from __future__ import annotations

import subprocess
import sys


def test_cli_package_importable():
    from app.interfaces import cli

    assert hasattr(cli, "main")


def test_cli_main_callable():
    from app.interfaces.cli import main

    assert callable(main)


def test_python_m_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "app.interfaces.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "n-agent" in result.stdout or "usage" in result.stdout.lower()
