from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.domain.knowledge import (
    DuplicateKnowledgeBaseError,
    KnowledgeBase,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseRegistry,
    KnowledgeBaseType,
    KnowledgeProbeStatus,
)


class SQLiteKnowledgeBaseRegistry(KnowledgeBaseRegistry):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    base_type TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    api_key TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    default_top_k INTEGER,
                    default_min_score REAL,
                    last_probe_status TEXT NOT NULL,
                    last_probe_error TEXT,
                    last_probed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def list_bases(self) -> list[KnowledgeBase]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM knowledge_bases ORDER BY created_at ASC").fetchall()
        return [self._row_to_base(row) for row in rows]

    async def get_base(self, kb_id: str) -> KnowledgeBase | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM knowledge_bases WHERE id = ?", (kb_id,)).fetchone()
        return self._row_to_base(row) if row else None

    async def create_base(self, base: KnowledgeBase, api_key: str | None = None) -> KnowledgeBase:
        now = datetime.now(timezone.utc)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO knowledge_bases(
                        id, name, description, base_type, base_url, dataset_id, api_key,
                        enabled, default_top_k, default_min_score, last_probe_status,
                        last_probe_error, last_probed_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        base.id,
                        base.name,
                        base.description,
                        base.base_type.value,
                        base.base_url,
                        base.dataset_id,
                        api_key or None,
                        int(base.enabled),
                        base.default_top_k,
                        base.default_min_score,
                        base.last_probe_status.value,
                        base.last_probe_error,
                        _dt_to_str(base.last_probed_at),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateKnowledgeBaseError(str(exc)) from exc
        created = await self.get_base(base.id)
        assert created is not None
        return created

    async def update_base(
        self,
        kb_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        base_type: KnowledgeBaseType | None = None,
        base_url: str | None = None,
        dataset_id: str | None = None,
        enabled: bool | None = None,
        default_top_k: int | None = None,
        default_min_score: float | None = None,
        clear_default_top_k: bool = False,
        clear_default_min_score: bool = False,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> KnowledgeBase:
        existing = await self.get_base(kb_id)
        if existing is None:
            raise KnowledgeBaseNotFoundError(kb_id)

        sets: list[str] = []
        params: list[object] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if base_type is not None:
            sets.append("base_type = ?")
            params.append(base_type.value)
        if base_url is not None:
            sets.append("base_url = ?")
            params.append(base_url)
        if dataset_id is not None:
            sets.append("dataset_id = ?")
            params.append(dataset_id)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(int(enabled))
        if clear_default_top_k:
            sets.append("default_top_k = NULL")
        elif default_top_k is not None:
            sets.append("default_top_k = ?")
            params.append(default_top_k)
        if clear_default_min_score:
            sets.append("default_min_score = NULL")
        elif default_min_score is not None:
            sets.append("default_min_score = ?")
            params.append(default_min_score)
        if clear_api_key or api_key == "":
            sets.append("api_key = NULL")
        elif api_key is not None:
            sets.append("api_key = ?")
            params.append(api_key)

        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(kb_id)

        try:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE knowledge_bases SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateKnowledgeBaseError(str(exc)) from exc

        updated = await self.get_base(kb_id)
        assert updated is not None
        return updated

    async def delete_base(self, kb_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb_id,))
            if cursor.rowcount == 0:
                raise KnowledgeBaseNotFoundError(kb_id)

    async def get_secret(self, kb_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT api_key FROM knowledge_bases WHERE id = ?", (kb_id,)).fetchone()
        if row is None:
            raise KnowledgeBaseNotFoundError(kb_id)
        return row["api_key"]

    async def update_probe_status(
        self,
        kb_id: str,
        status: KnowledgeProbeStatus,
        error: str | None = None,
        probed_at: datetime | None = None,
    ) -> None:
        probe_time = probed_at or datetime.now(timezone.utc)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_bases
                SET last_probe_status = ?, last_probe_error = ?, last_probed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, error, probe_time.isoformat(), now, kb_id),
            )
            if cursor.rowcount == 0:
                raise KnowledgeBaseNotFoundError(kb_id)

    def _row_to_base(self, row: sqlite3.Row) -> KnowledgeBase:
        return KnowledgeBase(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            base_type=KnowledgeBaseType(row["base_type"]),
            base_url=row["base_url"],
            dataset_id=row["dataset_id"],
            api_key_present=bool(row["api_key"]),
            enabled=bool(row["enabled"]),
            default_top_k=row["default_top_k"],
            default_min_score=row["default_min_score"],
            last_probe_status=KnowledgeProbeStatus(row["last_probe_status"]),
            last_probe_error=row["last_probe_error"],
            last_probed_at=_dt_from_str(row["last_probed_at"]),
            created_at=_dt_from_str(row["created_at"]),
            updated_at=_dt_from_str(row["updated_at"]),
        )


def _dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_from_str(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
