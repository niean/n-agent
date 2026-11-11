"""Local filesystem EvidenceStore (Infrastructure Layer).

T4 of the unified evaluation closed loop: implements the Domain
``EvidenceStore`` async Protocol (``app/domain/evaluation/ports.py``)
over the local filesystem, with the T3 ``SQLiteEvaluationStore`` as the
reference registry. The evidence root is constructor-injected (default
``locals/evaluations/`` wiring happens at the composition root, plan
T20); every file lives under ``<root>/snapshots/``.

Write order (spec 6.3 / plan T4):
    temp file (same dir) -> fsync -> atomic ``os.replace`` -> DB
    registration via the T3 store's sample/evidence APIs -> fail-closed
    live-reference verification. The file is durable BEFORE the
    reference row exists, so a failed registration leaves an
    unreferenced orphan that the TTL sweeper reclaims. After
    registration the store verifies at least one live reference row
    points at the ref: an idempotent registration replay onto an
    expiry-cleared row would otherwise return success with a ref that
    ``load_snapshot`` rejects (dangling ref + orphan file) -- that case
    raises instead, and a file created by this call is cleaned up.

Reference registry convention (documented judgment call):
    Snapshots are content-addressed: the file name is the SHA-256 of the
    domain canonical JSON encoding and the persisted bytes hash to
    exactly ``EvidenceSnapshot.content_sha256``. Registration reuses the
    T3 ``save_sample`` API with a store-generated idempotency key
    ``producer_event_id = evidence:{kind}:{subject_id}:{subject_revision}:{sha}``
    so identical re-captures for the same target are idempotent, while
    different targets sharing identical content each get their own
    reference row. ``redaction_version`` (needed to faithfully
    reconstruct the EvidenceSnapshot on load) travels inside the row's
    ``target`` JSON projection; ``schema_version`` is set to
    ``evidence.v{SCHEMA_VERSION}`` to mark evidence-store-owned rows.

TTL reclamation (spec 13: snapshots 7 days, results/reports 90 days):
    - Unreferenced orphans and crash-left temp files
      (``.__tmp_*.tmp``) older than the retention window are deleted,
      bounded to ``max_files`` deletions per sweep (shared budget).
    - Referenced non-expired files are never orphan-deleted.
    - Referenced files past the snapshot retention follow the
      expiry-revocation flow: first revoke queued jobs that could no
      longer execute against the purged evidence (via
      ``revoke_queued_jobs_for_samples``), then clear the content
      references (``clear_sample_snapshot_ref``), then purge the file.
      Results and reports are ledger rows with their own 90-day
      retention and are intentionally untouched here.
    - Expiry never enables implicit re-creation: revoked jobs are not
      claimable, purged refs are not loadable, and re-capturing
      identical content after its registration row was cleared fails
      closed (see write order above); only an explicit new capture
      against a live registration may store content again.

Security invariants (mirroring ``LocalArtifactContentStore``):
    - Disk filenames are server-generated (SHA-256 hex); snapshot_ref
      format is strictly validated, which rejects traversal (``..``,
      absolute paths, separators) by construction.
    - Path resolution rejects symlink components via per-component
      lstat; the sweeper skips symlinks and non-regular files without
      following or deleting them.
    - Writes are atomic (same-directory temp + fsync + os.replace). The
      content-reuse path refreshes an expired on-disk copy via the same
      temp+replace, so a concurrent sweep between the reuse check and
      registration cannot delete the file out from under a live
      registration.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from app.domain.evaluation.models import (
    EVIDENCE_SNAPSHOT_MAX_BYTES,
    SCHEMA_VERSION,
    EvaluationTarget,
    EvaluationValidationError,
    EvidenceSnapshot,
)

# Snapshot retention (spec 13: 快照 7 天；结果和报告 90 天属账本侧保留，
# 不由本文件存储管理).
SNAPSHOT_RETENTION: timedelta = timedelta(days=7)

# Default bound of deletions per sweep round (shared by snapshot files
# and crash-left temp files).
DEFAULT_SWEEP_MAX_FILES: int = 100

_SNAPSHOTS_DIR = "snapshots"

# snapshot_ref is server-generated: snapshots/{sha256}.json. The strict
# format rejects traversal (``..``, absolute paths, ``\\``, foreign
# extensions) by construction.
_REF_RE = re.compile(r"^snapshots/([0-9a-f]{64})\.json$")

_FILENAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")

# Crash-left temp files from interrupted atomic writes.
_TEMP_FILENAME_RE = re.compile(r"^\.__tmp_.*\.tmp$")

_CHUNK_SIZE = 64 * 1024

# Local copy of the 64-char lowercase-hex SHA-256 check. The domain
# validator (models._require_sha256_hex) is private/not exported, so it
# cannot be imported without reaching into a private name.
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceStoreError(RuntimeError):
    """证据存储层错误基类（实现声明的端口错误语义）。"""


class EvidenceNotFoundError(EvidenceStoreError):
    """快照文件缺失、未登记引用或内容不可恢复。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LocalEvidenceStore:
    """Local filesystem implementation of the EvidenceStore port.

    Collaborates with the T3 ``SQLiteEvaluationStore`` for reference
    registration and lifecycle queries; any object exposing
    ``save_sample`` / ``list_samples_by_snapshot_ref`` /
    ``clear_sample_snapshot_ref`` / ``revoke_queued_jobs_for_samples``
    (async) is accepted.
    """

    def __init__(
        self,
        root: Path,
        store: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        retention: timedelta = SNAPSHOT_RETENTION,
    ) -> None:
        self._root = Path(root)
        self._store = store
        self._clock = clock or _utcnow
        self._retention = retention

    # ------------------------------------------------------------------
    # Port methods
    # ------------------------------------------------------------------

    async def save_snapshot(
        self, target: EvaluationTarget, snapshot: EvidenceSnapshot
    ) -> str:
        """持久化快照并登记引用，返回 snapshot_ref。

        顺序：先原子写文件（临时文件 + fsync + rename），文件落盘成功后
        才调用 T3 store 登记引用；登记失败时文件保留为未引用孤儿，由
        TTL 回收处理，错误原样向上传播。登记成功后做 fail-closed 校验：
        必须存在指向该 ref 的有效引用行，否则抛错并清理本次新建的文件
        （幂等重放撞上已被到期撤销清除的登记行时，禁止"返回成功但 ref
        不可 load"的悬挂状态）。
        """
        if not isinstance(target, EvaluationTarget):
            raise EvaluationValidationError(
                "target must be an EvaluationTarget"
            )
        data = self._snapshot_bytes(snapshot)
        sha = self._snapshot_sha(snapshot)
        if hashlib.sha256(data).hexdigest() != sha:
            raise EvaluationValidationError(
                f"content_sha256 does not match sha256(canonical_bytes): "
                f"declared {sha!r}"
            )
        ref = f"{_SNAPSHOTS_DIR}/{sha}.json"
        now_dt = self._checked_now(None)
        cutoff = now_dt.astimezone(timezone.utc) - self._retention
        created = await asyncio.to_thread(
            self._write_snapshot_file_sync, sha, data, cutoff
        )
        await self._store.save_sample(
            self._registration_payload(target, snapshot, ref)
        )
        rows = await self._store.list_samples_by_snapshot_ref(ref)
        if not rows:
            # Fail-closed: no live reference (e.g. idempotent replay onto
            # an expiry-cleared registration row). Remove the file this
            # call created; a pre-existing (reused) file is left to the
            # TTL sweeper, which reclaims it as an unreferenced orphan.
            if created:
                await asyncio.to_thread(
                    self._delete_file_sync, self._snapshot_file(sha)
                )
            raise EvidenceStoreError(
                f"snapshot registration did not produce a live "
                f"reference: {ref}"
            )
        return ref

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot:
        """按 snapshot_ref 读取快照。

        只接受已登记引用的 ref：引用行提供 ``redaction_version``（快照
        可复现性的一部分，不随正文落盘），且行内 ``snapshot_hash`` 必须
        与 ref 摘要严格相等（空串不豁免校验）。同一 ref 存在多条引用行
        时按确定性规则取最早登记行（created_at, sample_id 排序的第一
        条）。文件缺失、ref 未登记或内容被并发删除抛
        EvidenceNotFoundError；内容/登记 hash 不一致抛
        EvidenceStoreError。
        """
        sha = self._parse_ref(snapshot_ref)
        rows = await self._store.list_samples_by_snapshot_ref(snapshot_ref)
        if not rows:
            raise EvidenceNotFoundError(
                f"snapshot ref has no live registration: {snapshot_ref}"
            )
        row = rows[0]
        return await asyncio.to_thread(self._read_snapshot_file_sync, sha, row)

    async def has_snapshot(self, snapshot_ref: str) -> bool:
        """判断 ref 是否存在已登记引用（存在性检查，不读取 2MiB 正文）。

        供 replay 等只需验证证据存在性的调用方使用；ref 非法按不存在
        处理（与 load_snapshot 的严格校验一致，但不抛错）。"""
        try:
            self._parse_ref(snapshot_ref)
        except Exception:
            return False
        rows = await self._store.list_samples_by_snapshot_ref(snapshot_ref)
        return bool(rows)

    async def purge_snapshot(self, snapshot_ref: str) -> bool:
        """删除传播的正文 purge（T18/spec 14：删除会话/文档时清除关联
        证据内容，tombstone 行保留审计）。

        仅接受合法 snapshot_ref（经 _parse_ref 严格校验，拒绝路径穿越），
        经 _resolve_existing 逐组件 lstat 解析（T18/Mi1：拒绝任何符号
        链接目录组件与符号链接文件，对齐 load_snapshot 的 TOCTOU 防御）
        后按 _delete_file_sync 的边界语义删除（拒绝符号链接/非常规文件）。
        文件不存在返回 False（重复删除事件幂等，不重复计数）；共享同一
        内容文件的其余引用行由调用方（deletion service）在 purge 前自行
        判定。"""
        sha = self._parse_ref(snapshot_ref)
        try:
            target = await asyncio.to_thread(self._resolve_existing, sha)
        except EvidenceNotFoundError:
            return False
        return await asyncio.to_thread(self._delete_file_sync, target)

    async def sweep(
        self,
        *,
        now: datetime | None = None,
        max_files: int = DEFAULT_SWEEP_MAX_FILES,
    ) -> Mapping[str, Any]:
        """带 TTL 的受限回收（单轮最多删除 max_files 个文件，快照文件与
        崩溃残留临时文件共享该预算）。

        ``now`` 省略时使用注入 clock；显式传入时必须是带时区的
        datetime，naive 值拒绝。

        返回报告 Mapping：
        - ``orphan_deleted``：未引用且过期的快照文件 ref；
        - ``expired_purged``：到期撤销流程 purge 的被引用文件 ref；
        - ``revoked_job_ids`` / ``cleared_sample_ids``：撤销与清除记录；
        - ``temp_deleted``：回收的 ``.__tmp_*.tmp`` 崩溃残留临时文件名；
        - ``skipped``：跳过的非快照命名/符号链接/非常规条目名（绝不
          跟随或删除）。
        """
        now_dt = self._checked_now(now)
        cutoff = now_dt.astimezone(timezone.utc) - self._retention
        entries, skipped = await asyncio.to_thread(
            self._list_snapshot_files_sync
        )
        report: dict[str, Any] = {
            "orphan_deleted": [],
            "expired_purged": [],
            "revoked_job_ids": [],
            "cleared_sample_ids": [],
            "temp_deleted": [],
            "skipped": skipped,
        }
        deleted_count = 0
        for ref, path, mtime in entries:
            if deleted_count >= max_files:
                break
            if mtime >= cutoff:
                continue  # 未到期：无论是否被引用都保留
            if ref is None:
                # Crash-left temp file: unreferenced by definition.
                if await asyncio.to_thread(self._delete_file_sync, path):
                    report["temp_deleted"].append(path.name)
                    deleted_count += 1
                continue
            rows = await self._store.list_samples_by_snapshot_ref(ref)
            if not rows:
                if await asyncio.to_thread(self._delete_file_sync, path):
                    report["orphan_deleted"].append(ref)
                    deleted_count += 1
                continue
            # 到期撤销流程：先撤销无法继续执行的 job，再清除内容引用，
            # 最后 purge 文件。
            sample_ids = [row["sample_id"] for row in rows]
            revoked = await self._store.revoke_queued_jobs_for_samples(
                sample_ids, now=now_dt
            )
            for sample_id in sample_ids:
                if await self._store.clear_sample_snapshot_ref(
                    sample_id, ref
                ):
                    report["cleared_sample_ids"].append(sample_id)
            if await asyncio.to_thread(self._delete_file_sync, path):
                report["revoked_job_ids"].extend(revoked)
                report["expired_purged"].append(ref)
                deleted_count += 1
        return report

    # ------------------------------------------------------------------
    # Time / snapshot byte contract
    # ------------------------------------------------------------------

    def _checked_now(self, now: datetime | None) -> datetime:
        """Resolve the effective now; naive datetimes are rejected so
        retention math can never silently mix aware/naive values."""
        dt = now if now is not None else self._clock()
        if not isinstance(dt, datetime) or dt.tzinfo is None:
            raise EvaluationValidationError(
                f"now must be a timezone-aware datetime: {dt!r}"
            )
        return dt

    @staticmethod
    def _snapshot_bytes(snapshot: Any) -> bytes:
        """Validate the byte contract the store persists (a real
        ``EvidenceSnapshot`` always satisfies it).

        The 2MiB cap mirrors domain ``EVIDENCE_SNAPSHOT_MAX_BYTES``:
        the domain constructor already rejects oversized content; this
        backs up the limit for inputs that bypass domain construction.
        """
        data = getattr(snapshot, "canonical_bytes", None)
        if not isinstance(data, (bytes, bytearray)):
            raise EvaluationValidationError(
                "snapshot must expose canonical_bytes as bytes"
            )
        if len(data) > EVIDENCE_SNAPSHOT_MAX_BYTES:
            raise EvaluationValidationError(
                f"evidence snapshot too large: {len(data)} bytes > "
                f"{EVIDENCE_SNAPSHOT_MAX_BYTES} byte limit"
            )
        return bytes(data)

    @staticmethod
    def _snapshot_sha(snapshot: Any) -> str:
        sha = getattr(snapshot, "content_sha256", None)
        if not isinstance(sha, str) or not _SHA256_HEX_RE.match(sha):
            raise EvaluationValidationError(
                f"snapshot.content_sha256 must be 64-char lowercase hex: "
                f"{sha!r}"
            )
        return sha

    @staticmethod
    def _registration_payload(
        target: EvaluationTarget, snapshot: Any, ref: str
    ) -> dict[str, Any]:
        """构造经 T3 ``save_sample`` 登记的引用行载荷。

        ``redaction_version`` 随 target JSON 投影保存（load 时还原快照
        所需）；不含 captured_at 使 payload hash 确定性可重放。
        """
        target_projection: dict[str, Any] = {
            "service": target.service,
            "kind": target.kind,
            "subject_id": target.subject_id,
            "subject_revision": target.subject_revision,
            "redaction_version": snapshot.redaction_version,
        }
        for field in ("trace_id", "span_id", "run_id", "session_id",
                      "retrieval_id"):
            value = getattr(target, field)
            if value is not None:
                target_projection[field] = value
        return {
            "service": target.service,
            "producer_event_id": (
                f"evidence:{target.kind}:{target.subject_id}:"
                f"{target.subject_revision}:{snapshot.content_sha256}"
            ),
            "snapshot_ref": ref,
            "snapshot_hash": snapshot.content_sha256,
            "schema_version": f"evidence.v{SCHEMA_VERSION}",
            "sample_probability": 1.0,
            "target": target_projection,
            "evidence_targets": [
                {
                    "source_service": target.service,
                    "subject_kind": target.kind,
                    "subject_id": target.subject_id,
                    "subject_revision": target.subject_revision,
                }
            ],
        }

    # ------------------------------------------------------------------
    # Filesystem: atomic write / safe resolve / bounded delete
    # ------------------------------------------------------------------

    def _snapshots_dir(self) -> Path:
        snapshots_root = self._root / _SNAPSHOTS_DIR
        self._ensure_safe_dir(self._root)
        self._ensure_safe_dir(snapshots_root)
        return snapshots_root

    def _snapshot_file(self, sha: str) -> Path:
        return self._root / _SNAPSHOTS_DIR / f"{sha}.json"

    def _write_snapshot_file_sync(
        self, sha: str, data: bytes, cutoff: datetime
    ) -> bool:
        """Atomically persist the snapshot bytes; return whether this
        call created the file (False = fresh content-addressed reuse).

        An existing but expired copy is rewritten via temp+replace to
        refresh its mtime: durable against a concurrent sweep deleting
        the expired file between the reuse check and DB registration.
        """
        snapshots_root = self._snapshots_dir()
        target = snapshots_root / f"{sha}.json"
        try:
            existing = target.lstat()
            if stat.S_ISLNK(existing.st_mode):
                raise EvidenceStoreError(
                    f"refuse to overwrite symlink: {target.name}"
                )
            if not stat.S_ISREG(existing.st_mode):
                raise EvidenceStoreError(
                    f"refuse to overwrite non-regular file: {target.name}"
                )
            if datetime.fromtimestamp(
                existing.st_mtime, tz=timezone.utc
            ) >= cutoff:
                return False  # fresh reuse
            # Expired on-disk copy: fall through to the atomic rewrite
            # below to refresh the mtime.
        except FileNotFoundError:
            pass
        fd, tmp_path = tempfile.mkstemp(
            dir=str(snapshots_root), prefix=".__tmp_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                offset = 0
                while offset < len(data):
                    chunk = data[offset:offset + _CHUNK_SIZE]
                    fh.write(chunk)
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
        return True

    def _read_snapshot_file_sync(
        self, sha: str, row: Mapping[str, Any]
    ) -> EvidenceSnapshot:
        # Strict equality: an empty/legacy row hash must NOT silently
        # skip the registered-hash verification.
        if row.get("snapshot_hash") != sha:
            raise EvidenceStoreError(
                f"registered snapshot_hash mismatch: "
                f"{row.get('snapshot_hash')!r} != {sha!r}"
            )
        target = self._resolve_existing(sha)
        try:
            data = target.read_bytes()
        except FileNotFoundError as exc:
            # TOCTOU: file vanished between resolve and open (e.g.
            # concurrent sweep); mirror content_store _read_sync.
            raise EvidenceNotFoundError(
                f"snapshot file disappeared during read: {target.name}"
            ) from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha:
            raise EvidenceStoreError(
                f"snapshot content hash mismatch: expected {sha}, "
                f"got {actual}"
            )
        try:
            content = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceStoreError(
                f"snapshot body is not valid JSON: {exc}"
            ) from exc
        if not isinstance(content, dict):
            raise EvidenceStoreError("snapshot body must be a JSON object")
        target_projection = row.get("target") or {}
        redaction_version = target_projection.get("redaction_version")
        if not isinstance(redaction_version, str) or not redaction_version:
            raise EvidenceStoreError(
                "registration row is missing redaction_version: "
                f"{row.get('sample_id')}"
            )
        return EvidenceSnapshot(
            content=content, redaction_version=redaction_version
        )

    def _resolve_existing(self, sha: str) -> Path:
        """逐组件 lstat 解析 <root>/snapshots/{sha}.json，拒绝任何符号
        链接组件（对齐 LocalArtifactContentStore 的 TOCTOU 防御）。"""
        current = self._root
        self._lstat_dir(current, "evidence root")
        current = current / _SNAPSHOTS_DIR
        self._lstat_dir(current, _SNAPSHOTS_DIR)
        current = current / f"{sha}.json"
        try:
            st = current.lstat()
        except FileNotFoundError as exc:
            raise EvidenceNotFoundError(
                f"snapshot file not found: {_SNAPSHOTS_DIR}/{sha}.json"
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise EvaluationValidationError(
                f"symlink component not allowed: {current.name}"
            )
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceStoreError(
                f"snapshot path is not a regular file: {current.name}"
            )
        return current

    def _lstat_dir(self, path: Path, label: str) -> None:
        try:
            st = path.lstat()
        except FileNotFoundError as exc:
            raise EvidenceNotFoundError(
                f"{label} not found: {path}"
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise EvaluationValidationError(
                f"{label} is a symlink: {path}"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise EvidenceStoreError(f"{label} is not a directory: {path}")

    def _ensure_safe_dir(self, path: Path) -> None:
        """确保 path 是真实目录（非符号链接），必要时创建并复核。"""
        try:
            st = path.lstat()
        except FileNotFoundError:
            path.mkdir(parents=True, exist_ok=True)
            try:
                st = path.lstat()
            except OSError as exc:
                raise EvidenceStoreError(
                    f"cannot stat directory after create: {path}"
                ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise EvaluationValidationError(f"directory is a symlink: {path}")
        if not stat.S_ISDIR(st.st_mode):
            raise EvidenceStoreError(f"path is not a directory: {path}")

    def _list_snapshot_files_sync(
        self,
    ) -> tuple[list[tuple[str | None, Path, datetime]], list[str]]:
        """枚举快照目录下的候选回收条目。

        返回 (entries, skipped)：entries 为 (ref, path, mtime) 三元组，
        ref 为 None 表示崩溃残留临时文件（``.__tmp_*.tmp``，无注册表
        条目，纯孤儿）；skipped 为跳过的条目名（符号链接、非常规文件、
        外来文件名——绝不跟随或删除）。
        """
        snapshots_root = self._root / _SNAPSHOTS_DIR
        try:
            st = snapshots_root.lstat()
        except FileNotFoundError:
            return [], []
        if stat.S_ISLNK(st.st_mode):
            raise EvaluationValidationError(
                f"directory is a symlink: {snapshots_root}"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise EvidenceStoreError(
                f"path is not a directory: {snapshots_root}"
            )
        entries: list[tuple[str | None, Path, datetime]] = []
        skipped: list[str] = []
        for entry in sorted(snapshots_root.iterdir(), key=lambda p: p.name):
            try:
                est = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(est.st_mode) or not stat.S_ISREG(est.st_mode):
                skipped.append(entry.name)
                continue
            if _TEMP_FILENAME_RE.match(entry.name):
                entries.append(
                    (
                        None,
                        entry,
                        datetime.fromtimestamp(est.st_mtime, tz=timezone.utc),
                    )
                )
                continue
            if not _FILENAME_RE.match(entry.name):
                skipped.append(entry.name)
                continue
            sha = entry.name[: -len(".json")]
            entries.append(
                (
                    f"{_SNAPSHOTS_DIR}/{sha}.json",
                    entry,
                    datetime.fromtimestamp(est.st_mtime, tz=timezone.utc),
                )
            )
        return entries, skipped

    @staticmethod
    def _delete_file_sync(path: Path) -> bool:
        """Delete a regular file; return whether a deletion actually
        happened (missing files report False so concurrent sweeps do not
        double-count). Refuses symlinks/non-regular files."""
        try:
            st = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode):
            raise EvidenceStoreError(f"refuse to delete symlink: {path.name}")
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceStoreError(
                f"refuse to delete non-regular file: {path.name}"
            )
        os.unlink(path)
        return True

    # ------------------------------------------------------------------
    # ref parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ref(snapshot_ref: str) -> str:
        if not isinstance(snapshot_ref, str) or not snapshot_ref:
            raise EvaluationValidationError(
                f"invalid snapshot_ref: {snapshot_ref!r}"
            )
        matched = _REF_RE.match(snapshot_ref)
        if matched is None:
            raise EvaluationValidationError(
                f"invalid snapshot_ref: {snapshot_ref!r}"
            )
        return matched.group(1)
