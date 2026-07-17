from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from app.domain.skill import CuratorState, CuratorStateStore


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curator_state (
            key TEXT PRIMARY KEY DEFAULT 'default',
            value TEXT NOT NULL
        )
        """
    )


def _state_to_dict(state: CuratorState) -> dict:
    return {
        "last_run_at": state.last_run_at,
        "last_run_duration_seconds": state.last_run_duration_seconds,
        "last_run_summary": state.last_run_summary,
        "last_report_path": state.last_report_path,
        "paused": state.paused,
        "run_count": state.run_count,
    }


def _dict_to_state(data: dict) -> CuratorState:
    return CuratorState(
        last_run_at=data.get("last_run_at"),
        last_run_duration_seconds=data.get("last_run_duration_seconds"),
        last_run_summary=data.get("last_run_summary"),
        last_report_path=data.get("last_report_path"),
        paused=bool(data.get("paused", False)),
        run_count=int(data.get("run_count", 0) or 0),
    )


class SqliteCuratorStateStore(CuratorStateStore):
    """SQLite 单行 KV 持久化 curator_state。

    与 sessions.db 共享 path 但独立 _connect()（类比 SkillUsageStore）。
    单行 key='default'，value 为 JSON 文本。迁移幂等。
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _initialize_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _load_sync(self) -> CuratorState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM curator_state WHERE key = ?", ("default",)
            ).fetchone()
        if row is None:
            return CuratorState()
        try:
            data = json.loads(row[0])
        except (ValueError, TypeError):
            return CuratorState()
        if not isinstance(data, dict):
            return CuratorState()
        return _dict_to_state(data)

    def _save_sync(self, state: CuratorState) -> None:
        payload = json.dumps(_state_to_dict(state), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO curator_state(key, value) VALUES (?, ?)",
                ("default", payload),
            )

    def _set_paused_sync(self, paused: bool) -> None:
        state = self._load_sync()
        new_state = CuratorState(
            last_run_at=state.last_run_at,
            last_run_duration_seconds=state.last_run_duration_seconds,
            last_run_summary=state.last_run_summary,
            last_report_path=state.last_report_path,
            paused=bool(paused),
            run_count=state.run_count,
        )
        self._save_sync(new_state)

    async def load(self) -> CuratorState:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, state: CuratorState) -> None:
        await asyncio.to_thread(self._save_sync, state)

    async def set_paused(self, paused: bool) -> None:
        await asyncio.to_thread(self._set_paused_sync, paused)
