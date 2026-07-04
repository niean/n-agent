from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["N_AGENT_SQLITE_PATH"] = str(tmp_path / "sessions.db")
    env["N_AGENT_WORKSPACE_ROOT"] = str(tmp_path)
    env["N_AGENT_SANDBOX_SCRATCH_ROOT"] = str(tmp_path / "sandbox-scratch")
    env["N_AGENT_SKILLS_ROOT"] = str(tmp_path / "skills")
    env["N_AGENT_PLUGINS_ROOT"] = str(tmp_path / "plugins")
    env["N_AGENT_GATEWAY_ENABLED"] = "false"
    env["N_AGENT_SCHEDULER_ENABLED"] = "false"
    return env


def test_acp_check_subprocess(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.interfaces.cli", "acp", "--check"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_base_env(tmp_path),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "ACP check OK" in result.stderr
    for needle in ("INFO", "WARNING", "Loaded", "Starting"):
        assert needle not in result.stdout, f"stdout leaked {needle!r}: {result.stdout!r}"


def test_acp_setup_subprocess(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.interfaces.cli", "acp", "--setup"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_base_env(tmp_path),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "ACP provider setup" in result.stderr
    assert result.stdout == ""


def test_acp_server_stdout_purity_on_eof(tmp_path: Path) -> None:
    # Use a temp file for stdout instead of PIPE: the ACP SDK's asyncio
    # write transport for sys.stdout interacts poorly with subprocess pipes
    # and can hang the event loop on shutdown. A regular file avoids this
    # while still letting us assert stdout carries no log text.
    stdout_file = tmp_path / "stdout.log"
    with stdout_file.open("wb") as fout, (tmp_path / "stderr.log").open("wb") as ferr:
        result = subprocess.run(
            [sys.executable, "-m", "app.interfaces.cli", "acp"],
            capture_output=False,
            timeout=15,
            env=_base_env(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=fout,
            stderr=ferr,
        )
    stdout_text = stdout_file.read_text()
    for needle in ("INFO", "WARNING", "Loaded", "Starting"):
        assert needle not in stdout_text, (
            f"stdout leaked {needle!r}: stdout={stdout_text!r}"
        )


def test_benign_method_not_found_filter_drops_ping_health() -> None:
    """Verify the filter suppresses benign ping/health method-not-found errors.

    The ACP SDK logs these via ``logging.exception`` when clients probe with
    methods not in the agent's router; they're noise on stderr, not signal.
    """
    import logging

    from app.interfaces.cli.commands.acp.command import _BenignMethodNotFoundFilter

    class _FakeRequestError(Exception):
        def __init__(self, code: int, data: dict | None) -> None:
            super().__init__("method not found")
            self.code = code
            self.data = data

    flt = _BenignMethodNotFoundFilter()

    def _record(exc: Exception) -> logging.LogRecord:
        return logging.LogRecord(
            name="acp", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="err", args=(), exc_info=(type(exc), exc, exc.__traceback__),
        )

    benign = _record(_FakeRequestError(-32601, {"method": "ping"}))
    real = _record(_FakeRequestError(-32601, {"method": "session/prompt"}))
    no_data = _record(RuntimeError("unrelated"))

    assert flt.filter(benign) is False, "ping method-not-found should be suppressed"
    assert flt.filter(real) is True, "session/prompt method-not-found should pass"
    assert flt.filter(no_data) is True, "non-RequestError exceptions should pass"
