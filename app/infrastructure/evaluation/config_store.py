"""SQLite evaluation_config 单行存储（T20；镜像 SqliteTaskConfigStore 房规）。

- id=1 CHECK 单行；version 列 CAS；BEGIN IMMEDIATE；禁止 INSERT OR
  IGNORE（首写竞争不得绕过 CAS）；
- 严格解析：损坏 JSON / 非对象 / 未知键 / 非法类型一律抛
  EvaluationConfigStoreError，绝不静默回退 env——管理面必须显式暴露
  损坏，运行面（current()）捕获后走 last-known-good；
- 独立于 task_config 表（评估子域独立账本 locals/evaluations.db，
  配置行与账本同库存储）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.domain.evaluation.config import (
    EvaluationConfigConflictError,
    EvaluationConfigOverrides,
    EvaluationConfigStore,
    EvaluationConfigStoreError,
    StoredEvaluationConfig,
)
from app.infrastructure.sqlite_support import open_sqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT    NOT NULL,
    version     INTEGER NOT NULL CHECK (version >= 1),
    updated_at  TEXT    NOT NULL,
    updated_by  TEXT    NOT NULL
)
"""


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteEvaluationConfigStore(EvaluationConfigStore):
    """SQLite 单行 evaluation_config 存储（CAS 乐观锁）。"""

    def __init__(
        self, db_path: str, *, initialize: bool = True, read_only: bool = False
    ) -> None:
        self.path = Path(db_path)
        self._read_only = read_only
        if not initialize or read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _initialize_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = open_sqlite(self.path, read_only=self._read_only)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_sync(self) -> StoredEvaluationConfig | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT config_json, version, updated_at, updated_by "
                "FROM evaluation_config WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        config_json, version, updated_at, updated_by = row
        try:
            data = json.loads(config_json)
        except (ValueError, TypeError) as exc:
            raise EvaluationConfigStoreError(
                f"config_json 不是合法 JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise EvaluationConfigStoreError("config_json 必须是 JSON 对象")
        overrides = EvaluationConfigOverrides.from_dict(data)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise EvaluationConfigStoreError("version 必须是 >= 1 的整数")
        return StoredEvaluationConfig(
            overrides=overrides,
            version=version,
            updated_at=str(updated_at),
            updated_by=str(updated_by),
        )

    async def get(self) -> StoredEvaluationConfig | None:
        return await asyncio.to_thread(self._get_sync)

    def get_sync(self) -> StoredEvaluationConfig | None:
        """同步读取（单行本地 SQLite，阻塞微秒级；组合根同步钩子使用）。"""
        return self._get_sync()

    def _save_sync(
        self,
        overrides: EvaluationConfigOverrides,
        expected_version: int,
        updated_by: str,
    ) -> StoredEvaluationConfig:
        now = _now_utc_iso()
        payload = json.dumps(overrides.to_dict(), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT version FROM evaluation_config WHERE id = 1"
                ).fetchone()
                current_version = row["version"] if row is not None else 0
                if current_version != expected_version:
                    conn.rollback()
                    raise EvaluationConfigConflictError(
                        f"version mismatch: expected {expected_version}, "
                        f"got {current_version}"
                    )
                next_version = current_version + 1
                conn.execute(
                    "INSERT INTO evaluation_config (id, config_json, "
                    "version, updated_at, updated_by) VALUES (1, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET config_json = excluded.config_json, "
                    "version = excluded.version, updated_at = excluded.updated_at, "
                    "updated_by = excluded.updated_by",
                    (payload, next_version, now, updated_by),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return StoredEvaluationConfig(
            overrides=overrides,
            version=next_version,
            updated_at=now,
            updated_by=updated_by,
        )

    async def save(
        self,
        overrides: EvaluationConfigOverrides,
        expected_version: int,
        updated_by: str,
    ) -> StoredEvaluationConfig:
        return await asyncio.to_thread(
            self._save_sync, overrides, expected_version, updated_by
        )
