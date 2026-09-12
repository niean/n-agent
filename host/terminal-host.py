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
import subprocess
import sys
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]
MODULE = "app.infrastructure.host_terminal.bridge_server"


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
                process = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True)
                if MODULE in process.stdout and f"--port {port}" in process.stdout:
                    if not healthy():
                        raise ValueError("existing host terminal bridge is unhealthy; inspect bridge.log")
                    print(f"host-terminal: healthy (pid {pid}, port {port})")
                    return 0
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
