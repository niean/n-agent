from __future__ import annotations

import asyncio
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.skill import SkillBackupStore as _SkillBackupStoreProtocol

# Top-level directories that live inside ``root`` but must never be included
# in a snapshot tar (would otherwise recurse into the archive itself) nor
# archived during a rollback.
_EXCLUDED_TOP = frozenset({".backups", ".archive"})


def _new_id() -> str:
    """Build a filesystem-safe, unique, chronologically-sortable id.

    The ISO-8601 prefix sorts chronologically; the random 8-char suffix
    guarantees uniqueness even when several snapshots land in the same second.
    """
    ts = datetime.now(timezone.utc).isoformat().replace(":", "-")
    return f"{ts}-{uuid4().hex[:8]}"


class SkillBackupStore(_SkillBackupStoreProtocol):
    """Filesystem-backed implementation of the :class:`SkillBackupStore` Protocol.

    Snapshots are gzipped tar archives stored under
    ``root/.backups/<id>/skills.tar.gz``. Every public method is async and
    delegates the blocking IO to ``asyncio.to_thread`` so the event loop is
    never blocked (mirroring the convention used by the other skill stores).
    """

    def __init__(self, root: Path, keep: int):
        self.root = Path(root)
        self.keep = keep
        self._backups_dir = self.root / ".backups"
        self._archive_dir = self.root / ".archive"

    # ------------------------------------------------------------------
    # synchronous helpers (run inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _snapshot_sync(self) -> str:
        snapshot_id = _new_id()
        snap_dir = self._backups_dir / snapshot_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        tar_path = snap_dir / "skills.tar.gz"

        # Archive every top-level entry of ``root`` EXCEPT the reserved
        # ``.backups``/``.archive`` directories. Adding each child individually
        # (rather than the root itself) keeps those dirs out of the tar and
        # avoids recursing into the archive we are currently writing.
        with tarfile.open(tar_path, "w:gz") as tar:
            for child in self.root.iterdir():
                if child.name in _EXCLUDED_TOP:
                    continue
                tar.add(child, arcname=child.name, recursive=True)

        self._prune_sync()
        return snapshot_id

    def _prune_sync(self) -> None:
        if not self._backups_dir.is_dir():
            return
        ids = sorted(d.name for d in self._backups_dir.iterdir() if d.is_dir())
        if len(ids) <= self.keep:
            return
        # Keep the most recent ``keep`` (last after ascending sort) and drop
        # the rest.
        for stale in ids[: len(ids) - self.keep]:
            shutil.rmtree(self._backups_dir / stale, ignore_errors=True)

    def _list_sync(self) -> list[str]:
        if not self._backups_dir.is_dir():
            return []
        return sorted(d.name for d in self._backups_dir.iterdir() if d.is_dir())

    def _rollback_sync(self, snapshot_id: str) -> bool:
        tar_path = self._backups_dir / snapshot_id / "skills.tar.gz"
        if not tar_path.is_file():
            return False

        # 1) Archive the current state: move every top-level entry except the
        #    reserved dirs into ``.archive/rollback-<id>/``.
        archive_dest = self._archive_dir / f"rollback-{_new_id()}"
        archive_dest.mkdir(parents=True, exist_ok=True)
        for child in self.root.iterdir():
            if child.name in _EXCLUDED_TOP:
                continue
            shutil.move(str(child), str(archive_dest / child.name))

        # 2) Restore the snapshot back into ``root``.
        with tarfile.open(tar_path, "r:gz") as tar:
            try:
                tar.extractall(path=self.root, filter="data")
            except TypeError:
                # ``filter`` keyword unsupported on older Python point
                # releases; fall back to the legacy extraction.
                tar.extractall(path=self.root)

        return True

    # ------------------------------------------------------------------
    # async public API (SkillBackupStore Protocol)
    # ------------------------------------------------------------------

    async def snapshot(self) -> str:
        return await asyncio.to_thread(self._snapshot_sync)

    async def list(self) -> list[str]:
        return await asyncio.to_thread(self._list_sync)

    async def rollback(self, snapshot_id: str) -> bool:
        return await asyncio.to_thread(self._rollback_sync, snapshot_id)
