from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import threading

import pytest


def test_browser_host_parser_has_required_public_flags() -> None:
    from app.interfaces.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "browser-host",
            "--token-path",
            "/a",
            "--sqlite-path",
            "/b",
            "--profile-root",
            "/c",
            "--check",
        ]
    )
    assert args.command == "browser-host"
    assert args.port == 8766
    assert args.check is True


def test_check_only_validates_and_prints_stable_ok(
    monkeypatch, capsys
) -> None:
    from app import browser_host_runtime
    from app.interfaces.cli.commands import browser_host

    captured = []
    monkeypatch.setattr(
        browser_host_runtime, "validate_browser_host_config", captured.append
    )
    monkeypatch.setattr(
        browser_host_runtime,
        "serve_browser_host",
        lambda config: (_ for _ in ()).throw(AssertionError("must not start")),
    )
    args = argparse.Namespace(
        token_path="/token",
        sqlite_path="/db",
        profile_root="/profiles",
        chrome_executable=None,
        port=8766,
        check=True,
    )
    assert browser_host.run(args) == 0
    assert len(captured) == 1
    assert capsys.readouterr().out == "ok\n"


def _secure_inputs(tmp_path: Path):
    token = tmp_path / "token"
    token.write_text("a" * 32 + "\n", encoding="utf-8")
    token.chmod(0o600)
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE browser_sessions (
                id TEXT PRIMARY KEY,
                n_agent_session_id TEXT NOT NULL,
                backend_type TEXT NOT NULL,
                status TEXT NOT NULL,
                profile_ref TEXT NOT NULL
            );
            CREATE TABLE browser_host_grants (
                browser_session_id TEXT PRIMARY KEY,
                n_agent_session_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
    database.chmod(0o600)
    profiles = tmp_path / "profiles"
    profiles.mkdir(mode=0o700)
    profiles.chmod(0o700)
    chrome = tmp_path / "Chrome"
    chrome.write_bytes(b"executable")
    chrome.chmod(0o700)
    from app.browser_host_runtime import BrowserHostConfig

    return BrowserHostConfig(
        token_path=token,
        sqlite_path=database,
        profile_root=profiles,
        chrome_executable=chrome,
        port=_unused_port(),
    )


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_check_validates_all_paths_and_releases_probe_socket(
    tmp_path: Path, monkeypatch
) -> None:
    from app.browser_host_runtime import (
        validate_browser_host_config,
    )

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    validate_browser_host_config(config)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", config.port))


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("token_mode", "browser_host_token_invalid"),
        ("token_symlink", "browser_host_token_invalid"),
        ("sqlite_schema", "browser_host_sqlite_invalid"),
        ("sqlite_mode", "browser_host_sqlite_invalid"),
        ("profile_mode", "browser_host_profile_invalid"),
        ("chrome_mode", "browser_host_chrome_invalid"),
    ],
)
def test_check_validator_matrix_is_stable_and_redacted(
    tmp_path: Path, monkeypatch, mutation: str, code: str
) -> None:
    from app.browser_host_runtime import (
        BrowserHostCommandError,
        validate_browser_host_config,
    )

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    if mutation == "token_mode":
        config.token_path.chmod(0o644)
    elif mutation == "token_symlink":
        actual = config.token_path
        link = tmp_path / "token-link"
        link.symlink_to(actual)
        object.__setattr__(config, "token_path", link)
    elif mutation == "sqlite_schema":
        config.sqlite_path.unlink()
        sqlite3.connect(config.sqlite_path).close()
    elif mutation == "sqlite_mode":
        config.sqlite_path.chmod(0o666)
    elif mutation == "profile_mode":
        config.profile_root.chmod(0o755)
    elif mutation == "chrome_mode":
        config.chrome_executable.chmod(0o722)  # type: ignore[union-attr]
    with pytest.raises(BrowserHostCommandError) as caught:
        validate_browser_host_config(config)
    assert caught.value.error_code == code
    assert str(tmp_path) not in str(caught.value)


def test_check_accepts_stock_macos_chrome_permissions(
    tmp_path: Path, monkeypatch
) -> None:
    """Stock macOS Chrome ships at 0o775 (group-writable; group is the trusted
    admin/staff). Validation must accept it -- rejecting group-write (0o020)
    would refuse every real Chrome install. Guards against re-tightening to
    0o022; other-writable (0o002) is still refused per the matrix above."""
    from app.browser_host_runtime import validate_browser_host_config

    config = _secure_inputs(tmp_path)
    config.chrome_executable.chmod(0o775)  # type: ignore[union-attr]
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    validate_browser_host_config(config)  # must not raise


@pytest.mark.parametrize(
    "raw",
    [
        b"contains space",
        b"contains\ttab",
        b"contains\x00nul",
        b"contains\x7fdel",
        "contains-é".encode("utf-8"),
    ],
)
def test_check_rejects_non_header_safe_token_bytes(
    tmp_path: Path, monkeypatch, raw: bytes
) -> None:
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    config.token_path.write_bytes(raw)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    with pytest.raises(browser_host_runtime.BrowserHostRuntimeError) as caught:
        browser_host_runtime.validate_browser_host_config(config)
    assert caught.value.error_code == "browser_host_token_invalid"
    assert raw.decode("utf-8", errors="ignore") not in str(caught.value)


@pytest.mark.parametrize(
    ("length", "accepted"), [(1, False), (31, False), (32, True)]
)
def test_check_enforces_minimum_token_length(
    tmp_path: Path, monkeypatch, length: int, accepted: bool
) -> None:
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    config.token_path.write_bytes(b"!" * length + b"\n")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    if accepted:
        browser_host_runtime.validate_browser_host_config(config)
    else:
        with pytest.raises(
            browser_host_runtime.BrowserHostRuntimeError
        ) as caught:
            browser_host_runtime.validate_browser_host_config(config)
        assert caught.value.error_code == "browser_host_token_invalid"


def test_server_startup_rejects_weak_token_before_composition(
    tmp_path: Path, monkeypatch
) -> None:
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    config.token_path.write_bytes(b"!")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    with pytest.raises(browser_host_runtime.BrowserHostRuntimeError) as caught:
        browser_host_runtime.serve_browser_host(config)
    assert caught.value.error_code == "browser_host_token_invalid"


def test_check_rejects_non_macos_without_constructing_controller(
    tmp_path: Path, monkeypatch
) -> None:
    from app.browser_host_runtime import (
        BrowserHostCommandError,
        validate_browser_host_config,
    )

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(BrowserHostCommandError) as caught:
        validate_browser_host_config(config)
    assert caught.value.error_code == "browser_host_platform_unsupported"


def test_check_uses_controller_profile_overlap_invariant(
    tmp_path: Path, monkeypatch
) -> None:
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_host_runtime, "_DEFAULT_PROFILE_ROOTS", (config.profile_root,)
    )
    with pytest.raises(browser_host_runtime.BrowserHostRuntimeError) as caught:
        browser_host_runtime.validate_browser_host_config(config)
    assert caught.value.error_code == "browser_host_profile_invalid"


def test_chrome_open_identity_mismatch_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    real_fstat = os.fstat

    def mismatched(fd):
        metadata = real_fstat(fd)
        values = list(metadata)
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", mismatched)
    with pytest.raises(browser_host_runtime.BrowserHostRuntimeError) as caught:
        browser_host_runtime._validated_chrome_executable(
            config.chrome_executable
        )
    assert caught.value.error_code == "browser_host_chrome_invalid"


def test_port_and_required_argument_boundaries() -> None:
    from app.interfaces.cli.main import build_parser

    parser = build_parser()
    for value in ("0", "65536", "true"):
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "browser-host",
                    "--token-path",
                    "/a",
                    "--sqlite-path",
                    "/b",
                    "--profile-root",
                    "/c",
                    "--port",
                    value,
                ]
            )
    with pytest.raises(SystemExit):
        parser.parse_args(["browser-host", "--check"])


def test_eaddrinuse_has_separate_stable_mapping(monkeypatch, capsys) -> None:
    from app import browser_host_runtime
    from app.interfaces.cli.commands import browser_host

    monkeypatch.setattr(
        browser_host_runtime,
        "serve_browser_host",
        lambda config: (_ for _ in ()).throw(
            browser_host_runtime.BrowserHostRuntimeError(
                "browser_host_address_in_use"
            )
        ),
    )
    args = argparse.Namespace(
        token_path="/token",
        sqlite_path="/db",
        profile_root="/profiles",
        chrome_executable=None,
        port=8766,
        check=False,
    )
    assert browser_host.run(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: browser_host_address_in_use\n"


def test_errors_never_echo_sensitive_paths_token_endpoint_or_argv(
    monkeypatch, capsys
) -> None:
    from app import browser_host_runtime
    from app.interfaces.cli.commands import browser_host

    sensitive = "/secret/token chrome --remote-debugging-port=4444"
    monkeypatch.setattr(
        browser_host_runtime,
        "serve_browser_host",
        lambda config: (_ for _ in ()).throw(RuntimeError(sensitive)),
    )
    args = argparse.Namespace(
        token_path="/secret/token",
        sqlite_path="/secret/db",
        profile_root="/secret/profile",
        chrome_executable="/secret/Chrome",
        port=4444,
        check=False,
    )
    assert browser_host.run(args) == 1
    rendered = capsys.readouterr().err
    assert rendered == "error: browser_host_internal_error\n"
    for fragment in ("/secret", "4444", "--remote-debugging"):
        assert fragment not in rendered


def test_builtin_browser_host_wins_over_plugin_same_name(caplog) -> None:
    from app.application.plugin_service import PluginCliCommand
    from app.interfaces.cli.main import build_parser

    called = []
    plugin = PluginCliCommand(
        plugin_key="evil",
        name="browser-host",
        help="replace builtin",
        description="replace builtin",
        setup_fn=lambda parser: called.append(parser),
        handler_fn=lambda args: 99,
        registration_index=0,
    )
    parser = build_parser(plugin_commands=[plugin])
    args = parser.parse_args(
        [
            "browser-host",
            "--token-path",
            "/a",
            "--sqlite-path",
            "/b",
            "--profile-root",
            "/c",
            "--check",
        ]
    )
    assert called == []
    assert args.command == "browser-host"
    assert "conflicts with builtin" in caplog.text


def test_main_dispatch_does_not_call_create_app(monkeypatch) -> None:
    import app.main
    import importlib
    cli_main_module = importlib.import_module("app.interfaces.cli.main")
    from app.interfaces.cli.commands import browser_host

    monkeypatch.setattr(
        app.main,
        "collect_plugin_cli_commands",
        lambda: (_ for _ in ()).throw(
            AssertionError("plugin discovery forbidden")
        ),
    )
    monkeypatch.setattr(
        app.main,
        "create_app",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_app forbidden")
        ),
    )
    monkeypatch.setattr(browser_host, "run", lambda args: 17)
    monkeypatch.setitem(cli_main_module._DISPATCH, "browser-host", browser_host.run)
    assert (
        cli_main_module.main(
            [
                "browser-host",
                "--token-path",
                "/a",
                "--sqlite-path",
                "/b",
                "--profile-root",
                "/c",
            ]
        )
        == 17
    )


def test_signal_cleanup_is_idempotent_and_handlers_restored(
    tmp_path: Path, monkeypatch
) -> None:
    from app.infrastructure.browser import host_bridge as bridge_module
    from app.infrastructure.browser import host_bridge_server as server_module
    from app.infrastructure.browser import host_chrome_controller as controller_module
    from app.infrastructure.browser.host_protocol import (
        HOST_CDP_MAX_SCREENSHOT_BYTES,
        max_json_response_bytes,
    )
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr(browser_host_runtime, "validate_browser_host_config", lambda *a, **k: None)
    monkeypatch.setattr(browser_host_runtime, "_validated_chrome_executable", lambda path: path)
    cleanup: list[str] = []
    captured_configs = {}

    class Controller:
        owner_thread_alive = False

        def __init__(self, config):
            cleanup.append("controller:init")
            captured_configs["controller"] = config

        def shutdown(self) -> bool:
            cleanup.append("controller:shutdown")
            return True

    class Bridge:
        def __init__(self, *a, **k):
            cleanup.append("bridge:init")
            self.config = type(
                "C",
                (),
                {
                    "bind_host": "127.0.0.1",
                    "port": config.port,
                    "max_request_bytes": 100,
                },
            )()

        def shutdown(self) -> bool:
            cleanup.append("bridge:shutdown")
            return True

    class Server:
        cleanup_confirmed = True
        requested = threading.Event()

        def serve_forever(self):
            lock = threading.Lock()
            lock.acquire()
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    handler = signal.getsignal(signum)
                    handler(signum, None)
            finally:
                lock.release()

        def request_shutdown(self) -> None:
            self.requested.set()

        def shutdown(self) -> None:
            if "server:shutdown" not in cleanup:
                cleanup.append("server:shutdown")

        def server_close(self):
            if "server:close" not in cleanup:
                cleanup.append("server:close")

    monkeypatch.setattr(controller_module, "HostChromeController", Controller)
    monkeypatch.setattr(bridge_module, "HostBridge", Bridge)
    def make_server(*args, **kwargs):
        captured_configs["server"] = args[1]
        return Server()

    monkeypatch.setattr(server_module, "make_server", make_server)
    previous = signal.getsignal(signal.SIGTERM)
    assert browser_host_runtime.serve_browser_host(config) == 0
    assert signal.getsignal(signal.SIGTERM) is previous
    assert cleanup.count("server:shutdown") == 1
    assert cleanup[-1] == "server:close"
    assert (
        captured_configs["controller"].max_screenshot_bytes
        == HOST_CDP_MAX_SCREENSHOT_BYTES
    )
    assert (
        captured_configs["server"].max_response_bytes
        == max_json_response_bytes(HOST_CDP_MAX_SCREENSHOT_BYTES)
    )


def test_partial_initialization_cleans_controller_when_bridge_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from app.infrastructure.browser import host_bridge as bridge_module
    from app.infrastructure.browser import host_chrome_controller as controller_module
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr(browser_host_runtime, "validate_browser_host_config", lambda *a, **k: None)
    monkeypatch.setattr(browser_host_runtime, "_validated_chrome_executable", lambda path: path)
    cleaned = []

    class Controller:
        def __init__(self, config):
            pass

        def shutdown(self) -> bool:
            cleaned.append("controller")
            return True

    monkeypatch.setattr(controller_module, "HostChromeController", Controller)
    monkeypatch.setattr(
        bridge_module,
        "HostBridge",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("/private/db")),
    )
    with pytest.raises(browser_host_runtime.BrowserHostRuntimeError) as caught:
        browser_host_runtime.serve_browser_host(config)
    assert caught.value.error_code == "browser_host_start_failed"
    assert cleaned == ["controller"]


def test_unconfirmed_server_cleanup_returns_nonzero(
    tmp_path: Path, monkeypatch
) -> None:
    from app.infrastructure.browser import host_bridge as bridge_module
    from app.infrastructure.browser import host_bridge_server as server_module
    from app.infrastructure.browser import host_chrome_controller as controller_module
    from app import browser_host_runtime

    config = _secure_inputs(tmp_path)
    monkeypatch.setattr(
        browser_host_runtime, "validate_browser_host_config", lambda *a, **k: None
    )
    monkeypatch.setattr(
        browser_host_runtime, "_validated_chrome_executable", lambda path: path
    )

    class Controller:
        owner_thread_alive = False

        def __init__(self, config):
            pass

        def shutdown(self) -> bool:
            return True

    class Bridge:
        def __init__(self, *args, **kwargs):
            pass

        def shutdown(self) -> bool:
            return False

    class Server:
        cleanup_confirmed = False

        def serve_forever(self):
            pass

        def shutdown(self) -> None:
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(controller_module, "HostChromeController", Controller)
    monkeypatch.setattr(bridge_module, "HostBridge", Bridge)
    monkeypatch.setattr(
        server_module, "make_server", lambda *args, **kwargs: Server()
    )
    assert browser_host_runtime.serve_browser_host(config) == 1
