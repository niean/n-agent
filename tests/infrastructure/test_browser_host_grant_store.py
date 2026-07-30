from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from app.infrastructure.browser.host_grant_store import (
    BrowserAuthorizationStoreError,
    SqliteBrowserAuthorizationStore,
)


def _create_database(path: Path, *, journal_mode: str = "delete") -> None:
    with sqlite3.connect(path) as connection:
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
        connection.execute(f"PRAGMA journal_mode = {journal_mode}")


def _insert_authorization(
    path: Path,
    *,
    session_id: str = "browser-1",
    n_agent_session_id: str = "agent-1",
    expires_at: str | None = None,
) -> None:
    expires_at = expires_at or (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO browser_sessions(
                id, n_agent_session_id, backend_type, status, profile_ref
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                n_agent_session_id,
                "host_cdp",
                "active",
                "profile-1",
            ),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO browser_host_grants(
                browser_session_id, n_agent_session_id, actor_id,
                policy_version, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                n_agent_session_id,
                "actor-1",
                "system-v1",
                expires_at,
            ),
        )


def test_loads_frozen_joined_authorization_and_missing_returns_none(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path)
    _insert_authorization(path)

    store = SqliteBrowserAuthorizationStore(path)
    snapshot = store.load_authorization("browser-1")

    assert snapshot is not None
    assert snapshot.browser_session_id == "browser-1"
    assert snapshot.n_agent_session_id == "agent-1"
    assert snapshot.backend_type == "host_cdp"
    assert snapshot.status == "active"
    assert snapshot.profile_ref == "profile-1"
    assert snapshot.actor_id == "actor-1"
    assert snapshot.policy_version == "system-v1"
    assert snapshot.expires_at.tzinfo is timezone.utc
    with pytest.raises((AttributeError, TypeError)):
        snapshot.status = "closed"  # type: ignore[misc]
    assert store.load_authorization("missing") is None


def test_timezone_aware_expiry_is_normalized_to_utc(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path)
    _insert_authorization(path, expires_at="2026-07-29T16:00:00+08:00")

    snapshot = SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")

    assert snapshot is not None
    assert snapshot.expires_at == datetime(2026, 7, 29, 8, tzinfo=timezone.utc)


def test_wal_commits_are_visible_without_creating_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path, journal_mode="wal")
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        _insert_authorization(path)
        store = SqliteBrowserAuthorizationStore(path)
        first = store.load_authorization("browser-1")
        assert first is not None

        writer.execute(
            "UPDATE browser_host_grants SET actor_id = ? WHERE browser_session_id = ?",
            ("actor-2", "browser-1"),
        )
        writer.commit()
        second = store.load_authorization("browser-1")
        assert second is not None and second.actor_id == "actor-2"
    finally:
        writer.close()


@pytest.mark.parametrize("name", ["space name.db", "question?.db", "hash#.db", "percent%.db"])
def test_database_uri_percent_encodes_path(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    _create_database(path)
    _insert_authorization(path)
    assert (
        SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
        is not None
    )


def test_connects_read_only_query_only_with_bounded_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path)
    _insert_authorization(path)
    real_connect = sqlite3.connect
    observed: dict[str, object] = {}

    def recording_connect(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        observed["database"] = database
        observed["uri"] = kwargs.get("uri")
        connection = real_connect(database, *args, **kwargs)
        connection.set_trace_callback(
            lambda statement: observed.setdefault("statements", []).append(statement)  # type: ignore[union-attr]
        )
        return connection

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    store = SqliteBrowserAuthorizationStore(path, busy_timeout_ms=250)
    assert store.load_authorization("browser-1") is not None

    assert observed["uri"] is True
    assert "mode=ro" in str(observed["database"])
    statements = [str(value).lower() for value in observed["statements"]]  # type: ignore[index]
    assert any("pragma query_only" in value for value in statements)
    assert any("pragma busy_timeout" in value and "250" in value for value in statements)
    selects = [value for value in statements if value.lstrip().startswith("select")]
    assert len(selects) == 1
    assert "join browser_sessions" in " ".join(selects[0].split())
    assert "?" not in selects[0]  # trace expands the sole bound parameter

    with pytest.raises(ValueError, match="browser_authorization_store_config_invalid"):
        SqliteBrowserAuthorizationStore(path, busy_timeout_ms=0)
    with pytest.raises(ValueError, match="browser_authorization_store_config_invalid"):
        SqliteBrowserAuthorizationStore(path, busy_timeout_ms=5001)


def test_rejects_relative_missing_non_regular_and_symlink_paths(tmp_path: Path) -> None:
    relative = Path("sessions.db")
    directory = tmp_path / "directory"
    directory.mkdir()
    real = tmp_path / "real.db"
    _create_database(real)
    link = tmp_path / "link.db"
    link.symlink_to(real)

    for path in (relative, tmp_path / "missing.db", directory, link):
        with pytest.raises(BrowserAuthorizationStoreError) as caught:
            SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
        assert caught.value.error_code == "browser_authorization_store_unsafe"
        assert str(path) not in str(caught.value)

    assert not (tmp_path / "missing.db").exists()
    assert not (tmp_path / "missing.db-journal").exists()
    assert not (tmp_path / "missing.db-wal").exists()


def test_rejects_symlink_or_owner_mismatch_in_existing_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    path = real_parent / "sessions.db"
    _create_database(path)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(BrowserAuthorizationStoreError) as caught:
        SqliteBrowserAuthorizationStore(linked_parent / "sessions.db").load_authorization(
            "browser-1"
        )
    assert caught.value.error_code == "browser_authorization_store_unsafe"

    real_lstat = Path.lstat

    def wrong_owner(candidate: Path) -> os.stat_result:
        metadata = real_lstat(candidate)
        if candidate == real_parent:
            values = list(metadata)
            values[4] = os.geteuid() + 10
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", wrong_owner)
    with pytest.raises(BrowserAuthorizationStoreError) as caught:
        SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
    assert caught.value.error_code == "browser_authorization_store_unsafe"


def test_rejects_database_owner_mismatch_and_replacement_around_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path)
    real_lstat = Path.lstat

    def wrong_owner(candidate: Path) -> os.stat_result:
        metadata = real_lstat(candidate)
        if candidate == path:
            values = list(metadata)
            values[4] = os.geteuid() + 10
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", wrong_owner)
    with pytest.raises(BrowserAuthorizationStoreError) as caught:
        SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
    assert caught.value.error_code == "browser_authorization_store_unsafe"

    monkeypatch.setattr(Path, "lstat", real_lstat)
    real_connect = sqlite3.connect

    def replacing_connect(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(database, *args, **kwargs)
        replacement = tmp_path / "replacement.db"
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        return connection

    monkeypatch.setattr(sqlite3, "connect", replacing_connect)
    with pytest.raises(BrowserAuthorizationStoreError) as caught:
        SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
    assert caught.value.error_code == "browser_authorization_store_unsafe"


@pytest.mark.parametrize("kind", ["missing_schema", "schema_drift", "corrupt", "locked"])
def test_database_failures_are_stable_and_non_diagnostic(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "sessions.db"
    if kind == "corrupt":
        path.write_bytes(b"not sqlite")
    elif kind == "missing_schema":
        sqlite3.connect(path).close()
    else:
        _create_database(path)
        if kind == "schema_drift":
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE browser_sessions RENAME COLUMN status TO state")

    locker: sqlite3.Connection | None = None
    if kind == "locked":
        locker = sqlite3.connect(path)
        locker.execute("PRAGMA locking_mode = EXCLUSIVE")
        locker.execute("BEGIN EXCLUSIVE")
    try:
        store = SqliteBrowserAuthorizationStore(path, busy_timeout_ms=1)
        with pytest.raises(BrowserAuthorizationStoreError) as caught:
            store.load_authorization("browser-1")
        assert caught.value.error_code == "browser_authorization_store_unhealthy"
        assert str(path) not in str(caught.value)
        assert "sqlite" not in str(caught.value).lower()
    finally:
        if locker is not None:
            locker.rollback()
            locker.close()


def test_naive_or_invalid_expiry_is_store_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path)
    for expires_at in ("2026-07-29T08:00:00", "invalid"):
        _insert_authorization(path, expires_at=expires_at)
        with pytest.raises(BrowserAuthorizationStoreError) as caught:
            SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
        assert caught.value.error_code == "browser_authorization_store_unhealthy"


def test_database_io_error_is_stable_and_hides_raw_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path)

    def fail_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise OSError(f"raw IO failure at {path}")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    with pytest.raises(BrowserAuthorizationStoreError) as caught:
        SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
    assert caught.value.error_code == "browser_authorization_store_unhealthy"
    assert str(path) not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("mode", [0o770, 0o702, 0o1777])
def test_rejects_group_world_writable_ancestor(
    tmp_path: Path, mode: int
) -> None:
    authority_parent = tmp_path / "authority"
    authority_parent.mkdir()
    path = authority_parent / "sessions.db"
    _create_database(path)
    authority_parent.chmod(mode)
    try:
        with pytest.raises(BrowserAuthorizationStoreError) as caught:
            SqliteBrowserAuthorizationStore(path).load_authorization("browser-1")
        assert caught.value.error_code == "browser_authorization_store_unsafe"
    finally:
        authority_parent.chmod(0o700)


@pytest.mark.parametrize("mode", [0o660, 0o666])
def test_rejects_group_world_writable_database_file(
    tmp_path: Path, mode: int
) -> None:
    path = tmp_path / "sessions.db"
    _create_database(path)
    path.chmod(mode)
    try:
        with pytest.raises(BrowserAuthorizationStoreError) as caught:
            SqliteBrowserAuthorizationStore(path).load_authorization(
                "browser-1"
            )
        assert caught.value.error_code == "browser_authorization_store_unsafe"
    finally:
        path.chmod(0o600)
