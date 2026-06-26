from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.domain.external_memory import ExternalMemoryConfigRegistry


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS external_memory_global_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled_providers TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class SQLiteExternalMemoryConfig(ExternalMemoryConfigRegistry):
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def create_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.commit()

    def get_enabled(self) -> set[str] | None:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT enabled_providers FROM external_memory_global_config WHERE id = 1"
            )
            row = cursor.fetchone()
            if row is None:
                return None
            enabled_list = json.loads(row[0])
            return set(enabled_list)

    def set_enabled(self, provider_names: list[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO external_memory_global_config
                (id, enabled_providers, updated_at)
                VALUES (1, ?, CURRENT_TIMESTAMP)
                """,
                (json.dumps(provider_names),),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn
