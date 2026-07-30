"""Strict read-only authorization snapshots for the host browser bridge."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat
from urllib.parse import quote

from app.domain.browser import BrowserBackendType, BrowserSessionStatus


_MIN_BUSY_TIMEOUT_MS = 1
_MAX_BUSY_TIMEOUT_MS = 5_000

_AUTHORIZATION_QUERY = """
SELECT
    sessions.id AS browser_session_id,
    sessions.n_agent_session_id AS n_agent_session_id,
    sessions.backend_type AS backend_type,
    sessions.status AS status,
    sessions.profile_ref AS profile_ref,
    grants.actor_id AS actor_id,
    grants.policy_version AS policy_version,
    grants.expires_at AS expires_at
FROM browser_host_grants AS grants
JOIN browser_sessions AS sessions
  ON sessions.id = grants.browser_session_id
 AND sessions.n_agent_session_id = grants.n_agent_session_id
WHERE grants.browser_session_id = ?
"""


class BrowserAuthorizationStoreError(RuntimeError):
    """Public, deliberately non-diagnostic authorization-store failure."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class HostAuthorizationSnapshot:
    """One complete, immutable grant/session authorization view."""

    browser_session_id: str
    n_agent_session_id: str
    backend_type: BrowserBackendType
    status: BrowserSessionStatus
    profile_ref: str
    actor_id: str
    policy_version: str
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def revoked(self) -> bool:
        # Revocation is represented by deletion of the grant row.
        return False


class SqliteBrowserAuthorizationStore:
    """Reads the shared N-Agent SQLite database without ever mutating it."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = 250,
    ) -> None:
        self._path = Path(path)
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not _MIN_BUSY_TIMEOUT_MS
            <= busy_timeout_ms
            <= _MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError("browser_authorization_store_config_invalid")
        self._busy_timeout_ms = busy_timeout_ms

    def load_authorization(
        self, session_id: str
    ) -> HostAuthorizationSnapshot | None:
        before = self._validate_path()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self._read_only_uri(),
                uri=True,
                timeout=self._busy_timeout_ms / 1000,
            )
            after = self._validate_path()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise BrowserAuthorizationStoreError(
                    "browser_authorization_store_unsafe"
                )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                _AUTHORIZATION_QUERY, (session_id,)
            ).fetchone()
        except BrowserAuthorizationStoreError:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unhealthy"
            ) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
        if row is None:
            return None
        try:
            return _snapshot_from_row(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unhealthy"
            ) from None

    def _read_only_uri(self) -> str:
        # Keep "/" as the URI path delimiter while escaping every character
        # that could become query/fragment syntax in a SQLite file URI.
        return f"file:{quote(os.fspath(self._path), safe='/')}?mode=ro"

    def _validate_path(self) -> os.stat_result:
        if not self._path.is_absolute():
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unsafe"
            )
        expected_uid = os.geteuid()
        current = Path(self._path.anchor)
        try:
            root_metadata = current.lstat()
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_uid not in {0, expected_uid}
                or stat.S_IMODE(root_metadata.st_mode) & 0o022
            ):
                raise BrowserAuthorizationStoreError(
                    "browser_authorization_store_unsafe"
                )
            for component in self._path.parts[1:-1]:
                current /= component
                metadata = current.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid not in {0, expected_uid}
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise BrowserAuthorizationStoreError(
                        "browser_authorization_store_unsafe"
                    )
            database_metadata = self._path.lstat()
        except BrowserAuthorizationStoreError:
            raise
        except OSError as exc:
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unsafe"
            ) from None
        if (
            stat.S_ISLNK(database_metadata.st_mode)
            or not stat.S_ISREG(database_metadata.st_mode)
            or database_metadata.st_uid != expected_uid
            or stat.S_IMODE(database_metadata.st_mode) & 0o022
        ):
            raise BrowserAuthorizationStoreError(
                "browser_authorization_store_unsafe"
            )
        return database_metadata


def _snapshot_from_row(row: sqlite3.Row) -> HostAuthorizationSnapshot:
    required_text_fields = (
        "browser_session_id",
        "n_agent_session_id",
        "profile_ref",
        "actor_id",
        "policy_version",
    )
    values = {field: row[field] for field in required_text_fields}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ValueError("invalid_authorization")
    expires_at_raw = row["expires_at"]
    if not isinstance(expires_at_raw, str):
        raise ValueError("invalid_authorization")
    expires_at = datetime.fromisoformat(expires_at_raw)
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("invalid_authorization")
    return HostAuthorizationSnapshot(
        browser_session_id=values["browser_session_id"],
        n_agent_session_id=values["n_agent_session_id"],
        backend_type=BrowserBackendType(row["backend_type"]),
        status=BrowserSessionStatus(row["status"]),
        profile_ref=values["profile_ref"],
        actor_id=values["actor_id"],
        policy_version=values["policy_version"],
        expires_at=expires_at.astimezone(timezone.utc),
    )


__all__ = [
    "BrowserAuthorizationStoreError",
    "HostAuthorizationSnapshot",
    "SqliteBrowserAuthorizationStore",
]
