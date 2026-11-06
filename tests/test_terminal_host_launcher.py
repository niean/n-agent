"""Unit checks for host bridge protocol upgrades without real processes."""

from contextlib import contextmanager
import importlib.util
import io
import json
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
import urllib.error

import pytest


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[1] / "host/terminal-host.py"
    spec = importlib.util.spec_from_file_location("terminal_host_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Opener:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"grants": [], "error_code": None}

    @contextmanager
    def open(self, request, timeout):
        assert request.full_url == "http://127.0.0.1:8765/v1/script-grants"
        assert request.get_header("X-n-agent-host-token") == "z" * 32
        assert json.loads(request.data) == {"operation": "list", "grant": None}
        raw = json.dumps(self.body).encode()
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "test", {}, io.BytesIO(raw))
        yield io.BytesIO(raw)


@pytest.mark.parametrize("status,body,expected", [
    (200, None, True),
    (404, {}, False),
    (503, {"grants": [], "error_code": "host_script_grants_unavailable"}, True),
])
def test_protocol_probe_authenticates_and_only_404_requires_upgrade(launcher, tmp_path, status, body, expected):
    token = tmp_path / "token"
    token.write_text("z" * 32)
    token.chmod(0o600)
    assert launcher._script_grants_supported(Opener(status, body), "8765", token) is expected


def test_protocol_probe_rejects_insecure_token_and_auth_failure(launcher, tmp_path):
    token = tmp_path / "token"
    token.write_text("z" * 32)
    token.chmod(0o644)
    with pytest.raises(ValueError):
        launcher._script_grants_supported(Opener(), "8765", token)
    token.chmod(0o600)
    with pytest.raises(ValueError):
        launcher._script_grants_supported(Opener(401, {}), "8765", token)


def test_legacy_shutdown_only_signals_verified_managed_process(launcher, monkeypatch):
    responses = iter([
        SimpleNamespace(stdout=f"python -m {launcher.MODULE} --port 8765"),
        SimpleNamespace(stdout="unrelated --port 8765"),
    ])
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: next(responses))
    signals = []
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    launcher._stop_legacy_bridge(1234, "8765")
    assert signals == [(1234, signal.SIGINT)]


def test_legacy_shutdown_never_signals_unmanaged_or_other_port(launcher, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout=f"python -m {launcher.MODULE} --port 87650"
    ))
    monkeypatch.setattr(launcher.os, "kill", lambda *args: pytest.fail("must not signal another process"))
    with pytest.raises(ValueError):
        launcher._stop_legacy_bridge(1234, "8765")


@pytest.mark.parametrize("supported,action", [(True, "start"), (False, "start"), (False, "status")])
def test_launcher_reuses_current_bridge_and_upgrades_legacy_only_on_start(
    launcher, tmp_path, monkeypatch, supported, action,
):
    install = tmp_path / "install"
    locals_dir = install / "locals"
    locals_dir.mkdir(parents=True)
    runtime = install / "host-terminal-runtime"
    runtime.mkdir(mode=0o700)
    pid_file = runtime / "bridge.pid"
    pid_file.write_text("1234\n")
    env = {
        "N_AGENT_HOST_TERMINAL_ENABLED": "true",
        "N_AGENT_HOST_TERMINAL_BRIDGE_URL": "http://host.docker.internal:8765",
        "N_AGENT_HOST_TERMINAL_POLICY_PATH": "/private/policy.yaml",
        "N_AGENT_HOST_TERMINAL_TOKEN_PATH": "/private/token",
    }
    config = {"services": {"n-agent": {"environment": env, "volumes": [
        {"type": "bind", "target": "/app/locals", "source": str(locals_dir)},
        {"type": "bind", "target": "/private", "source": str(tmp_path / "private")},
        {"type": "bind", "target": "/workspace", "source": str(tmp_path / "workspace")},
    ]}}}
    state = {"old": True, "new": False}
    stopped = []

    class HealthOpener:
        def open(self, url, timeout):
            if not state["old"] and not state["new"]:
                raise OSError("not running")
            return io.BytesIO(b'{"status":"ok"}')

    def stop(pid, port):
        stopped.append((pid, port))
        state["old"] = False

    def start(*args, **kwargs):
        state["new"] = True
        return SimpleNamespace(pid=5555, poll=lambda: None)

    monkeypatch.setattr(launcher.sys, "argv", ["terminal-host.py", action])
    monkeypatch.setenv("HOST_TERMINAL_PYTHON", sys.executable)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(config)))
    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    monkeypatch.setattr(launcher.urllib.request, "build_opener", lambda *a: HealthOpener())
    monkeypatch.setattr(launcher, "_managed_bridge", lambda *a: True)
    monkeypatch.setattr(launcher, "_script_grants_supported", lambda *a: supported)
    monkeypatch.setattr(launcher, "_stop_legacy_bridge", stop)
    if action == "status":
        with pytest.raises(ValueError, match="outdated"):
            launcher.main()
        assert stopped == []
    else:
        assert launcher.main() == 0
        assert state["new"] is (not supported)
        assert stopped == ([] if supported else [(1234, "8765")])
        assert pid_file.read_text().strip() == ("1234" if supported else "5555")
