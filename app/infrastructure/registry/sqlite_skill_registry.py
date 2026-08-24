from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.domain.skill import (
    Skill,
    SkillNotFoundError,
    SkillReadiness,
    SkillRegistry,
    SkillSource,
    SkillValidationError,
)
from app.domain.skill_format import skill_frontmatter_from_dict


def _initialize_skill_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            description TEXT,
            platforms_json TEXT NOT NULL DEFAULT '[]',
            frontmatter_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            readiness TEXT NOT NULL,
            last_scan_status TEXT,
            last_scan_error TEXT,
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            chat_selectable INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_skills_enabled ON skills(enabled);
        """
    )
    _migrate_add_source_column(conn)
    _migrate_add_chat_selectable_column(conn)


def _migrate_add_source_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add `source` column to legacy skills tables.

    Existing rows default to 'user' (SkillSource.USER).
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
    if "source" not in columns:
        conn.execute("ALTER TABLE skills ADD COLUMN source TEXT NOT NULL DEFAULT 'user'")


def _migrate_add_chat_selectable_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add `chat_selectable` column to legacy skills tables.

    Existing rows default to 1 (True). Mirrors the `source` migration.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
    if "chat_selectable" not in columns:
        conn.execute(
            "ALTER TABLE skills ADD COLUMN chat_selectable INTEGER NOT NULL DEFAULT 1"
        )


class SQLiteSkillRegistry(SkillRegistry):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _initialize_skill_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def list_skills(self, include_disabled: bool = True) -> list[Skill]:
        with self._connect() as conn:
            if include_disabled:
                rows = conn.execute("SELECT * FROM skills ORDER BY name").fetchall()
            else:
                rows = conn.execute("SELECT * FROM skills WHERE enabled = 1 ORDER BY name").fetchall()
        return [_skill_from_row(row) for row in rows]

    async def get_skill(self, name: str) -> Skill | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
        return _skill_from_row(row) if row else None

    async def upsert_skill(self, skill: Skill) -> Skill:
        now = datetime.now(timezone.utc)
        try:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id, created_at FROM skills WHERE name = ?", (skill.name,)
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE skills
                        SET relative_path = ?, description = ?, platforms_json = ?, frontmatter_json = ?,
                            enabled = ?, readiness = ?, last_scan_status = ?, last_scan_error = ?,
                            last_seen_at = ?, updated_at = ?, source = ?, chat_selectable = ?
                        WHERE name = ?
                        """,
                        (
                            skill.relative_path, skill.description,
                            json.dumps(skill.platforms), json.dumps(skill.frontmatter.raw),
                            int(skill.enabled), skill.readiness.value,
                            skill.last_scan_status, skill.last_scan_error,
                            _dt_str(skill.last_seen_at), now.isoformat(),
                            skill.source.value, int(skill.chat_selectable),
                            skill.name,
                        ),
                    )
                else:
                    created_at = skill.created_at or now
                    conn.execute(
                        """
                        INSERT INTO skills(id, name, relative_path, description, platforms_json, frontmatter_json,
                            enabled, readiness, last_scan_status, last_scan_error, last_seen_at, created_at, updated_at, source, chat_selectable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            skill.id, skill.name, skill.relative_path, skill.description,
                            json.dumps(skill.platforms), json.dumps(skill.frontmatter.raw),
                            int(skill.enabled), skill.readiness.value,
                            skill.last_scan_status, skill.last_scan_error,
                            _dt_str(skill.last_seen_at), created_at.isoformat(), now.isoformat(),
                            skill.source.value, int(skill.chat_selectable),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise SkillValidationError("skill name already exists") from exc
        result = await self.get_skill(skill.name)
        assert result is not None
        return result

    async def delete_skill(self, name: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM skills WHERE name = ?", (name,))
            return cursor.rowcount > 0

    async def set_enabled(self, name: str, enabled: bool) -> Skill:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE skills SET enabled = ?, updated_at = ? WHERE name = ?",
                (int(enabled), now, name),
            )
            if cursor.rowcount == 0:
                raise SkillNotFoundError(name)
        skill = await self.get_skill(name)
        assert skill is not None
        return skill

    async def set_chat_selectable(self, name: str, value: bool) -> Skill:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE skills SET chat_selectable = ?, updated_at = ? WHERE name = ?",
                (int(bool(value)), now, name),
            )
            if cursor.rowcount == 0:
                raise SkillNotFoundError(name)
        skill = await self.get_skill(name)
        assert skill is not None
        return skill

    async def replace_all_skills(self, skills: Iterable[Skill]) -> list[Skill]:
        skills_list = list(skills)
        names = {s.name for s in skills_list}
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            existing_rows = conn.execute(
                "SELECT name, enabled, created_at, source, chat_selectable FROM skills"
            ).fetchall()
            existing = {
                row["name"]: (
                    bool(row["enabled"]),
                    row["created_at"],
                    row["source"],
                    bool(row["chat_selectable"]) if "chat_selectable" in row.keys() else True,
                )
                for row in existing_rows
            }
            if names:
                placeholders = ",".join("?" for _ in names)
                conn.execute(
                    f"DELETE FROM skills WHERE name NOT IN ({placeholders})",
                    tuple(names),
                )
            else:
                conn.execute("DELETE FROM skills")
            for skill in skills_list:
                prev = existing.get(skill.name)
                enabled = prev[0] if prev else skill.enabled
                created_at = (
                    datetime.fromisoformat(prev[1]) if prev else (skill.created_at or now)
                )
                # Preserve the EXISTING source so a rescan does not downgrade an
                # agent-created (source=agent) or seed skill back to USER.
                source_value = prev[2] if prev else skill.source.value
                # Preserve the EXISTING chat_selectable so a rescan does not
                # reset a runtime "hide from chat" setting back to default.
                chat_selectable_value = prev[3] if prev else skill.chat_selectable
                if prev:
                    conn.execute(
                        """
                        UPDATE skills
                        SET relative_path = ?, description = ?, platforms_json = ?, frontmatter_json = ?,
                            enabled = ?, readiness = ?, last_scan_status = ?, last_scan_error = ?,
                            last_seen_at = ?, updated_at = ?, source = ?, chat_selectable = ?
                        WHERE name = ?
                        """,
                        (
                            skill.relative_path, skill.description,
                            json.dumps(skill.platforms), json.dumps(skill.frontmatter.raw),
                            int(enabled), skill.readiness.value,
                            skill.last_scan_status, skill.last_scan_error,
                            now.isoformat(), now.isoformat(), source_value,
                            int(chat_selectable_value), skill.name,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO skills(id, name, relative_path, description, platforms_json, frontmatter_json,
                            enabled, readiness, last_scan_status, last_scan_error, last_seen_at, created_at, updated_at, source, chat_selectable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            skill.id, skill.name, skill.relative_path, skill.description,
                            json.dumps(skill.platforms), json.dumps(skill.frontmatter.raw),
                            int(enabled), skill.readiness.value,
                            skill.last_scan_status, skill.last_scan_error,
                            now.isoformat(), created_at.isoformat(), now.isoformat(),
                            source_value, int(chat_selectable_value),
                        ),
                    )
        return await self.list_skills(include_disabled=True)


def _skill_from_row(row: sqlite3.Row) -> Skill:
    raw = json.loads(row["frontmatter_json"] or "{}")
    platforms = json.loads(row["platforms_json"] or "[]")
    # Reuse the same metadata-aware construction as the file loader so that
    # normalized raw (legacy fields sunk to metadata) round-trips correctly.
    fm = skill_frontmatter_from_dict(raw, row["name"], platforms)
    # source column may be absent on legacy rows read mid-migration; default to USER.
    try:
        source_value = row["source"]
    except (IndexError, KeyError):
        source_value = None
    try:
        source = SkillSource(source_value) if source_value else SkillSource.USER
    except ValueError:
        source = SkillSource.USER
    # chat_selectable column may be absent on legacy rows read mid-migration; default True.
    try:
        chat_selectable = bool(row["chat_selectable"])
    except (IndexError, KeyError):
        chat_selectable = True
    return Skill(
        id=row["id"], name=row["name"], relative_path=row["relative_path"],
        description=row["description"] or "",
        platforms=platforms,
        frontmatter=fm, enabled=bool(row["enabled"]),
        readiness=SkillReadiness(row["readiness"]),
        last_scan_status=row["last_scan_status"], last_scan_error=row["last_scan_error"],
        last_seen_at=_dt_parse(row["last_seen_at"]),
        created_at=_dt_parse(row["created_at"]),
        updated_at=_dt_parse(row["updated_at"]),
        source=source,
        chat_selectable=chat_selectable,
    )


def _dt_str(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
