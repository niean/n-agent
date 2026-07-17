from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.domain.skill import CuratorSkillReport, SkillUsage, SkillUsageRegistry


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_usage (
            name TEXT PRIMARY KEY,
            created_by TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 0,
            view_count INTEGER NOT NULL DEFAULT 0,
            patch_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            last_used_at TEXT,
            last_viewed TEXT,
            last_patched_at TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            pinned INTEGER NOT NULL DEFAULT 0,
            archived_at TEXT
        )
        """
    )


def _dt_str(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_usage(row: sqlite3.Row) -> SkillUsage:
    return SkillUsage(
        created_by=row["created_by"],
        use_count=row["use_count"],
        view_count=row["view_count"],
        patch_count=row["patch_count"],
        created_at=_dt_parse(row["created_at"]),
        last_used_at=_dt_parse(row["last_used_at"]),
        last_viewed=_dt_parse(row["last_viewed"]),
        last_patched_at=_dt_parse(row["last_patched_at"]),
        state=row["state"],
        pinned=bool(row["pinned"]),
        archived_at=_dt_parse(row["archived_at"]),
    )


# Default origin for usage rows created on-demand by increment/set methods.
_DEFAULT_CREATED_BY = "foreground"


class SkillUsageStore(SkillUsageRegistry):
    """SQLite-backed implementation of :class:`SkillUsageRegistry`.

    Each public method is async and delegates the synchronous ``sqlite3`` work
    to ``asyncio.to_thread`` so the event loop is never blocked. Every call
    opens an independent short-lived connection (mirroring
    ``SqliteSkillRegistry``/``SqliteProviderRegistry``).
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _initialize_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # synchronous helpers (run inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _get_sync(self, name: str) -> SkillUsage | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_usage WHERE name = ?", (name,)
            ).fetchone()
        return _row_to_usage(row) if row else None

    def _upsert_sync(self, name: str, usage: SkillUsage) -> SkillUsage:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO skill_usage(
                    name, created_by, use_count, view_count, patch_count,
                    created_at, last_used_at, last_viewed, last_patched_at,
                    state, pinned, archived_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    usage.created_by,
                    usage.use_count,
                    usage.view_count,
                    usage.patch_count,
                    _dt_str(usage.created_at),
                    _dt_str(usage.last_used_at),
                    _dt_str(usage.last_viewed),
                    _dt_str(usage.last_patched_at),
                    usage.state,
                    int(usage.pinned),
                    _dt_str(usage.archived_at),
                ),
            )
        result = self._get_sync(name)
        assert result is not None
        return result

    def _ensure_row_sync(self, conn: sqlite3.Connection, name: str) -> None:
        """Insert a default row for ``name`` if it does not already exist."""
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO skill_usage(
                name, created_by, use_count, view_count, patch_count,
                created_at, last_used_at, last_viewed, last_patched_at,
                state, pinned, archived_at
            )
            VALUES (?, ?, 0, 0, 0, ?, NULL, NULL, NULL, 'active', 0, NULL)
            """,
            (name, _DEFAULT_CREATED_BY, now),
        )

    def _increment_use_sync(self, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)
            conn.execute(
                "UPDATE skill_usage SET use_count = use_count + 1, last_used_at = ? "
                "WHERE name = ?",
                (now, name),
            )

    def _increment_view_sync(self, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)
            conn.execute(
                "UPDATE skill_usage SET view_count = view_count + 1, last_viewed = ? "
                "WHERE name = ?",
                (now, name),
            )

    def _increment_patch_sync(self, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)
            conn.execute(
                "UPDATE skill_usage SET patch_count = patch_count + 1, last_patched_at = ? "
                "WHERE name = ?",
                (now, name),
            )

    def _set_state_sync(self, name: str, state: str) -> None:
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)
            conn.execute(
                "UPDATE skill_usage SET state = ? WHERE name = ?",
                (state, name),
            )

    def _set_pinned_sync(self, name: str, pinned: bool) -> None:
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)
            conn.execute(
                "UPDATE skill_usage SET pinned = ? WHERE name = ?",
                (int(pinned), name),
            )

    def _list_curator_managed_sync(
        self, prune_seeds: bool, protected_names: set[str]
    ) -> list[CuratorSkillReport]:
        """列出 curator-managed skill：source=AGENT 总是纳入；source=SEED 仅
        prune_seeds=True 时纳入；排除 protected_names。LEFT JOIN skills 表取
        source，无 usage 记录的 skill 返回 _persisted=False（Service 锚定）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.name, s.source,
                       su.use_count, su.view_count, su.patch_count,
                       su.created_at AS su_created_at,
                       su.last_used_at, su.last_viewed, su.last_patched_at,
                       su.state, su.pinned, su.archived_at
                FROM skills s
                LEFT JOIN skill_usage su ON s.name = su.name
                """
            ).fetchall()
        reports: list[CuratorSkillReport] = []
        for row in rows:
            name = row["name"]
            if name in protected_names:
                continue
            source = row["source"] or "user"
            if source == "agent":
                pass
            elif source == "seed" and prune_seeds:
                pass
            else:
                continue
            use_count = int(row["use_count"] or 0)
            view_count = int(row["view_count"] or 0)
            patch_count = int(row["patch_count"] or 0)
            created_at = _dt_parse(row["su_created_at"])
            last_used = _dt_parse(row["last_used_at"])
            last_viewed = _dt_parse(row["last_viewed"])
            last_patched = _dt_parse(row["last_patched_at"])
            state = row["state"] or "active"
            pinned = bool(row["pinned"])
            activity_dts = [d for d in (last_used, last_viewed, last_patched) if d]
            last_activity_at = max(activity_dts).isoformat() if activity_dts else None
            activity_count = use_count + view_count + patch_count
            persisted = row["use_count"] is not None
            reports.append(
                CuratorSkillReport(
                    name=name,
                    source=source,
                    state=state,
                    pinned=pinned,
                    use_count=use_count,
                    view_count=view_count,
                    patch_count=patch_count,
                    created_at=created_at,
                    last_used_at=last_used,
                    last_viewed=last_viewed,
                    last_patched_at=last_patched,
                    last_activity_at=last_activity_at,
                    activity_count=activity_count,
                    _persisted=persisted,
                )
            )
        return reports

    def _seed_record_if_missing_sync(self, name: str) -> None:
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)

    def _archive_skill_sync(self, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)
            conn.execute(
                "UPDATE skill_usage SET state = ?, archived_at = ? WHERE name = ?",
                ("archived", now, name),
            )

    def _restore_skill_sync(self, name: str) -> None:
        with self._connect() as conn:
            self._ensure_row_sync(conn, name)
            conn.execute(
                "UPDATE skill_usage SET state = ?, archived_at = NULL WHERE name = ?",
                ("active", name),
            )

    # ------------------------------------------------------------------
    # async public API (SkillUsageRegistry)
    # ------------------------------------------------------------------

    async def get(self, name: str) -> SkillUsage | None:
        return await asyncio.to_thread(self._get_sync, name)

    async def upsert(self, name: str, usage: SkillUsage) -> SkillUsage:
        return await asyncio.to_thread(self._upsert_sync, name, usage)

    async def increment_use(self, name: str) -> None:
        await asyncio.to_thread(self._increment_use_sync, name)

    async def increment_view(self, name: str) -> None:
        await asyncio.to_thread(self._increment_view_sync, name)

    async def increment_patch(self, name: str) -> None:
        await asyncio.to_thread(self._increment_patch_sync, name)

    async def set_state(self, name: str, state: str) -> None:
        await asyncio.to_thread(self._set_state_sync, name, state)

    async def set_pinned(self, name: str, pinned: bool) -> None:
        await asyncio.to_thread(self._set_pinned_sync, name, pinned)

    async def list_curator_managed(
        self, prune_seeds: bool, protected_names: set[str]
    ) -> list[CuratorSkillReport]:
        return await asyncio.to_thread(
            self._list_curator_managed_sync, prune_seeds, protected_names
        )

    async def seed_record_if_missing(self, name: str) -> None:
        await asyncio.to_thread(self._seed_record_if_missing_sync, name)

    async def archive_skill(self, name: str) -> None:
        await asyncio.to_thread(self._archive_skill_sync, name)

    async def restore_skill(self, name: str) -> None:
        await asyncio.to_thread(self._restore_skill_sync, name)
