# app/infrastructure/registry/sqlite_external_memory_provider_registry.py
from __future__ import annotations
import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.external_memory_provider import (
    DuplicateExternalMemoryProviderError, ExternalMemoryProviderConfig,
    ExternalMemoryProviderNotFoundError, ExternalMemoryProviderRegistry,
    ExternalMemoryProviderSecret, ExternalMemoryProviderType,
    ExternalMemoryProbeStatus, ExternalMemoryProviderValidationError,
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS external_memory_providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    provider_type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    extra_config TEXT NOT NULL DEFAULT '{}',
    last_probe_status TEXT NOT NULL DEFAULT 'unknown',
    last_probe_error TEXT,
    last_probed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

class SQLiteExternalMemoryProviderRegistry(ExternalMemoryProviderRegistry):
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def create_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def list_providers(self) -> list[ExternalMemoryProviderConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_memory_providers ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_cfg(r) for r in rows]

    def get_provider(self, id: str) -> ExternalMemoryProviderConfig | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_memory_providers WHERE id = ?", (id,)
            ).fetchone()
        return self._row_to_cfg(row) if row else None

    def create_provider(self, *, id, name, provider_type, base_url, api_key, enabled, extra_config):
        if enabled:
            self._assert_no_other_enabled(id)
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO external_memory_providers
                    (id, name, provider_type, base_url, api_key, enabled,
                     extra_config, last_probe_status, last_probe_error, last_probed_at,
                     created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (id, name, provider_type.value, base_url, api_key, int(enabled),
                     json.dumps(extra_config or {}, ensure_ascii=False),
                     ExternalMemoryProbeStatus.UNKNOWN.value, None, None, now, now),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: external_memory_providers.name" in str(exc):
                raise DuplicateExternalMemoryProviderError(f"name {name!r} already exists") from exc
            raise
        cfg = self.get_provider(id)
        assert cfg is not None
        return cfg

    def update_provider(self, id, *, name=None, base_url=None, api_key=None,
                        clear_api_key=False, enabled=None, extra_config=None):
        existing = self.get_provider(id)
        if existing is None:
            raise ExternalMemoryProviderNotFoundError(id)
        if enabled:
            self._assert_no_other_enabled(id)
        sets, params = [], []
        if name is not None: sets.append("name = ?"); params.append(name)
        if base_url is not None: sets.append("base_url = ?"); params.append(base_url)
        if clear_api_key or api_key == "":
            sets.append("api_key = NULL")
        elif api_key is not None:
            sets.append("api_key = ?"); params.append(api_key)
        if enabled is not None: sets.append("enabled = ?"); params.append(int(enabled))
        if extra_config is not None:
            sets.append("extra_config = ?")
            params.append(json.dumps(extra_config, ensure_ascii=False))
        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(id)
        try:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE external_memory_providers SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateExternalMemoryProviderError(str(exc)) from exc
        cfg = self.get_provider(id)
        assert cfg is not None
        return cfg

    def delete_provider(self, id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM external_memory_providers WHERE id = ?", (id,)
            )
            conn.commit()
            if cur.rowcount == 0:
                raise ExternalMemoryProviderNotFoundError(id)
            return True

    def get_secret(self, id: str) -> ExternalMemoryProviderSecret | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT api_key FROM external_memory_providers WHERE id = ?", (id,)
            ).fetchone()
        if row is None:
            return None
        return ExternalMemoryProviderSecret(id=id, api_key=row["api_key"])

    def save_probe_status(self, id, status, error=None):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE external_memory_providers
                SET last_probe_status=?, last_probe_error=?, last_probed_at=?, updated_at=?
                WHERE id=?""",
                (status.value, error, now, now, id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise ExternalMemoryProviderNotFoundError(id)

    def _assert_no_other_enabled(self, exclude_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM external_memory_providers WHERE enabled=1 AND id != ?",
                (exclude_id,),
            ).fetchone()
        if row is not None:
            raise ExternalMemoryProviderValidationError(
                f"another enabled external-query provider {row['name']!r} exists; "
                "at most one external-query provider can be enabled"
            )

    def _row_to_cfg(self, row: sqlite3.Row) -> ExternalMemoryProviderConfig:
        probe = row["last_probe_status"]
        return ExternalMemoryProviderConfig(
            id=row["id"], name=row["name"],
            provider_type=ExternalMemoryProviderType(row["provider_type"]),
            base_url=row["base_url"],
            api_key_present=bool(row["api_key"]),
            enabled=bool(row["enabled"]),
            extra_config=json.loads(row["extra_config"] or "{}"),
            probe_status=ExternalMemoryProbeStatus(probe) if probe else None,
            last_probe_error=row["last_probe_error"],
            last_probed_at=datetime.fromisoformat(row["last_probed_at"]) if row["last_probed_at"] else None,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
