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


def test_cli_subcommands_help_available():
    for cmd in (
        "provider",
        "knowledge",
        "mcp",
        "schedule",
        "sandbox",
        "memory",
        "platform",
        "doctor",
        "config",
        "logs",
    ):
        result = subprocess.run(
            [sys.executable, "-m", "app.interfaces.cli", cmd, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"{cmd} --help failed: {result.stderr}"
        assert cmd in result.stdout.lower() or "usage" in result.stdout.lower()


def test_sessions_browse_flag_registered():
    result = subprocess.run(
        [sys.executable, "-m", "app.interfaces.cli", "sessions", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--browse" in result.stdout
