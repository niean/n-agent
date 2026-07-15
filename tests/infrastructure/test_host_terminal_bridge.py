from __future__ import annotations

import hashlib
import http.client
import json
import os
from dataclasses import replace
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from app.infrastructure.host_terminal.bridge_server import (
    HostTerminalBridge,
    HostTerminalBridgeConfig,
    make_server,
)


TOKEN = b"z" * 32


def _limits() -> str:
    return """limits:
  default_timeout_seconds: 1
  max_timeout_seconds: 5
  max_stdout_bytes: 4096
  max_stderr_bytes: 4096
  max_args: 2
  max_arg_length: 32
  max_total_args_length: 64
  max_concurrency: 1
"""


def _command_policy(executable: Path, *, arg: str = "hello") -> str:
    return f"""schema_version: 1
version: v1
{_limits()}targets:
  - type: command
    rule_id: echo
    executable: {executable}
    args:
      - exact: {arg}
"""


def _skill_policy(source: bytes, *, version: str = "v1") -> str:
    digest = hashlib.sha256(source).hexdigest()
    return f"""schema_version: 1
version: {version}
{_limits()}targets:
  - type: skill_script
    rule_id: camera
    skill_name: camera
    script_relative_path: scripts/run.py
    sha256: {digest}
    args: []
"""


def _secure_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)


def _bridge(
    tmp_path: Path,
    policy_text: str,
    *,
    popen_factory=None,
    request_read_timeout_seconds: float = 0.2,
    max_http_handler_threads: int = 16,
    max_concurrency: int = 1,
    codesign_runner=None,
) -> HostTerminalBridge:
    policy = tmp_path / "private" / "policy.yaml"
    token = tmp_path / "private" / "token"
    _secure_write(policy, policy_text.encode())
    _secure_write(token, TOKEN + b"\n")
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    python = Path(sys.executable).resolve(strict=True)
    config = HostTerminalBridgeConfig(
        policy_path=policy,
        token_path=token,
        skills_root=skills,
        python_executable=python,
        snapshot_root=tmp_path / "snapshots",
        trusted_executable_roots=tuple(
            {str(python.parent), str(Path("/bin").resolve()), str(Path("/usr/bin").resolve())}
        ),
        port=0,
        max_concurrency=max_concurrency,
        terminate_grace_seconds=0.2,
        request_read_timeout_seconds=request_read_timeout_seconds,
        max_http_handler_threads=max_http_handler_threads,
        required_executable_owner_uid=os.geteuid(),
    )
    kwargs = {} if popen_factory is None else {"popen_factory": popen_factory}
    if codesign_runner is not None:
        kwargs["codesign_runner"] = codesign_runner
    return HostTerminalBridge(config, **kwargs)


def _config_with_authority_paths(
    tmp_path: Path, policy_path: Path, token_path: Path
) -> HostTerminalBridgeConfig:
    skills = tmp_path / "skills"
    snapshots = tmp_path / "snapshots"
    skills.mkdir(parents=True, exist_ok=True)
    snapshots.mkdir(mode=0o700, parents=True, exist_ok=True)
    _secure_write(
        policy_path,
        _command_policy(Path("/bin/echo").resolve(strict=True)).encode(),
    )
    _secure_write(token_path, TOKEN + b"\n")
    python = Path(sys.executable).resolve(strict=True)
    return HostTerminalBridgeConfig(
        policy_path=policy_path,
        token_path=token_path,
        skills_root=skills,
        python_executable=python,
        snapshot_root=snapshots,
        trusted_executable_roots=(str(python.parent), str(Path("/usr/bin").resolve())),
        port=0,
        terminate_grace_seconds=0.2,
        request_read_timeout_seconds=0.2,
        required_executable_owner_uid=os.geteuid(),
    )


def _payload(bridge: HostTerminalBridge, target: dict[str, object], **limits: int) -> dict[str, object]:
    snapshot = bridge.policy_loader.snapshot
    assert snapshot is not None
    return {
        "protocol_version": "1",
        "request_id": "req-1",
        "target": target,
        "n_agent_policy_version": snapshot.version,
        "n_agent_content_digest": snapshot.content_digest,
        "limits": {
            "timeout_seconds": limits.get("timeout_seconds", 2),
            "max_stdout_bytes": limits.get("max_stdout_bytes", 1024),
            "max_stderr_bytes": limits.get("max_stderr_bytes", 1024),
            "max_concurrency": 1,
        },
    }


def test_requires_loopback_binding(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="host_bridge_loopback_required"):
        HostTerminalBridgeConfig(
            policy_path="p",
            token_path="t",
            skills_root="s",
            python_executable="/usr/bin/python3",
            snapshot_root="x",
            trusted_executable_roots=("/usr/bin",),
            bind_host="0.0.0.0",
        )


def test_required_command_owner_defaults_to_root() -> None:
    config = HostTerminalBridgeConfig(
        policy_path="p",
        token_path="t",
        skills_root="s",
        python_executable="/usr/bin/python3",
        snapshot_root="x",
        trusted_executable_roots=("/usr/bin",),
    )
    assert config.required_executable_owner_uid == 0


def test_command_source_rejects_non_required_owner_but_skill_python_does_not(
    tmp_path: Path,
) -> None:
    command = tmp_path / "trusted" / "command"
    _secure_write(command, b"#!/bin/sh\nexit 0\n")
    command.chmod(0o500)
    bridge = _bridge(tmp_path, _command_policy(command))
    bridge.config = replace(
        bridge.config,
        trusted_executable_roots=(command.parent, Path(sys.executable).resolve().parent),
        required_executable_owner_uid=0,
    )
    target = {"type": "command", "executable": str(command), "args": ["hello"]}
    _, denied = bridge.execute_payload(_payload(bridge, target))
    assert denied["error_code"] == "host_executable_denied"

    source = b"print('skill-python-owner-independent')\n"
    skill_bridge = _bridge(tmp_path / "skill-case", _skill_policy(source))
    skill_bridge.config = replace(
        skill_bridge.config, required_executable_owner_uid=0
    )
    script = Path(skill_bridge.config.skills_root) / "camera" / "scripts" / "run.py"
    _secure_write(script, source)
    skill_target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    _, allowed = skill_bridge.execute_payload(_payload(skill_bridge, skill_target))
    assert allowed["status"] == "success"


@pytest.mark.parametrize("authority,root", [("policy", "skills"), ("token", "skills"), ("policy", "snapshots"), ("token", "snapshots")])
def test_rejects_authority_files_inside_model_writable_roots(
    tmp_path: Path, authority: str, root: str
) -> None:
    private = tmp_path / "private"
    policy = private / "policy.yaml"
    token = private / "token"
    unsafe = tmp_path / root / ("policy.yaml" if authority == "policy" else "token")
    if authority == "policy":
        policy = unsafe
    else:
        token = unsafe
    config = _config_with_authority_paths(tmp_path, policy, token)
    with pytest.raises(ValueError, match="host_bridge_authority_path_unsafe"):
        HostTerminalBridge(config)


def test_startup_removes_safe_stale_snapshots_and_rejects_unsafe_one(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "private" / "policy.yaml"
    token = tmp_path / "private" / "token"
    config = _config_with_authority_paths(tmp_path, policy, token)
    stale = Path(config.snapshot_root) / "host-skill-stale.py"
    stale_command = Path(config.snapshot_root) / "host-command-stale"
    _secure_write(stale, b"old")
    _secure_write(stale_command, b"old")
    stale_command.chmod(0o500)
    bridge = HostTerminalBridge(config)
    assert not stale.exists()
    assert not stale_command.exists()
    bridge.shutdown()

    unsafe_target = tmp_path / "unsafe-target.py"
    _secure_write(unsafe_target, b"unsafe")
    stale.symlink_to(unsafe_target)
    with pytest.raises(ValueError, match="host_bridge_stale_snapshot_unsafe"):
        HostTerminalBridge(config)


def test_host_model_writable_roots_reject_authority_and_trusted_executable_root(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    base = _config_with_authority_paths(tmp_path, private / "policy.yaml", private / "token")
    with pytest.raises(ValueError, match="host_bridge_authority_path_unsafe"):
        HostTerminalBridge(replace(base, model_writable_roots=(private,)))
    with pytest.raises(ValueError, match="host_bridge_trusted_root_unsafe"):
        HostTerminalBridge(
            replace(base, model_writable_roots=(Path(base.trusted_executable_roots[0]),))
        )


@pytest.mark.parametrize("relationship", ["equal", "inside", "parent"])
def test_snapshot_root_rejects_bidirectional_model_writable_overlap_before_write_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relationship: str
) -> None:
    private = tmp_path / "private"
    base = _config_with_authority_paths(tmp_path, private / "policy.yaml", private / "token")
    snapshots = tmp_path / "fresh-snapshots"
    writable = {
        "equal": snapshots,
        "inside": snapshots / "model-writable",
        "parent": tmp_path,
    }[relationship]
    monkeypatch.setattr(
        HostTerminalBridge,
        "_cleanup_stale_snapshots",
        lambda self: pytest.fail("cleanup must not run after overlap rejection"),
    )

    with pytest.raises(ValueError, match="host_bridge_snapshot_root_unsafe"):
        HostTerminalBridge(
            replace(
                base,
                snapshot_root=snapshots,
                model_writable_roots=(writable,),
            )
        )

    assert not snapshots.exists()


def test_command_executes_private_verified_snapshot_when_source_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = tmp_path / "trusted" / "command"
    _secure_write(command, b"#!/bin/sh\nprintf 'verified-command\\n'\n")
    command.chmod(0o500)
    bridge = _bridge(tmp_path, _command_policy(command))
    bridge.config = replace(bridge.config, trusted_executable_roots=(command.parent,))
    real_popen = __import__("subprocess").Popen

    def replace_before_popen(argv, **kwargs):
        command.rename(command.with_name("command-held"))
        _secure_write(command, b"#!/bin/sh\nprintf 'replacement-command\\n'\n")
        command.chmod(0o500)
        assert Path(argv[0]) != command
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(bridge, "_popen_factory", replace_before_popen)
    target = {"type": "command", "executable": str(command), "args": ["hello"]}
    _, result = bridge.execute_payload(_payload(bridge, target))
    assert result["status"] == "success", result["error_code"]
    assert result["stdout"] == "verified-command\n"
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin Mach-O regression")
def test_darwin_macho_which_executes_from_private_snapshot(tmp_path: Path) -> None:
    command = Path("/usr/bin/which")
    bridge = _bridge(tmp_path, _command_policy(command, arg="sh"))
    bridge.config = replace(bridge.config, required_executable_owner_uid=0)
    target = {"type": "command", "executable": str(command), "args": ["sh"]}

    status, result = bridge.execute_payload(_payload(bridge, target))

    assert status == 200
    assert result["status"] == "success", result["error_code"]
    assert result["exit_code"] == 0
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


@pytest.mark.parametrize("failure", ["sign", "verify", "timeout"])
def test_macho_codesign_failure_is_safe_and_cleans_snapshot(
    tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = tmp_path / "trusted" / "command"
    _secure_write(command, b"\xcf\xfa\xed\xfe" + b"fixture")
    command.chmod(0o500)

    def failing(argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
        operation = argv[1]
        if failure == "timeout" and operation == "--force":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        failed_operation = "--force" if failure == "sign" else "--verify"
        return subprocess.CompletedProcess(argv, 1 if operation == failed_operation else 0)

    bridge = _bridge(tmp_path, _command_policy(command), codesign_runner=failing)
    bridge.config = replace(bridge.config, trusted_executable_roots=(command.parent,))
    monkeypatch.setattr(bridge, "_is_macho", lambda source: True)
    monkeypatch.setattr(
        bridge, "_verify_codesign_executable", lambda: Path("/usr/bin/codesign")
    )
    target = {"type": "command", "executable": str(command), "args": ["hello"]}

    _, result = bridge.execute_payload(_payload(bridge, target))

    assert result["error_code"] == "host_command_signing_failed"
    assert result["stderr"] == ""
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


def test_macho_snapshot_replacement_after_signing_is_rejected_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = tmp_path / "trusted" / "command"
    _secure_write(command, b"\xcf\xfa\xed\xfe" + b"fixture")
    command.chmod(0o500)
    replacement_target = tmp_path / "replacement"
    _secure_write(replacement_target, b"replacement")

    def replacing(argv, **kwargs):
        snapshot = Path(argv[-1])
        if argv[1] == "--force":
            snapshot.unlink()
            snapshot.symlink_to(replacement_target)
        return subprocess.CompletedProcess(argv, 0)

    bridge = _bridge(tmp_path, _command_policy(command), codesign_runner=replacing)
    bridge.config = replace(bridge.config, trusted_executable_roots=(command.parent,))
    monkeypatch.setattr(bridge, "_is_macho", lambda source: True)
    monkeypatch.setattr(
        bridge, "_verify_codesign_executable", lambda: Path("/usr/bin/codesign")
    )
    target = {"type": "command", "executable": str(command), "args": ["hello"]}

    _, result = bridge.execute_payload(_payload(bridge, target))

    assert result["error_code"] == "host_command_snapshot_invalid"
    assert replacement_target.read_bytes() == b"replacement"
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


def test_trusted_executable_root_swap_is_rejected_before_open(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    command = trusted / "command"
    _secure_write(command, b"#!/bin/sh\nexit 0\n")
    command.chmod(0o500)
    bridge = _bridge(tmp_path, _command_policy(command))
    bridge.config = replace(bridge.config, trusted_executable_roots=(trusted,))
    bridge._validate_trusted_roots()
    trusted.rename(tmp_path / "trusted-held")
    attacker = tmp_path / "attacker"
    _secure_write(attacker / "command", b"#!/bin/sh\nexit 0\n")
    (attacker / "command").chmod(0o500)
    trusted.symlink_to(attacker, target_is_directory=True)
    target = {"type": "command", "executable": str(command), "args": ["hello"]}
    _, result = bridge.execute_payload(_payload(bridge, target))
    assert result["error_code"] == "host_executable_denied"


def test_policy_snapshot_is_a_global_concurrency_ceiling(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    command = tmp_path / "trusted" / "command"
    _secure_write(
        command,
        f"#!/bin/sh\ntouch {marker!s}\nsleep 1\n".encode(),
    )
    command.chmod(0o500)
    bridge = _bridge(tmp_path, _command_policy(command), max_concurrency=2)
    bridge.config = replace(bridge.config, trusted_executable_roots=(command.parent,))
    target = {"type": "command", "executable": str(command), "args": ["hello"]}
    first: list[dict[str, object]] = []
    thread = threading.Thread(
        target=lambda: first.append(bridge.execute_payload(_payload(bridge, target))[1])
    )
    thread.start()
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    status, second = bridge.execute_payload(_payload(bridge, target))
    assert status == 409
    assert second["error_code"] == "host_bridge_busy"
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_http_authentication_precedes_oversize_body_processing(tmp_path: Path) -> None:
    echo = Path("/bin/echo").resolve(strict=True)
    bridge = _bridge(tmp_path, _command_policy(echo))
    server = make_server(bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request(
            "POST",
            "/v1/execute",
            body=b"",
            headers={
                "X-N-Agent-Host-Token": "wrong",
                "Content-Type": "application/json",
                "Content-Length": str(bridge.config.max_request_bytes + 1),
            },
        )
        response = connection.getresponse()
        assert response.status == 401
        assert json.loads(response.read())["error_code"] == "host_bridge_auth_failed"

        connection.request(
            "POST",
            "/private-undisclosed-route",
            body=b"",
            headers={"X-N-Agent-Host-Token": "wrong", "Content-Length": "0"},
        )
        unknown = connection.getresponse()
        assert unknown.status == 401
        assert json.loads(unknown.read())["error_code"] == "host_bridge_auth_failed"
    finally:
        server.shutdown()
        server.server_close()


def test_authenticated_partial_body_times_out_without_hanging_shutdown(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, _command_policy(Path("/bin/echo").resolve(strict=True)))
    server = make_server(bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(server.server_address, timeout=2)
    try:
        client.sendall(
            b"POST /v1/execute HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            + b"X-N-Agent-Host-Token: " + TOKEN + b"\r\n"
            + b"Content-Type: application/json\r\n"
            + b"Content-Length: 100\r\n\r\n{}"
        )
        time.sleep(0.05)
        started = time.monotonic()
        server.shutdown()
        server.server_close()
        assert time.monotonic() - started < 1
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 400
        assert json.loads(response.read())["error_code"] == "host_bridge_invalid_request"
    finally:
        client.close()
        server.shutdown()
        server.server_close()


def test_slow_drip_body_is_stopped_by_absolute_deadline(tmp_path: Path) -> None:
    bridge = _bridge(
        tmp_path,
        _command_policy(Path("/bin/echo").resolve(strict=True)),
        request_read_timeout_seconds=0.25,
    )
    server = make_server(bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = socket.create_connection(server.server_address, timeout=2)
    started = time.monotonic()
    try:
        client.sendall(
            b"POST /v1/execute HTTP/1.1\r\nHost: localhost\r\n"
            + b"X-N-Agent-Host-Token: " + TOKEN + b"\r\n"
            + b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\n{}"
        )
        for _ in range(6):
            time.sleep(0.07)  # below the old per-operation timeout
            try:
                client.sendall(b" ")
            except OSError:
                break
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 400
        assert json.loads(response.read())["error_code"] == "host_bridge_invalid_request"
        assert time.monotonic() - started < 1
    finally:
        client.close()
        server.shutdown()
        server.server_close()


def test_absolute_header_deadline_and_handler_cap(tmp_path: Path) -> None:
    bridge = _bridge(
        tmp_path,
        _command_policy(Path("/bin/echo").resolve(strict=True)),
        request_read_timeout_seconds=0.25,
        max_http_handler_threads=1,
    )
    server = make_server(bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    slow = socket.create_connection(server.server_address, timeout=2)
    excess = socket.create_connection(server.server_address, timeout=2)
    try:
        slow.sendall(b"G")
        time.sleep(0.05)
        started = time.monotonic()
        excess.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
        try:
            rejected = excess.recv(1)
        except ConnectionResetError:
            rejected = b""
        assert rejected == b""
        assert time.monotonic() - started < 0.5

        for byte in b"ET /healthz":
            time.sleep(0.07)
            try:
                slow.sendall(bytes([byte]))
            except OSError:
                break
        try:
            closed = slow.recv(1)
        except ConnectionResetError:
            closed = b""
        assert closed == b""
        assert time.monotonic() - started < 1.5
    finally:
        slow.close()
        excess.close()
        shutdown_started = time.monotonic()
        server.shutdown()
        server.server_close()
        assert time.monotonic() - shutdown_started < 1


def test_header_watchdog_identity_survives_reused_fd_key(tmp_path: Path) -> None:
    bridge = _bridge(
        tmp_path,
        _command_policy(Path("/bin/echo").resolve(strict=True)),
        request_read_timeout_seconds=1,
    )
    server = make_server(bridge)

    class SameFdConnection:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def fileno(self) -> int:
            return 42

        def shutdown(self, how: int) -> None:
            self.shutdown_calls += 1

    old = SameFdConnection()
    replacement = SameFdConnection()
    old_token = server._start_header_deadline(old)  # type: ignore[attr-defined,arg-type]
    replacement_token = server._start_header_deadline(  # type: ignore[attr-defined,arg-type]
        replacement
    )
    try:
        server._cancel_header_deadline(old, old_token)  # type: ignore[attr-defined,arg-type]
        assert replacement in server._header_timers  # type: ignore[attr-defined,operator]
        server._expire_header(old, old_token)  # type: ignore[attr-defined,arg-type]
        assert replacement in server._header_timers  # type: ignore[attr-defined,operator]
        assert replacement.shutdown_calls == 0
    finally:
        server._cancel_header_deadline(  # type: ignore[attr-defined,arg-type]
            replacement, replacement_token
        )
        server.server_close()


def test_executes_exact_command_as_argv_and_rejects_policy_drift_before_start(
    tmp_path: Path,
) -> None:
    executable = Path("/usr/bin/true").resolve(strict=True)
    starts = 0
    import subprocess

    def counted(*args, **kwargs):
        nonlocal starts
        starts += 1
        assert kwargs["shell"] is False
        assert Path(args[0][0]).name.startswith("host-command-")
        assert args[0][1:] == ["hello"]
        return subprocess.Popen(*args, **kwargs)

    bridge = _bridge(tmp_path, _command_policy(executable), popen_factory=counted)
    bridge.config = replace(bridge.config, required_executable_owner_uid=0)
    payload = _payload(
        bridge, {"type": "command", "executable": str(executable), "args": ["hello"]}
    )
    status, result = bridge.execute_payload(payload)
    assert status == 200
    assert result["exit_code"] == 0
    assert result["status"] == "success"
    assert result["stdout"] == ""
    assert starts == 1

    payload["n_agent_policy_version"] = "stale"
    status, result = bridge.execute_payload(payload)
    assert status == 409
    assert result["error_code"] == "host_policy_version_mismatch"
    assert starts == 1


def test_skill_hash_symlink_private_snapshot_and_cleanup(tmp_path: Path) -> None:
    source = b"print('snapshot-ok')\n"
    bridge = _bridge(tmp_path, _skill_policy(source))
    script = Path(bridge.config.skills_root) / "camera" / "scripts" / "run.py"
    _secure_write(script, source)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    status, result = bridge.execute_payload(_payload(bridge, target))
    assert status == 200
    assert result["stdout"] == "snapshot-ok\n"
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []

    script.unlink()
    outside = tmp_path / "outside.py"
    _secure_write(outside, source)
    script.symlink_to(outside)
    status, result = bridge.execute_payload(_payload(bridge, target))
    assert status == 200
    assert result["error_code"] == "skill_script_path_denied"
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


def test_skill_python_isolated_path_blocks_cwd_module_but_keeps_stdlib_and_cwd(
    tmp_path: Path,
) -> None:
    source = b"""import json, os, sys
assert '' not in sys.path
assert os.getcwd() not in sys.path
assert json.loads('{\"ok\": true}')['ok'] is True
assert open('relative.txt', encoding='utf-8').read() == 'cwd-ok'
try:
    import shadow_probe
except ModuleNotFoundError:
    print('isolated-ok')
else:
    raise RuntimeError('writable cwd module loaded')
"""
    bridge = _bridge(tmp_path, _skill_policy(source))
    skill = Path(bridge.config.skills_root) / "camera"
    _secure_write(skill / "scripts" / "run.py", source)
    _secure_write(skill / "relative.txt", b"cwd-ok")
    marker = skill / "shadow-loaded"
    _secure_write(
        skill / "shadow_probe.py",
        f"open({str(marker)!r}, 'w').write('unsafe')\n".encode(),
    )
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    _, result = bridge.execute_payload(_payload(bridge, target))
    assert result["status"] == "success"
    assert result["stdout"] == "isolated-ok\n"
    assert not marker.exists()


def test_skill_executes_verified_bytes_when_source_is_replaced_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = b"print('verified-bytes')\n"
    bridge = _bridge(tmp_path, _skill_policy(approved))
    script = Path(bridge.config.skills_root) / "camera" / "scripts" / "run.py"
    _secure_write(script, approved)
    original_write = bridge._write_snapshot

    def replace_then_snapshot(source: bytes) -> Path:
        _secure_write(script, b"print('attacker-bytes')\n")
        return original_write(source)

    monkeypatch.setattr(bridge, "_write_snapshot", replace_then_snapshot)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(approved).hexdigest(),
        "args": [],
    }
    _, result = bridge.execute_payload(_payload(bridge, target))
    assert result["status"] == "success"
    assert result["stdout"] == "verified-bytes\n"
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


def test_openat_traversal_is_stable_when_intermediate_directory_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = b"print('held-directory')\n"
    bridge = _bridge(tmp_path, _skill_policy(approved))
    skill = Path(bridge.config.skills_root) / "camera"
    script = skill / "scripts" / "run.py"
    _secure_write(script, approved)
    attacker = tmp_path / "attacker-scripts"
    _secure_write(attacker / "run.py", b"print('attacker-directory')\n")
    real_open = __import__("os").open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "run.py" and dir_fd is not None and not swapped:
            swapped = True
            (skill / "scripts").rename(skill / "scripts-held")
            (skill / "scripts").symlink_to(attacker, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("app.infrastructure.host_terminal.bridge_server.os.open", swapping_open)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(approved).hexdigest(),
        "args": [],
    }
    _, result = bridge.execute_payload(_payload(bridge, target))
    assert swapped is True
    assert result["status"] == "success"
    assert result["stdout"] == "held-directory\n"


def test_skill_cwd_remains_fd_pinned_when_entire_skill_root_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b"from pathlib import Path\nprint(Path('identity.txt').read_text())\n"
    bridge = _bridge(tmp_path, _skill_policy(source))
    skill = Path(bridge.config.skills_root) / "camera"
    _secure_write(skill / "scripts" / "run.py", source)
    _secure_write(skill / "identity.txt", b"verified-directory")
    original_run = bridge._run_process

    def replace_then_run(*args, **kwargs):
        skill.rename(Path(bridge.config.skills_root) / "camera-held")
        _secure_write(skill / "identity.txt", b"attacker-directory")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(bridge, "_run_process", replace_then_run)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    _, result = bridge.execute_payload(_payload(bridge, target))
    assert result["status"] == "success"
    assert result["stdout"] == "verified-directory\n"


def test_skill_hash_mismatch_never_starts_process(tmp_path: Path) -> None:
    approved = b"print('approved')\n"
    starts = 0

    def forbidden(*args, **kwargs):
        nonlocal starts
        starts += 1
        raise AssertionError("must not start")

    bridge = _bridge(tmp_path, _skill_policy(approved), popen_factory=forbidden)
    script = Path(bridge.config.skills_root) / "camera" / "scripts" / "run.py"
    _secure_write(script, b"print('changed')\n")
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(approved).hexdigest(),
        "args": [],
    }
    _, result = bridge.execute_payload(_payload(bridge, target))
    assert result["error_code"] == "skill_script_hash_mismatch"
    assert starts == 0
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


def test_disconnect_terminates_process_group_and_cleans_snapshot(tmp_path: Path) -> None:
    source = b"import time\ntime.sleep(5)\n"
    bridge = _bridge(tmp_path, _skill_policy(source))
    script = Path(bridge.config.skills_root) / "camera" / "scripts" / "run.py"
    _secure_write(script, source)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    started = time.monotonic()
    _, result = bridge.execute_payload(
        _payload(bridge, target), disconnected=lambda: True
    )
    assert time.monotonic() - started < 2
    assert result["error_code"] == "host_execution_cancelled"
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


@pytest.mark.parametrize("failure", ["pipe_start", "disconnect_callback"])
def test_post_registration_exception_always_reaps_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import subprocess

    source = b"import time\ntime.sleep(5)\n"
    started_pids: list[int] = []

    def captured(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        started_pids.append(process.pid)
        return process

    bridge = _bridge(tmp_path, _skill_policy(source), popen_factory=captured)
    script = Path(bridge.config.skills_root) / "camera" / "scripts" / "run.py"
    _secure_write(script, source)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    disconnected = None
    if failure == "pipe_start":
        monkeypatch.setattr(
            "app.infrastructure.host_terminal.bridge_server._BoundedPipe.start",
            lambda self: (_ for _ in ()).throw(RuntimeError("injected")),
        )
    else:
        disconnected = lambda: (_ for _ in ()).throw(RuntimeError("injected"))
    _, result = bridge.execute_payload(
        _payload(bridge, target), disconnected=disconnected
    )
    assert result["error_code"] == "host_bridge_internal_error"
    assert started_pids and _pid_absent(started_pids[0])
    assert bridge._active_processes == {}
    assert bridge.healthy
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


def _pid_absent(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def test_timeout_kills_child_that_ignores_term(tmp_path: Path) -> None:
    source = b"""import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(10)'])
pathlib.Path('child.pid').write_text(str(child.pid))
time.sleep(10)
"""
    bridge = _bridge(tmp_path, _skill_policy(source))
    skill = Path(bridge.config.skills_root) / "camera"
    _secure_write(skill / "scripts" / "run.py", source)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    _, result = bridge.execute_payload(
        _payload(bridge, target, timeout_seconds=1)
    )
    child_pid = int((skill / "child.pid").read_text())
    assert result["error_code"] == "host_execution_timeout"
    assert _pid_absent(child_pid)
    assert bridge.healthy


def test_shutdown_terminates_active_process_group_and_refuses_new_work(
    tmp_path: Path,
) -> None:
    source = b"""import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(10)'])
pathlib.Path('shutdown-child.pid').write_text(str(child.pid))
time.sleep(10)
"""
    bridge = _bridge(tmp_path, _skill_policy(source))
    skill = Path(bridge.config.skills_root) / "camera"
    _secure_write(skill / "scripts" / "run.py", source)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    result: list[dict[str, object]] = []

    def execute() -> None:
        _, response = bridge.execute_payload(_payload(bridge, target, timeout_seconds=5))
        result.append(response)

    thread = threading.Thread(target=execute)
    thread.start()
    pid_file = skill / "shutdown-child.pid"
    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text())
    bridge.shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert _pid_absent(child_pid)
    assert bridge.healthy is False
    status, denied = bridge.execute_payload(_payload(bridge, target))
    assert status == 503
    assert denied["error_code"] == "host_bridge_unhealthy"


def test_truncation_timeout_cleanup_and_immediate_busy(tmp_path: Path) -> None:
    source = b"import time\nprint('x' * 100)\ntime.sleep(1)\n"
    bridge = _bridge(tmp_path, _skill_policy(source))
    script = Path(bridge.config.skills_root) / "camera" / "scripts" / "run.py"
    _secure_write(script, source)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": hashlib.sha256(source).hexdigest(),
        "args": [],
    }
    first_result: list[dict[str, object]] = []

    def run() -> None:
        _, result = bridge.execute_payload(
            _payload(
                bridge,
                target,
                timeout_seconds=1,
                max_stdout_bytes=8,
                max_stderr_bytes=8,
            )
        )
        first_result.append(result)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.1)
    status, busy = bridge.execute_payload(_payload(bridge, target))
    assert status == 409
    assert busy["error_code"] == "host_bridge_busy"
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert first_result[0]["status"] == "timeout"
    assert first_result[0]["stdout_truncated"] is True
    assert len(first_result[0]["stdout"].encode()) <= 8
    assert list(Path(bridge.config.snapshot_root).iterdir()) == []


def test_health_discloses_no_policy_path_or_version(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, _command_policy(Path("/bin/echo").resolve(strict=True)))
    payload = bridge.health_payload()
    serialized = json.dumps(payload)
    assert payload == {"status": "ok"}
    assert "v1" not in serialized
    assert str(tmp_path) not in serialized

    server = make_server(bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        headers = json.dumps(dict(response.getheaders())).lower()
        response.read()
        assert "server" not in headers
        assert "nagenthostbridge" not in headers
        assert "v1" not in headers
        assert str(tmp_path).lower() not in headers
    finally:
        server.shutdown()
        server.server_close()
