"""Local filesystem ArtifactContentStore.

Security-critical infrastructure for the Artifact workbench: path traversal
defense, per-component symlink rejection, bounded reads, atomic writes, and
ownership-scoped deletion.

content_ref scheme (opaque to callers):
  - item:{artifact_id}/{server_filename}        owned, deletable
  - published:{publish_id}/{server_filename}    publish snapshot, deletable
  - attachment:{task_id}/{stored_name}          read-only source descriptor
  - workspace:{relative_path}                   read-only source descriptor

Only item/published refs may be deleted via delete_owned. Attachment and
workspace refs are read-only source references that must never be deleted by
the content store.
"""
from __future__ import annotations

import asyncio
import os
import re
import stat
import tempfile
from pathlib import Path
from uuid import uuid4

from app.domain.artifact import (
    ArtifactContentUnavailableError,
    ArtifactSource,
    ArtifactValidationError,
)


# Identifier components (artifact_id, task_id, publish_id): alphanumeric,
# underscore, hyphen. No path separators, no dots.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Filename components (stored_name, server_filename): alphanumeric, dot,
# underscore, hyphen. Slashes and backslashes are rejected. Exact "." or
# ".." are rejected separately.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Safe extension derived from the display filename: leading dot + 1-8
# alphanumeric chars.
_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

_ITEM_PREFIX = "item:"
_PUBLISHED_PREFIX = "published:"
_ATTACHMENT_PREFIX = "attachment:"
_WORKSPACE_PREFIX = "workspace:"

_ITEMS_DIR = "items"
_PUBLISHED_DIR = "published"

_CHUNK_SIZE = 64 * 1024


class LocalArtifactContentStore:
    """Local filesystem implementation of ArtifactContentStore.

    Security invariants:
      - Disk filenames are server-generated (uuid4 hex); the client-supplied
        ``filename`` is display-only and never used as a disk path.
      - ALL path resolution validates every component via lstat and rejects
        symlinks per-component (not a single resolved is_symlink check).
      - Attachment/workspace resolvers accept only structured descriptors,
        never raw client filesystem paths.
      - delete_owned rejects attachment/workspace refs; only item/published
        refs may be deleted.
      - Writes are atomic (same-dir temp + fsync + os.replace) and stream
        size-limit accumulation; over-limit writes abort and clean up temp.
      - Reads are bounded: max_bytes is enforced before returning.
    """

    def __init__(
        self,
        root: Path,
        attachments_root: Path,
        workspace_root: Path,
        *,
        max_bytes: int = 20 * 1024 * 1024,
        publish_max_bytes: int = 10 * 1024 * 1024,
        inline_max_bytes: int = 256 * 1024,
    ) -> None:
        self._root = Path(root)
        self._attachments_root = Path(attachments_root)
        self._workspace_root = Path(workspace_root)
        self._max_bytes = max_bytes
        self._publish_max_bytes = publish_max_bytes
        self._inline_max_bytes = inline_max_bytes

    # ------------------------------------------------------------------
    # Port methods
    # ------------------------------------------------------------------

    async def read(self, content_ref: str, *, max_bytes: int) -> bytes:
        scheme, parts = self._parse_ref(content_ref)
        root = self._root_for_scheme(scheme)
        return await asyncio.to_thread(self._read_sync, root, parts, max_bytes)

    async def write_atomic(
        self, artifact_id: str, filename: str, data: bytes
    ) -> str:
        return await asyncio.to_thread(
            self._write_atomic_sync, artifact_id, filename, data
        )

    async def delete_owned(self, content_ref: str) -> bool:
        return await asyncio.to_thread(self._delete_owned_sync, content_ref)

    async def materialize_source(
        self,
        source_kind: ArtifactSource,
        source_ref: str,
        artifact_id: str,
    ) -> str:
        return await asyncio.to_thread(
            self._materialize_source_sync, source_kind, source_ref, artifact_id
        )

    async def copy_to_publish_snapshot(
        self, src_ref: str, publish_id: str, *, inline: str | None = None
    ) -> str:
        return await asyncio.to_thread(
            self._copy_to_publish_snapshot_sync, src_ref, publish_id, inline
        )

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def _read_sync(
        self, root: Path, parts: list[str], max_bytes: int
    ) -> bytes:
        if max_bytes < 0:
            raise ArtifactValidationError("max_bytes must be non-negative")
        path = self._resolve_existing(root, parts)
        try:
            st = path.lstat()
        except OSError as exc:
            raise ArtifactContentUnavailableError(
                f"unreadable: {exc}"
            ) from exc
        if not stat.S_ISREG(st.st_mode):
            raise ArtifactContentUnavailableError("not a regular file")
        # Enforce bound BEFORE returning: if declared size exceeds max_bytes,
        # reject immediately; then stream-read as defense-in-depth.
        if st.st_size > max_bytes:
            raise ArtifactValidationError(
                f"content size {st.st_size} exceeds max_bytes {max_bytes}"
            )
        chunks: list[bytes] = []
        total = 0
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArtifactValidationError(
                            f"content exceeds max_bytes ({max_bytes})"
                        )
                    chunks.append(chunk)
        except OSError as exc:
            raise ArtifactContentUnavailableError(
                f"read error: {exc}"
            ) from exc
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # write_atomic
    # ------------------------------------------------------------------

    def _write_atomic_sync(
        self, artifact_id: str, filename: str, data: bytes
    ) -> str:
        _validate_id(artifact_id, "artifact_id")
        server_filename = _generate_server_filename(filename)
        items_root = self._root / _ITEMS_DIR
        artifact_dir = items_root / artifact_id
        self._ensure_safe_dir(items_root)
        self._ensure_safe_dir(artifact_dir)
        self._write_file_bounded(
            artifact_dir, server_filename, data, self._max_bytes
        )
        return f"{_ITEM_PREFIX}{artifact_id}/{server_filename}"

    # ------------------------------------------------------------------
    # delete_owned
    # ------------------------------------------------------------------

    def _delete_owned_sync(self, content_ref: str) -> bool:
        scheme, parts = self._parse_ref(content_ref)
        if scheme not in ("item", "published"):
            raise ArtifactValidationError(
                f"delete_owned rejects {scheme} refs "
                f"(only item/published are deletable)"
            )
        root = self._root_for_scheme(scheme)
        # Walk parent components (all but leaf) to reject symlinks. If the
        # parent chain is missing the content is already gone -> False.
        try:
            parent = self._resolve_parent(root, parts)
        except ArtifactContentUnavailableError:
            return False
        leaf_name = parts[-1]
        target = parent / leaf_name
        try:
            st = target.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode):
            raise ArtifactValidationError(
                f"refuse to delete symlink: {leaf_name}"
            )
        if not stat.S_ISREG(st.st_mode):
            raise ArtifactValidationError(
                f"refuse to delete non-regular file: {leaf_name}"
            )
        try:
            os.unlink(target)
        except OSError as exc:
            raise ArtifactContentUnavailableError(
                f"delete failed: {exc}"
            ) from exc
        return True

    # ------------------------------------------------------------------
    # materialize_source
    # ------------------------------------------------------------------

    def _materialize_source_sync(
        self,
        source_kind: ArtifactSource,
        source_ref: str,
        artifact_id: str,
    ) -> str:
        _validate_id(artifact_id, "artifact_id")
        if source_kind is ArtifactSource.TASK_ATTACHMENT:
            src_root, src_parts = self._parse_typed_ref(
                source_ref, "attachment"
            )
        elif source_kind is ArtifactSource.TASK_ARTIFACT:
            src_root, src_parts = self._parse_typed_ref(
                source_ref, "workspace"
            )
        else:
            raise ArtifactValidationError(
                f"materialize_source does not support source_kind "
                f"{source_kind!r} (manual/session are inline or out of scope)"
            )
        src_path = self._resolve_existing(src_root, src_parts)
        server_filename = _generate_server_filename(src_parts[-1])
        items_root = self._root / _ITEMS_DIR
        artifact_dir = items_root / artifact_id
        self._ensure_safe_dir(items_root)
        self._ensure_safe_dir(artifact_dir)
        # Stream-copy source into owned storage without modifying source.
        self._stream_copy(src_path, artifact_dir, server_filename,
                          self._max_bytes)
        return f"{_ITEM_PREFIX}{artifact_id}/{server_filename}"

    # ------------------------------------------------------------------
    # copy_to_publish_snapshot
    # ------------------------------------------------------------------

    def _copy_to_publish_snapshot_sync(
        self, src_ref: str, publish_id: str, inline: str | None
    ) -> str:
        _validate_id(publish_id, "publish_id")
        published_root = self._root / _PUBLISHED_DIR
        publish_dir = published_root / publish_id
        self._ensure_safe_dir(published_root)
        self._ensure_safe_dir(publish_dir)
        if inline is not None:
            data = inline.encode("utf-8")
            if len(data) > self._inline_max_bytes:
                raise ArtifactValidationError(
                    f"inline content {len(data)} exceeds "
                    f"inline_max_bytes {self._inline_max_bytes}"
                )
            server_filename = _generate_server_filename("snapshot.txt")
            self._write_file_bounded(
                publish_dir, server_filename, data, self._inline_max_bytes
            )
            return f"{_PUBLISHED_PREFIX}{publish_id}/{server_filename}"
        # Copy from owned item ref.
        scheme, parts = self._parse_ref(src_ref)
        if scheme != "item":
            raise ArtifactValidationError(
                "copy_to_publish_snapshot requires an item ref "
                "when inline is None"
            )
        src_root = self._root_for_scheme(scheme)
        src_path = self._resolve_existing(src_root, parts)
        server_filename = _generate_server_filename(parts[-1])
        self._stream_copy(
            src_path, publish_dir, server_filename, self._publish_max_bytes
        )
        return f"{_PUBLISHED_PREFIX}{publish_id}/{server_filename}"

    # ------------------------------------------------------------------
    # Path resolution helpers (per-component symlink rejection)
    # ------------------------------------------------------------------

    def _root_for_scheme(self, scheme: str) -> Path:
        if scheme == "item":
            return self._root / _ITEMS_DIR
        if scheme == "published":
            return self._root / _PUBLISHED_DIR
        if scheme == "attachment":
            return self._attachments_root
        if scheme == "workspace":
            return self._workspace_root
        raise ArtifactValidationError(f"unknown scheme: {scheme}")

    def _resolve_existing(self, root: Path, parts: list[str]) -> Path:
        """Resolve root/parts for reading. All components must exist;
        intermediate must be dirs, leaf must be a regular file. Rejects
        ANY symlink component (per-component lstat check)."""
        self._check_root(root)
        current = root
        for i, part in enumerate(parts):
            is_leaf = i == len(parts) - 1
            current = current / part
            try:
                st = current.lstat()
            except FileNotFoundError as exc:
                raise ArtifactContentUnavailableError(
                    f"missing path component: {part}"
                ) from exc
            if stat.S_ISLNK(st.st_mode):
                raise ArtifactValidationError(
                    f"symlink component not allowed: {part}"
                )
            if is_leaf:
                if not stat.S_ISREG(st.st_mode):
                    raise ArtifactValidationError(
                        f"target is not a regular file: {part}"
                    )
            else:
                if not stat.S_ISDIR(st.st_mode):
                    raise ArtifactValidationError(
                        f"path component is not a directory: {part}"
                    )
        return current

    def _resolve_parent(self, root: Path, parts: list[str]) -> Path:
        """Resolve all but the last component for delete/write targets.
        Each parent component must exist, be a directory, and not be a
        symlink. Returns the parent directory Path."""
        if len(parts) < 2:
            # Need at least root + one component; parent is root itself.
            self._check_root(root)
            return root
        self._check_root(root)
        current = root
        for part in parts[:-1]:
            current = current / part
            try:
                st = current.lstat()
            except FileNotFoundError as exc:
                raise ArtifactContentUnavailableError(
                    f"missing parent component: {part}"
                ) from exc
            if stat.S_ISLNK(st.st_mode):
                raise ArtifactValidationError(
                    f"symlink parent component not allowed: {part}"
                )
            if not stat.S_ISDIR(st.st_mode):
                raise ArtifactValidationError(
                    f"parent component is not a directory: {part}"
                )
        return current

    def _check_root(self, root: Path) -> None:
        try:
            st = root.lstat()
        except OSError as exc:
            raise ArtifactContentUnavailableError(
                f"root inaccessible: {root}"
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise ArtifactValidationError("root must not be a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise ArtifactValidationError("root must be a directory")

    def _ensure_safe_dir(self, path: Path) -> None:
        """Ensure path is a real directory (not a symlink). Create if
        missing. Re-verify after creation to defend against TOCTOU."""
        try:
            st = path.lstat()
        except FileNotFoundError:
            path.mkdir(parents=False, exist_ok=False)
            try:
                st = path.lstat()
            except OSError as exc:
                raise ArtifactContentUnavailableError(
                    f"cannot stat directory after create: {path}"
                ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise ArtifactValidationError(
                f"directory is a symlink: {path}"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise ArtifactValidationError(
                f"path is not a directory: {path}"
            )

    # ------------------------------------------------------------------
    # Write helpers (atomic + bounded + streaming)
    # ------------------------------------------------------------------

    def _write_file_bounded(
        self,
        directory: Path,
        filename: str,
        data: bytes,
        max_bytes: int,
    ) -> None:
        """Write data to directory/filename atomically with size limit.

        Same-directory temp file, fsync, os.replace. Streams in chunks
        accumulating size; over-limit aborts and cleans up temp.
        """
        target = directory / filename
        # Reject if target already exists as a symlink (do not write through).
        try:
            existing = target.lstat()
            if stat.S_ISLNK(existing.st_mode):
                raise ArtifactValidationError(
                    f"refuse to overwrite symlink: {filename}"
                )
        except FileNotFoundError:
            pass
        fd, tmp_path = tempfile.mkstemp(
            dir=str(directory), prefix=".__tmp_", suffix=".tmp"
        )
        total = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                offset = 0
                while offset < len(data):
                    chunk = data[offset:offset + _CHUNK_SIZE]
                    fh.write(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArtifactValidationError(
                            f"content size {total} exceeds limit "
                            f"{max_bytes}"
                        )
                    offset += _CHUNK_SIZE
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _stream_copy(
        self,
        src: Path,
        dst_dir: Path,
        dst_name: str,
        max_bytes: int,
    ) -> None:
        """Stream-copy src to dst_dir/dst_name atomically with size limit.

        Reads source in chunks, writes to a same-directory temp file,
        accumulates size, fsyncs, and os.replace. Over-limit aborts and
        cleans up temp. Does NOT modify the source file.
        """
        target = dst_dir / dst_name
        try:
            existing = target.lstat()
            if stat.S_ISLNK(existing.st_mode):
                raise ArtifactValidationError(
                    f"refuse to overwrite symlink: {dst_name}"
                )
        except FileNotFoundError:
            pass
        # Re-lstat source immediately before opening, mirroring _read_sync:
        # _resolve_existing already checked, but defend against TOCTOU swap
        # to a symlink between resolution and open.
        try:
            src_st = src.lstat()
        except OSError as exc:
            raise ArtifactContentUnavailableError(
                f"source unreadable: {exc}"
            ) from exc
        if stat.S_ISLNK(src_st.st_mode):
            raise ArtifactValidationError("source is a symlink")
        if not stat.S_ISREG(src_st.st_mode):
            raise ArtifactContentUnavailableError("source is not a regular file")
        fd, tmp_path = tempfile.mkstemp(
            dir=str(dst_dir), prefix=".__tmp_", suffix=".tmp"
        )
        total = 0
        try:
            with open(src, "rb") as src_fh, os.fdopen(fd, "wb") as dst_fh:
                while True:
                    chunk = src_fh.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst_fh.write(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArtifactValidationError(
                            f"content size {total} exceeds limit "
                            f"{max_bytes}"
                        )
                dst_fh.flush()
                os.fsync(dst_fh.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # content_ref parsing
    # ------------------------------------------------------------------

    def _parse_ref(self, content_ref: str) -> tuple[str, list[str]]:
        """Parse a content_ref into (scheme, parts). Raises on invalid."""
        if not isinstance(content_ref, str) or not content_ref:
            raise ArtifactValidationError(
                "content_ref must be a non-empty string"
            )
        for prefix, scheme in (
            (_ITEM_PREFIX, "item"),
            (_PUBLISHED_PREFIX, "published"),
            (_ATTACHMENT_PREFIX, "attachment"),
            (_WORKSPACE_PREFIX, "workspace"),
        ):
            if content_ref.startswith(prefix):
                rest = content_ref[len(prefix):]
                return scheme, self._parse_scheme_parts(scheme, rest)
        raise ArtifactValidationError(
            f"unknown content_ref scheme: {content_ref!r}"
        )

    def _parse_typed_ref(
        self, source_ref: str, expected_scheme: str
    ) -> tuple[Path, list[str]]:
        """Parse a source_ref expected to be a specific scheme (used by
        materialize_source). Returns (root, parts)."""
        scheme, parts = self._parse_ref(source_ref)
        if scheme != expected_scheme:
            raise ArtifactValidationError(
                f"expected {expected_scheme} ref, got {scheme}: {source_ref!r}"
            )
        return self._root_for_scheme(scheme), parts

    def _parse_scheme_parts(self, scheme: str, rest: str) -> list[str]:
        if scheme in ("item", "published", "attachment"):
            if "/" not in rest:
                raise ArtifactValidationError(
                    f"invalid {scheme} ref: missing filename component"
                )
            id_part, file_part = rest.split("/", 1)
            _validate_id(id_part, f"{scheme} id")
            _validate_filename(file_part, f"{scheme} filename")
            if "/" in file_part:
                raise ArtifactValidationError(
                    f"invalid {scheme} ref: nested path in filename"
                )
            return [id_part, file_part]
        # workspace
        return _validate_workspace_path(rest)


# ---------------------------------------------------------------------------
# Module-level validation helpers
# ---------------------------------------------------------------------------


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ArtifactValidationError(f"{name} must be a non-empty string")
    if not _ID_RE.match(value):
        raise ArtifactValidationError(
            f"{name} contains invalid characters (allowed: alphanumeric, "
            f"underscore, hyphen)"
        )


def _validate_filename(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ArtifactValidationError(f"{name} must be a non-empty string")
    if value in (".", ".."):
        raise ArtifactValidationError(f"{name} must not be {value!r}")
    if not _FILENAME_RE.match(value):
        raise ArtifactValidationError(
            f"{name} contains invalid characters (allowed: alphanumeric, "
            f"dot, underscore, hyphen)"
        )


def _validate_workspace_path(rel: str) -> list[str]:
    if not isinstance(rel, str) or not rel:
        raise ArtifactValidationError("workspace path must be non-empty")
    if rel[0] == "/":
        raise ArtifactValidationError("absolute path not allowed")
    if "\\" in rel:
        raise ArtifactValidationError("backslash not allowed")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ArtifactValidationError(
                f"invalid path component: {part!r}"
            )
    return parts


def _generate_server_filename(filename: str) -> str:
    """Generate a server-controlled disk filename (uuid4 hex + optional
    safe extension). The client-supplied filename is display-only."""
    base = uuid4().hex
    ext = _safe_ext(filename)
    return base + ext


def _safe_ext(filename: str) -> str:
    """Derive a safe extension from the display filename, or empty string."""
    if not isinstance(filename, str) or not filename:
        return ""
    idx = filename.rfind(".")
    if idx <= 0:
        return ""
    ext = filename[idx:].lower()
    if _SAFE_EXT_RE.match(ext):
        return ext
    return ""
