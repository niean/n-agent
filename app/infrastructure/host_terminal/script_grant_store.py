"""Host-private durable grant records, serialized across bridge processes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
from typing import Iterator

from app.domain.host_script_grant import HostScriptGrant


class HostScriptGrantStoreError(ValueError):
    def __init__(self, error_code: str = "host_script_grants_unavailable") -> None:
        self.error_code = error_code
        super().__init__(error_code)


class HostScriptGrantStore:
    def __init__(
        self,
        snapshot_root: str | os.PathLike[str],
        *,
        forbidden_roots: tuple[str | os.PathLike[str], ...] = (),
    ) -> None:
        snapshot = Path(snapshot_root).absolute()
        self.root = snapshot.with_name(snapshot.name + "-script-grants")
        self.path = self.root / "grants.json"
        self._forbidden = tuple(Path(p).resolve() for p in forbidden_roots)
        self._lock = threading.Lock()

    def _directory(self) -> int:
        # Do not follow any symlink below the canonical parent. Existing system
        # aliases (e.g. /var on macOS) are resolved before selecting this root.
        parent = self.root.parent.resolve(strict=True)
        root = parent / self.root.name
        if any(
            root == p or root.is_relative_to(p) or p.is_relative_to(root)
            for p in self._forbidden
        ):
            raise HostScriptGrantStoreError()
        root.mkdir(mode=0o700, exist_ok=True)
        meta = root.lstat()
        if (
            not stat.S_ISDIR(meta.st_mode)
            or meta.st_uid != os.geteuid()
            or stat.S_IMODE(meta.st_mode) & ~0o700
        ):
            raise HostScriptGrantStoreError()
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino):
            os.close(fd)
            raise HostScriptGrantStoreError()
        return fd

    @contextmanager
    def _locked(self) -> Iterator[int]:
        with self._lock:
            directory = lock = None
            try:
                directory = self._directory()
                lock = os.open(
                    "lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                self._check_file(os.fstat(lock))
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield directory
            except HostScriptGrantStoreError:
                raise
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
                raise HostScriptGrantStoreError() from exc
            finally:
                if lock is not None:
                    os.close(lock)
                if directory is not None:
                    os.close(directory)

    @staticmethod
    def _check_file(meta: os.stat_result) -> None:
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.geteuid()
            or stat.S_IMODE(meta.st_mode) & ~0o600
            or meta.st_nlink != 1
        ):
            raise HostScriptGrantStoreError()

    def _read(self, directory: int) -> list[HostScriptGrant]:
        try:
            fd = os.open(
                "grants.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return []
        try:
            self._check_file(os.fstat(fd))
            raw = bytearray()
            while chunk := os.read(fd, 65536):
                raw.extend(chunk)
                if len(raw) > 1048576:
                    raise HostScriptGrantStoreError()
        finally:
            os.close(fd)
        values = json.loads(raw)
        if not isinstance(values, list):
            raise HostScriptGrantStoreError()
        grants = [HostScriptGrant.from_dict(v) for v in values]
        if len({g.identity for g in grants}) != len(grants):
            raise HostScriptGrantStoreError()
        return grants

    def list(self) -> list[HostScriptGrant]:
        with self._locked() as directory:
            return self._read(directory)

    def apply(self, operation: str, grant: HostScriptGrant) -> list[HostScriptGrant]:
        if (
            operation not in ("grant", "revoke")
            or not isinstance(grant, HostScriptGrant)
            or grant.state != ("active" if operation == "grant" else "revoked")
        ):
            raise HostScriptGrantStoreError("host_script_grants_invalid_request")
        with self._locked() as directory:
            values = self._read(directory)
            old = next((g for g in values if g.identity == grant.identity), None)
            if old is not None:
                matching_revoke = (
                    operation == "revoke"
                    and old.revision == grant.revision
                    and replace(grant, state=old.state, updated_at=old.updated_at)
                    == old
                )
                if old == grant or (matching_revoke and old.state == "revoked"):
                    return values
                if grant.revision < old.revision or (
                    grant.revision == old.revision and not matching_revoke
                ):
                    raise HostScriptGrantStoreError("host_script_grants_stale_revision")
            values = sorted(
                [g for g in values if g.identity != grant.identity] + [grant],
                key=lambda g: g.identity,
            )
            encoded = json.dumps(
                [g.to_dict() for g in values], separators=(",", ":")
            ).encode()
            if len(encoded) > 1048576:
                raise HostScriptGrantStoreError()
            name = "pending-" + os.urandom(16).hex()
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(
                    name, "grants.json", src_dir_fd=directory, dst_dir_fd=directory
                )
                os.fsync(directory)
            finally:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
            return values
