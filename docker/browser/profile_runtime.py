#!/usr/bin/env python3
"""Private control plane for persistent Chromium profiles in the browser pod."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROFILE_RE = re.compile(r"bp-(?:container|host_cdp)-[a-f0-9]{12}$")
RUNTIME_ID = uuid.uuid4().hex
PROFILE_ROOT = Path(os.environ.get("CHROMIUM_USER_DATA_DIR", "/data/profiles"))
DISPLAY = os.environ.get("DISPLAY", ":99")
WIDTH = os.environ.get("SCREEN_WIDTH", "1280")
HEIGHT = os.environ.get("SCREEN_HEIGHT", "960")
CDP_INTERNAL_START_PORT = int(os.environ.get("CDP_PROFILE_INTERNAL_START_PORT", "19222"))
CDP_EXTERNAL_START_PORT = int(os.environ.get("CDP_PROFILE_EXTERNAL_START_PORT", "20222"))
LOCK = threading.Lock()
RUNTIMES: dict[str, tuple[int, subprocess.Popen[bytes], subprocess.Popen[bytes]]] = {}


def _start_profile(profile_ref: str) -> int:
    with LOCK:
        existing = RUNTIMES.get(profile_ref)
        if existing is not None and existing[1].poll() is None and existing[2].poll() is None:
            return existing[0]
        offset = len(RUNTIMES)
        internal_port = CDP_INTERNAL_START_PORT + offset
        port = CDP_EXTERNAL_START_PORT + offset
        profile_path = PROFILE_ROOT / profile_ref
        profile_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A browser-container restart can leave Chromium's process-local
        # singleton links behind. The manager has no live entry for this
        # profile at this point, so they cannot belong to a managed process.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (profile_path / name).unlink(missing_ok=True)
        command = [
            "chromium", "--no-sandbox", f"--remote-debugging-port={internal_port}",
            f"--user-data-dir={profile_path}", "--restore-last-session",
            "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-extensions", "--disable-sync",
            "--metrics-recording-only", "--mute-audio", "--disable-dev-shm-usage",
            "--disable-component-update", f"--window-size={WIDTH},{HEIGHT}",
        ]
        process = subprocess.Popen(command, env={**os.environ, "DISPLAY": DISPLAY})
        proxy = subprocess.Popen([
            "socat", f"TCP-LISTEN:{port},fork,bind=0.0.0.0",
            f"TCP:127.0.0.1:{internal_port}",
        ])
        RUNTIMES[profile_ref] = (port, process, proxy)
    for _ in range(50):
        if process.poll() is not None:
            break
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{internal_port}/json/version", timeout=0.2):  # noqa: S310
                return port
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("chromium_profile_start_failed")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "runtime_id": RUNTIME_ID})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        match = re.fullmatch(r"/profiles/([^/]+)", self.path)
        if match is None or not PROFILE_RE.fullmatch(match.group(1)):
            self._json(400, {"error": "invalid_profile_ref"})
            return
        try:
            port = _start_profile(match.group(1))
        except Exception:
            self._json(503, {"error": "profile_unavailable"})
            return
        self._json(200, {"cdp_port": port, "runtime_id": RUNTIME_ID})

    def _json(self, status: int, value: dict[str, object]) -> None:
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def _shutdown(_signum: int, _frame: object) -> None:
    """Let Chromium flush session/profile data before the pod exits."""
    with LOCK:
        runtimes = tuple(RUNTIMES.values())
    for _port, chrome, proxy in runtimes:
        proxy.terminate()
        chrome.terminate()
    for _port, chrome, proxy in runtimes:
        for process in (chrome, proxy):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    ThreadingHTTPServer(("0.0.0.0", 9223), Handler).serve_forever()
