from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = (
    PROJECT_ROOT
    / ".harness/framework/skills/harness/run-e2e-tests/scripts/run-e2e.py"
)


def _run(command: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), command, str(timeout)],
        cwd=PROJECT_ROOT / "tests",
        capture_output=True,
        text=True,
        check=False,
    )


def test_runner_executes_from_repository_root() -> None:
    result = _run(f'test "$(pwd)" = "{PROJECT_ROOT}"')

    assert result.returncode == 0
    assert result.stdout == "e2e: executed\n"


def test_runner_preserves_nonzero_exit_code() -> None:
    result = _run("exit 7")

    assert result.returncode == 7
    assert "e2e: failed (exit_code=7)" in result.stderr


def test_runner_times_out_without_waiting_for_full_command() -> None:
    started = time.monotonic()
    result = _run("sleep 30", timeout=1)

    assert result.returncode == 124
    assert time.monotonic() - started < 8
    assert "e2e: timed_out (timeoutSeconds=1)" in result.stderr
