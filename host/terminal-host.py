#!/usr/bin/env python3
"""Start/check the host terminal bridge using resolved Compose configuration.

Run with python3 host/terminal-host.py [start|status|check]. No camera action is sent.
HOST_TERMINAL_PYTHON overrides the isolated host-terminal-venv interpreter.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import urllib.request
import urllib.error

REPO = Path(__file__).resolve().parents[1]
MODULE = "app.infrastructure.host_terminal.bridge_server"


def _script_grants_supported(opener, port: str, token_path: Path) -> bool:
    """Probe the authenticated protocol without executing a host command."""
    try:
        fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as token_file:
            metadata = os.fstat(token_file.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & ~0o600
            ):
                raise ValueError("invalid host terminal token permissions")
            raw_token = token_file.read(4097)
        token = raw_token.removesuffix(b"\n").decode("utf-8")
        if not 32 <= len(raw_token) <= 4096 or "\n" in token or "\r" in token:
            raise ValueError("invalid host terminal token")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/script-grants",
            data=b'{"operation":"list","grant":null}',
            headers={"Content-Type": "application/json", "X-N-Agent-Host-Token": token},
            method="POST",
        )
        try:
            with opener.open(request, timeout=2) as response:
                body, status = response.read(1048577), 200
        except urllib.error.HTTPError as error:
            try:
                if error.code == 404:
                    return False
                body, status = error.read(1048577), error.code
            finally:
                error.close()
        if len(body) > 1048576:
            raise ValueError("host terminal protocol response is too large")
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"grants", "error_code"}
            or not isinstance(payload["grants"], list)
            or not (
                (status == 200 and payload["error_code"] is None)
                or (status == 503 and payload["error_code"] == "host_script_grants_unavailable")
            )
        ):
            raise ValueError("host terminal protocol probe failed")
        return True
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("host terminal authorization protocol probe failed") from error


def _managed_bridge(pid: int, port: str) -> bool:
    process = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
    )
    words = process.stdout.split()
    return any(
        words[index:index + 2] == ["-m", MODULE]
        for index in range(len(words) - 1)
    ) and any(
        words[index:index + 2] == ["--port", port]
        for index in range(len(words) - 1)
    )


def _stop_legacy_bridge(pid: int, port: str) -> None:
    if not _managed_bridge(pid, port):
        raise ValueError("refusing to stop an unmanaged host terminal process")
    try:
        # SIGINT unwinds the old server's finally block and cleans up its children.
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _managed_bridge(pid, port):
            return
        time.sleep(0.1)
    if _managed_bridge(pid, port):
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _managed_bridge(pid, port):
            return
        time.sleep(0.1)
    raise ValueError("legacy host terminal bridge did not stop")


def main() -> int:
    action = sys.argv[1] if len(sys.argv) == 2 else "start"
    if action not in {"start", "status", "check"}:
        raise ValueError("usage: terminal-host.py [start|status|check]")
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO / "docker", check=True, capture_output=True, text=True,
    )
    service = json.loads(result.stdout)["services"]["n-agent"]
    env = service["environment"]
    if str(env.get("N_AGENT_HOST_TERMINAL_ENABLED", "false")).lower() not in {"true", "1", "yes"}:
        print("host-terminal: disabled")
        return 0
    match = re.fullmatch(r"http://host\.docker\.internal:([1-9][0-9]{0,4})/?", env["N_AGENT_HOST_TERMINAL_BRIDGE_URL"])
    if not match or not 1 <= int(match[1]) <= 65535:
        raise ValueError("invalid host terminal bridge URL")
    port = match[1]
    if action == "check":
        # Verify the actual client network path, not only host loopback health.
        probe = (
            "import json,os,urllib.request;"
            "o=urllib.request.build_opener(urllib.request.ProxyHandler({}));"
            "u=os.environ['N_AGENT_HOST_TERMINAL_BRIDGE_URL'].rstrip('/')+'/healthz';"
            "r=o.open(u,timeout=5);"
            "assert json.load(r)=={'status':'ok'};"
            "print('host-terminal: container connection healthy')"
        )
        subprocess.run(["docker", "compose", "exec", "-T", "n-agent", "python", "-c", probe],
                       cwd=REPO / "docker", check=True)
        return 0
    mounts = {v["target"]: Path(v["source"]) for v in service["volumes"] if v["type"] == "bind"}

    def host_path(container: str) -> Path:
        for target in sorted(mounts, key=len, reverse=True):
            if container == target or container.startswith(target + "/"):
                return mounts[target] / container[len(target):].lstrip("/")
        raise ValueError("host terminal path has no bind mount")

    install = mounts["/app/locals"].parent
    runtime = install / "host-terminal-runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_uid != os.getuid() or runtime.stat().st_mode & 0o077:
        raise ValueError("host terminal runtime must be private and owned by this user")
    with (runtime / "start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_file = runtime / "bridge.pid"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def healthy() -> bool:
            try:
                with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                    return json.load(response) == {"status": "ok"}
            except (OSError, ValueError):
                return False

        if pid_file.exists():
            pid = pid_file.read_text().strip()
            if pid.isdigit():
                if _managed_bridge(int(pid), port):
                    if not healthy():
                        raise ValueError("existing host terminal bridge is unhealthy; inspect bridge.log")
                    if _script_grants_supported(
                        opener, port, host_path(env["N_AGENT_HOST_TERMINAL_TOKEN_PATH"])
                    ):
                        print(f"host-terminal: healthy (pid {pid}, port {port})")
                        return 0
                    if action == "status":
                        raise ValueError("host terminal bridge protocol is outdated; run start to upgrade")
                    _stop_legacy_bridge(int(pid), port)
                    print("host-terminal: upgrading authorization protocol")
        if action == "status":
            raise ValueError("host terminal bridge is not running")
        if healthy():
            raise ValueError("bridge port is occupied by an unmanaged service")
        python = Path(os.environ.get("HOST_TERMINAL_PYTHON", str(install / "host-terminal-venv/bin/python")))
        if not python.is_file():
            raise ValueError(f"missing interpreter: {python}; create a venv and install host/terminal-requirements.txt")
        subprocess.run([str(python), "-c", "import httpx, yaml"], check=True)
        script_python = python.resolve(strict=True)
        args = [str(python), "-m", MODULE,
                "--policy", str(host_path(env["N_AGENT_HOST_TERMINAL_POLICY_PATH"])),
                "--token", str(host_path(env["N_AGENT_HOST_TERMINAL_TOKEN_PATH"])),
                "--skills-root", str(host_path("/workspace/skills")),
                "--python", str(script_python),
                "--snapshot-root", str(runtime / "snapshots"),
                "--trusted-root", str(script_python.parent),
                "--trusted-root", "/usr/bin", "--port", port]
        for target, source in mounts.items():
            if target == "/workspace" or target.startswith("/workspace-"):
                args.extend(["--model-writable-root", str(source)])
        child_env = dict(os.environ, PYTHONPATH=str(REPO))
        with (runtime / "bridge.log").open("a") as log:
            process = subprocess.Popen(args, cwd=REPO, env=child_env,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
        pid_file.write_text(str(process.pid) + "\n")
        for _ in range(15):
            if process.poll() is not None:
                raise ValueError(f"host terminal bridge exited; inspect {runtime / 'bridge.log'}")
            if healthy():
                print(f"host-terminal: ready (pid {process.pid}, port {port})")
                return 0
            time.sleep(1)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise ValueError(f"host terminal bridge startup timed out; inspect {runtime / 'bridge.log'}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"host-terminal: {exc}", file=sys.stderr)
        sys.exit(1)
