"""SQLite persistence for the Artifact subdomain (Infrastructure Layer).

Implements the ``ArtifactRegistry`` async Protocol from
``app/domain/artifact.py``. Shares the sessions.db path but opens
independent connections. Enables WAL, foreign_keys, and busy_timeout per
connection. Async methods wrap sync sqlite3 via ``asyncio.to_thread``.

Two tables:
  - ``artifacts``: artifact aggregate root rows with a unique source key
    ``(source_kind, source_ref)`` and query indexes on ``updated_at+id``,
    ``source_kind``, ``kind``, ``status``.
  - ``published_artifacts``: immutable published snapshots with a nullable
    FK to ``artifacts.id`` (ON DELETE SET NULL) and a partial unique index
    ensuring at most one ``status='active'`` row per non-null artifact_id.

``register_published`` performs replacement in a single ``BEGIN IMMEDIATE``
transaction: revoke old active (if ``revoke_artifact_id`` set), insert new
active, sync ArtifactStatus to PUBLISHED -- with full rollback on exception.
On concurrent unique-violation for the same artifact_id, the existing active
publish is reread and returned.

``list_attachment_sources`` reads from the ``task_attachments`` table
(created by ``sqlite_task_registry``); it does NOT create or modify that
table. The backfill query uses a stable cursor by attachment id.

datetime<->storage: all datetimes stored as UTC ISO-8601 strings; conversion
happens at the registry boundary (``_dt_to_str`` / ``_str_to_dt``).
JSON fields use ``ensure_ascii=False`` (project convention for user-facing
JSON).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path

from app.domain.artifact import (
    Artifact,
    ArtifactAttachmentSource,
    ArtifactConflictError,
    ArtifactDeleteGraph,
    ArtifactError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactListPage,
    ArtifactNotFoundError,
    ArtifactRevision,
    ArtifactRevisionConflictError,
    ArtifactRevisionNotFoundError,
    ArtifactRevisionValidationError,
    ArtifactSource,
    ArtifactStatus,
    PublishedArtifact,
    PublishedArtifactStatus,
    RevisionListCursor,
    RevisionListPage,
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

ARTIFACT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    mime TEXT NOT NULL,
    content_ref TEXT,
    inline_content TEXT,
    size INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_context_ref TEXT,
    source_session_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    classification TEXT,
    labels_json TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    current_revision_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_source
    ON artifacts(source_kind, source_ref);
CREATE INDEX IF NOT EXISTS idx_artifacts_updated_at_id
    ON artifacts(updated_at, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_source_kind
    ON artifacts(source_kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind
    ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_status
    ON artifacts(status);

CREATE TABLE IF NOT EXISTS artifact_revisions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    parent_revision_id TEXT,
    rollback_from_revision_id TEXT,
    content_ref TEXT,
    inline_content TEXT,
    size INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    mime TEXT NOT NULL,
    kind TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    source_session_id TEXT,
    source_run_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, revision_number),
    CHECK (revision_number >= 1),
    CHECK (size >= 0),
    CHECK ((content_ref IS NOT NULL) + (inline_content IS NOT NULL) = 1),
    CHECK (content_ref IS NULL OR content_ref <> ''),
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_revision_id) REFERENCES artifact_revisions(id) ON DELETE SET NULL,
    FOREIGN KEY(rollback_from_revision_id) REFERENCES artifact_revisions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_list
    ON artifact_revisions(artifact_id, revision_number DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_revisions_checksum ON artifact_revisions(checksum);
CREATE INDEX IF NOT EXISTS idx_revisions_created_at ON artifact_revisions(created_at);

CREATE TABLE IF NOT EXISTS published_artifacts (
    publish_id TEXT PRIMARY KEY,
    artifact_id TEXT,
    snapshot_name TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL,
    snapshot_mime TEXT NOT NULL,
    snapshot_content_ref TEXT,
    snapshot_inline_content TEXT,
    snapshot_size INTEGER NOT NULL,
    snapshot_checksum TEXT NOT NULL,
    snapshot_summary TEXT NOT NULL DEFAULT '',
    published_by TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    revoked_at TEXT,
    published_revision_id TEXT,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL,
    FOREIGN KEY(published_revision_id) REFERENCES artifact_revisions(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_published_active_artifact
    ON published_artifacts(artifact_id)
    WHERE status = 'active' AND artifact_id IS NOT NULL;
"""

# ---------------------------------------------------------------------------
# Helpers (datetime / json conversion at boundary)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dt_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_utc(value).isoformat()


def _str_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _labels_to_json(labels: tuple[str, ...] | None) -> str | None:
    if labels is None:
        return None
    return json.dumps(list(labels), ensure_ascii=False)


def _parse_labels_json(value: str | None) -> tuple[str, ...] | None:
    """Parse labels_json with strict error on corruption.

    NULL or empty string -> None. Corrupted JSON or non-array JSON ->
    ArtifactError (not a silent default).
    """
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ArtifactError(f"corrupted labels_json: {exc}") from exc
    if not isinstance(parsed, list):
        raise ArtifactError(
            f"labels_json must be a JSON array, got {type(parsed).__name__}"
        )
    return tuple(str(item) for item in parsed)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SQLiteArtifactRegistry:
    """SQLite implementation of the ArtifactRegistry async Protocol.

    Shares the sessions.db path but opens independent connections. Each
    connection enables WAL, foreign_keys, and busy_timeout (5000ms).
    """

    BUSY_TIMEOUT_MS = 5000

    _INSERT_ARTIFACT_SQL = """
        INSERT INTO artifacts (
            id, name, kind, mime, content_ref, inline_content, size,
            checksum, source_kind, source_ref, source_context_ref,
            source_session_id, summary,
            classification, labels_json, status, created_by, created_at,
            updated_at, current_revision_id
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """

    _INSERT_PUBLISHED_SQL = """
        INSERT INTO published_artifacts (
            publish_id, artifact_id, snapshot_name, snapshot_kind,
            snapshot_mime, snapshot_content_ref, snapshot_inline_content,
            snapshot_size, snapshot_checksum, snapshot_summary,
            published_by, published_at, status, revoked_at,
            published_revision_id
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """

    _INSERT_REVISION_SQL = """
        INSERT INTO artifact_revisions (
            id, artifact_id, revision_number, parent_revision_id,
            rollback_from_revision_id, content_ref, inline_content,
            size, checksum, mime, kind, change_summary, created_by,
            source_session_id, source_run_id, created_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
        return conn

    def _ensure_schema(self) -> None:
        """Idempotent schema creation for artifacts + published_artifacts."""
        with self._connect() as conn:
            conn.executescript(ARTIFACT_SCHEMA_SQL)
            self._migrate_add_source_session_id(conn)
            self._migrate_add_revision_columns(conn)
            conn.commit()

    @staticmethod
    def _migrate_add_source_session_id(conn: sqlite3.Connection) -> None:
        """Idempotent migration: add source_session_id column if absent.

        Existing artifacts tables predate the session-association column;
        ADD COLUMN is idempotent via PRAGMA table_info check. The
        source_session_id index is created here (not in ARTIFACT_SCHEMA_SQL)
        so old DBs do not fail on CREATE INDEX before the column exists.
        """
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(artifacts)")
        }
        if "source_session_id" not in cols:
            conn.execute(
                "ALTER TABLE artifacts ADD COLUMN source_session_id TEXT"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifacts_source_session_id "
            "ON artifacts(source_session_id)"
        )

    @staticmethod
    def _migrate_add_revision_columns(conn: sqlite3.Connection) -> None:
        """Idempotent migration: add current_revision_id to artifacts and
        published_revision_id to published_artifacts if absent.

        For old DBs predating the revision feature, these columns are added
        via ALTER TABLE.  FK constraints cannot be added via ALTER TABLE ADD
        COLUMN in older SQLite builds with self-referencing tables, so the
        columns are plain TEXT with application-layer validation.  The FK
        constraints on artifact_revisions (artifact_id, parent_revision_id,
        rollback_from_revision_id) are defined in the CREATE TABLE statement
        and apply to new DBs.
        """
        art_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(artifacts)")
        }
        if "current_revision_id" not in art_cols:
            conn.execute(
                "ALTER TABLE artifacts ADD COLUMN current_revision_id TEXT"
            )
        pub_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(published_artifacts)")
        }
        if "published_revision_id" not in pub_cols:
            conn.execute(
                "ALTER TABLE published_artifacts ADD COLUMN published_revision_id TEXT"
            )

    # ------------------------------------------------------------------
    # Row -> domain object conversion
    # ------------------------------------------------------------------

    def _row_to_artifact(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=row["id"],
            name=row["name"],
            kind=ArtifactKind(row["kind"]),
            mime=row["mime"],
            content_ref=row["content_ref"],
            inline_content=row["inline_content"],
            size=row["size"],
            checksum=row["checksum"],
            source_kind=ArtifactSource(row["source_kind"]),
            source_ref=row["source_ref"],
            source_context_ref=row["source_context_ref"],
            source_session_id=row["source_session_id"],
            summary=row["summary"] or "",
            classification=row["classification"],
            labels=_parse_labels_json(row["labels_json"]),
            status=ArtifactStatus(row["status"]),
            created_by=row["created_by"] or "",
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
            current_revision_id=row["current_revision_id"],
        )

    def _row_to_published(self, row: sqlite3.Row) -> PublishedArtifact:
        return PublishedArtifact(
            publish_id=row["publish_id"],
            artifact_id=row["artifact_id"],
            snapshot_name=row["snapshot_name"],
            snapshot_kind=ArtifactKind(row["snapshot_kind"]),
            snapshot_mime=row["snapshot_mime"],
            snapshot_content_ref=row["snapshot_content_ref"],
            snapshot_inline_content=row["snapshot_inline_content"],
            snapshot_size=row["snapshot_size"],
            snapshot_checksum=row["snapshot_checksum"],
            snapshot_summary=row["snapshot_summary"] or "",
            published_at=_str_to_dt(row["published_at"]),
            published_by=row["published_by"] or "",
            status=PublishedArtifactStatus(row["status"]),
            revoked_at=_str_to_dt(row["revoked_at"]),
            published_revision_id=row["published_revision_id"],
        )

    def _row_to_revision(self, row: sqlite3.Row) -> ArtifactRevision:
        return ArtifactRevision(
            id=row["id"],
            artifact_id=row["artifact_id"],
            revision_number=row["revision_number"],
            parent_revision_id=row["parent_revision_id"],
            rollback_from_revision_id=row["rollback_from_revision_id"],
            content_ref=row["content_ref"],
            inline_content=row["inline_content"],
            size=row["size"],
            checksum=row["checksum"],
            mime=row["mime"],
            kind=ArtifactKind(row["kind"]),
            created_at=_str_to_dt(row["created_at"]),
            change_summary=row["change_summary"] or "",
            created_by=row["created_by"] or "",
            source_session_id=row["source_session_id"],
            source_run_id=row["source_run_id"],
        )

    @staticmethod
    def _row_to_attachment_source(row: sqlite3.Row) -> ArtifactAttachmentSource:
        return ArtifactAttachmentSource(
            attachment_id=row["id"],
            task_id=row["task_id"],
            stored_name=row["stored_name"],
            filename=row["filename"],
            content_type=row["content_type"],
            size=row["size"],
            checksum=row["checksum"],
            uploaded_by=row["uploaded_by"],
            created_at=_str_to_dt(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # Params builders
    # ------------------------------------------------------------------

    def _artifact_params(self, artifact: Artifact, now: datetime) -> tuple:
        created_at = artifact.created_at or now
        updated_at = artifact.updated_at or now
        return (
            artifact.id,
            artifact.name,
            artifact.kind.value,
            artifact.mime,
            artifact.content_ref,
            artifact.inline_content,
            artifact.size,
            artifact.checksum,
            artifact.source_kind.value,
            artifact.source_ref,
            artifact.source_context_ref,
            artifact.source_session_id,
            artifact.summary,
            artifact.classification,
            _labels_to_json(artifact.labels),
            artifact.status.value,
            artifact.created_by,
            _dt_to_str(created_at),
            _dt_to_str(updated_at),
            artifact.current_revision_id,
        )

    def _published_params(
        self, published: PublishedArtifact, now: datetime,
    ) -> tuple:
        published_at = published.published_at or now
        return (
            published.publish_id,
            published.artifact_id,
            published.snapshot_name,
            published.snapshot_kind.value,
            published.snapshot_mime,
            published.snapshot_content_ref,
            published.snapshot_inline_content,
            published.snapshot_size,
            published.snapshot_checksum,
            published.snapshot_summary,
            published.published_by,
            _dt_to_str(published_at),
            published.status.value,
            _dt_to_str(published.revoked_at),
            published.published_revision_id,
        )

    @staticmethod
    def _revision_params(revision: ArtifactRevision) -> tuple:
        return (
            revision.id,
            revision.artifact_id,
            revision.revision_number,
            revision.parent_revision_id,
            revision.rollback_from_revision_id,
            revision.content_ref,
            revision.inline_content,
            revision.size,
            revision.checksum,
            revision.mime,
            revision.kind.value,
            revision.change_summary,
            revision.created_by,
            revision.source_session_id,
            revision.source_run_id,
            _dt_to_str(revision.created_at),
        )

    # ------------------------------------------------------------------
    # Artifact CRUD (sync implementations)
    # ------------------------------------------------------------------

    def _create_artifact_sync(self, artifact: Artifact) -> Artifact:
        now = _now()
        created_at = artifact.created_at or now
        updated_at = artifact.updated_at or now
        resolved = dataclass_replace(
            artifact, created_at=created_at, updated_at=updated_at,
        )
        with self._connect() as conn:
            try:
                conn.execute(
                    self._INSERT_ARTIFACT_SQL,
                    self._artifact_params(resolved, now),
                )
            except sqlite3.IntegrityError as e:
                conn.rollback()
                raise ArtifactConflictError(
                    f"create_artifact integrity error: {e}"
                ) from e
        return resolved

    def _get_artifact_sync(self, artifact_id: str) -> Artifact | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,),
            ).fetchone()
        return self._row_to_artifact(row) if row else None

    def _list_artifacts_sync(
        self,
        *,
        source_kind: ArtifactSource | None = None,
        source_context_ref: str | None = None,
        source_session_id: str | None = None,
        kind: ArtifactKind | None = None,
        status: ArtifactStatus | None = None,
        q: str | None = None,
        cursor: ArtifactListCursor | None = None,
        limit: int = 50,
    ) -> ArtifactListPage:
        limit = max(1, min(limit, 500))
        conditions: list[str] = []
        params: list[object] = []
        if source_kind is not None:
            conditions.append("source_kind = ?")
            params.append(source_kind.value)
        if source_context_ref is not None:
            conditions.append("source_context_ref = ?")
            params.append(source_context_ref)
        if source_session_id is not None:
            conditions.append("source_session_id = ?")
            params.append(source_session_id)
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind.value)
        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)
        if q is not None:
            conditions.append("(name LIKE ? OR summary LIKE ?)")
            q_pattern = f"%{q}%"
            params.extend([q_pattern, q_pattern])
        if cursor is not None:
            cursor_updated = _dt_to_str(cursor.updated_at)
            conditions.append(
                "(updated_at < ? OR (updated_at = ? AND id < ?))"
            )
            params.extend([cursor_updated, cursor_updated, cursor.artifact_id])

        where_clause = (
            " WHERE " + " AND ".join(conditions) if conditions else ""
        )
        sql = (
            f"SELECT * FROM artifacts{where_clause} "
            "ORDER BY updated_at DESC, id DESC LIMIT ?"
        )
        params.append(limit + 1)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        items = tuple(self._row_to_artifact(r) for r in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = ArtifactListCursor(
                updated_at=last.updated_at, artifact_id=last.id,
            )
        return ArtifactListPage(items=items, next_cursor=next_cursor)

    def _update_artifact_sync(self, artifact: Artifact) -> Artifact:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM artifacts WHERE id = ?", (artifact.id,),
            ).fetchone()
            if existing is None:
                conn.rollback()
                raise ArtifactNotFoundError(
                    f"artifact not found: {artifact.id}"
                )
            conn.execute(
                """
                UPDATE artifacts SET
                    name = ?, kind = ?, mime = ?, content_ref = ?,
                    inline_content = ?, size = ?, checksum = ?,
                    source_kind = ?, source_ref = ?, source_context_ref = ?,
                    source_session_id = ?,
                    summary = ?, classification = ?, labels_json = ?,
                    status = ?, created_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    artifact.name,
                    artifact.kind.value,
                    artifact.mime,
                    artifact.content_ref,
                    artifact.inline_content,
                    artifact.size,
                    artifact.checksum,
                    artifact.source_kind.value,
                    artifact.source_ref,
                    artifact.source_context_ref,
                    artifact.source_session_id,
                    artifact.summary,
                    artifact.classification,
                    _labels_to_json(artifact.labels),
                    artifact.status.value,
                    artifact.created_by,
                    _dt_to_str(now),
                    artifact.id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact.id,),
            ).fetchone()
            conn.commit()
        return self._row_to_artifact(row)

    def _delete_artifact_sync(self, artifact_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM artifacts WHERE id = ?", (artifact_id,),
            )
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    def _get_by_source_sync(
        self, source_kind: ArtifactSource, source_ref: str,
    ) -> Artifact | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE source_kind = ? AND source_ref = ?",
                (source_kind.value, source_ref),
            ).fetchone()
        return self._row_to_artifact(row) if row else None

    def _count_artifacts_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM artifacts",
            ).fetchone()
        return int(row["cnt"])

    def _list_task_artifacts_missing_session_sync(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        """Return task-source artifacts with NULL source_session_id.

        Ordered by id for stable cursor-less batching; the backfill loops
        until an empty/short batch is returned. As rows get their
        source_session_id populated they drop out of subsequent batches.
        """
        clamped = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE source_kind IN ('task_artifact', 'task_attachment')
                  AND source_session_id IS NULL
                ORDER BY id ASC LIMIT ?
                """,
                (clamped,),
            ).fetchall()
        return tuple(self._row_to_artifact(r) for r in rows)

    def _list_artifacts_with_empty_mime_sync(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        """Return artifacts with empty mime, ordered by id for stable batching.

        As rows get their mime populated they drop out of subsequent batches.
        """
        clamped = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE COALESCE(mime, '') = ''
                ORDER BY id ASC LIMIT ?
                """,
                (clamped,),
            ).fetchall()
        return tuple(self._row_to_artifact(r) for r in rows)

    # ------------------------------------------------------------------
    # PublishedArtifact lifecycle (sync implementations)
    # ------------------------------------------------------------------

    def _register_published_sync(
        self,
        published: PublishedArtifact,
        *,
        revoke_artifact_id: str | None = None,
    ) -> PublishedArtifact:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 1. Revoke old active publish for revoke_artifact_id
                if revoke_artifact_id is not None:
                    conn.execute(
                        "UPDATE published_artifacts "
                        "SET status = 'revoked', revoked_at = ? "
                        "WHERE artifact_id = ? AND status = 'active'",
                        (_dt_to_str(now), revoke_artifact_id),
                    )
                # 2. Insert new active publish
                conn.execute(
                    self._INSERT_PUBLISHED_SQL,
                    self._published_params(published, now),
                )
                # 3. Sync artifact status to PUBLISHED
                if published.artifact_id is not None:
                    conn.execute(
                        "UPDATE artifacts SET status = 'published', "
                        "updated_at = ? WHERE id = ?",
                        (_dt_to_str(now), published.artifact_id),
                    )
                # 4. Re-read the inserted row
                row = conn.execute(
                    "SELECT * FROM published_artifacts WHERE publish_id = ?",
                    (published.publish_id,),
                ).fetchone()
                conn.commit()
                return self._row_to_published(row)
            except sqlite3.IntegrityError as e:
                conn.rollback()
                # Reread committed state: if there is an active publish
                # for this artifact_id, the conflict was the partial unique
                # index (concurrent active-publish collision) -> reread and
                # return the existing active publish.
                #
                # This avoids fragile error-message parsing: a PK violation
                # on publish_id or an FK violation will NOT find an active
                # publish for a *different* artifact_id, so it falls through
                # to ArtifactConflictError. Only a genuine concurrent
                # active-publish collision on the same artifact_id returns
                # the existing row.
                if published.artifact_id is not None:
                    existing = conn.execute(
                        "SELECT * FROM published_artifacts "
                        "WHERE artifact_id = ? AND status = 'active'",
                        (published.artifact_id,),
                    ).fetchone()
                    if existing is not None:
                        return self._row_to_published(existing)
                # No active publish found -> the conflict was NOT a
                # concurrent active-publish collision (e.g. duplicate
                # publish_id PK, or FK violation) -> raise.
                raise ArtifactConflictError(
                    f"register_published integrity error: {e}"
                ) from e

    def _get_published_sync(
        self, publish_id: str,
    ) -> PublishedArtifact | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM published_artifacts WHERE publish_id = ?",
                (publish_id,),
            ).fetchone()
        return self._row_to_published(row) if row else None

    def _get_active_publish_sync(
        self, artifact_id: str,
    ) -> PublishedArtifact | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM published_artifacts "
                "WHERE artifact_id = ? AND status = 'active' LIMIT 1",
                (artifact_id,),
            ).fetchone()
        return self._row_to_published(row) if row else None

    def _list_published_sync(
        self, artifact_id: str | None = None,
    ) -> tuple[PublishedArtifact, ...]:
        with self._connect() as conn:
            if artifact_id is not None:
                rows = conn.execute(
                    "SELECT * FROM published_artifacts "
                    "WHERE artifact_id = ? "
                    "ORDER BY published_at DESC, publish_id DESC",
                    (artifact_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM published_artifacts "
                    "ORDER BY published_at DESC, publish_id DESC",
                ).fetchall()
        return tuple(self._row_to_published(r) for r in rows)

    def _revoke_published_sync(
        self, artifact_id: str,
    ) -> PublishedArtifact | None:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Find active publish for this artifact
            row = conn.execute(
                "SELECT * FROM published_artifacts "
                "WHERE artifact_id = ? AND status = 'active'",
                (artifact_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE published_artifacts "
                    "SET status = 'revoked', revoked_at = ? "
                    "WHERE publish_id = ?",
                    (_dt_to_str(now), row["publish_id"]),
                )
                row = conn.execute(
                    "SELECT * FROM published_artifacts "
                    "WHERE publish_id = ?",
                    (row["publish_id"],),
                ).fetchone()
                conn.commit()
                return self._row_to_published(row)
            # No active: find latest revoked (idempotent -- already revoked)
            row = conn.execute(
                "SELECT * FROM published_artifacts "
                "WHERE artifact_id = ? AND status = 'revoked' "
                "ORDER BY revoked_at DESC, publish_id DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
            conn.commit()
            return self._row_to_published(row) if row else None

    def _delete_published_by_artifact_sync(self, artifact_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM published_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )
            conn.commit()
            return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Backfill batch read (sync implementation)
    # ------------------------------------------------------------------

    def _list_attachment_sources_sync(
        self,
        *,
        after_attachment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ArtifactAttachmentSource, ...]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            if after_attachment_id is not None:
                rows = conn.execute(
                    "SELECT id, task_id, filename, stored_name, "
                    "content_type, size, checksum, uploaded_by, created_at "
                    "FROM task_attachments "
                    "WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after_attachment_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, task_id, filename, stored_name, "
                    "content_type, size, checksum, uploaded_by, created_at "
                    "FROM task_attachments "
                    "ORDER BY id ASC LIMIT ?",
                    (limit,),
                ).fetchall()
        return tuple(self._row_to_attachment_source(r) for r in rows)

    # ------------------------------------------------------------------
    # Revision lifecycle (sync implementations)
    # ------------------------------------------------------------------

    def _create_with_initial_revision_sync(
        self, artifact: Artifact, initial: ArtifactRevision,
    ) -> tuple[Artifact, ArtifactRevision]:
        """Single transaction: insert artifact (with current_revision_id) +
        insert initial revision."""
        if initial.artifact_id != artifact.id:
            raise ArtifactRevisionValidationError(
                "initial revision artifact_id does not match artifact id"
            )
        if initial.revision_number != 1 or not initial.is_initial:
            raise ArtifactRevisionValidationError(
                "initial revision must have revision_number=1 with no parent "
                "and no rollback_from"
            )
        now = _now()
        created_at = artifact.created_at or now
        updated_at = artifact.updated_at or now
        resolved_artifact = dataclass_replace(
            artifact,
            created_at=created_at,
            updated_at=updated_at,
            current_revision_id=initial.id,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    self._INSERT_ARTIFACT_SQL,
                    self._artifact_params(resolved_artifact, now),
                )
                conn.execute(
                    self._INSERT_REVISION_SQL,
                    self._revision_params(initial),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                conn.rollback()
                raise ArtifactConflictError(
                    f"create_artifact_with_initial_revision integrity error: {e}"
                ) from e
        return resolved_artifact, initial

    def _append_revision_sync(
        self, artifact_id: str, revision: ArtifactRevision, *,
        expected_revision_id: str,
    ) -> ArtifactRevision:
        """Append a revision under BEGIN IMMEDIATE with CAS.

        Validates: expected_revision_id == current; parent == current;
        revision_number == MAX(existing)+1; parent/rollback belong to
        same artifact.  Inserts revision + updates current_revision_id.
        """
        if revision.artifact_id != artifact_id:
            raise ArtifactRevisionValidationError(
                "revision artifact_id does not match artifact_id argument"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                art_row = conn.execute(
                    "SELECT current_revision_id FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if art_row is None:
                    conn.rollback()
                    raise ArtifactNotFoundError(
                        f"artifact not found: {artifact_id}"
                    )
                current_rev_id = art_row["current_revision_id"]
                if current_rev_id != expected_revision_id:
                    conn.rollback()
                    raise ArtifactRevisionConflictError(
                        f"CAS conflict: expected current_revision_id="
                        f"{expected_revision_id}, actual={current_rev_id}"
                    )
                # parent must equal current
                if revision.parent_revision_id != current_rev_id:
                    conn.rollback()
                    raise ArtifactRevisionValidationError(
                        "parent_revision_id must equal current_revision_id"
                    )
                # Compute next revision_number
                max_row = conn.execute(
                    "SELECT MAX(revision_number) AS max_num "
                    "FROM artifact_revisions WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                max_num = max_row["max_num"] if max_row and max_row["max_num"] else 0
                expected_next = max_num + 1
                if revision.revision_number != expected_next:
                    conn.rollback()
                    raise ArtifactRevisionValidationError(
                        f"revision_number must be {expected_next}, "
                        f"got {revision.revision_number}"
                    )
                # Validate parent/rollback belong to same artifact
                if revision.parent_revision_id is not None:
                    parent_row = conn.execute(
                        "SELECT artifact_id FROM artifact_revisions WHERE id = ?",
                        (revision.parent_revision_id,),
                    ).fetchone()
                    if parent_row is None or parent_row["artifact_id"] != artifact_id:
                        conn.rollback()
                        raise ArtifactRevisionValidationError(
                            "parent_revision_id does not belong to this artifact"
                        )
                if revision.rollback_from_revision_id is not None:
                    rb_row = conn.execute(
                        "SELECT artifact_id FROM artifact_revisions WHERE id = ?",
                        (revision.rollback_from_revision_id,),
                    ).fetchone()
                    if rb_row is None or rb_row["artifact_id"] != artifact_id:
                        conn.rollback()
                        raise ArtifactRevisionValidationError(
                            "rollback_from_revision_id does not belong to this artifact"
                        )
                # Insert revision
                conn.execute(
                    self._INSERT_REVISION_SQL,
                    self._revision_params(revision),
                )
                # Update current_revision_id
                conn.execute(
                    "UPDATE artifacts SET current_revision_id = ?, updated_at = ? "
                    "WHERE id = ?",
                    (revision.id, _dt_to_str(_now()), artifact_id),
                )
                row = conn.execute(
                    "SELECT * FROM artifact_revisions WHERE id = ?",
                    (revision.id,),
                ).fetchone()
                conn.commit()
                return self._row_to_revision(row)
            except sqlite3.IntegrityError as e:
                conn.rollback()
                raise ArtifactRevisionConflictError(
                    f"append_revision integrity error: {e}"
                ) from e

    def _get_revision_sync(
        self, artifact_id: str, revision_id: str,
    ) -> ArtifactRevision | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_revisions "
                "WHERE id = ? AND artifact_id = ?",
                (revision_id, artifact_id),
            ).fetchone()
        return self._row_to_revision(row) if row else None

    def _list_revisions_sync(
        self, artifact_id: str, *,
        cursor: RevisionListCursor | None = None,
        limit: int = 50,
    ) -> RevisionListPage:
        limit = max(1, min(limit, 100))
        conditions = ["artifact_id = ?"]
        params: list[object] = [artifact_id]
        if cursor is not None:
            if cursor.artifact_id != artifact_id:
                raise ArtifactRevisionValidationError(
                    "cursor artifact_id does not match requested artifact_id"
                )
            conditions.append(
                "(revision_number < ? OR "
                "(revision_number = ? AND id < ?))"
            )
            params.extend([
                cursor.revision_number,
                cursor.revision_number,
                cursor.id,
            ])
        where_clause = " WHERE " + " AND ".join(conditions)
        sql = (
            f"SELECT * FROM artifact_revisions{where_clause} "
            "ORDER BY revision_number DESC, id DESC LIMIT ?"
        )
        params.append(limit + 1)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = tuple(self._row_to_revision(r) for r in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = RevisionListCursor(
                artifact_id=artifact_id,
                revision_number=last.revision_number,
                id=last.id,
            )
        return RevisionListPage(items=items, next_cursor=next_cursor)

    def _delete_artifact_graph_sync(self, artifact_id: str) -> ArtifactDeleteGraph:
        """Delete artifact + all revisions + published rows in one transaction.

        Returns deduplicated content refs and publish snapshot ids for
        best-effort post-commit cleanup.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Collect revision content_refs (dedup, preserving order)
            rev_rows = conn.execute(
                "SELECT content_ref FROM artifact_revisions WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchall()
            revision_refs = tuple(
                dict.fromkeys(  # ordered dedup
                    row["content_ref"] for row in rev_rows
                    if row["content_ref"]
                )
            )
            # Legacy artifact content_ref
            art_row = conn.execute(
                "SELECT content_ref FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            legacy_ref = art_row["content_ref"] if art_row else None
            # Publish snapshot ids
            pub_rows = conn.execute(
                "SELECT publish_id FROM published_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchall()
            publish_ids = tuple(row["publish_id"] for row in pub_rows)
            # Delete published_artifacts for this artifact
            conn.execute(
                "DELETE FROM published_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )
            # Delete artifact (CASCADE deletes revisions via FK)
            conn.execute(
                "DELETE FROM artifacts WHERE id = ?", (artifact_id,),
            )
            conn.commit()
        return ArtifactDeleteGraph(
            revision_content_refs=revision_refs,
            legacy_artifact_content_ref=legacy_ref,
            publish_snapshot_ids=publish_ids,
        )

    # ------------------------------------------------------------------
    # Migration / backfill (sync implementations)
    # ------------------------------------------------------------------

    def _list_revision_migration_candidates_sync(
        self, *, cursor: ArtifactListCursor | None, limit: int,
    ) -> ArtifactListPage:
        """Return artifacts with NULL current_revision_id, paginated."""
        clamped = max(1, min(limit, 500))
        conditions = ["current_revision_id IS NULL"]
        params: list[object] = []
        if cursor is not None:
            cursor_updated = _dt_to_str(cursor.updated_at)
            conditions.append(
                "(updated_at < ? OR (updated_at = ? AND id < ?))"
            )
            params.extend([cursor_updated, cursor_updated, cursor.artifact_id])
        where_clause = " WHERE " + " AND ".join(conditions)
        sql = (
            f"SELECT * FROM artifacts{where_clause} "
            "ORDER BY updated_at DESC, id DESC LIMIT ?"
        )
        params.append(clamped + 1)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = tuple(self._row_to_artifact(r) for r in rows[:clamped])
        next_cursor = None
        if len(rows) > clamped:
            last = items[-1]
            next_cursor = ArtifactListCursor(
                updated_at=last.updated_at, artifact_id=last.id,
            )
        return ArtifactListPage(items=items, next_cursor=next_cursor)

    def _commit_initial_revision_backfill_sync(
        self, artifact_id: str, revision: ArtifactRevision,
    ) -> ArtifactRevision:
        """Backfill the initial revision under BEGIN IMMEDIATE.

        Three states:
          1. current_revision_id points to a valid revision of this artifact
             -> skip (idempotent).
          2. current_revision_id is NULL but a revision_number=1 row exists
             -> backfill current_revision_id only.
          3. current_revision_id is NULL and no revision exists -> insert
             the passed revision + backfill current_revision_id.

        If current_revision_id points to a revision of another artifact or
        a non-existent revision, raise (do not auto-fix).
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                art_row = conn.execute(
                    "SELECT current_revision_id FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if art_row is None:
                    conn.rollback()
                    raise ArtifactNotFoundError(
                        f"artifact not found: {artifact_id}"
                    )
                current_rev_id = art_row["current_revision_id"]

                # State 1: current_revision_id is set
                if current_rev_id is not None:
                    rev_row = conn.execute(
                        "SELECT * FROM artifact_revisions "
                        "WHERE id = ? AND artifact_id = ?",
                        (current_rev_id, artifact_id),
                    ).fetchone()
                    if rev_row is not None:
                        # Valid pointer -> skip (idempotent)
                        conn.commit()
                        return self._row_to_revision(rev_row)
                    # Pointer is invalid (cross-artifact or non-existent)
                    conn.rollback()
                    raise ArtifactRevisionValidationError(
                        f"current_revision_id={current_rev_id} does not point "
                        f"to a valid revision of artifact {artifact_id}"
                    )

                # State 2: current_revision_id is NULL, check for rev_number=1
                existing_rev = conn.execute(
                    "SELECT * FROM artifact_revisions "
                    "WHERE artifact_id = ? AND revision_number = 1",
                    (artifact_id,),
                ).fetchone()
                if existing_rev is not None:
                    # Backfill current_revision_id only
                    conn.execute(
                        "UPDATE artifacts SET current_revision_id = ?, "
                        "updated_at = ? WHERE id = ?",
                        (existing_rev["id"], _dt_to_str(_now()), artifact_id),
                    )
                    conn.commit()
                    return self._row_to_revision(existing_rev)

                # State 3: no revision at all -> insert + backfill
                if revision.artifact_id != artifact_id:
                    conn.rollback()
                    raise ArtifactRevisionValidationError(
                        "revision artifact_id does not match artifact_id argument"
                    )
                if revision.revision_number != 1 or not revision.is_initial:
                    conn.rollback()
                    raise ArtifactRevisionValidationError(
                        "backfill revision must be initial (revision_number=1, "
                        "no parent, no rollback_from)"
                    )
                conn.execute(
                    self._INSERT_REVISION_SQL,
                    self._revision_params(revision),
                )
                conn.execute(
                    "UPDATE artifacts SET current_revision_id = ?, "
                    "updated_at = ? WHERE id = ?",
                    (revision.id, _dt_to_str(_now()), artifact_id),
                )
                inserted_row = conn.execute(
                    "SELECT * FROM artifact_revisions WHERE id = ?",
                    (revision.id,),
                ).fetchone()
                conn.commit()
                return self._row_to_revision(inserted_row)
            except sqlite3.IntegrityError as e:
                conn.rollback()
                raise ArtifactConflictError(
                    f"commit_initial_revision_backfill integrity error: {e}"
                ) from e

    def _register_revision_publish_sync(
        self, published: PublishedArtifact, *, artifact_id: str,
        revision_id: str, expected_current_revision_id: str,
    ) -> PublishedArtifact:
        """Register a revision publish under BEGIN IMMEDIATE with CAS.

        Re-verifies current_revision_id, reuses active publish with same
        checksum, or revokes old + inserts new.
        """
        now = _now()
        resolved = dataclass_replace(
            published, published_revision_id=revision_id,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # CAS check
                art_row = conn.execute(
                    "SELECT current_revision_id FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if art_row is None:
                    conn.rollback()
                    raise ArtifactNotFoundError(
                        f"artifact not found: {artifact_id}"
                    )
                if art_row["current_revision_id"] != expected_current_revision_id:
                    conn.rollback()
                    raise ArtifactRevisionConflictError(
                        f"CAS conflict: expected current_revision_id="
                        f"{expected_current_revision_id}, "
                        f"actual={art_row['current_revision_id']}"
                    )
                # Reuse: same checksum active publish for this artifact
                existing = conn.execute(
                    "SELECT * FROM published_artifacts "
                    "WHERE artifact_id = ? AND status = 'active' "
                    "AND snapshot_checksum = ?",
                    (artifact_id, resolved.snapshot_checksum),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return self._row_to_published(existing)
                # Revoke old active
                conn.execute(
                    "UPDATE published_artifacts "
                    "SET status = 'revoked', revoked_at = ? "
                    "WHERE artifact_id = ? AND status = 'active'",
                    (_dt_to_str(now), artifact_id),
                )
                # Insert new active publish
                conn.execute(
                    self._INSERT_PUBLISHED_SQL,
                    self._published_params(resolved, now),
                )
                # Sync artifact status
                conn.execute(
                    "UPDATE artifacts SET status = 'published', "
                    "updated_at = ? WHERE id = ?",
                    (_dt_to_str(now), artifact_id),
                )
                row = conn.execute(
                    "SELECT * FROM published_artifacts WHERE publish_id = ?",
                    (resolved.publish_id,),
                ).fetchone()
                conn.commit()
                return self._row_to_published(row)
            except sqlite3.IntegrityError as e:
                conn.rollback()
                # Check for concurrent same-checksum reuse: if a concurrent
                # transaction inserted an active publish with the same checksum,
                # return it.  Only return when checksums match -- otherwise the
                # IntegrityError was a PK/FK/CHECK violation (not a concurrent
                # reuse), and returning the old active (different checksum)
                # would mask the error.
                existing = conn.execute(
                    "SELECT * FROM published_artifacts "
                    "WHERE artifact_id = ? AND status = 'active' "
                    "AND snapshot_checksum = ?",
                    (artifact_id, resolved.snapshot_checksum),
                ).fetchone()
                if existing is not None:
                    return self._row_to_published(existing)
                raise ArtifactConflictError(
                    f"register_revision_publish integrity error: {e}"
                ) from e

    def _count_artifacts_without_revision_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM artifacts "
                "WHERE current_revision_id IS NULL"
            ).fetchone()
        return int(row["cnt"])

    # ==================================================================
    # Async public API (wraps sync via asyncio.to_thread)
    # ==================================================================

    async def create_artifact(self, artifact: Artifact) -> Artifact:
        return await asyncio.to_thread(self._create_artifact_sync, artifact)

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        return await asyncio.to_thread(self._get_artifact_sync, artifact_id)

    async def list_artifacts(
        self,
        *,
        source_kind: ArtifactSource | None = None,
        source_context_ref: str | None = None,
        source_session_id: str | None = None,
        kind: ArtifactKind | None = None,
        status: ArtifactStatus | None = None,
        q: str | None = None,
        cursor: ArtifactListCursor | None = None,
        limit: int = 50,
    ) -> ArtifactListPage:
        return await asyncio.to_thread(
            self._list_artifacts_sync,
            source_kind=source_kind,
            source_context_ref=source_context_ref,
            source_session_id=source_session_id,
            kind=kind,
            status=status,
            q=q,
            cursor=cursor,
            limit=limit,
        )

    async def update_artifact(self, artifact: Artifact) -> Artifact:
        return await asyncio.to_thread(self._update_artifact_sync, artifact)

    async def delete_artifact(self, artifact_id: str) -> bool:
        return await asyncio.to_thread(self._delete_artifact_sync, artifact_id)

    async def get_by_source(
        self, source_kind: ArtifactSource, source_ref: str,
    ) -> Artifact | None:
        return await asyncio.to_thread(
            self._get_by_source_sync, source_kind, source_ref,
        )

    async def count_artifacts(self) -> int:
        return await asyncio.to_thread(self._count_artifacts_sync)

    async def list_task_artifacts_missing_session(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        return await asyncio.to_thread(
            self._list_task_artifacts_missing_session_sync, limit=limit,
        )

    async def list_artifacts_with_empty_mime(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        return await asyncio.to_thread(
            self._list_artifacts_with_empty_mime_sync, limit=limit,
        )

    async def register_published(
        self,
        published: PublishedArtifact,
        *,
        revoke_artifact_id: str | None = None,
    ) -> PublishedArtifact:
        return await asyncio.to_thread(
            self._register_published_sync,
            published,
            revoke_artifact_id=revoke_artifact_id,
        )

    async def get_published(
        self, publish_id: str,
    ) -> PublishedArtifact | None:
        return await asyncio.to_thread(self._get_published_sync, publish_id)

    async def get_active_publish(
        self, artifact_id: str,
    ) -> PublishedArtifact | None:
        return await asyncio.to_thread(
            self._get_active_publish_sync, artifact_id,
        )

    async def list_published(
        self, artifact_id: str | None = None,
    ) -> tuple[PublishedArtifact, ...]:
        return await asyncio.to_thread(
            self._list_published_sync, artifact_id,
        )

    async def revoke_published(
        self, artifact_id: str,
    ) -> PublishedArtifact | None:
        return await asyncio.to_thread(
            self._revoke_published_sync, artifact_id,
        )

    async def delete_published_by_artifact(
        self, artifact_id: str,
    ) -> int:
        return await asyncio.to_thread(
            self._delete_published_by_artifact_sync, artifact_id,
        )

    async def list_attachment_sources(
        self,
        *,
        after_attachment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ArtifactAttachmentSource, ...]:
        return await asyncio.to_thread(
            self._list_attachment_sources_sync,
            after_attachment_id=after_attachment_id,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Revision lifecycle (async wrappers)
    # ------------------------------------------------------------------

    async def create_artifact_with_initial_revision(
        self, artifact: Artifact, initial: ArtifactRevision,
    ) -> tuple[Artifact, ArtifactRevision]:
        return await asyncio.to_thread(
            self._create_with_initial_revision_sync, artifact, initial,
        )

    async def append_revision(
        self, artifact_id: str, revision: ArtifactRevision, *,
        expected_revision_id: str,
    ) -> ArtifactRevision:
        return await asyncio.to_thread(
            self._append_revision_sync,
            artifact_id, revision,
            expected_revision_id=expected_revision_id,
        )

    async def get_revision(
        self, artifact_id: str, revision_id: str,
    ) -> ArtifactRevision | None:
        return await asyncio.to_thread(
            self._get_revision_sync, artifact_id, revision_id,
        )

    async def list_revisions(
        self, artifact_id: str, *,
        cursor: RevisionListCursor | None = None,
        limit: int = 50,
    ) -> RevisionListPage:
        return await asyncio.to_thread(
            self._list_revisions_sync,
            artifact_id,
            cursor=cursor,
            limit=limit,
        )

    async def delete_artifact_graph(
        self, artifact_id: str,
    ) -> ArtifactDeleteGraph:
        return await asyncio.to_thread(
            self._delete_artifact_graph_sync, artifact_id,
        )

    async def list_revision_migration_candidates(
        self, *, cursor: ArtifactListCursor | None, limit: int,
    ) -> ArtifactListPage:
        return await asyncio.to_thread(
            self._list_revision_migration_candidates_sync,
            cursor=cursor,
            limit=limit,
        )

    async def commit_initial_revision_backfill(
        self, artifact_id: str, revision: ArtifactRevision,
    ) -> ArtifactRevision:
        return await asyncio.to_thread(
            self._commit_initial_revision_backfill_sync,
            artifact_id, revision,
        )

    async def register_revision_publish(
        self, published: PublishedArtifact, *, artifact_id: str,
        revision_id: str, expected_current_revision_id: str,
    ) -> PublishedArtifact:
        return await asyncio.to_thread(
            self._register_revision_publish_sync,
            published,
            artifact_id=artifact_id,
            revision_id=revision_id,
            expected_current_revision_id=expected_current_revision_id,
        )

    async def count_artifacts_without_revision(self) -> int:
        return await asyncio.to_thread(
            self._count_artifacts_without_revision_sync,
        )
