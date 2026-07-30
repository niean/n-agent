"""Browser Host Bridge runtime composition and validation."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import platform
import signal
import socket
import stat
from typing import Any

from app.domain.browser_policy import BROWSER_POLICY_VERSION


_DEFAULT_CHROME_EXECUTABLES = (
    Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    ),
    Path(
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing"
    ),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)
_DEFAULT_PROFILE_ROOTS = (
    Path.home() / "Library/Application Support/Google/Chrome",
    Path.home()
    / "Library/Application Support/Google/Chrome for Testing",
    Path.home() / "Library/Application Support/Chromium",
)


class BrowserHostRuntimeError(RuntimeError):
    """Stable public runtime failure with no diagnostic path payload."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


BrowserHostCommandError = BrowserHostRuntimeError


@dataclass(frozen=True)
class BrowserHostConfig:
    token_path: Path
    sqlite_path: Path
    profile_root: Path
    chrome_executable: Path | None = None
    port: int = 8766

    def __post_init__(self) -> None:
        for value in (
            self.token_path,
            self.sqlite_path,
            self.profile_root,
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise BrowserHostCommandError(
                    "browser_host_config_invalid"
                )
        if (
            self.chrome_executable is not None
            and (
                not isinstance(self.chrome_executable, Path)
                or not self.chrome_executable.is_absolute()
            )
        ):
            raise BrowserHostCommandError("browser_host_config_invalid")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise BrowserHostCommandError("browser_host_port_invalid")


BrowserHostCommandConfig = BrowserHostConfig


def validate_browser_host_config(
    config: BrowserHostConfig, *, probe_port: bool = True
) -> None:
    """Run every startup validator without constructing the controller."""
    _ = BROWSER_POLICY_VERSION  # fixed internal policy, intentionally no CLI
    try:
        _validate_token_path(config.token_path)
        _validate_sqlite(config.sqlite_path)
        _validate_profile_root(config.profile_root)
        _validated_chrome_executable(config.chrome_executable)
        if probe_port:
            _probe_port(config.port)
    except BrowserHostCommandError:
        raise
    except Exception:
        raise BrowserHostCommandError("browser_host_check_failed") from None


def serve_browser_host(config: BrowserHostConfig) -> int:
    """Compose resources, serve on the main thread, and clean up in reverse."""
    from app.infrastructure.browser.host_bridge import (
        HostBridge,
        HostBridgeConfig,
    )
    from app.infrastructure.browser.host_bridge_server import (
        BrowserHostBridgeServerConfig,
        make_server,
    )
    from app.infrastructure.browser.host_chrome_controller import (
        HostChromeController,
        HostChromeControllerConfig,
    )
    from app.infrastructure.browser.host_grant_store import (
        SqliteBrowserAuthorizationStore,
    )
    from app.infrastructure.browser.host_protocol import (
        max_json_response_bytes,
    )

    validate_browser_host_config(config, probe_port=False)
    executable = _validated_chrome_executable(config.chrome_executable)
    controller: Any | None = None
    bridge: Any | None = None
    server: Any | None = None
    old_handlers: dict[int, Any] = {}
    try:
        store = SqliteBrowserAuthorizationStore(config.sqlite_path)
        controller_config = HostChromeControllerConfig(
            profile_root=config.profile_root,
            chrome_executable=executable,
        )
        controller = HostChromeController(controller_config)
        bridge = HostBridge(
            HostBridgeConfig(
                token_path=config.token_path,
                bind_host="127.0.0.1",
                port=config.port,
            ),
            authorization_store=store,
            cdp_controller=controller,
        )
        try:
            server = make_server(
                bridge,
                BrowserHostBridgeServerConfig(
                    bind_host="127.0.0.1",
                    port=config.port,
                    max_response_bytes=max_json_response_bytes(),
                ),
            )
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise BrowserHostCommandError(
                    "browser_host_address_in_use"
                ) from None
            raise BrowserHostCommandError(
                "browser_host_start_failed"
            ) from None

        def stop_handler(signum: int, frame: Any) -> None:
            del signum, frame
            server.request_shutdown()

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop_handler)
        server.serve_forever()
        cleanup_confirmed = (
            server.cleanup_confirmed is True
            and controller.owner_thread_alive is False
        )
        return 0 if cleanup_confirmed else 1
    except BrowserHostCommandError:
        raise
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise BrowserHostCommandError(
                "browser_host_address_in_use"
            ) from None
        raise BrowserHostCommandError("browser_host_start_failed") from None
    except Exception:
        raise BrowserHostCommandError("browser_host_start_failed") from None
    finally:
        for signum, previous in old_handlers.items():
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                pass
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        elif bridge is not None:
            try:
                bridge.shutdown()
            except Exception:
                pass
        elif controller is not None:
            try:
                controller.shutdown()
            except Exception:
                pass


def _validate_token_path(path: Path) -> None:
    from app.infrastructure.browser.host_cdp_backend import (
        HostCdpBackendError,
        load_secure_token,
    )

    try:
        _reject_symlink_components(path)
        load_secure_token(path)
    except (HostCdpBackendError, OSError, ValueError):
        raise BrowserHostCommandError("browser_host_token_invalid") from None


def _validate_sqlite(path: Path) -> None:
    from app.infrastructure.browser.host_grant_store import (
        BrowserAuthorizationStoreError,
        SqliteBrowserAuthorizationStore,
    )

    try:
        _reject_symlink_components(path)
        SqliteBrowserAuthorizationStore(path).load_authorization(
            "__browser_host_check__"
        )
    except (BrowserAuthorizationStoreError, OSError, ValueError):
        raise BrowserHostCommandError("browser_host_sqlite_invalid") from None


def _validate_profile_root(path: Path) -> None:
    try:
        _reject_symlink_components(path)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
        finally:
            os.close(fd)
        if (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise OSError
        normalized = _normalized_absolute(path)
        if any(
            _paths_overlap(
                normalized, _normalized_absolute(default_root)
            )
            for default_root in _DEFAULT_PROFILE_ROOTS
        ):
            raise OSError
    except (OSError, ValueError):
        raise BrowserHostCommandError(
            "browser_host_profile_invalid"
        ) from None


def _validated_chrome_executable(configured: Path | None) -> Path:
    if platform.system() != "Darwin":
        raise BrowserHostCommandError("browser_host_platform_unsupported")
    candidates = (
        (configured,) if configured is not None else _DEFAULT_CHROME_EXECUTABLES
    )
    for candidate in candidates:
        try:
            _reject_symlink_components(candidate)
            before = candidate.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid not in {0, os.geteuid()}
                # Reject other-writable (0o002) only. macOS Chrome ships at
                # 0o775 (group-writable, group is the trusted admin/staff);
                # rejecting group-write (0o020) would refuse every stock
                # Chrome install. Ownership (root/euid) + no-symlink + TOCTOU
                # checks above still bind the executable to a trusted owner.
                or stat.S_IMODE(before.st_mode) & 0o002
                or not os.access(candidate, os.X_OK)
            ):
                raise OSError
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(candidate, flags)
            try:
                opened = os.fstat(fd)
            finally:
                os.close(fd)
            if (
                (before.st_dev, before.st_ino)
                != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise OSError
            return candidate
        except (OSError, ValueError):
            continue
    raise BrowserHostCommandError("browser_host_chrome_invalid")


def _probe_port(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise BrowserHostCommandError(
                "browser_host_address_in_use"
            ) from None
        raise BrowserHostCommandError("browser_host_port_probe_failed") from None
    finally:
        probe.close()


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.normcase(os.path.abspath(os.fspath(path))))


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


__all__ = [
    "BrowserHostCommandConfig",
    "BrowserHostCommandError",
    "BrowserHostConfig",
    "BrowserHostRuntimeError",
    "serve_browser_host",
    "validate_browser_host_config",
]
