"""场景资源账目持久化（plan T4）；复用评估账本 DB 文件。

不拥有 PRAGMA user_version（唯一 schema 所有者是 SQLiteEvaluationStore）；
沿用 execution_store 模式在共享 ``evaluation_schema_migrations`` 登记
独立版本号（1001 为 usage 回填、1002 为 dashboard-execution，本表从
1003 起占用，经 INSERT OR IGNORE 冲突检查占号）。

状态迁移全部走 CAS（owner_token + 租约）；租约过期的记录可被恢复方
收养（换发新 token），保证崩溃/SIGTERM 后可重入恢复。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationLeaseError,
    EvaluationNotFoundError,
)
from app.domain.evaluation.models import EvaluationValidationError
from app.domain.evaluation.scenario_resources import (
    RELEASE_ORDER,
    RESOURCE_STATES,
    ResourceIntent,
)
from app.infrastructure.sqlite_support import apply_journal_mode, open_sqlite

# 迁移版本号：1001（usage 回填）/ 1002（dashboard-execution）已占用
SCHEMA_MIGRATION_VERSION: int = 1003
SCHEMA_MIGRATION_NAME: str = "scenario-resources"

_DEFAULT_LEASE_SECONDS: float = 600.0

_FINGERPRINT_KEYS = (
    "execution_id",
    "case_id",
    "resource_kind",
    "release_rule",
    "retention_rule",
    "external_id",
    "ref",
    "expected_sha256",
    "process_identity",
)


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _row_to_record(row) -> dict:
    if row is None:
        return None
    value = dict(row)
    ref = value.pop("ref")
    value["ref"] = ref
    return value


class SQLiteScenarioResourceStore:
    """evaluation_scenario_resources / evaluation_scenario_source_refs。"""

    def __init__(self, db_path: str, *, lease_seconds: float = _DEFAULT_LEASE_SECONDS):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lease_seconds = lease_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_scenario_resources (
                resource_id TEXT PRIMARY KEY,
                business_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                resource_kind TEXT NOT NULL,
                release_order INTEGER NOT NULL,
                release_rule TEXT NOT NULL,
                retention_rule TEXT NOT NULL,
                state TEXT NOT NULL,
                external_id TEXT,
                ref TEXT,
                expected_sha256 TEXT,
                process_identity TEXT,
                retained_until REAL,
                error_code TEXT,
                owner_token TEXT,
                lease_until REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS scenario_resource_execution "
                "ON evaluation_scenario_resources(execution_id,state)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS scenario_resource_retention "
                "ON evaluation_scenario_resources(state,retained_until)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_scenario_source_refs (
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_revision TEXT,
                resource_id TEXT NOT NULL REFERENCES evaluation_scenario_resources(resource_id),
                PRIMARY KEY (source_kind, source_id, source_revision, resource_id))""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS scenario_source_ref_lookup "
                "ON evaluation_scenario_source_refs(source_kind,source_id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS evaluation_schema_migrations("
                "version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)"
            )
            # 冲突检查后占号：同号异名冲突拒绝（防两个迁移抢占同一段）
            existing = conn.execute(
                "SELECT name FROM evaluation_schema_migrations WHERE version=?",
                (SCHEMA_MIGRATION_VERSION,),
            ).fetchone()
            if existing is not None and existing["name"] != SCHEMA_MIGRATION_NAME:
                raise EvaluationConflictError(
                    f"migration version {SCHEMA_MIGRATION_VERSION} already "
                    f"occupied by {existing['name']!r}"
                )
            conn.execute(
                "INSERT OR IGNORE INTO evaluation_schema_migrations "
                "(version,name,applied_at) VALUES (?,?,?)",
                (SCHEMA_MIGRATION_VERSION, SCHEMA_MIGRATION_NAME, _iso(0)),
            )

    def _connect(self):
        conn = open_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        apply_journal_mode(conn)
        return conn

    # ------------------------------------------------------------------
    # intent / CAS 状态迁移
    # ------------------------------------------------------------------

    async def register_intent(self, intent: ResourceIntent, *, now: float) -> dict:
        return await asyncio.to_thread(self._register_intent, intent, now)

    def _register_intent(self, intent: ResourceIntent, now: float) -> dict:
        fingerprint = json.dumps(
            {key: getattr(intent, key) for key in _FINGERPRINT_KEYS},
            ensure_ascii=False, sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE business_key=?",
                (intent.business_key,),
            ).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise EvaluationConflictError(
                        f"business_key payload conflict: {intent.business_key!r}"
                    )
                return _row_to_record(previous)
            resource_id = uuid.uuid4().hex
            token = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO evaluation_scenario_resources(
                    resource_id,business_key,fingerprint,execution_id,case_id,
                    resource_kind,release_order,release_rule,retention_rule,state,
                    external_id,ref,expected_sha256,process_identity,
                    owner_token,lease_until,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    resource_id, intent.business_key, fingerprint,
                    intent.execution_id, intent.case_id, intent.resource_kind,
                    RELEASE_ORDER[intent.resource_kind], intent.release_rule,
                    intent.retention_rule, "intent", intent.external_id,
                    intent.ref, intent.expected_sha256,
                    intent.process_identity, token, now + self._lease_seconds,
                    _iso(now), _iso(now),
                ),
            )
            return _row_to_record(conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone())

    def _leased_row(self, conn, resource_id: str, token: str | None, now: float):
        """当前记录行；token 非 None 时强制 CAS（token 匹配 + 租约有效）。"""
        row = conn.execute(
            "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise EvaluationNotFoundError("scenario resource not found")
        if token is not None and (
            row["owner_token"] != token
            or row["lease_until"] is None
            or row["lease_until"] <= now
        ):
            raise EvaluationLeaseError("scenario resource lease lost")
        return row

    async def confirm_owned(
        self, resource_id: str, *, external_id: str, owner_token: str, now: float,
        ref: str | None = None,
    ) -> dict:
        return await asyncio.to_thread(
            self._confirm_owned, resource_id, external_id, ref, owner_token, now,
        )

    def _confirm_owned(self, resource_id, external_id, ref, token, now) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._leased_row(conn, resource_id, token, now)
            if row["state"] not in ("intent", "unknown"):
                raise EvaluationValidationError(
                    f"confirm_owned 要求 intent/unknown 状态，当前 {row['state']!r}"
                )
            conn.execute(
                """UPDATE evaluation_scenario_resources SET state='owned',
                    external_id=?,ref=COALESCE(?,ref),owner_token=?,
                    lease_until=?,error_code=NULL,updated_at=? WHERE resource_id=?""",
                (external_id, ref, token, now + self._lease_seconds,
                 _iso(now), resource_id),
            )
            return _row_to_record(conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone())

    async def transition(
        self, resource_id: str, owner_token: str, *, state: str, now: float,
        error_code: str | None = None, retained_until: float | None = None,
    ) -> dict:
        return await asyncio.to_thread(
            self._transition, resource_id, owner_token, state,
            error_code, retained_until, now,
        )

    def _transition(self, resource_id, token, state, error_code, retained_until, now) -> dict:
        if state not in RESOURCE_STATES:
            raise EvaluationValidationError(
                f"state 必须在封闭集合内: {state!r}"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._leased_row(conn, resource_id, token, now)
            if row["state"] == state:
                return _row_to_record(row)  # 幂等重放
            if row["state"] in ("released", "cleanup_failed"):
                raise EvaluationValidationError(
                    f"终态 {row['state']!r} 不得再迁移"
                )
            conn.execute(
                """UPDATE evaluation_scenario_resources SET state=?,
                    error_code=?,retained_until=COALESCE(?,retained_until),
                    updated_at=? WHERE resource_id=?""",
                (state, error_code, retained_until, _iso(now), resource_id),
            )
            return _row_to_record(conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone())

    async def force_release(self, resource_id: str, *, now: float) -> dict:
        """系统级释放（无 token）：仅限用户删除优先与 TTL 到期清理路径。

        用户删除始终优先（spec 6.2）：删除传播与 TTL 清理不与执行方
        竞争写同一意图，收敛目标一致（released），幂等重放安全。"""
        return await asyncio.to_thread(self._force_release, resource_id, now)

    def _force_release(self, resource_id, now) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone()
            if row is None:
                raise EvaluationNotFoundError("scenario resource not found")
            if row["state"] == "released":
                return _row_to_record(row)  # 幂等
            if row["state"] != "retained":
                raise EvaluationValidationError(
                    f"force_release 仅适用 retained 记录，当前 {row['state']!r}"
                )
            conn.execute(
                "UPDATE evaluation_scenario_resources SET state='released',"
                "error_code=NULL,owner_token=NULL,lease_until=NULL,updated_at=? "
                "WHERE resource_id=?",
                (_iso(now), resource_id),
            )
            return _row_to_record(conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone())

    async def adopt_expired(self, resource_id: str, *, now: float) -> dict:
        """租约过期的记录换发新 token（恢复方收养；租约未过期拒绝）。"""
        return await asyncio.to_thread(self._adopt_expired, resource_id, now)

    def _adopt_expired(self, resource_id, now) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone()
            if row is None:
                raise EvaluationNotFoundError("scenario resource not found")
            if row["lease_until"] is not None and row["lease_until"] > now:
                raise EvaluationLeaseError(
                    "scenario resource lease still valid"
                )
            token = uuid.uuid4().hex
            conn.execute(
                "UPDATE evaluation_scenario_resources SET owner_token=?,"
                "lease_until=?,updated_at=? WHERE resource_id=?",
                (token, now + self._lease_seconds, _iso(now), resource_id),
            )
            return _row_to_record(conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone())

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def get(self, resource_id: str) -> dict:
        return await asyncio.to_thread(self._get, resource_id)

    def _get(self, resource_id) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone()
            if row is None:
                raise EvaluationNotFoundError("scenario resource not found")
            return _row_to_record(row)

    async def get_by_business_key(self, business_key: str) -> dict | None:
        return await asyncio.to_thread(self._get_by_business_key, business_key)

    def _get_by_business_key(self, business_key) -> dict | None:
        with self._connect() as conn:
            return _row_to_record(conn.execute(
                "SELECT * FROM evaluation_scenario_resources WHERE business_key=?",
                (business_key,),
            ).fetchone())

    async def list_for_execution(self, execution_id: str) -> list[dict]:
        return await asyncio.to_thread(self._list_for_execution, execution_id)

    def _list_for_execution(self, execution_id) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM evaluation_scenario_resources
                    WHERE execution_id=?
                    ORDER BY release_order,created_at,resource_id""",
                (execution_id,),
            ).fetchall()
            return [_row_to_record(row) for row in rows]

    async def list_expired_retention(self, *, now: float) -> list[dict]:
        return await asyncio.to_thread(self._list_expired_retention, now)

    def _list_expired_retention(self, now) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM evaluation_scenario_resources
                    WHERE state='retained' AND retained_until<=?
                    ORDER BY retained_until,resource_id""",
                (now,),
            ).fetchall()
            return [_row_to_record(row) for row in rows]

    async def list_stalled_executions(self, *, now: float) -> list[str]:
        """遗留未收敛账目的执行单（恢复调度查询，D111）。

        只含存在租约已过期的 intent/owned/unknown 记录的 execution_id
        （去重、升序）；活跃租约与已终态（released/retained/
        cleanup_failed）记录不出现——收养语义由 recover_interrupted
        的租约门另行把关，此处只缩小扫描面。"""
        return await asyncio.to_thread(self._list_stalled_executions, now)

    def _list_stalled_executions(self, now) -> list[str]:
        with self._connect() as conn:
            has_execution_store = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evaluation_executions'"
            ).fetchone() is not None
            active_guard = "" if not has_execution_store else """
                AND NOT EXISTS (SELECT 1 FROM evaluation_executions e
                    WHERE e.execution_id=r.execution_id AND e.phase IN ('running','scoring')
                      AND e.lease_until>?)
            """
            rows = conn.execute(
                """SELECT DISTINCT r.execution_id FROM evaluation_scenario_resources r
                    WHERE r.state IN ('intent','owned','unknown')
                      AND r.lease_until IS NOT NULL AND r.lease_until<=?
                """ + active_guard + " ORDER BY r.execution_id",
                (now, now) if has_execution_store else (now,),
            ).fetchall()
            return [row["execution_id"] for row in rows]

    # ------------------------------------------------------------------
    # 源反向依赖
    # ------------------------------------------------------------------

    async def attach_source_ref(
        self, resource_id: str, *, source_kind: str, source_id: str,
        revision: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._attach_source_ref, resource_id, source_kind, source_id, revision,
        )

    def _attach_source_ref(self, resource_id, source_kind, source_id, revision) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM evaluation_scenario_resources WHERE resource_id=?",
                (resource_id,),
            ).fetchone() is None:
                raise EvaluationNotFoundError("scenario resource not found")
            conn.execute(
                """INSERT OR IGNORE INTO evaluation_scenario_source_refs(
                    source_kind,source_id,source_revision,resource_id)
                    VALUES(?,?,?,?)""",
                (source_kind, source_id, revision, resource_id),
            )

    async def list_resource_ids_by_source(
        self, source_kind: str, source_id: str, *, revision: str | None = None,
    ) -> list[str]:
        return await asyncio.to_thread(
            self._list_by_source, source_kind, source_id, revision,
        )

    def _list_by_source(self, source_kind, source_id, revision) -> list[str]:
        with self._connect() as conn:
            if revision is None:
                rows = conn.execute(
                    """SELECT resource_id FROM evaluation_scenario_source_refs
                        WHERE source_kind=? AND source_id=?""",
                    (source_kind, source_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT resource_id FROM evaluation_scenario_source_refs
                        WHERE source_kind=? AND source_id=? AND source_revision=?""",
                    (source_kind, source_id, revision),
                ).fetchall()
            return [row["resource_id"] for row in rows]
