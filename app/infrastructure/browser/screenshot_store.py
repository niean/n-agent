"""SQLite-metadata + filesystem-backed BrowserScreenshotStore.

Validates PNG/JPEG magic bytes and decodes via Pillow to enforce pixel
limits (reject pixel bombs / decode failures). Stores image bytes on
disk with atomic writes and metadata in SQLite. Supports per-session
quota, TTL, and path-traversal / symlink rejection.
"""
from __future__ import annotations

import io
import os
import secrets
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screenshots (
    ref TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_screenshots_session ON screenshots(session_id);
CREATE INDEX IF NOT EXISTS idx_screenshots_expires
    ON screenshots(expires_at) WHERE expires_at IS NOT NULL;
"""


class SqliteBrowserScreenshotStore:
    """Persist browser screenshots to disk with SQLite metadata."""

    def __init__(
        self,
        store_dir: Path,
        *,
        max_pixels: int = 10_000_000,
        max_per_session: int = 20,
        ttl_seconds: int | None = 86_400,
    ) -> None:
        self._store_dir = Path(store_dir)
        self._screenshots_dir = self._store_dir / "screenshots"
        self._db_path = self._store_dir / "meta.db"
        self._max_pixels = max_pixels
        self._max_per_session = max_per_session
        self._ttl_seconds = ttl_seconds
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- persist -----------------------------------------------------------

    async def persist(self, session_id: str, data: bytes, content_type: str) -> str:
        width, height = self._validate_image(data, content_type)
        self._enforce_quota(session_id)

        ref = secrets.token_urlsafe(16)
        target = self._screenshots_dir / ref
        self._atomic_write(target, data)

        now = datetime.now(timezone.utc)
        expires_at: str | None
        if self._ttl_seconds is not None:
            expires_at = (now + timedelta(seconds=self._ttl_seconds)).isoformat()
        else:
            expires_at = None
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO screenshots(
                        ref, session_id, content_type, size_bytes,
                        width, height, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ref, session_id, content_type, len(data),
                        width, height, now.isoformat(), expires_at,
                    ),
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return ref

    def _validate_image(self, data: bytes, content_type: str) -> tuple[int, int]:
        """Validate magic bytes and decode via Pillow. Returns (width, height)."""
        if content_type == "image/png":
            if not data.startswith(_PNG_MAGIC):
                raise ValueError("invalid PNG magic bytes")
        elif content_type == "image/jpeg":
            if not data.startswith(_JPEG_MAGIC):
                raise ValueError("invalid JPEG magic bytes")
        else:
            raise ValueError(f"unsupported content type: {content_type}")

        try:
            img = Image.open(io.BytesIO(data))
            width, height = img.size
            if width * height > self._max_pixels:
                raise ValueError(
                    f"pixel limit exceeded: {width}x{height} = {width * height}"
                )
            img.verify()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"image decode failed: {exc}") from exc
        return width, height

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=self._screenshots_dir, prefix=".tmp_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _enforce_quota(self, session_id: str) -> None:
        """Evict oldest screenshots if session exceeds quota."""
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM screenshots WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            if count < self._max_per_session:
                return
            to_evict = count - self._max_per_session + 1
            rows = conn.execute(
                """
                SELECT ref FROM screenshots
                WHERE session_id = ?
                ORDER BY created_at ASC, ref ASC
                LIMIT ?
                """,
                (session_id, to_evict),
            ).fetchall()
            for row in rows:
                ref = row["ref"]
                conn.execute("DELETE FROM screenshots WHERE ref = ?", (ref,))
                (self._screenshots_dir / ref).unlink(missing_ok=True)

    # -- read --------------------------------------------------------------

    async def read(self, screenshot_ref: str) -> bytes | None:
        path = self._screenshots_dir / screenshot_ref
        root = self._screenshots_dir.resolve()
        # Path traversal: resolved path must be under root
        try:
            real = Path(os.path.realpath(path))
            real.relative_to(root)
        except (ValueError, OSError):
            return None
        # Symlink rejection
        if os.path.islink(path):
            return None
        if not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    # -- delete_session ----------------------------------------------------

    async def delete_session(self, session_id: str) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ref FROM screenshots WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            conn.execute(
                "DELETE FROM screenshots WHERE session_id = ?",
                (session_id,),
            )
        for row in rows:
            (self._screenshots_dir / row["ref"]).unlink(missing_ok=True)

    # -- TTL cleanup -------------------------------------------------------

    async def cleanup_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ref FROM screenshots WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM screenshots WHERE ref = ?", (row["ref"],))
        for row in rows:
            (self._screenshots_dir / row["ref"]).unlink(missing_ok=True)
        return len(rows)

    # -- metadata helpers (extra methods beyond Protocol) ------------------

    async def get_metadata(self, ref: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM screenshots WHERE ref = ?", (ref,)
            ).fetchone()
        return dict(row) if row else None

    async def list_session_refs(self, session_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ref FROM screenshots WHERE session_id = ? ORDER BY created_at ASC, ref ASC",
                (session_id,),
            ).fetchall()
        return [row["ref"] for row in rows]

    async def find_session_ref_at_or_before(
        self, session_id: str, captured_at: str
    ) -> str | None:
        """Return the newest retained screenshot at or before ``captured_at``."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ref FROM screenshots
                WHERE session_id = ? AND created_at <= ?
                ORDER BY created_at DESC, ref DESC
                LIMIT 1
                """,
                (session_id, captured_at),
            ).fetchone()
        return str(row["ref"]) if row is not None else None
