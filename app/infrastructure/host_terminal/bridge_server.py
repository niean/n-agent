"""Standard-library loopback server for restricted host process execution."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from app.domain.host_terminal import (
    HostCommandTarget,
    HostSkillScriptTarget,
    HostTerminalExecutionLimits,
)
from app.domain.host_terminal_policy import (
    HostTerminalPolicy,
    HostTerminalPolicyRequest,
    HostTerminalPolicySnapshot,
)
from app.infrastructure.host_terminal.http_client import AUTH_HEADER, load_secure_token
from app.infrastructure.host_terminal.policy_loader import HostTerminalPolicyLoader


_REQUEST_FIELDS = {
    "protocol_version",
    "request_id",
    "target",
    "n_agent_policy_version",
    "n_agent_content_digest",
    "limits",
}
_LIMIT_FIELDS = {
    "timeout_seconds",
    "max_stdout_bytes",
    "max_stderr_bytes",
    "max_concurrency",
}
_PINNED_CWD_BOOTSTRAP = (
    "import os,runpy,sys;"
    "fd=int(sys.argv[1]);script=sys.argv[2];"
    "sys.argv=[script,*sys.argv[3:]];"
    "os.fchdir(fd);runpy.run_path(script,run_name='__main__')"
)
_CODESIGN_PATH = Path("/usr/bin/codesign")
_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


@dataclass(frozen=True)
class HostTerminalBridgeConfig:
    policy_path: str | os.PathLike[str]
    token_path: str | os.PathLike[str]
    skills_root: str | os.PathLike[str]
    python_executable: str | os.PathLike[str]
    snapshot_root: str | os.PathLike[str]
    trusted_executable_roots: tuple[str | os.PathLike[str], ...]
    model_writable_roots: tuple[str | os.PathLike[str], ...] = ()
    bind_host: str = "127.0.0.1"
    port: int = 8765
    max_request_bytes: int = 262_144
    max_concurrency: int = 1
    terminate_grace_seconds: float = 1.0
    request_read_timeout_seconds: float = 5.0
    max_http_handler_threads: int = 16
    required_executable_owner_uid: int = 0
    codesign_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.bind_host != "127.0.0.1":
            raise ValueError("host_bridge_loopback_required")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 0 <= self.port <= 65535:
            raise ValueError("host_bridge_port_invalid")
        if (
            isinstance(self.max_request_bytes, bool)
            or not isinstance(self.max_request_bytes, int)
            or self.max_request_bytes <= 0
            or isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency <= 0
            or self.terminate_grace_seconds <= 0
            or self.request_read_timeout_seconds <= 0
            or isinstance(self.max_http_handler_threads, bool)
            or not isinstance(self.max_http_handler_threads, int)
            or self.max_http_handler_threads <= 0
            or isinstance(self.required_executable_owner_uid, bool)
            or not isinstance(self.required_executable_owner_uid, int)
            or self.required_executable_owner_uid < 0
            or isinstance(self.codesign_timeout_seconds, bool)
            or not isinstance(self.codesign_timeout_seconds, (int, float))
            or self.codesign_timeout_seconds <= 0
        ):
            raise ValueError("host_bridge_limits_invalid")
        if not self.trusted_executable_roots:
            raise ValueError("host_bridge_trusted_roots_required")


class HostTerminalBridge:
    """Owns local authorization and process lifecycle; it never trusts client paths."""

    def __init__(
        self,
        config: HostTerminalBridgeConfig,
        *,
        policy_loader: HostTerminalPolicyLoader | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        codesign_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.config = config
        self.policy_loader = policy_loader or HostTerminalPolicyLoader(config.policy_path)
        self._token = load_secure_token(Path(config.token_path))
        self._admission_lock = threading.Lock()
        self._active_executions = 0
        self._popen_factory = popen_factory
        self._codesign_runner = codesign_runner
        self._health_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._active_processes: dict[int, subprocess.Popen[bytes]] = {}
        self._shutting_down = False
        self._cleanup_uncertain = False
        self._healthy = self.policy_loader.refresh()
        self._verify_skills_root()
        self._validate_snapshot_root_overlap()
        self._prepare_snapshot_root()
        self._validate_authority_paths()
        self._validate_trusted_roots()
        self._cleanup_stale_snapshots()

    @property
    def healthy(self) -> bool:
        with self._health_lock:
            return (
                self._healthy
                and not self._shutting_down
                and not self._cleanup_uncertain
                and self.policy_loader.snapshot is not None
            )

    def authenticate(self, supplied: str | None) -> bool:
        if supplied is None:
            return False
        try:
            encoded = supplied.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(encoded, self._token)

    def refresh_policy(self) -> None:
        refreshed = self.policy_loader.refresh()
        with self._health_lock:
            if self.policy_loader.snapshot is None:
                self._healthy = False
            elif refreshed and not self._cleanup_uncertain:
                self._healthy = True

    def health_payload(self) -> dict[str, str]:
        return {"status": "ok" if self.healthy else "unhealthy"}

    def shutdown(self) -> None:
        """Refuse new work and synchronously eliminate every known process group."""
        with self._health_lock:
            self._shutting_down = True
            self._healthy = False
        with self._process_lock:
            processes = tuple(self._active_processes.values())
        for process in processes:
            if not self._terminate_process_group(process):
                self._mark_unhealthy()
        deadline = time.monotonic() + self.config.terminate_grace_seconds
        while time.monotonic() < deadline:
            with self._process_lock:
                if not self._active_processes:
                    return
            time.sleep(0.01)
        with self._process_lock:
            if any(_group_exists(pgid) for pgid in self._active_processes):
                self._mark_unhealthy()

    def execute_payload(
        self,
        payload: Any,
        *,
        disconnected: Callable[[], bool] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        started = time.monotonic()
        request_id = payload.get("request_id", "") if isinstance(payload, dict) else ""
        admitted = False
        try:
            self.refresh_policy()
            snapshot = self.policy_loader.snapshot
            if snapshot is None or not self.healthy:
                return 503, _response(
                    request_id, started, error_code="host_bridge_unhealthy"
                )
            with self._admission_lock:
                effective_limit = min(
                    self.config.max_concurrency, snapshot.limits.max_concurrency
                )
                if self._active_executions >= effective_limit:
                    return 409, _response(
                        request_id, started, error_code="host_bridge_busy"
                    )
                self._active_executions += 1
                admitted = True
            try:
                request = _parse_request(payload)
                request_id = request["request_id"]
            except (TypeError, ValueError):
                return 400, _response(
                    request_id, started, error_code="host_bridge_invalid_request"
                )
            if (
                request["n_agent_policy_version"] != snapshot.version
                or request["n_agent_content_digest"] != snapshot.content_digest
            ):
                return 409, _response(
                    request_id,
                    started,
                    error_code="host_policy_version_mismatch",
                )
            target = request["target"]
            limits = request["limits"]
            decision = HostTerminalPolicy(snapshot).evaluate(
                HostTerminalPolicyRequest(target=target, requested_limits=limits)
            )
            if not decision.allowed:
                return 403, _response(
                    request_id, started, error_code="host_target_not_allowed"
                )
            return 200, self._execute_authorized(
                request_id, target, limits, snapshot, started, disconnected
            )
        except Exception:
            return 500, _response(
                request_id, started, error_code="host_bridge_internal_error"
            )
        finally:
            if admitted:
                with self._admission_lock:
                    self._active_executions -= 1

    def _execute_authorized(
        self,
        request_id: str,
        target: HostCommandTarget | HostSkillScriptTarget,
        limits: HostTerminalExecutionLimits,
        snapshot: HostTerminalPolicySnapshot,
        started: float,
        disconnected: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        snapshot_path: Path | None = None
        command_snapshot_path: Path | None = None
        skill_cwd_fd: int | None = None
        try:
            if isinstance(target, HostCommandTarget):
                command_snapshot_path = self._snapshot_executable(
                    Path(target.executable)
                )
                argv = [str(command_snapshot_path), *target.args]
                cwd = "/"
            else:
                source, skill_cwd_fd = self._read_skill_source(target)
                if hashlib.sha256(source).hexdigest() != target.sha256:
                    return _response(
                        request_id,
                        started,
                        error_code="skill_script_hash_mismatch",
                    )
                policy_hashes = {
                    rule.sha256
                    for rule in snapshot.skill_script_rules
                    if rule.skill_name == target.skill_name
                    and rule.script_relative_path == target.script_relative_path
                }
                if policy_hashes != {target.sha256}:
                    return _response(
                        request_id,
                        started,
                        error_code="skill_script_hash_mismatch",
                    )
                snapshot_path = self._write_snapshot(source)
                python = self._verify_executable(Path(self.config.python_executable))
                argv = [
                    str(python),
                    "-I",
                    "-u",
                    "-c",
                    _PINNED_CWD_BOOTSTRAP,
                    str(skill_cwd_fd),
                    str(snapshot_path),
                    *target.args,
                ]
                cwd = "/"
            return self._run_process(
                request_id,
                argv,
                cwd,
                limits,
                started,
                disconnected,
                cwd_fd=skill_cwd_fd,
            )
        except _BridgeDenied as exc:
            return _response(request_id, started, error_code=exc.error_code)
        finally:
            if skill_cwd_fd is not None:
                try:
                    os.close(skill_cwd_fd)
                except OSError:
                    pass
            if snapshot_path is not None:
                try:
                    snapshot_path.unlink(missing_ok=True)
                except OSError:
                    self._mark_unhealthy()
            if command_snapshot_path is not None:
                try:
                    command_snapshot_path.unlink(missing_ok=True)
                except OSError:
                    self._mark_unhealthy()

    def _verify_executable(self, path: Path, *, require_owner: bool = False) -> Path:
        if not path.is_absolute():
            raise _BridgeDenied("host_executable_denied")
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise _BridgeDenied("host_executable_denied") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or resolved != path
            or not os.access(path, os.X_OK)
        ):
            raise _BridgeDenied("host_executable_denied")
        try:
            roots = list(self._validated_trusted_roots())
        except ValueError as exc:
            raise _BridgeDenied("host_executable_denied") from exc
        matching_root = next((root for root in roots if _within(resolved, root)), None)
        if matching_root is None:
            raise _BridgeDenied("host_executable_denied")
        root_metadata = matching_root.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
            or (
                require_owner
                and root_metadata.st_uid
                != self.config.required_executable_owner_uid
            )
        ):
            raise _BridgeDenied("host_executable_denied")
        current = matching_root
        for component in resolved.parent.relative_to(matching_root).parts:
            current /= component
            metadata = current.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or (
                    require_owner
                    and metadata.st_uid != self.config.required_executable_owner_uid
                )
            ):
                raise _BridgeDenied("host_executable_denied")
        return resolved

    def _snapshot_executable(self, path: Path) -> Path:
        resolved = self._verify_executable(path, require_owner=True)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            before = resolved.lstat()
            fd = os.open(resolved, flags)
            try:
                opened = os.fstat(fd)
                if (
                    (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != self.config.required_executable_owner_uid
                    or stat.S_IMODE(opened.st_mode) & 0o022
                    or not stat.S_IMODE(opened.st_mode) & 0o100
                ):
                    raise _BridgeDenied("host_executable_denied")
                chunks: list[bytes] = []
                while chunk := os.read(fd, 1024 * 1024):
                    chunks.append(chunk)
            finally:
                os.close(fd)
        except _BridgeDenied:
            raise
        except OSError as exc:
            raise _BridgeDenied("host_executable_denied") from exc
        source = b"".join(chunks)
        snapshot = self._write_command_snapshot(source, signing=self._is_macho(source))
        try:
            if self._is_macho(source):
                self._sign_macho_snapshot(snapshot)
            self._revalidate_command_snapshot(snapshot)
            return snapshot
        except Exception:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                self._mark_unhealthy()
            raise

    @staticmethod
    def _is_macho(source: bytes) -> bool:
        return source[:4] in _MACHO_MAGICS

    def _verify_codesign_executable(self) -> Path:
        path = _CODESIGN_PATH
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise _BridgeDenied("host_command_signing_failed") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not os.access(path, os.X_OK)
        ):
            raise _BridgeDenied("host_command_signing_failed")
        current = Path(path.anchor)
        for component in path.parent.parts[1:]:
            current /= component
            component_metadata = current.lstat()
            if (
                stat.S_ISLNK(component_metadata.st_mode)
                or not stat.S_ISDIR(component_metadata.st_mode)
                or component_metadata.st_uid != 0
                or stat.S_IMODE(component_metadata.st_mode) & 0o022
            ):
                raise _BridgeDenied("host_command_signing_failed")
        return path

    def _sign_macho_snapshot(self, snapshot: Path) -> None:
        codesign = self._verify_codesign_executable()
        common = {
            "shell": False,
            "env": {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": self.config.codesign_timeout_seconds,
            "check": False,
        }
        try:
            signed = self._codesign_runner(
                [str(codesign), "--force", "--sign", "-", str(snapshot)], **common
            )
            if signed.returncode != 0:
                raise _BridgeDenied("host_command_signing_failed")
            self._revalidate_command_snapshot(snapshot, expected_mode=0o700)
            snapshot.chmod(0o500)
            verified = self._codesign_runner(
                [str(codesign), "--verify", "--strict", str(snapshot)], **common
            )
            if verified.returncode != 0:
                raise _BridgeDenied("host_command_signing_failed")
        except _BridgeDenied:
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _BridgeDenied("host_command_signing_failed") from exc

    def _revalidate_command_snapshot(
        self, snapshot: Path, *, expected_mode: int = 0o500
    ) -> None:
        try:
            self._validate_private_snapshot_root()
        except (OSError, ValueError) as exc:
            raise _BridgeDenied("host_command_snapshot_invalid") from exc
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            metadata = snapshot.lstat()
            fd = os.open(snapshot, flags)
            try:
                opened = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise _BridgeDenied("host_command_snapshot_invalid") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != expected_mode
            or snapshot.parent.resolve(strict=True)
            != Path(self.config.snapshot_root).resolve(strict=True)
        ):
            raise _BridgeDenied("host_command_snapshot_invalid")

    def _read_skill_source(
        self, target: HostSkillScriptTarget
    ) -> tuple[bytes, int]:
        descriptors: list[int] = []
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0)
            )
            root_fd = os.open(self.config.skills_root, directory_flags)
            descriptors.append(root_fd)
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise _BridgeDenied("skill_script_path_denied")
            skill_fd = os.open(target.skill_name, directory_flags, dir_fd=root_fd)
            descriptors.append(skill_fd)
            skill_stat = os.fstat(skill_fd)
            if not stat.S_ISDIR(skill_stat.st_mode):
                raise _BridgeDenied("skill_script_path_denied")

            parent_fd = skill_fd
            components = target.script_relative_path.split("/")
            for component in components[:-1]:
                parent_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                descriptors.append(parent_fd)
                if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
                    raise _BridgeDenied("skill_script_path_denied")
            file_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            script_fd = os.open(components[-1], file_flags, dir_fd=parent_fd)
            descriptors.append(script_fd)
            opened = os.fstat(script_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise _BridgeDenied("skill_script_path_denied")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(script_fd, 65536):
                total += len(chunk)
                if total > 16 * 1024 * 1024:
                    raise _BridgeDenied("skill_script_path_denied")
                chunks.append(chunk)

            # Transfer ownership of the verified Skill directory fd to the caller.
            descriptors.remove(skill_fd)
            return b"".join(chunks), skill_fd
        except _BridgeDenied:
            raise
        except OSError as exc:
            raise _BridgeDenied("skill_script_path_denied") from exc
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _prepare_snapshot_root(self) -> None:
        root = Path(self.config.snapshot_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._validate_private_snapshot_root()

    def _validate_private_snapshot_root(self) -> None:
        root = Path(self.config.snapshot_root)
        metadata = root.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("host_bridge_snapshot_root_unsafe")

    def _validate_snapshot_root_overlap(self) -> None:
        snapshot = Path(self.config.snapshot_root).resolve(strict=False)
        for configured in self.config.model_writable_roots:
            writable = Path(configured).resolve(strict=False)
            if _within(snapshot, writable) or _within(writable, snapshot):
                raise ValueError("host_bridge_snapshot_root_unsafe")
        skills = Path(self.config.skills_root).resolve(strict=False)
        if _within(snapshot, skills) or _within(skills, snapshot):
            raise ValueError("host_bridge_snapshot_root_unsafe")

    def _verify_skills_root(self) -> None:
        root = Path(self.config.skills_root)
        try:
            metadata = root.lstat()
        except OSError as exc:
            raise ValueError("host_bridge_skills_root_unsafe") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("host_bridge_skills_root_unsafe")

    def _validate_authority_paths(self) -> None:
        roots = (
            Path(self.config.skills_root).resolve(strict=True),
            Path(self.config.snapshot_root).resolve(strict=True),
            *(Path(root).resolve(strict=False) for root in self.config.model_writable_roots),
        )
        for configured in (self.config.policy_path, self.config.token_path):
            authority = Path(configured).resolve(strict=True)
            if any(_within(authority, root) or _within(root, authority) for root in roots):
                raise ValueError("host_bridge_authority_path_unsafe")

    def _validate_trusted_roots(self) -> None:
        self._validated_trusted_roots()

    def _validated_trusted_roots(self) -> tuple[Path, ...]:
        writable = tuple(
            Path(root).resolve(strict=False) for root in self.config.model_writable_roots
        )
        validated: list[Path] = []
        for configured in self.config.trusted_executable_roots:
            root = Path(configured)
            try:
                resolved = root.resolve(strict=True)
            except OSError as exc:
                raise ValueError("host_bridge_trusted_root_unsafe") from exc
            if resolved != root or any(
                _within(resolved, candidate) or _within(candidate, resolved)
                for candidate in writable
            ):
                raise ValueError("host_bridge_trusted_root_unsafe")
            current = Path(resolved.anchor)
            anchor_metadata = current.lstat()
            if (
                stat.S_ISLNK(anchor_metadata.st_mode)
                or not stat.S_ISDIR(anchor_metadata.st_mode)
                or stat.S_IMODE(anchor_metadata.st_mode) & 0o022
            ):
                raise ValueError("host_bridge_trusted_root_unsafe")
            for component in resolved.parts[1:]:
                current /= component
                metadata = current.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise ValueError("host_bridge_trusted_root_unsafe")
            validated.append(resolved)
        return tuple(validated)

    def _cleanup_stale_snapshots(self) -> None:
        root = Path(self.config.snapshot_root)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            root_fd = os.open(root, flags)
            try:
                with os.scandir(root_fd) as entries:
                    for entry in entries:
                        is_skill = (
                            entry.name.startswith("host-skill-")
                            and entry.name.endswith(".py")
                        )
                        is_command = entry.name.startswith("host-command-")
                        if not (is_skill or is_command):
                            continue
                        metadata = entry.stat(follow_symlinks=False)
                        if (
                            entry.is_symlink()
                            or not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_uid != os.geteuid()
                            or stat.S_IMODE(metadata.st_mode)
                            != (0o600 if is_skill else 0o500)
                        ):
                            raise ValueError("host_bridge_stale_snapshot_unsafe")
                        os.unlink(entry.name, dir_fd=root_fd)
            finally:
                os.close(root_fd)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("host_bridge_stale_snapshot_cleanup_failed") from exc

    def _write_snapshot(self, source: bytes) -> Path:
        fd, name = tempfile.mkstemp(
            prefix="host-skill-", suffix=".py", dir=self.config.snapshot_root
        )
        path = Path(name)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(source)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        return path

    def _write_command_snapshot(self, source: bytes, *, signing: bool = False) -> Path:
        fd, name = tempfile.mkstemp(prefix="host-command-", dir=self.config.snapshot_root)
        path = Path(name)
        try:
            os.fchmod(fd, 0o700 if signing else 0o500)
            view = memoryview(source)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        return path

    def _run_process(
        self,
        request_id: str,
        argv: list[str],
        cwd: str,
        limits: HostTerminalExecutionLimits,
        started: float,
        disconnected: Callable[[], bool] | None,
        *,
        cwd_fd: int | None = None,
    ) -> dict[str, Any]:
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
        }
        try:
            process = self._popen_factory(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                pass_fds=() if cwd_fd is None else (cwd_fd,),
            )
        except OSError as exc:
            raise _BridgeDenied("host_process_start_failed") from exc
        with self._process_lock:
            if self._shutting_down:
                self._terminate_process_group(process)
                raise _BridgeDenied("host_bridge_unhealthy")
            self._active_processes[process.pid] = process
        stdout: _BoundedPipe | None = None
        stderr: _BoundedPipe | None = None
        try:
            stdout = _BoundedPipe(process.stdout, limits.max_stdout_bytes)
            stderr = _BoundedPipe(process.stderr, limits.max_stderr_bytes)
            stdout.start()
            stderr.start()
            deadline = started + limits.timeout_seconds
            timed_out = False
            cancelled = False
            while process.poll() is None:
                if disconnected is not None and disconnected():
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.02)
            if timed_out or cancelled:
                if not self._terminate_process_group(process):
                    self._mark_unhealthy()
            else:
                process.wait()
                # A child may outlive a normally-exited leader; never leak its group.
                if _group_exists(process.pid) and not self._terminate_process_group(process):
                    self._mark_unhealthy()
            stdout.join(timeout=self.config.terminate_grace_seconds)
            stderr.join(timeout=self.config.terminate_grace_seconds)
            if stdout.is_alive() or stderr.is_alive():
                self._mark_unhealthy()
            if cancelled:
                return _response(
                    request_id,
                    started,
                    error_code="host_execution_cancelled",
                    status="error",
                    exit_code=process.returncode,
                    stdout=stdout.text,
                    stderr=stderr.text,
                    stdout_truncated=stdout.truncated,
                    stderr_truncated=stderr.truncated,
                )
            if timed_out:
                return _response(
                    request_id,
                    started,
                    error_code="host_execution_timeout",
                    status="timeout",
                    exit_code=process.returncode,
                    stdout=stdout.text,
                    stderr=stderr.text,
                    stdout_truncated=stdout.truncated,
                    stderr_truncated=stderr.truncated,
                )
            success = process.returncode == 0
            return _response(
                request_id,
                started,
                status="success" if success else "error",
                exit_code=process.returncode,
                stdout=stdout.text,
                stderr=stderr.text,
                stdout_truncated=stdout.truncated,
                stderr_truncated=stderr.truncated,
                error_code=None if success else "host_execution_failed",
            )
        except _BridgeDenied:
            raise
        except Exception as exc:
            raise _BridgeDenied("host_bridge_internal_error") from exc
        finally:
            cleanup_confirmed = True
            if process.poll() is None or _group_exists(process.pid):
                cleanup_confirmed = self._terminate_process_group(process)
            for reader in (stdout, stderr):
                if reader is not None:
                    try:
                        if reader.ident is not None:
                            reader.join(timeout=self.config.terminate_grace_seconds)
                            if reader.is_alive():
                                cleanup_confirmed = False
                        elif reader.pipe is not None:
                            reader.pipe.close()
                    except Exception:
                        cleanup_confirmed = False
            if not cleanup_confirmed:
                self._mark_unhealthy()
            with self._process_lock:
                if not _group_exists(process.pid):
                    self._active_processes.pop(process.pid, None)

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> bool:
        pgid = process.pid
        try:
            if _group_exists(pgid):
                os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + self.config.terminate_grace_seconds
            while _group_exists(pgid) and time.monotonic() < deadline:
                time.sleep(0.01)
            if _group_exists(pgid):
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        finally:
            try:
                process.wait(timeout=self.config.terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                # Reap the leader even if group inspection/signalling was uncertain.
                try:
                    process.kill()
                    process.wait(timeout=self.config.terminate_grace_seconds)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            deadline = time.monotonic() + self.config.terminate_grace_seconds
            while _group_exists(pgid) and time.monotonic() < deadline:
                time.sleep(0.01)
            if _group_exists(pgid):
                return False
        # Final observable state is authoritative even if a signal raced with exit.
        return process.returncode is not None and not _group_exists(pgid)

    def _mark_unhealthy(self) -> None:
        with self._health_lock:
            self._healthy = False
            self._cleanup_uncertain = True


class _BoundedPipe(threading.Thread):
    def __init__(self, pipe: Any, maximum: int) -> None:
        super().__init__(daemon=True)
        self.pipe = pipe
        self.maximum = maximum
        self.data = bytearray()
        self.truncated = False

    def run(self) -> None:
        if self.pipe is None:
            return
        try:
            while chunk := self.pipe.read(65536):
                available = self.maximum - len(self.data)
                if available > 0:
                    self.data.extend(chunk[:available])
                if len(chunk) > available:
                    self.truncated = True
        finally:
            self.pipe.close()

    @property
    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


class _BridgeDenied(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _parse_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _REQUEST_FIELDS:
        raise ValueError("invalid_request")
    if payload["protocol_version"] != "1":
        raise ValueError("invalid_protocol")
    request_id = payload["request_id"]
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ValueError("invalid_request_id")
    limits_data = payload["limits"]
    if not isinstance(limits_data, dict) or set(limits_data) != _LIMIT_FIELDS:
        raise ValueError("invalid_limits")
    limits = HostTerminalExecutionLimits(**limits_data)
    target_data = payload["target"]
    if not isinstance(target_data, dict):
        raise ValueError("invalid_target")
    kind = target_data.get("type")
    if kind == "command" and set(target_data) == {"type", "executable", "args"}:
        if not isinstance(target_data["args"], list):
            raise ValueError("invalid_args")
        target = HostCommandTarget(
            executable=target_data["executable"], args=tuple(target_data["args"])
        )
    elif kind == "skill_script" and set(target_data) == {
        "type",
        "skill_name",
        "script_relative_path",
        "sha256",
        "args",
    }:
        if not isinstance(target_data["args"], list):
            raise ValueError("invalid_args")
        target = HostSkillScriptTarget(
            skill_name=target_data["skill_name"],
            script_relative_path=target_data["script_relative_path"],
            sha256=target_data["sha256"],
            args=tuple(target_data["args"]),
        )
    else:
        raise ValueError("invalid_target")
    version = payload["n_agent_policy_version"]
    digest = payload["n_agent_content_digest"]
    if not isinstance(version, str) or not version:
        raise ValueError("invalid_policy_version")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("invalid_policy_digest")
    return {
        **payload,
        "target": target,
        "limits": limits,
    }


def _response(
    request_id: str,
    started: float,
    *,
    status: str = "error",
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    error_code: str | None,
) -> dict[str, Any]:
    return {
        "request_id": request_id if isinstance(request_id, str) else "",
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "error_code": error_code,
    }


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _peer_disconnected(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, OSError):
        return False


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists, but inability to inspect it is cleanup uncertainty.
        return True


def make_server(
    bridge: HostTerminalBridge,
) -> ThreadingHTTPServer:
    class BridgeHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
            self._handler_slots = threading.BoundedSemaphore(
                bridge.config.max_http_handler_threads
            )
            self._header_timer_lock = threading.Lock()
            self._header_timers: dict[
                socket.socket, tuple[object, threading.Timer]
            ] = {}
            super().__init__(server_address, handler)

        def process_request(
            self, request: socket.socket, client_address: tuple[str, int]
        ) -> None:
            if not self._handler_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            token = self._start_header_deadline(request)
            try:
                super().process_request(request, client_address)
            except Exception:
                self._cancel_header_deadline(request, token)
                self._handler_slots.release()
                raise

        def _start_header_deadline(self, request: socket.socket) -> object:
            token = object()
            timer = threading.Timer(
                bridge.config.request_read_timeout_seconds,
                self._expire_header,
                args=(request, token),
            )
            timer.daemon = True
            with self._header_timer_lock:
                self._header_timers[request] = (token, timer)
            timer.start()
            return token

        def process_request_thread(
            self, request: socket.socket, client_address: tuple[str, int]
        ) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._cancel_header_deadline(request)
                self._handler_slots.release()

        def _expire_header(self, request: socket.socket, token: object) -> None:
            with self._header_timer_lock:
                entry = self._header_timers.get(request)
                if entry is None or entry[0] is not token:
                    return
                self._header_timers.pop(request)
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def _cancel_header_deadline(
            self, request: socket.socket, token: object | None = None
        ) -> None:
            with self._header_timer_lock:
                entry = self._header_timers.get(request)
                if entry is None or (token is not None and entry[0] is not token):
                    return
                self._header_timers.pop(request)
            entry[1].cancel()

        def shutdown(self) -> None:
            bridge.shutdown()
            super().shutdown()

        def server_close(self) -> None:
            bridge.shutdown()
            super().server_close()

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(bridge.config.request_read_timeout_seconds)

        def parse_request(self) -> bool:
            parsed = super().parse_request()
            if parsed:
                server = self.server
                assert isinstance(server, BridgeHTTPServer)
                server._cancel_header_deadline(self.connection)
                self.connection.settimeout(None)
            return parsed

        def do_GET(self) -> None:
            if self.path != "/healthz":
                self._send(404, _response("", time.monotonic(), error_code="not_found"))
                return
            self._send(200 if bridge.healthy else 503, bridge.health_payload())

        def do_POST(self) -> None:
            # Authentication deliberately precedes route selection and all body handling.
            if not bridge.authenticate(self.headers.get(AUTH_HEADER)):
                self._send(
                    401,
                    _response("", time.monotonic(), error_code="host_bridge_auth_failed"),
                )
                return
            if self.path != "/v1/execute":
                self._send(404, _response("", time.monotonic(), error_code="not_found"))
                return
            if self.headers.get_content_type() != "application/json":
                self._send(
                    415,
                    _response("", time.monotonic(), error_code="host_bridge_invalid_request"),
                )
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0 or length > bridge.config.max_request_bytes:
                self._send(
                    413,
                    _response("", time.monotonic(), error_code="host_bridge_invalid_request"),
                )
                return
            deadline = time.monotonic() + bridge.config.request_read_timeout_seconds
            chunks: list[bytes] = []
            remaining = length
            try:
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise TimeoutError
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            except (TimeoutError, socket.timeout, OSError):
                self.connection.settimeout(None)
                self._send(
                    400,
                    _response("", time.monotonic(), error_code="host_bridge_invalid_request"),
                )
                return
            self.connection.settimeout(None)
            body = b"".join(chunks)
            if len(body) != length:
                self._send(
                    400,
                    _response("", time.monotonic(), error_code="host_bridge_invalid_request"),
                )
                return
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(
                    400,
                    _response("", time.monotonic(), error_code="host_bridge_invalid_request"),
                )
                return
            status_code, response = bridge.execute_payload(
                payload, disconnected=lambda: _peer_disconnected(self.connection)
            )
            self._send(status_code, response)

        def _send(self, status_code: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            try:
                # send_response() adds a version-bearing Server header.
                self.send_response_only(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            return

    return BridgeHTTPServer((bridge.config.bind_host, bridge.config.port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="N-Agent host terminal loopback bridge")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--skills-root", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--trusted-root", action="append", required=True)
    parser.add_argument("--model-writable-root", action="append", default=[])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    bridge = HostTerminalBridge(
        HostTerminalBridgeConfig(
            policy_path=args.policy,
            token_path=args.token,
            skills_root=args.skills_root,
            python_executable=args.python,
            snapshot_root=args.snapshot_root,
            trusted_executable_roots=tuple(args.trusted_root),
            model_writable_roots=tuple(args.model_writable_root),
            port=args.port,
        )
    )
    server = make_server(bridge)
    try:
        server.serve_forever()
    finally:
        bridge.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()


__all__ = [
    "HostTerminalBridge",
    "HostTerminalBridgeConfig",
    "make_server",
]
