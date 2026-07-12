# app/infrastructure/usage/sqlite_usage_recorder.py
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.domain.usage import (
    CanonicalUsage, CompressionStat, OverviewStats, SessionUsageStats,
    SessionUsageSummary, UsageCost, UsageRecord,
    compute_normalized_tokens,
)

# (column_name, sql_type, default_value)
_COLUMN_SPECS: list[tuple[str, str, str]] = [
    ("input_tokens", "INTEGER", "0"),
    ("output_tokens", "INTEGER", "0"),
    ("cache_read_tokens", "INTEGER", "0"),
    ("cache_write_tokens", "INTEGER", "0"),
    ("reasoning_tokens", "INTEGER", "0"),
    ("total_tokens", "INTEGER", "0"),
    ("api_call_count", "INTEGER", "0"),
    ("estimated_cost_usd", "REAL", "0"),
    ("cost_status", "TEXT", "'unknown'"),
    ("pricing_version", "TEXT", "NULL"),
]


class SqliteUsageRecorder:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def init(self) -> None:
        conn = self._connect()
        try:
            # Sessions table migration: add usage columns if missing.
            # Skip migration when sessions table does not exist yet (it is
            # created by SQLiteMemoryStore); migration runs on a later init().
            sessions_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone() is not None
            if sessions_exists:
                cur = conn.execute("PRAGMA table_info(sessions)")
                existing = {row[1] for row in cur.fetchall()}
                for name, sql_type, default in _COLUMN_SPECS:
                    if name not in existing:
                        conn.execute(
                            f"ALTER TABLE sessions ADD COLUMN {name} {sql_type} DEFAULT {default}"
                        )
            # usage_records table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    model TEXT, provider TEXT,
                    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
                    reasoning_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0, cost_status TEXT DEFAULT 'unknown',
                    latency_ms INTEGER, created_at TEXT NOT NULL,
                    requested_model TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """
            )
            # usage_records migration: add requested_model / trigger_type / request_messages / response_message columns if missing
            ur_cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)").fetchall()}
            if "requested_model" not in ur_cols:
                conn.execute("ALTER TABLE usage_records ADD COLUMN requested_model TEXT")
            if "trigger_type" not in ur_cols:
                conn.execute("ALTER TABLE usage_records ADD COLUMN trigger_type TEXT")
            if "request_messages" not in ur_cols:
                conn.execute("ALTER TABLE usage_records ADD COLUMN request_messages TEXT")
            if "response_message" not in ur_cols:
                conn.execute("ALTER TABLE usage_records ADD COLUMN response_message TEXT")
            if "tools" not in ur_cols:
                conn.execute("ALTER TABLE usage_records ADD COLUMN tools TEXT")
            if "generation_params" not in ur_cols:
                conn.execute("ALTER TABLE usage_records ADD COLUMN generation_params TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_records_session ON usage_records(session_id)"
            )
            # compression_stats table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS compression_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    before_tokens INTEGER, after_tokens INTEGER,
                    tokens_saved INTEGER, compression_ratio REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """
            )
            # compression_stats migration: add before/after messages columns
            cs_cols = {row[1] for row in conn.execute("PRAGMA table_info(compression_stats)").fetchall()}
            if "before_messages_json" not in cs_cols:
                conn.execute("ALTER TABLE compression_stats ADD COLUMN before_messages_json TEXT")
            if "after_messages_json" not in cs_cols:
                conn.execute("ALTER TABLE compression_stats ADD COLUMN after_messages_json TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_compression_stats_session ON compression_stats(session_id)"
            )
            conn.commit()
        finally:
            conn.close()

    async def record_call(
        self, session_id: str, model: str | None, provider: str | None,
        usage: CanonicalUsage, cost: UsageCost, latency_ms: int | None,
        requested_model: str | None = None,
        trigger_type: str | None = None,
        request_messages: str | None = None,
        response_message: str | None = None,
        tools: str | None = None,
        generation_params: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO usage_records
                   (session_id, model, provider, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, reasoning_tokens,
                    total_tokens, estimated_cost_usd, cost_status, latency_ms, created_at,
                    requested_model, trigger_type, request_messages, response_message, tools, generation_params)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, model, provider, usage.input_tokens, usage.output_tokens,
                    usage.cache_read_tokens, usage.cache_write_tokens, usage.reasoning_tokens,
                    usage.total_tokens, float(cost.amount_usd), cost.status, latency_ms, now,
                    requested_model, trigger_type, request_messages, response_message, tools, generation_params,
                ),
            )
            conn.execute(
                """UPDATE sessions SET
                       input_tokens = input_tokens + ?,
                       output_tokens = output_tokens + ?,
                       cache_read_tokens = cache_read_tokens + ?,
                       cache_write_tokens = cache_write_tokens + ?,
                       reasoning_tokens = reasoning_tokens + ?,
                       total_tokens = total_tokens + ?,
                       api_call_count = api_call_count + 1,
                       estimated_cost_usd = estimated_cost_usd + ?,
                       cost_status = ?,
                       pricing_version = ?
                   WHERE id = ?""",
                (
                    usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                    usage.cache_write_tokens, usage.reasoning_tokens, usage.total_tokens,
                    float(cost.amount_usd), cost.status, cost.pricing_version, session_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def get_session_stats(self, session_id: str) -> SessionUsageStats:
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT input_tokens, output_tokens, cache_read_tokens,
                          cache_write_tokens, reasoning_tokens, total_tokens,
                          api_call_count, estimated_cost_usd, cost_status
                   FROM sessions WHERE id = ?""",
                (session_id,),
            ).fetchone()
            if row is None:
                return SessionUsageStats(session_id=session_id)
            return SessionUsageStats(
                session_id=session_id,
                input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
                cache_read_tokens=row["cache_read_tokens"], cache_write_tokens=row["cache_write_tokens"],
                reasoning_tokens=row["reasoning_tokens"], total_tokens=row["total_tokens"],
                api_call_count=row["api_call_count"],
                estimated_cost_usd=str(row["estimated_cost_usd"]),
                cost_status=row["cost_status"],
                normalized_tokens=compute_normalized_tokens(
                    row["input_tokens"], row["cache_read_tokens"], row["output_tokens"],
                ),
            )
        finally:
            conn.close()

    async def list_records(self, session_id: str, limit: int = 50) -> list[UsageRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM usage_records WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    async def record_compression(
        self, session_id: str, before_tokens: int, after_tokens: int,
        before_messages: str | None = None,
        after_messages: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        saved = max(before_tokens - after_tokens, 0)
        ratio = (after_tokens / before_tokens) if before_tokens > 0 else 0.0
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO compression_stats
                   (session_id, before_tokens, after_tokens, tokens_saved, compression_ratio, created_at,
                    before_messages_json, after_messages_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, before_tokens, after_tokens, saved, ratio, now,
                 before_messages, after_messages),
            )
            conn.commit()
        finally:
            conn.close()

    async def list_compressions(self, session_id: str) -> list[CompressionStat]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM compression_stats WHERE session_id = ? ORDER BY created_at DESC""",
                (session_id,),
            ).fetchall()
            return [self._row_to_compression(r) for r in rows]
        finally:
            conn.close()

    async def get_overview_stats(self) -> OverviewStats:
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(api_call_count), 0) AS api_call_count,
                       COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                       COUNT(*) AS session_count
                   FROM sessions"""
            ).fetchone()
            if row is None:
                return OverviewStats()
            return OverviewStats(
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
                reasoning_tokens=row["reasoning_tokens"],
                total_tokens=row["total_tokens"],
                api_call_count=row["api_call_count"],
                estimated_cost_usd=str(row["estimated_cost_usd"]),
                session_count=row["session_count"],
                normalized_tokens=compute_normalized_tokens(
                    row["input_tokens"], row["cache_read_tokens"], row["output_tokens"],
                ),
            )
        finally:
            conn.close()

    async def list_sessions_paginated(
        self, page: int, page_size: int,
    ) -> tuple[list[SessionUsageSummary], int]:
        page = max(page, 1)
        page_size = max(min(page_size, 500), 1)
        offset = (page - 1) * page_size
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            rows = conn.execute(
                """SELECT s.id, s.title, s.created_at, s.source,
                          s.input_tokens, s.output_tokens, s.cache_read_tokens,
                          s.cache_write_tokens, s.reasoning_tokens, s.total_tokens,
                          s.api_call_count, s.estimated_cost_usd, s.cost_status,
                          (SELECT COUNT(*) FROM messages m
                           WHERE m.session_id = s.id
                             AND m.role = 'user'
                             AND m.is_summary = 0) AS turn_count
                   FROM sessions s
                   ORDER BY s.updated_at DESC, s.id DESC
                   LIMIT ? OFFSET ?""",
                (page_size, offset),
            ).fetchall()
            summaries = [
                SessionUsageSummary(
                    session_id=r["id"],
                    title=r["title"] or "",
                    created_at=r["created_at"] or "",
                    source=r["source"] or "",
                    input_tokens=r["input_tokens"],
                    output_tokens=r["output_tokens"],
                    cache_read_tokens=r["cache_read_tokens"],
                    cache_write_tokens=r["cache_write_tokens"],
                    reasoning_tokens=r["reasoning_tokens"],
                    total_tokens=r["total_tokens"],
                    api_call_count=r["api_call_count"],
                    estimated_cost_usd=str(r["estimated_cost_usd"]),
                    cost_status=r["cost_status"],
                    normalized_tokens=compute_normalized_tokens(
                        r["input_tokens"], r["cache_read_tokens"], r["output_tokens"],
                    ),
                    turn_count=r["turn_count"] if "turn_count" in r.keys() else 0,
                )
                for r in rows
            ]
            return summaries, total
        finally:
            conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> UsageRecord:
        keys = row.keys()
        requested = row["requested_model"] if "requested_model" in keys else None
        trigger = row["trigger_type"] if "trigger_type" in keys else None
        req_msgs = row["request_messages"] if "request_messages" in keys else None
        resp_msg = row["response_message"] if "response_message" in keys else None
        tools = row["tools"] if "tools" in keys else None
        gen_params = row["generation_params"] if "generation_params" in keys else None
        return UsageRecord(
            id=row["id"], session_id=row["session_id"],
            model=row["model"], provider=row["provider"],
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"], cache_write_tokens=row["cache_write_tokens"],
            reasoning_tokens=row["reasoning_tokens"], total_tokens=row["total_tokens"],
            estimated_cost_usd=str(row["estimated_cost_usd"]),
            cost_status=row["cost_status"], latency_ms=row["latency_ms"],
            created_at=row["created_at"],
            requested_model=requested,
            trigger_type=trigger,
            request_messages=req_msgs,
            response_message=resp_msg,
            tools=tools,
            generation_params=gen_params,
            normalized_tokens=compute_normalized_tokens(
                row["input_tokens"], row["cache_read_tokens"], row["output_tokens"],
            ),
        )

    def _row_to_compression(self, row: sqlite3.Row) -> CompressionStat:
        keys = row.keys()
        before_msgs = row["before_messages_json"] if "before_messages_json" in keys else None
        after_msgs = row["after_messages_json"] if "after_messages_json" in keys else None
        return CompressionStat(
            id=row["id"], session_id=row["session_id"],
            before_tokens=row["before_tokens"], after_tokens=row["after_tokens"],
            tokens_saved=row["tokens_saved"], compression_ratio=row["compression_ratio"],
            created_at=row["created_at"],
            before_messages=before_msgs,
            after_messages=after_msgs,
        )
