#!/usr/bin/env python3
"""Run a configured end-to-end test command with process-group cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def fail(message: str) -> None:
    print(f"e2e: failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 5
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if process.poll() is None:
        process.wait()


def main() -> int:
    if len(sys.argv) != 3:
        fail("usage: run-e2e.py <command> <timeout-seconds>")
    command = sys.argv[1]
    try:
        timeout = int(sys.argv[2])
    except ValueError:
        fail("timeout must be an integer")
    if timeout < 1:
        fail("timeout must be positive")

    repo_root = Path(__file__).resolve().parents[6]
    process = subprocess.Popen(command, shell=True, cwd=repo_root, start_new_session=True)
    try:
        result = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        print(f"e2e: timed_out (timeoutSeconds={timeout})", file=sys.stderr)
        return 124
    if result:
        print(f"e2e: failed (exit_code={result})", file=sys.stderr)
    else:
        print("e2e: executed")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
