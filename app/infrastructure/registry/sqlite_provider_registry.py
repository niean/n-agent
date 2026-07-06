from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.domain.provider import (
    DuplicateProviderError,
    ProviderConfig,
    ProviderNotFoundError,
    ProviderRegistry,
)


class SQLiteProviderRegistry(ProviderRegistry):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS providers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    provider_type TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key TEXT,
                    extra_headers_json TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    supports_vision INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_providers_active "
                "ON providers(is_active) WHERE is_active = 1"
            )
            self._ensure_supports_vision_column(conn)

    def _ensure_supports_vision_column(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
        if "supports_vision" not in cols:
            try:
                conn.execute(
                    "ALTER TABLE providers ADD COLUMN supports_vision INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    "UPDATE providers SET supports_vision = 1 WHERE provider_type = ?",
                    ("openai-compatible",),
                )
            except sqlite3.OperationalError:
                pass  # 列已存在

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def list_providers(self) -> list[ProviderConfig]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM providers ORDER BY created_at ASC").fetchall()
        return [self._row_to_cfg(row) for row in rows]

    async def get_provider(self, provider_id: str) -> ProviderConfig | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
        return self._row_to_cfg(row) if row else None

    async def create_provider(self, config: ProviderConfig, api_key: str) -> ProviderConfig:
        provider_id = config.id or uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO providers(id, name, provider_type, base_url, model, api_key,
                        extra_headers_json, is_active, created_at, updated_at, supports_vision)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider_id,
                        config.name,
                        config.provider_type,
                        config.base_url,
                        config.model,
                        api_key or None,
                        json.dumps(config.extra_headers) if config.extra_headers else None,
                        1 if config.is_active else 0,
                        now.isoformat(),
                        now.isoformat(),
                        1 if config.supports_vision else 0,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateProviderError(str(exc)) from exc
        cfg = await self.get_provider(provider_id)
        assert cfg is not None
        return cfg

    async def update_provider(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        provider_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
        supports_vision: bool | None = None,
    ) -> ProviderConfig:
        existing = await self.get_provider(provider_id)
        if existing is None:
            raise ProviderNotFoundError(provider_id)
        sets: list[str] = []
        params: list = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if base_url is not None:
            sets.append("base_url = ?")
            params.append(base_url)
        if model is not None:
            sets.append("model = ?")
            params.append(model)
        if provider_type is not None:
            sets.append("provider_type = ?")
            params.append(provider_type)
        if extra_headers is not None:
            sets.append("extra_headers_json = ?")
            params.append(json.dumps(extra_headers))
        if clear_api_key:
            sets.append("api_key = NULL")
        elif api_key is not None:
            sets.append("api_key = ?")
            params.append(api_key)
        if supports_vision is not None:
            sets.append("supports_vision = ?")
            params.append(1 if supports_vision else 0)
        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(provider_id)
        try:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE providers SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateProviderError(str(exc)) from exc
        cfg = await self.get_provider(provider_id)
        assert cfg is not None
        return cfg

    async def delete_provider(self, provider_id: str) -> None:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            if cur.rowcount == 0:
                raise ProviderNotFoundError(provider_id)

    async def set_active(self, provider_id: str) -> ProviderConfig:
        existing = await self.get_provider(provider_id)
        if existing is None:
            raise ProviderNotFoundError(provider_id)
        with self._connect() as conn:
            conn.execute("UPDATE providers SET is_active = 0 WHERE is_active = 1")
            conn.execute("UPDATE providers SET is_active = 1 WHERE id = ?", (provider_id,))
        cfg = await self.get_provider(provider_id)
        assert cfg is not None
        return cfg

    async def get_active(self) -> ProviderConfig | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM providers WHERE is_active = 1").fetchone()
        return self._row_to_cfg(row) if row else None

    async def get_secret(self, provider_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT api_key FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
        if row is None:
            raise ProviderNotFoundError(provider_id)
        return row["api_key"]

    def _row_to_cfg(self, row: sqlite3.Row) -> ProviderConfig:
        return ProviderConfig(
            id=row["id"],
            name=row["name"],
            provider_type=row["provider_type"],
            base_url=row["base_url"],
            model=row["model"],
            api_key_present=bool(row["api_key"]),
            is_active=bool(row["is_active"]),
            extra_headers=json.loads(row["extra_headers_json"]) if row["extra_headers_json"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            supports_vision=bool(row["supports_vision"]) if "supports_vision" in row.keys() else False,
        )
