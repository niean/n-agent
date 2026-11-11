"""SQLite persistence for the Evaluation subdomain (Infrastructure Layer).

T3 of the unified evaluation closed loop: implements the Domain
``EvaluationStore`` async Protocol (``app/domain/evaluation/ports.py``) on
top of a dedicated SQLite database (default wiring target is
``locals/evaluations.db``, kept separate from sessions.db; the path is
constructor-injected and the composition root owns the default).

Schema per spec 6.3 (``.harness/specs/active/spec-261109-design-evaluation.md``):
11 core tables all prefixed ``evaluation_`` (samples / definitions /
experiments / jobs / attempts / results / feedback / deliveries /
sink_configs / producer_state / evidence_targets) plus three store-owned
auxiliary tables:

- ``evaluation_deletion_tombstones``: persistent deletion tombstones required
  by the plan as an T18 prerequisite (tombstones must survive restarts and
  be idempotent under repeated deletion events; deletion-revoked results and
  feedback are excluded from the current-view queries via this table while
  the immutable history rows are retained).
- ``evaluation_deletion_tasks``: persistent external-deletion task store
  (plan T3, consumer T18; minimal insert/list/update scope here, T18
  extends the retry semantics).
- ``evaluation_schema_migrations``: applied-migration log backing
  ``PRAGMA user_version`` (this database file is owned by this store, so it
  legitimately owns the schema version sequence).
- ``evaluation_budget_ledger``: daily budget ledger for the T6 worker
  (spec 13) -- transactional reservation at claim time plus settlement at
  job terminal, one row per UTC date. USD amounts are Decimal strings,
  tokens/calls are integers. Crash-left reservations stay reserved until a
  conservative settlement, so limits can never be under-counted.

T9 delivery-scheduling extensions (documented per plan, all covered by
tests; schema version bumped 1 -> 2 with idempotent ALTER migration):
- ``evaluation_deliveries.payload_json`` + ``retry_count`` columns: the
  delivery worker freezes the sink wire payload (canonical JSON) BEFORE the
  first send and persists it here; the worker-registered payload from
  ``commit_result_with_deliveries`` only leaves ``payload_hash`` as a
  commitment anchor. After freezing, ``payload_json``/``payload_hash`` are
  the authoritative same-ID-same-payload retry anchor across restarts.
  ``retry_count`` is the real network-retry counter (backoff exponent and
  the 24h dead-letter window); ``attempt_count`` counts status transitions
  and is never used for backoff math.
- ``claim_next_delivery`` / ``freeze_delivery_payload`` /
  ``schedule_delivery_retry``: the recoverable delivery queue (claim/lease
  CAS mirrors jobs), frozen-payload registration (idempotent, conflicts
  rejected), and retry scheduling (state-machine-validated retry_wait +
  retry counter).

T10 gauge-snapshot extensions (documented per plan, all covered by tests;
schema version bumped 2 -> 3 with idempotent CREATE INDEX IF NOT EXISTS
migration): read-only window/producer aggregate queries
(``query_window_snapshot`` / ``query_producer_snapshot``) plus three
covering indexes for the periodic gauge refresh
(``idx_results_evaluated_at`` / ``idx_jobs_updated_at`` /
``idx_tombstones_kind_id``) -- the results ledger grows unbounded, so the
refresh path must never full-scan.

T6 worker extensions (documented per plan, all covered by tests):
- ``evaluation_jobs.error_type`` column: terminal ``error`` jobs record the
  worker-level failure class (deadline_exceeded / attempt_cap_exceeded /
  the last call's error_type); per-call detail remains in attempts.
- ``heartbeat_job`` / ``cancel_job`` / ``fail_job``: lease renewal CAS,
  authoritative cancel (clears the lease token so in-flight submissions
  fail closed), and terminal error transition.
- ``append_call_attempt``: one attempt row per actual evaluator call
  (spec 6.4 retries), index taken from ``attempt_count`` inside the same
  ``BEGIN IMMEDIATE`` as the counter bump, so concurrent claimers and
  stale workers can never collide on ``UNIQUE(job_id, attempt_index)``.
  The row created by ``claim_next_job`` is the claim marker (no call
  happened yet); call rows follow it.

Task 4 query extensions (documented per plan, all covered by tests; no
schema change -- read-only layer on the existing ledger):
- ``query_results_page`` gains ``created_from``/``created_to`` (UTC ISO,
  half-open ``[from, to)``), ``definition_version`` (dual-sided via jobs /
  feedback) and ``provenance`` filters, plus a ``snapshot_at`` upper-bound
  parameter; without any new filter it keeps the legacy two-part cursor
  and return shape ``(rows, next_cursor)`` so gate/CLI/service callers are
  untouched (the legacy compat branch claims no snapshot guarantee).
- ``query_results_page_snapshot``: the snapshot-semantics entry returning
  ``(rows, next_cursor, snapshot_at)`` -- the first page takes the query
  UTC instant as an exclusive upper bound encoded into a v2 opaque cursor
  (base64url JSON: filter fingerprint + snapshot + keyset boundary), later
  pages reuse it so interleaved writes never leak into an open pagination;
  filter fingerprints (sha256-16 of canonical filter JSON) reject stale
  cursors after filter changes. Default limit 50, capped at 200.
- ``aggregate_metric_results``: server-side filtered aggregation grouped
  by (definition_id, definition_version, provenance, output_type) with
  numeric min/max/sum/count, boolean pass/count, per-status counts, UTC+8
  daily distribution and categorical observed-value counts; the human
  branch resolves currency by full feedback thread (v8 seven-tuple)
  before time/status filtering -- out-of-window revisions never resurrect
  old values and a forged cross-thread ``supersedes_result_id`` cannot
  hide another thread's latest value (tombstone rules unchanged).
- ``list_annotation_queue``: annotation-worklist rows with state
  (all/pending/annotated) and reason (low_score/disputed/anomalous)
  applied in SQL before keyset paging (created_at DESC, result_id);
  "annotated" means a dashboard-actor full-thread-latest feedback exists
  with ``created_at >= source_result.created_at`` (EXISTS subquery, spec
  4.4 row-level fact; final semantics are assembled by the Application
  layer). Each row carries the target four-tuple, definition identity,
  output_type, typed value, evaluated_at and the latest human feedback
  (revision/created_at/value).

Design decisions (documented per plan):
- Timestamps: UTC ISO-8601 strings everywhere (house convention, see
  ``sqlite_task_registry`` module docstring); converted at the store
  boundary. ISO strings of the same format order lexicographically ==
  chronologically, which the claim ordering relies on.
- IDs: ``uuid4().hex`` generated server-side where the caller does not
  supply one; idempotency is anchored on explicit keys (samples:
  UNIQUE(service, producer_event_id); jobs: idempotency_key; definitions:
  UNIQUE(definition_id, version)), and repeat calls with the same key return
  the original id deterministically.
- Journal mode: routes every connection through
  ``app.infrastructure.sqlite_support.apply_journal_mode`` (DELETE mode).
  WAL is deliberately NOT used: WAL requires mmap of the ``-shm`` index,
  which is unreliable on macOS Docker bind mounts (SIGBUS). This follows the
  same house constraint as all other SQLite adapters.
- Foreign keys: intentionally no cross-table FK constraints. The evaluation
  ledger is append-only and must survive deletion propagation (T18 purges
  evidence bodies while keeping tombstone rows); FK cascades would fight
  that lifecycle.
- Immutability is enforced at the database layer with BEFORE UPDATE triggers
  (definitions, results, attempts), so even raw SQL cannot rewrite history;
  revisions create new rows linked by ``supersedes_result_id`` /
  ``supersedes_id`` / higher ``revision``.
- State transitions never invent store-local rules: job and delivery status
  changes go through ``app.domain.evaluation.state`` transition functions,
  whose ``IllegalEvaluationTransition`` propagates to callers.
- Concurrency: every public method is an ``asyncio.to_thread`` wrapper over
  a sync ``_*_sync`` implementation with an independent ``_connect()``
  (WAL-equivalent pragmas + busy_timeout); claim and commit paths run under
  ``BEGIN IMMEDIATE`` so concurrent workers serialize and the CAS conditions
  decide the winner.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from app.domain.evaluation.errors import (
    EvaluationBudgetExhaustedError,
    EvaluationConflictError,
    EvaluationDeletionConflictError,
    EvaluationLeaseError,
    EvaluationNotFoundError,
    EvaluationStaleSequenceError,
    EvaluationStoreError,
)
from app.domain.evaluation.models import (
    EvaluationValidationError,
    canonical_json_bytes,
)
from app.domain.evaluation.state import (
    EvaluationDeliveryStatus,
    EvaluationJobStatus,
    transition_delivery_status,
    transition_job_status,
)
from app.infrastructure.sqlite_support import apply_journal_mode

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
# 错误契约的权威定义在 Domain（app/domain/evaluation/errors.py，T5 起上移，
# Application 层禁止 import app.infrastructure）；本模块经上方 import 原样
# re-export，保持 T3 既有的导入路径与测试兼容。

__all__ = [
    "SQLiteEvaluationStore",
    "EvaluationStoreError",
    "EvaluationNotFoundError",
    "EvaluationConflictError",
    "EvaluationLeaseError",
    "EvaluationStaleSequenceError",
    "EvaluationBudgetExhaustedError",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 8

# v8 schema 版本行名称（迁移日志）。v8 = definition usage 表（Task 2，
# 经 SCHEMA_SQL 幂等提前建好，不占独立版本行）+ feedback 完整线程唯一
# 索引迁移（本 task）。
_SCHEMA_VERSION_NAME: str = "v8-definition-usage"

# T8 数据回填迁移（spec 4.3 usage 回填时序）：与 schema 版本共用
# evaluation_schema_migrations 账本，但使用独立版本号段（1001 起），
# 避免与后续 schema 版本（v9+）冲突。检查标记、回填与记录标记同事务
# 提交：回填中任一步失败整体回滚，账本无标记，下次启动重试（日志不能
# 充当持久标记）。
_USAGE_BACKFILL_VERSION: int = 1001
_USAGE_BACKFILL_NAME: str = "data-backfill-definition-usage"

# v8 迁移（schema v7 -> v8）：feedback 线程唯一索引升级。
# 旧 uq_feedback_revision_key 是六元组（actor_ref/target_service/
# target_kind/target_subject_id/definition_id/revision），线程身份缺失
# target_subject_revision 与 definition_version 两个维度，导致"同 subject
# 不同 revision / 同 definition 不同 version"的评分互相占用 revision
# 槽位并互相覆盖。v8 将唯一键补全为完整线程七元组 + revision 八列；
# 查询（get_latest_feedback / list_current_feedback / revision 缺省自增）
# 同步按完整线程过滤。
#
# 索引迁移只在本迁移块执行、不放入 SCHEMA_SQL：executescript 先于迁移块
# 运行，旧库遗留的同线程同 revision 冲突行会让 CREATE UNIQUE INDEX 在
# 降级判定之前直接失败。旧库存在冲突行时唯一索引无法建立，降级为同列集
# 非唯一索引并输出结构化 warning（只含冲突计数，不含行内容）；新库无旧
# 索引、无冲突行，恒落唯一索引。
_FEEDBACK_LEGACY_INDEX: str = "uq_feedback_revision_key"
_FEEDBACK_THREAD_UNIQUE_INDEX: str = "uq_feedback_thread_revision_key"
_FEEDBACK_THREAD_NONUNIQUE_INDEX: str = "ix_feedback_thread_revision_key"
_FEEDBACK_THREAD_INDEX_DDL: str = (
    "CREATE {unique} INDEX IF NOT EXISTS {name} "
    "ON evaluation_feedback("
    "actor_ref, target_service, target_kind, target_subject_id, "
    "target_subject_revision, definition_id, definition_version, revision)"
)

# v6 迁移（schema v6 -> v7，幂等）：增列 ——
# - evaluation_results.evaluation_trace_id / evaluation_span_id：worker
#   提交 result 时冻结的 evaluation.run Trace 身份（hex，OTel 禁用/
#   作用域外为 NULL）。OTel ScoreSink 投影的 evaluation_trace 字段从本
#   列读取（24 项三方审阅 #12：此前投影用目标业务 trace 身份冒充评估
#   Trace，业务身份与评估身份混淆；evaluation.run 与业务 Trace 经 Link
#   关联，是两条 Trace）。旧行为 NULL（投影为空评估身份，语义诚实）。
# - evaluation_samples.cohort：抽样 cohort（random/failure，非捕获路径
#   为空串）。窗口快照按 cohort 分组（24 项三方审阅 #24：故障定向
#   cohort 的偏置分布不得混入总体质量统计，spec 7.1/AC17 cohort 分离）。
#   旧行为空串（Gauge 投影 other 系列）。
_V7_MIGRATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "evaluation_results",
        "evaluation_trace_id",
        "ALTER TABLE evaluation_results "
        "ADD COLUMN evaluation_trace_id TEXT",
    ),
    (
        "evaluation_results",
        "evaluation_span_id",
        "ALTER TABLE evaluation_results "
        "ADD COLUMN evaluation_span_id TEXT",
    ),
    (
        "evaluation_samples",
        "cohort",
        "ALTER TABLE evaluation_samples "
        "ADD COLUMN cohort TEXT NOT NULL DEFAULT ''",
    ),
)

# T18 迁移（schema v4 -> v5，幂等）：evaluation_deletion_tasks 增列 + 级联
# 查询覆盖索引 ——
# - result_id：外部删除任务回链被删 result 行（删除任务从 delivery 冻结
#   的 external_id 登记时写入）；审计与任务溯源所需，旧行为 NULL
# - idx_jobs_sample_id（Mi2）：删除级联三查询与窗口快照 error 分支按
#   jobs.sample_id 关联样本的覆盖索引
_V5_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "result_id",
        "ALTER TABLE evaluation_deletion_tasks ADD COLUMN result_id TEXT",
    ),
)

_V5_MIGRATION_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_jobs_sample_id",
        "CREATE INDEX IF NOT EXISTS idx_jobs_sample_id "
        "ON evaluation_jobs(sample_id)",
    ),
)

# T8 迁移（schema v3 -> v4，幂等）：evaluation_results 重建 ——
# - provenance CHECK 扩展 ('calibration', 'self_judge')（T8 provenance
#   语义：校准 advisory 与 self_judge 结果行；语义见
#   app/application/evaluation_calibration.py 模块 docstring）；
# - 增列 explanation（Judge 解释正文，worker 提交时按 spec 13 限长
#   2000 UTF-8 字节；Langfuse 投影经 T9 comment 路径自动消费）。
# SQLite 不支持 ALTER CHECK：CHECK 变更必须重建表（新表 -> 拷贝 -> 换名
# -> 重建索引/触发器），在 BEGIN IMMEDIATE 内串行执行；新库由 SCHEMA_SQL
# 直接建全（经 sqlite_master SQL 探测跳过重建）。
_EVALUATION_RESULTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS evaluation_results (
    result_id TEXT PRIMARY KEY,
    job_id TEXT,
    feedback_id TEXT,
    provenance TEXT NOT NULL CHECK (provenance IN
        ('automatic', 'human', 'calibration', 'self_judge')),
    metric_name TEXT NOT NULL,
    metric_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    skip_reason TEXT,
    typed_value_json TEXT,
    passed INTEGER,
    explanation TEXT,
    explanation_ref TEXT,
    evidence_hash TEXT NOT NULL DEFAULT '',
    evaluation_trace_id TEXT,
    evaluation_span_id TEXT,
    evaluated_at TEXT NOT NULL,
    supersedes_result_id TEXT,
    created_at TEXT NOT NULL,
    CHECK (
        (provenance IN ('automatic', 'calibration', 'self_judge')
            AND job_id IS NOT NULL AND feedback_id IS NULL)
        OR (provenance = 'human'
            AND job_id IS NULL AND feedback_id IS NOT NULL)
    )
);
"""

# results 表索引与 append-only 触发器：SCHEMA_SQL 与 v3->v4 表重建共用
# （重建 DROP TABLE 会连带删除索引/触发器，重建后必须逐条重立）。
_EVALUATION_RESULTS_INDEX_DDL: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_results_job_metric "
    "ON evaluation_results(job_id, metric_name) WHERE job_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_results_feedback "
    "ON evaluation_results(feedback_id) WHERE feedback_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_results_metric "
    "ON evaluation_results(metric_name)",
    "CREATE INDEX IF NOT EXISTS idx_results_evaluated_at "
    "ON evaluation_results(evaluated_at)",
    "CREATE INDEX IF NOT EXISTS idx_results_supersedes "
    "ON evaluation_results(supersedes_result_id)",
    """CREATE TRIGGER IF NOT EXISTS trg_evaluation_results_no_update
BEFORE UPDATE ON evaluation_results
BEGIN
    SELECT RAISE(ABORT, 'evaluation_results is immutable (revisions create new results)');
END""",
)

# T10 窗口快照的目标 trace 身份 GLOB 模式（32 位 / 16 位小写 hex）；
# 与 EvaluationTarget 的 hex 校验同一判定，用于 unlinked 计数。
_TRACE_ID_GLOB: str = "[0-9a-f]" * 32
_SPAN_ID_GLOB: str = "[0-9a-f]" * 16

# T10 迁移（schema v2 -> v3，幂等）：Gauge 窗口快照查询的覆盖索引。
# results 账本无界增长且 query_window_snapshot 是周期性 Gauge 刷新的
# 数据源——缺索引时每次刷新都是全表扫描；anti-join 的 tombstones 同理。
# CREATE INDEX IF NOT EXISTS 本身幂等，仍纳入迁移日志与版本推进，保持
# 与 T9 v1->v2 同一迁移模式（并发首启在 BEGIN IMMEDIATE 内串行化）。
_V3_MIGRATION_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_results_evaluated_at",
        "CREATE INDEX IF NOT EXISTS idx_results_evaluated_at "
        "ON evaluation_results(evaluated_at)",
    ),
    (
        "idx_jobs_updated_at",
        "CREATE INDEX IF NOT EXISTS idx_jobs_updated_at "
        "ON evaluation_jobs(updated_at)",
    ),
    (
        "idx_tombstones_kind_id",
        "CREATE INDEX IF NOT EXISTS idx_tombstones_kind_id "
        "ON evaluation_deletion_tombstones(subject_kind, subject_id)",
    ),
)

# T9 迁移：evaluation_deliveries 增列（v1 库经 ALTER 幂等升级；新库直接
# 由 SCHEMA_SQL 建全）。
# - payload_json：投递调度器在首次发送前冻结的 sink wire payload（canonical
#   JSON 文本）。commit 时登记的 worker payload 仅留 payload_hash 作承诺
#   锚点；冻结后的 payload_json/payload_hash 是重试与重启恢复的权威依据
#   （同 ID 同 payload，绝不重新评分）。
# - retry_count：真实网络重试计数（退避指数与 24h 窗口的依据）。
#   attempt_count 是状态迁移次数，不用于退避计算。
_V2_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("payload_json", "ALTER TABLE evaluation_deliveries ADD COLUMN payload_json TEXT"),
    (
        "retry_count",
        "ALTER TABLE evaluation_deliveries ADD COLUMN retry_count "
        "INTEGER NOT NULL DEFAULT 0",
    ),
)

SCHEMA_SQL_HEAD = """
CREATE TABLE IF NOT EXISTS evaluation_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_samples (
    sample_id TEXT PRIMARY KEY,
    producer_event_id TEXT NOT NULL,
    service TEXT NOT NULL,
    target_json TEXT NOT NULL DEFAULT '{}',
    snapshot_ref TEXT NOT NULL DEFAULT '',
    snapshot_hash TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL,
    sample_probability REAL NOT NULL DEFAULT 1.0,
    sampling_policy_version TEXT NOT NULL DEFAULT '',
    cohort TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_samples_service_event
    ON evaluation_samples(service, producer_event_id);

CREATE TABLE IF NOT EXISTS evaluation_definitions (
    definition_id TEXT NOT NULL,
    version TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    metric_name TEXT NOT NULL DEFAULT '',
    target_kind TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (definition_id, version)
);
CREATE TRIGGER IF NOT EXISTS trg_evaluation_definitions_no_update
BEFORE UPDATE ON evaluation_definitions
BEGIN
    SELECT RAISE(ABORT, 'evaluation_definitions is immutable');
END;

CREATE TABLE IF NOT EXISTS evaluation_experiments (
    experiment_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    dataset_hash TEXT NOT NULL,
    target_config_hash TEXT NOT NULL,
    baseline_id TEXT,
    evaluator_set_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'execute',
    status TEXT NOT NULL,
    gate_report_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_jobs (
    job_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    experiment_id TEXT,
    replicate INTEGER NOT NULL DEFAULT 0,
    requested_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    idempotency_key TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expiry TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    budget_reserved_usd TEXT,
    error_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_idempotency
    ON evaluation_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON evaluation_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON evaluation_jobs(updated_at);
-- T18/Mi2：删除级联查询（list_result_ids_for_samples /
-- list_deliveries_for_samples / unlink_deliveries_for_samples）与窗口
-- 快照 error 分支经 jobs.sample_id 关联样本
CREATE INDEX IF NOT EXISTS idx_jobs_sample_id
    ON evaluation_jobs(sample_id);

CREATE TABLE IF NOT EXISTS evaluation_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL,
    judge_request_json TEXT NOT NULL DEFAULT '{}',
    judge_model TEXT,
    usage_known INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd TEXT,
    pricing_version TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error_type TEXT,
    output_ref TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (job_id, attempt_index)
);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON evaluation_attempts(job_id);
CREATE TRIGGER IF NOT EXISTS trg_evaluation_attempts_no_update
BEFORE UPDATE ON evaluation_attempts
BEGIN
    SELECT RAISE(ABORT, 'evaluation_attempts is append-only');
END;

"""

# results 表与其索引/触发器由 _EVALUATION_RESULTS_TABLE_SQL /
# _EVALUATION_RESULTS_INDEX_DDL 常量装配（SCHEMA_SQL 与 v3->v4 表重建
# 共用同一 DDL，避免双处维护漂移）。

SCHEMA_SQL_TAIL = """CREATE TABLE IF NOT EXISTS evaluation_feedback (
    feedback_id TEXT PRIMARY KEY,
    actor_ref TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    definition_version TEXT NOT NULL DEFAULT '',
    target_service TEXT NOT NULL DEFAULT '',
    target_kind TEXT NOT NULL DEFAULT '',
    target_subject_id TEXT NOT NULL DEFAULT '',
    target_subject_revision TEXT NOT NULL DEFAULT '',
    target_json TEXT NOT NULL DEFAULT '{}',
    value_json TEXT NOT NULL DEFAULT '{}',
    comment_ref TEXT,
    revision INTEGER NOT NULL,
    supersedes_id TEXT,
    created_at TEXT NOT NULL
);
-- feedback 线程唯一索引（v8：uq_feedback_thread_revision_key，完整线程
-- 七元组 + revision）不在 SCHEMA_SQL 内定义：索引建立统一由 v8 迁移块
-- 执行（旧库遗留冲突行需要降级判定，executescript 先于迁移块运行会让
-- CREATE UNIQUE INDEX 提前失败）。新库经迁移块恒落唯一索引。

CREATE TABLE IF NOT EXISTS evaluation_deliveries (
    delivery_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL,
    sink TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    external_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error_type TEXT,
    payload_hash TEXT NOT NULL DEFAULT '',
    payload_json TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expiry TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (result_id, sink, projection_version)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_result
    ON evaluation_deliveries(result_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_status
    ON evaluation_deliveries(status, next_retry_at);

CREATE TABLE IF NOT EXISTS evaluation_sink_configs (
    sink TEXT NOT NULL,
    project TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    external_score_config_id TEXT NOT NULL,
    schema_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (sink, project, definition_version)
);

CREATE TABLE IF NOT EXISTS evaluation_producer_state (
    producer_id TEXT NOT NULL,
    counter_epoch TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    captured_count INTEGER NOT NULL DEFAULT 0,
    capture_error_count INTEGER NOT NULL DEFAULT 0,
    queue_full_count INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (producer_id, counter_epoch)
);

CREATE TABLE IF NOT EXISTS evaluation_evidence_targets (
    sample_id TEXT NOT NULL,
    source_service TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_revision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (sample_id, source_service, subject_kind, subject_id, subject_revision)
);
CREATE INDEX IF NOT EXISTS idx_evidence_targets_subject
    ON evaluation_evidence_targets(
        source_service, subject_kind, subject_id, subject_revision
    );

CREATE TABLE IF NOT EXISTS evaluation_deletion_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_revision TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (subject_kind, subject_id, subject_revision)
);
CREATE INDEX IF NOT EXISTS idx_tombstones_kind_id
    ON evaluation_deletion_tombstones(subject_kind, subject_id);

-- Persistent external-deletion task store (plan T3, consumer T18).
-- Minimal scope here: insert + list-by-status + status/attempt update on a
-- single mutable row (no retry/backoff history); T18 extends semantics.
CREATE TABLE IF NOT EXISTS evaluation_deletion_tasks (
    task_id TEXT PRIMARY KEY,
    sink TEXT NOT NULL,
    external_id TEXT,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_revision TEXT NOT NULL DEFAULT '',
    result_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'deletion_pending', 'done')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (sink, subject_kind, subject_id, subject_revision, external_id)
);
CREATE INDEX IF NOT EXISTS idx_deletion_tasks_status
    ON evaluation_deletion_tasks(status, next_retry_at);

-- Daily budget ledger (plan T6 supplement, spec 13): transactional
-- reservation at claim time + settlement at job terminal. One row per UTC
-- date; USD amounts are Decimal strings (house convention), tokens/calls
-- are integers. Reserved-but-unsettled amounts survive crashes (conservative
-- over-count is safe; limits can only be enforced from this ledger, never
-- from post-hoc fee accumulation).
CREATE TABLE IF NOT EXISTS evaluation_budget_ledger (
    date_key TEXT PRIMARY KEY,
    reserved_usd TEXT NOT NULL DEFAULT '0',
    settled_usd TEXT NOT NULL DEFAULT '0',
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    settled_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_calls INTEGER NOT NULL DEFAULT 0,
    settled_calls INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Judge calibration reports (plan T8, spec 10): one row per calibration
-- cache key (metric + definition behavior hash + both dataset hashes).
-- status is the gate-facing verdict (passed/advisory); report_json holds
-- the full consistency + repeatability report. Recalibration of the same
-- key overwrites (latest wins); a definition/dataset hash change produces
-- a new key, so prior reports stay untouched (audit trail).
CREATE TABLE IF NOT EXISTS evaluation_calibrations (
    cache_key TEXT PRIMARY KEY,
    metric_name TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'advisory')),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calibrations_definition
    ON evaluation_calibrations(definition_id, definition_hash, created_at);

-- Reviewed-dataset export audit (plan T21, spec 15): one row per
-- controlled export. Records the approval source (actor + reviewed
-- result ids) so exported manifests are traceable to human review;
-- source_result_ids live only here, never in the exported manifest
-- (原始身份不外带). Additive table: old DBs gain it via SCHEMA_SQL
-- (CREATE TABLE IF NOT EXISTS), no data migration needed.
CREATE TABLE IF NOT EXISTS evaluation_export_audit (
    audit_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    dataset_hash TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    source_result_ids_json TEXT NOT NULL,
    case_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_export_audit_dataset
    ON evaluation_export_audit(dataset_id, dataset_version, created_at);

-- 本地 producer 捕获计数持久化（#28）：单行（id=1）保存 n-agent 本地
-- producer 的累计计数与 counter epoch，重启恢复不归零。Additive table:
-- old DBs gain it via SCHEMA_SQL (CREATE TABLE IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS evaluation_capture_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    counter_epoch TEXT NOT NULL,
    values_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- Definition usage（definition 线上启用登记）：每定义版本一行，
-- online_enabled 由 CAS（revision 乐观锁）切换，注册路径默认停用
-- （save_definition_with_usage 落 online_enabled=0；显式 upsert 才能启用，
-- 幂等重注册不复活停用状态）。FK 声明与 evaluation_definitions 主键
-- 对齐；本库 foreign_keys 关闭（账本语义），存在性校验由写路径
-- SELECT definitions 先查兜底。Additive table: old DBs gain it via
-- SCHEMA_SQL (CREATE TABLE IF NOT EXISTS), no data migration needed;
-- schema 版本行记 v8（与 feedback 完整线程索引迁移同事务提交）。
CREATE TABLE IF NOT EXISTS evaluation_definition_usage (
    definition_id TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    online_enabled INTEGER NOT NULL CHECK (online_enabled IN (0,1)),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (definition_id, definition_version),
    FOREIGN KEY (definition_id, definition_version) REFERENCES evaluation_definitions(definition_id, version)
);
"""


# v3->v4 重建后重立的 results 索引/触发器语句（分条执行，不在迁移事务内
# 使用 executescript——后者会隐式提交、破坏迁移原子性）。
_SCHEMA_SQL_RESULTS_SECTION = (
    "\n"
    + _EVALUATION_RESULTS_TABLE_SQL
    + "\n"
    + "".join(ddl + ";\n" for ddl in _EVALUATION_RESULTS_INDEX_DDL)
)

SCHEMA_SQL = SCHEMA_SQL_HEAD + _SCHEMA_SQL_RESULTS_SECTION + SCHEMA_SQL_TAIL


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    """UTC ISO-8601 string; the single timestamp convention of this store."""
    if dt is None:
        dt = _utcnow()
    return dt.astimezone(timezone.utc).isoformat()


def _hash_mapping(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the domain canonical JSON encoding (shared anchor)."""
    return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(text: str | None, default: Any) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ---------------------------------------------------------------------------
# Task 4: 结果分页游标 v2（过滤指纹 + 快照上界 + keyset 边界）与
# 聚合/标注队列查询共用常量
# ---------------------------------------------------------------------------

# 单页上限（默认 50 不变，超过 200 封顶，防止单页超载）
_RESULTS_PAGE_MAX_LIMIT: int = 200

# 进入 fingerprint 的过滤参数集合（规范化过滤 dict 的 key 集；
# snapshot_at 是快照上界不是过滤条件，单独编码进 token 的 snap 字段）
_RESULTS_FILTER_KEYS: tuple[str, ...] = (
    "target_service",
    "target_kind",
    "target_subject_id",
    "target_subject_revision",
    "definition_id",
    "definition_version",
    "experiment_id",
    "status",
    "provenance",
    "created_from",
    "created_to",
)

# 标注队列封闭集（复用 evaluation_review 的语义词汇）
_ANNOTATION_STATES: tuple[str, ...] = ("all", "pending", "annotated")
_ANNOTATION_REASONS: tuple[str, ...] = ("low_score", "disputed", "anomalous")
_ANNOTATION_DEFAULT_THRESHOLD: float = 0.5

# "已标注"判定的可信标注 actor（spec 4.4：dashboard 标注线程）
_ANNOTATION_DASHBOARD_ACTOR: str = "dashboard"


def _feedback_thread_latest_predicate(alias: str) -> str:
    """feedback 完整线程（v8 七元组）latest 判定谓词。

    同线程存在更高 revision（或同 revision 更晚 created_at/feedback_id）
    的行时，``{alias}`` 行不是 latest；与 Task 1 dedupe_latest 口径一致。
    """
    return (
        f"NOT EXISTS (SELECT 1 FROM evaluation_feedback g "
        f"WHERE g.actor_ref = {alias}.actor_ref "
        f"AND g.target_service = {alias}.target_service "
        f"AND g.target_kind = {alias}.target_kind "
        f"AND g.target_subject_id = {alias}.target_subject_id "
        f"AND g.target_subject_revision = {alias}.target_subject_revision "
        f"AND g.definition_id = {alias}.definition_id "
        f"AND g.definition_version = {alias}.definition_version "
        f"AND (g.revision > {alias}.revision "
        f"OR (g.revision = {alias}.revision "
        f"AND (g.created_at > {alias}.created_at "
        f"OR (g.created_at = {alias}.created_at "
        f"AND g.feedback_id > {alias}.feedback_id)))))"
    )


_FEEDBACK_THREAD_LATEST_PREDICATE = _feedback_thread_latest_predicate("f")

# 标注队列行级事实：source 结果 (target 四元组, definition_id+version) 上
# 最新一条人评（任意 actor，按 created_at/feedback_id 择新；tombstone 排除）
_LATEST_FEEDBACK_JSON_SUBQUERY = (
    "SELECT json_object('feedback_id', lf.feedback_id, "
    "'actor_ref', lf.actor_ref, 'revision', lf.revision, "
    "'created_at', lf.created_at, 'value', json(lf.value_json)) "
    "FROM evaluation_feedback lf "
    "WHERE lf.target_service = c.target_service "
    "AND lf.target_kind = c.target_kind "
    "AND lf.target_subject_id = c.target_subject_id "
    "AND lf.target_subject_revision = c.target_subject_revision "
    "AND lf.definition_id = c.definition_id "
    "AND lf.definition_version = c.definition_version "
    "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones lt "
    "  WHERE lt.subject_kind = 'feedback' "
    "  AND lt.subject_id = lf.feedback_id) "
    "ORDER BY lf.created_at DESC, lf.feedback_id DESC LIMIT 1"
)


def _results_filter_fingerprint(**filters: Any) -> str:
    """规范化过滤 dict（key 排序 canonical JSON）的 sha256 前 16 hex。"""
    payload = json.dumps(
        filters, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _encode_result_cursor_v2(
    fingerprint: str, snapshot_at: str | None,
    created_at: str, result_id: str,
) -> str:
    """编码 v2 不透明游标：base64 urlsafe(JSON(v/fp/snap/cur))。"""
    payload = {
        "v": 2,
        "fp": fingerprint,
        "snap": snapshot_at,
        "cur": [created_at, result_id],
    }
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _parse_result_cursor(cursor: str) -> dict[str, Any]:
    """解析 results 游标，返回 legacy / v2 两类结构化描述。

    旧两段式 ``"{created_at}|{result_id}"`` 保留兼容（仅原结果接口且
    无新增过滤的分支接受）；其余一律按 v2 token 解析，任何结构不合均抛
    EvaluationValidationError。
    """
    if "|" in cursor:
        created_at, result_id = cursor.split("|", 1)
        return {
            "kind": "legacy",
            "created_at": created_at,
            "result_id": result_id,
        }
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        if not isinstance(raw, dict) or raw.get("v") != 2:
            raise ValueError("unsupported cursor version")
        fingerprint = raw["fp"]
        snapshot_at = raw["snap"]
        boundary = raw["cur"]
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("fingerprint missing")
        if snapshot_at is not None and not isinstance(snapshot_at, str):
            raise ValueError("snapshot_at must be a string or null")
        if (
            not isinstance(boundary, list)
            or len(boundary) != 2
            or not all(isinstance(item, str) for item in boundary)
        ):
            raise ValueError("boundary must be [created_at, result_id]")
        return {
            "kind": "v2",
            "fp": fingerprint,
            "snap": snapshot_at,
            "created_at": boundary[0],
            "result_id": boundary[1],
        }
    except Exception as exc:
        raise EvaluationValidationError(
            f"非法 results 游标: {cursor!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SQLiteEvaluationStore:
    """SQLite implementation of the evaluation ledger (spec 6.3).

    Owns a dedicated database file (independent of sessions.db). All public
    methods are async and delegate to sync implementations via
    ``asyncio.to_thread``; each call opens an independent connection with
    DELETE journal mode, foreign_keys off (append-only ledger survives
    deletion propagation), and a 5000ms busy_timeout.
    """

    BUSY_TIMEOUT_MS = 5000
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        # 降级索引诊断 once 标记：首次 list_current_feedback 探测后不再
        # 重复执行 sqlite_master 探测与全表 GROUP BY 计数
        self._feedback_degraded_diag_checked = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=self.BUSY_TIMEOUT_MS / 1000.0
        )
        conn.row_factory = sqlite3.Row
        apply_journal_mode(conn)
        conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
        return conn

    def _ensure_schema(self) -> None:
        """Idempotent schema creation + migration log.

        Safe for empty DBs, repeated startup, and concurrent startups:
        the v1->v2 column check + ALTERs and the version check + insert all
        run inside one ``BEGIN IMMEDIATE`` so concurrent starters serialize
        before the column check (an ALTER outside the transaction would let
        two starters both see a missing column and race on "duplicate
        column name"), and a PK IntegrityError on the version row (a
        concurrently won race that slipped through) is mapped to idempotent
        success. ``PRAGMA user_version`` is owned by this store because this
        database file has exactly one schema owner.
        """
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("BEGIN IMMEDIATE")
            try:
                # v1 -> v2 幂等迁移：旧库缺列时 ALTER（新库由 SCHEMA_SQL
                # 建全）；列检查与 ALTER 必须在事务内，并发首启串行化
                existing_columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(evaluation_deliveries)"
                    ).fetchall()
                }
                for column, ddl in _V2_MIGRATION_COLUMNS:
                    if column not in existing_columns:
                        conn.execute(ddl)
                # v2 -> v3 幂等迁移：T10 Gauge 窗口快照覆盖索引
                #（CREATE INDEX IF NOT EXISTS 幂等；新库由 SCHEMA_SQL 建全）
                for _name, ddl in _V3_MIGRATION_INDEXES:
                    conn.execute(ddl)
                # v3 -> v4 幂等迁移（T8）：results 表重建（provenance
                # CHECK 扩展 + explanation 列）；新库由 SCHEMA_SQL 建全，
                # 经 sqlite_master SQL 探测跳过
                self._migrate_results_table_v4(conn)
                # v4 -> v5 幂等迁移（T18）：deletion_tasks 增 result_id
                # 关联列 + 级联查询覆盖索引（列检查与 DDL 必须在事务内，
                # 并发首启串行化）
                v5_columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(evaluation_deletion_tasks)"
                    ).fetchall()
                }
                for column, ddl in _V5_MIGRATION_COLUMNS:
                    if column not in v5_columns:
                        conn.execute(ddl)
                for _name, ddl in _V5_MIGRATION_INDEXES:
                    conn.execute(ddl)
                # v6 -> v7 幂等迁移：results 增 evaluation_trace_id/
                # evaluation_span_id、samples 增 cohort（列检查与 DDL
                # 必须在事务内，并发首启串行化；v3 旧库经 v4 重建后亦落
                # 在本检查内）
                v7_tables: dict[str, set[str]] = {}
                for table, column, ddl in _V7_MIGRATION_COLUMNS:
                    if table not in v7_tables:
                        v7_tables[table] = {
                            row["name"]
                            for row in conn.execute(
                                f"PRAGMA table_info({table})"
                            ).fetchall()
                        }
                    if column not in v7_tables[table]:
                        conn.execute(ddl)
                # v7 -> v8 幂等迁移：feedback 线程唯一索引升级（完整线程
                # 七元组 + revision）。DROP 旧索引与建立新索引必须在事务
                # 内，并发首启串行化；旧库遗留冲突行降级为非唯一索引
                # （详见 _migrate_feedback_thread_index）。
                self._migrate_feedback_thread_index(conn)
                applied = conn.execute(
                    "SELECT version FROM evaluation_schema_migrations "
                    "WHERE version = ?",
                    (SCHEMA_VERSION,),
                ).fetchone()
                if applied is None:
                    try:
                        conn.execute(
                            "INSERT INTO evaluation_schema_migrations "
                            "(version, name, applied_at) VALUES (?, ?, ?)",
                            (SCHEMA_VERSION, _SCHEMA_VERSION_NAME, _iso()),
                        )
                    except sqlite3.IntegrityError:
                        # Lost a concurrent-startup race: the other starter
                        # recorded the same version row. Idempotent success.
                        pass
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _migrate_results_table_v4(conn: sqlite3.Connection) -> None:
        """v3 -> v4（T8，幂等）：evaluation_results 重建。

        provenance CHECK 扩展与新增列无法 ALTER（SQLite 不支持修改
        CHECK），按"新表 -> 拷贝 -> 换名 -> 重立索引/触发器"重建，全程
        在调用方的 BEGIN IMMEDIATE 事务内。新库（SCHEMA_SQL 已建全）经
        sqlite_master SQL 探测跳过；探测条件为建表 SQL 未含新 provenance
        值 'calibration'。

        运维约束（部署顺序，显式文档化）：表重建持整个评估账本的写事务
        （DELETE journal，无 WAL 读写并发），重建期间阻塞其他连接的全部
        读写——迁移必须先于 worker 启动执行，或在旧进程全部排空后执行；
        若并发 worker commit 撞上 busy_timeout（5000ms）失败，恢复语义
        = job 重试 + lease 过期重领（账本无半提交状态，重建原子）。"""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'evaluation_results'"
        ).fetchone()
        if row is None or "'calibration'" in (row["sql"] or ""):
            return
        create_v4 = _EVALUATION_RESULTS_TABLE_SQL.replace(
            "CREATE TABLE IF NOT EXISTS evaluation_results",
            "CREATE TABLE evaluation_results_v4",
        )
        columns = (
            "result_id, job_id, feedback_id, provenance, metric_name, "
            "metric_version, status, skip_reason, typed_value_json, passed, "
            "explanation_ref, evidence_hash, evaluated_at, "
            "supersedes_result_id, created_at"
        )
        conn.execute(create_v4)
        conn.execute(
            f"INSERT INTO evaluation_results_v4 ({columns}) "
            f"SELECT {columns} FROM evaluation_results"
        )
        conn.execute("DROP TABLE evaluation_results")
        conn.execute(
            "ALTER TABLE evaluation_results_v4 RENAME TO evaluation_results"
        )
        # DROP TABLE 连带删除索引与触发器：逐条重立（分条执行，不用
        # executescript——其隐式提交会破坏迁移事务原子性）
        for ddl in _EVALUATION_RESULTS_INDEX_DDL:
            conn.execute(ddl)

    @staticmethod
    def _migrate_feedback_thread_index(conn: sqlite3.Connection) -> None:
        """v7 -> v8（幂等）：feedback 线程唯一索引升级。

        任一目标索引（唯一或降级非唯一）已存在时跳过（迁移已完成，且
        DROP 旧索引属于一次性升级步骤）。否则：DROP 旧六元组索引 -> 尝试
        建立完整线程八列唯一索引；旧库遗留同线程同 revision 冲突行时
        IntegrityError 降级为同列集非唯一索引，并输出一条只含冲突计数的
        结构化 warning（不含 feedback 行内容）。全程在调用方的
        BEGIN IMMEDIATE 事务内。"""
        index_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'evaluation_feedback'"
            ).fetchall()
        }
        if (
            _FEEDBACK_THREAD_UNIQUE_INDEX in index_names
            or _FEEDBACK_THREAD_NONUNIQUE_INDEX in index_names
        ):
            return
        conn.execute(f"DROP INDEX IF EXISTS {_FEEDBACK_LEGACY_INDEX}")
        try:
            conn.execute(
                _FEEDBACK_THREAD_INDEX_DDL.format(
                    unique="UNIQUE", name=_FEEDBACK_THREAD_UNIQUE_INDEX
                )
            )
            return
        except sqlite3.IntegrityError:
            # 旧库遗留冲突行：唯一索引无法建立，降级为非唯一索引。
            conflict_rows = SQLiteEvaluationStore._feedback_thread_duplicate_rows(
                conn
            )
            conn.execute(
                _FEEDBACK_THREAD_INDEX_DDL.format(
                    unique="", name=_FEEDBACK_THREAD_NONUNIQUE_INDEX
                )
            )
            logger.warning(
                "feedback 线程唯一索引迁移降级：%s 因历史同线程同 revision "
                "冲突（duplicate_rows=%d）无法建立，改建同列集非唯一索引 "
                "%s；current 视图按 created_at/feedback_id 决定性择一",
                _FEEDBACK_THREAD_UNIQUE_INDEX,
                conflict_rows,
                _FEEDBACK_THREAD_NONUNIQUE_INDEX,
            )

    @staticmethod
    def _feedback_thread_duplicate_rows(conn: sqlite3.Connection) -> int:
        """完整线程 + revision 分组下的重复行计数（每组保留 1 行之外的多余
        行数）。诊断用受限计数，不含任何 feedback 行内容。"""
        return conn.execute(
            "SELECT COALESCE(SUM(n - 1), 0) FROM ("
            "  SELECT COUNT(*) AS n FROM evaluation_feedback "
            "  GROUP BY actor_ref, target_service, target_kind, "
            "  target_subject_id, target_subject_revision, definition_id, "
            "  definition_version, revision HAVING COUNT(*) > 1)"
        ).fetchone()[0]

    @staticmethod
    def _feedback_degraded_index_present(conn: sqlite3.Connection) -> bool:
        """降级非唯一线程索引是否存在于当前库（v8 迁移降级分支的遗留状态）。"""
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (_FEEDBACK_THREAD_NONUNIQUE_INDEX,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Samples + evidence_targets reverse index
    # ------------------------------------------------------------------

    async def save_sample(self, sample: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._save_sample_sync, sample)

    def _save_sample_sync(self, sample: Mapping[str, Any]) -> str:
        service = sample.get("service")
        producer_event_id = sample.get("producer_event_id")
        if not service or not producer_event_id:
            raise ValueError("sample 需要 service 与 producer_event_id")
        payload_hash = _hash_mapping(sample)
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # T18 删除传播门禁：迟到样本（含新 retrieval_id 携带已删文档
            # 版本）命中 tombstone 时拒绝写入，且先于幂等重放判定——已删
            # 内容不得以"同 producer_event_id 同内容"重放复活
            if self._sample_blocked_by_tombstone(conn, sample):
                conn.rollback()
                raise EvaluationDeletionConflictError(
                    "sample 命中删除 tombstone（subject 已删除，拒绝迟到"
                    f"写入）: service={service!r} "
                    f"producer_event_id={producer_event_id!r}"
                )
            row = conn.execute(
                "SELECT sample_id, payload_hash FROM evaluation_samples "
                "WHERE service = ? AND producer_event_id = ?",
                (service, producer_event_id),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] == payload_hash:
                    conn.commit()
                    return row["sample_id"]
                conn.rollback()
                raise EvaluationConflictError(
                    f"producer_event_id 冲突: service={service!r} "
                    f"producer_event_id={producer_event_id!r} 内容 hash 不一致"
                )
            sample_id = sample.get("sample_id") or uuid4().hex
            conn.execute(
                "INSERT INTO evaluation_samples (sample_id, producer_event_id, "
                "service, target_json, snapshot_ref, snapshot_hash, "
                "schema_version, captured_at, sample_probability, "
                "sampling_policy_version, cohort, payload_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample_id,
                    producer_event_id,
                    service,
                    _json_dumps(sample.get("target") or {}),
                    sample.get("snapshot_ref") or "",
                    sample.get("snapshot_hash") or "",
                    sample.get("schema_version") or "",
                    sample.get("captured_at") or now,
                    float(sample.get("sample_probability", 1.0)),
                    sample.get("sampling_policy_version") or "",
                    str(sample.get("cohort") or ""),
                    payload_hash,
                    now,
                ),
            )
            for target in sample.get("evidence_targets") or ():
                try:
                    source_service = target["source_service"]
                    subject_kind = target["subject_kind"]
                    subject_id = target["subject_id"]
                except (KeyError, TypeError) as exc:
                    raise EvaluationValidationError(
                        "evidence_targets 条目缺少必需键 "
                        "(source_service/subject_kind/subject_id): "
                        f"{target!r}"
                    ) from exc
                conn.execute(
                    "INSERT OR IGNORE INTO evaluation_evidence_targets "
                    "(sample_id, source_service, subject_kind, subject_id, "
                    "subject_revision, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        sample_id,
                        source_service,
                        subject_kind,
                        subject_id,
                        target.get("subject_revision") or "",
                        now,
                    ),
                )
            conn.commit()
            return sample_id

    async def load_sample(self, sample_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._load_sample_sync, sample_id)

    def _load_sample_sync(self, sample_id: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_samples WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(f"sample not found: {sample_id}")
        sample = _row_to_dict(row)
        sample["target"] = _json_loads(sample.pop("target_json"), {})
        sample.pop("payload_hash", None)
        return sample

    async def list_sample_ids_by_subject(
        self,
        source_service: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: str | None = None,
    ) -> list[str]:
        return await asyncio.to_thread(
            self._list_sample_ids_by_subject_sync,
            source_service,
            subject_kind,
            subject_id,
            subject_revision,
        )

    def _list_sample_ids_by_subject_sync(
        self,
        source_service: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: str | None,
    ) -> list[str]:
        sql = (
            "SELECT sample_id FROM evaluation_evidence_targets "
            "WHERE source_service = ? AND subject_kind = ? AND subject_id = ?"
        )
        params: list[Any] = [source_service, subject_kind, subject_id]
        if subject_revision is not None:
            sql += " AND subject_revision = ?"
            params.append(subject_revision)
        sql += " ORDER BY created_at, sample_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r["sample_id"] for r in rows]

    # ------------------------------------------------------------------
    # Definitions (immutable)
    # ------------------------------------------------------------------

    async def save_definition(self, definition: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._save_definition_sync, definition)

    def _save_definition_sync(self, definition: Mapping[str, Any]) -> str:
        definition_id = definition.get("definition_id")
        version = definition.get("version")
        if not definition_id or not version:
            raise ValueError("definition 需要 definition_id 与 version")
        definition_hash = _hash_mapping(definition)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT definition_hash FROM evaluation_definitions "
                "WHERE definition_id = ? AND version = ?",
                (definition_id, version),
            ).fetchone()
            if row is not None:
                if row["definition_hash"] == definition_hash:
                    conn.commit()
                    return definition_id
                conn.rollback()
                raise EvaluationConflictError(
                    f"definition 版本冲突: {definition_id}@{version} "
                    "同版本内容不一致（定义不可变，需新版本）"
                )
            conn.execute(
                "INSERT INTO evaluation_definitions (definition_id, version, "
                "definition_json, definition_hash, metric_name, target_kind, "
                "method, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    definition_id,
                    version,
                    _json_dumps(dict(definition)),
                    definition_hash,
                    definition.get("metric_name") or "",
                    definition.get("target_kind") or "",
                    definition.get("method") or "",
                    _iso(),
                ),
            )
            conn.commit()
            return definition_id

    async def get_definition(
        self, definition_id: str, version: str
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self._get_definition_sync, definition_id, version
        )

    async def list_definitions(
        self, target_kind: str | None = None
    ) -> list[Mapping[str, Any]]:
        """按目标种类列出已注册定义（T22 线上样本作业展开的解析源）。

        返回 definition_id/version/metric_name/target_kind/method 最小
        投影（调用方按 DefinitionKey 引用定义本体，无需 definition_json
        全文）；``target_kind`` 为 None 时返回全部定义。"""
        return await asyncio.to_thread(self._list_definitions_sync, target_kind)

    def _list_definitions_sync(
        self, target_kind: str | None
    ) -> list[Mapping[str, Any]]:
        with self._connect() as conn:
            if target_kind is None:
                rows = conn.execute(
                    "SELECT definition_id, version, metric_name, "
                    "target_kind, method FROM evaluation_definitions "
                    "ORDER BY definition_id, version"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT definition_id, version, metric_name, "
                    "target_kind, method FROM evaluation_definitions "
                    "WHERE target_kind = ? ORDER BY definition_id, version",
                    (target_kind,),
                ).fetchall()
        return [dict(row) for row in rows]

    def _get_definition_sync(
        self, definition_id: str, version: str
    ) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_definitions "
                "WHERE definition_id = ? AND version = ?",
                (definition_id, version),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(
                f"definition not found: {definition_id}@{version}"
            )
        definition = _row_to_dict(row)
        definition["definition"] = _json_loads(
            definition.pop("definition_json"), {}
        )
        return definition

    # ------------------------------------------------------------------
    # Definition usage（online on/off 登记）
    # ------------------------------------------------------------------

    async def get_definition_usage(
        self, definition_id: str, definition_version: str
    ) -> Mapping[str, Any] | None:
        return await asyncio.to_thread(
            self._get_definition_usage_sync, definition_id, definition_version
        )

    def _get_definition_usage_sync(
        self, definition_id: str, definition_version: str
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT definition_id, definition_version, online_enabled, "
                "revision, updated_at, updated_by "
                "FROM evaluation_definition_usage "
                "WHERE definition_id = ? AND definition_version = ?",
                (definition_id, definition_version),
            ).fetchone()
        if row is None:
            return None
        usage = _row_to_dict(row)
        usage["online_enabled"] = bool(usage["online_enabled"])
        return usage

    async def upsert_definition_usage(
        self,
        definition_id: str,
        definition_version: str,
        *,
        online_enabled: bool,
        expected_revision: int,
        updated_by: str,
    ) -> Mapping[str, Any]:
        """CAS 切换定义的 online_enabled（无行时 expected_revision=0 创建
        revision=1；revision 乐观锁防并发互踩，参照 config_store CAS）。"""
        return await asyncio.to_thread(
            self._upsert_definition_usage_sync,
            definition_id,
            definition_version,
            online_enabled,
            expected_revision,
            updated_by,
        )

    def _upsert_definition_usage_sync(
        self,
        definition_id: str,
        definition_version: str,
        online_enabled: bool,
        expected_revision: int,
        updated_by: str,
    ) -> dict[str, Any]:
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            definition = conn.execute(
                "SELECT 1 FROM evaluation_definitions "
                "WHERE definition_id = ? AND version = ?",
                (definition_id, definition_version),
            ).fetchone()
            if definition is None:
                conn.rollback()
                raise EvaluationNotFoundError(
                    f"definition not found: {definition_id}@{definition_version}"
                )
            row = conn.execute(
                "SELECT revision FROM evaluation_definition_usage "
                "WHERE definition_id = ? AND definition_version = ?",
                (definition_id, definition_version),
            ).fetchone()
            current_revision = row["revision"] if row is not None else 0
            if current_revision != expected_revision:
                conn.rollback()
                raise EvaluationConflictError(
                    f"usage revision mismatch: {definition_id}@"
                    f"{definition_version} expected {expected_revision}, "
                    f"got {current_revision}"
                )
            next_revision = current_revision + 1
            if row is None:
                conn.execute(
                    "INSERT INTO evaluation_definition_usage (definition_id, "
                    "definition_version, online_enabled, revision, "
                    "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        definition_id,
                        definition_version,
                        1 if online_enabled else 0,
                        next_revision,
                        now,
                        updated_by,
                    ),
                )
            else:
                updated = conn.execute(
                    "UPDATE evaluation_definition_usage SET "
                    "online_enabled = ?, revision = ?, updated_at = ?, "
                    "updated_by = ? WHERE definition_id = ? "
                    "AND definition_version = ? AND revision = ?",
                    (
                        1 if online_enabled else 0,
                        next_revision,
                        now,
                        updated_by,
                        definition_id,
                        definition_version,
                        current_revision,
                    ),
                )
                if updated.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE 下不可达
                    conn.rollback()
                    raise EvaluationConflictError(
                        f"usage CAS 更新失败: {definition_id}@"
                        f"{definition_version}"
                    )
            conn.commit()
        return {
            "definition_id": definition_id,
            "definition_version": definition_version,
            "online_enabled": online_enabled,
            "revision": next_revision,
            "updated_at": now,
            "updated_by": updated_by,
        }

    async def save_definition_with_usage(
        self, definition: Mapping[str, Any], usage: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """单事务写 definition + usage（注册路径，online_enabled=False）。

        幂等语义与 save_definition 一致：同键同版本同内容返回既有定义行
        且不触碰 usage（用户停用状态不被重注册复活）；同键同版本异内容
        抛 EvaluationConflictError。"""
        return await asyncio.to_thread(
            self._save_definition_with_usage_sync, definition, usage
        )

    def _save_definition_with_usage_sync(
        self, definition: Mapping[str, Any], usage: Mapping[str, Any]
    ) -> dict[str, Any]:
        definition_id = definition.get("definition_id")
        version = definition.get("version")
        if not definition_id or not version:
            raise ValueError("definition 需要 definition_id 与 version")
        definition_hash = _hash_mapping(definition)
        updated_by = str(usage.get("updated_by") or "")
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT definition_hash FROM evaluation_definitions "
                "WHERE definition_id = ? AND version = ?",
                (definition_id, version),
            ).fetchone()
            if row is not None:
                if row["definition_hash"] != definition_hash:
                    conn.rollback()
                    raise EvaluationConflictError(
                        f"definition 版本冲突: {definition_id}@{version} "
                        "同版本内容不一致（定义不可变，需新版本）"
                    )
                # 幂等命中：不写 usage（停用状态不被复活）
                conn.commit()
            else:
                conn.execute(
                    "INSERT INTO evaluation_definitions (definition_id, "
                    "version, definition_json, definition_hash, "
                    "metric_name, target_kind, method, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        definition_id,
                        version,
                        _json_dumps(dict(definition)),
                        definition_hash,
                        definition.get("metric_name") or "",
                        definition.get("target_kind") or "",
                        definition.get("method") or "",
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO evaluation_definition_usage (definition_id, "
                    "definition_version, online_enabled, revision, "
                    "updated_at, updated_by) VALUES (?, ?, 0, 1, ?, ?)",
                    (definition_id, version, now, updated_by),
                )
                conn.commit()
        return self._get_definition_sync(definition_id, version)

    async def backfill_definition_usage(self, *, updated_by: str) -> int:
        """为 method != 'human' 且无 usage 行的定义插入
        online_enabled=1/revision=1 行；幂等，返回插入行数。

        生产启动路径使用 backfill_definition_usage_once（migrations
        账本一次性标记，重启跳过）；本方法保留用于非幂等显式回填/
        单测契约（Protocol 契约见 evaluation_service
        EvaluationLedgerStore.backfill_definition_usage）。"""
        return await asyncio.to_thread(
            self._backfill_definition_usage_sync, updated_by
        )

    def _backfill_definition_usage_sync(self, updated_by: str) -> int:
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT INTO evaluation_definition_usage (definition_id, "
                "definition_version, online_enabled, revision, "
                "updated_at, updated_by) "
                "SELECT d.definition_id, d.version, 1, 1, ?, ? "
                "FROM evaluation_definitions d "
                "WHERE d.method != 'human' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM evaluation_definition_usage u "
                "WHERE u.definition_id = d.definition_id "
                "AND u.definition_version = d.version)",
                (now, updated_by),
            )
            inserted = cursor.rowcount
            conn.commit()
        return inserted

    async def backfill_definition_usage_once(self, *, updated_by: str) -> int:
        """一次性 usage 回填（T8，spec 4.3 回填时序；migrations 账本幂等）。

        单事务（BEGIN IMMEDIATE）内完成检查标记、回填与记录标记：
        - 账本已有回填版本标记时直接跳过（重启不重复回填，用户停用
          状态不复活，标记之后新注册版本保持 disabled）；
        - 无 usage 行的自动定义（method != 'human'）插入
          online_enabled=1（旧库升级路径）；
        - 本次回填前预设注册产生的 disabled usage（updated_by='system'
          的自动定义行）在本次回填中启用（空库首次启动路径；用户显式
          停用行 updated_by != 'system' 不受影响）；
        - human 定义（feedback.helpful）永不启用。
        回填中任一步失败整体回滚，账本无标记，下次启动重试。
        返回本次回填影响行数（标记已存在时为 0）。"""
        return await asyncio.to_thread(
            self._backfill_definition_usage_once_sync, updated_by
        )

    def _backfill_definition_usage_once_sync(self, updated_by: str) -> int:
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                applied = conn.execute(
                    "SELECT 1 FROM evaluation_schema_migrations "
                    "WHERE version = ?",
                    (_USAGE_BACKFILL_VERSION,),
                ).fetchone()
                if applied is not None:
                    conn.commit()
                    return 0
                inserted_cursor = conn.execute(
                    "INSERT INTO evaluation_definition_usage (definition_id, "
                    "definition_version, online_enabled, revision, "
                    "updated_at, updated_by) "
                    "SELECT d.definition_id, d.version, 1, 1, ?, ? "
                    "FROM evaluation_definitions d "
                    "WHERE d.method != 'human' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM evaluation_definition_usage u "
                    "WHERE u.definition_id = d.definition_id "
                    "AND u.definition_version = d.version)",
                    (now, updated_by),
                )
                inserted = inserted_cursor.rowcount
                enabled_cursor = conn.execute(
                    "UPDATE evaluation_definition_usage SET "
                    "online_enabled = 1, updated_at = ?, updated_by = ? "
                    "WHERE online_enabled = 0 AND updated_by = 'system' "
                    "AND EXISTS ("
                    "SELECT 1 FROM evaluation_definitions d "
                    "WHERE d.definition_id = "
                    "evaluation_definition_usage.definition_id "
                    "AND d.version = "
                    "evaluation_definition_usage.definition_version "
                    "AND d.method != 'human')",
                    (now, updated_by),
                )
                enabled = enabled_cursor.rowcount
                conn.execute(
                    "INSERT INTO evaluation_schema_migrations "
                    "(version, name, applied_at) VALUES (?, ?, ?)",
                    # 复用同一 now：账本 applied_at 与 usage updated_at
                    # 保持一致（同一事务同一时间戳）
                    (_USAGE_BACKFILL_VERSION, _USAGE_BACKFILL_NAME, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return inserted + enabled

    async def list_definitions_with_usage(
        self, *, target_kind: str | None = None
    ) -> list[Mapping[str, Any]]:
        """列出定义（最小投影）并附加 usage 状态。

        契约：每行为定义投影 + 嵌套 ``usage`` 键——定义无 usage 行时
        ``usage`` 为 None；有行时为
        ``{online_enabled, revision, updated_at, updated_by}``
        （online_enabled 为 bool）。"""
        return await asyncio.to_thread(
            self._list_definitions_with_usage_sync, target_kind
        )

    def _list_definitions_with_usage_sync(
        self, target_kind: str | None
    ) -> list[Mapping[str, Any]]:
        sql = (
            "SELECT d.definition_id, d.version, d.metric_name, "
            "d.target_kind, d.method, "
            "u.online_enabled, u.revision, u.updated_at, u.updated_by "
            "FROM evaluation_definitions d "
            "LEFT JOIN evaluation_definition_usage u "
            "ON u.definition_id = d.definition_id "
            "AND u.definition_version = d.version"
        )
        with self._connect() as conn:
            if target_kind is None:
                rows = conn.execute(
                    sql + " ORDER BY d.definition_id, d.version"
                ).fetchall()
            else:
                rows = conn.execute(
                    sql + " WHERE d.target_kind = ? "
                    "ORDER BY d.definition_id, d.version",
                    (target_kind,),
                ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = _row_to_dict(row)
            online_enabled = item.pop("online_enabled")
            revision = item.pop("revision")
            usage = None
            if revision is not None:
                usage = {
                    "online_enabled": bool(online_enabled),
                    "revision": revision,
                    "updated_at": item.pop("updated_at"),
                    "updated_by": item.pop("updated_by"),
                }
            else:
                item.pop("updated_at")
                item.pop("updated_by")
            item["usage"] = usage
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    async def save_experiment(self, experiment: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._save_experiment_sync, experiment)

    def _save_experiment_sync(self, experiment: Mapping[str, Any]) -> str:
        experiment_id = experiment.get("experiment_id")
        if not experiment_id:
            raise ValueError("experiment 需要 experiment_id")
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT experiment_id FROM evaluation_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is not None:
                conn.commit()
                return experiment_id
            conn.execute(
                "INSERT INTO evaluation_experiments (experiment_id, dataset_id, "
                "dataset_version, dataset_hash, target_config_hash, baseline_id, "
                "evaluator_set_hash, manifest_json, mode, status, "
                "gate_report_ref, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    experiment["dataset_id"],
                    experiment["dataset_version"],
                    experiment["dataset_hash"],
                    experiment["target_config_hash"],
                    experiment.get("baseline_id"),
                    experiment["evaluator_set_hash"],
                    experiment.get("manifest_json") or "",
                    experiment.get("mode") or "execute",
                    experiment["status"],
                    experiment.get("gate_report_ref"),
                    now,
                    now,
                ),
            )
            conn.commit()
            return experiment_id

    async def get_experiment(self, experiment_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._get_experiment_sync, experiment_id)

    def _get_experiment_sync(self, experiment_id: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(f"experiment not found: {experiment_id}")
        return _row_to_dict(row)

    async def list_experiments(
        self, *, limit: int = 100
    ) -> list[Mapping[str, Any]]:
        return await asyncio.to_thread(self._list_experiments_sync, limit)

    def _list_experiments_sync(self, limit: int) -> list[Mapping[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluation_experiments "
                "ORDER BY created_at DESC, experiment_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Export audit（T21 审核后导出审计）
    # ------------------------------------------------------------------

    async def save_export_audit(self, audit: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._save_export_audit_sync, audit)

    def _save_export_audit_sync(self, audit: Mapping[str, Any]) -> str:
        for key in (
            "audit_id",
            "dataset_id",
            "dataset_version",
            "dataset_hash",
            "manifest_path",
            "approved_by",
            "case_count",
        ):
            if not audit.get(key):
                raise ValueError(f"export audit 需要 {key}")
        if "source_result_ids_json" not in audit:
            raise ValueError("export audit 需要 source_result_ids_json")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO evaluation_export_audit (audit_id, dataset_id, "
                "dataset_version, dataset_hash, manifest_path, approved_by, "
                "source_result_ids_json, case_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit["audit_id"],
                    audit["dataset_id"],
                    audit["dataset_version"],
                    audit["dataset_hash"],
                    audit["manifest_path"],
                    audit["approved_by"],
                    audit["source_result_ids_json"],
                    audit["case_count"],
                    audit.get("created_at") or _iso(),
                ),
            )
            conn.commit()
        return audit["audit_id"]

    async def list_export_audits(
        self, *, dataset_id: str | None = None, limit: int = 100
    ) -> list[Mapping[str, Any]]:
        return await asyncio.to_thread(
            self._list_export_audits_sync, dataset_id, limit
        )

    def _list_export_audits_sync(
        self, dataset_id: str | None, limit: int
    ) -> list[Mapping[str, Any]]:
        sql = "SELECT * FROM evaluation_export_audit"
        params: list[Any] = []
        if dataset_id is not None:
            sql += " WHERE dataset_id = ?"
            params.append(dataset_id)
        sql += " ORDER BY created_at DESC, audit_id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        audits = []
        for row in rows:
            audit = _row_to_dict(row)
            audit["source_result_ids"] = _json_loads(
                audit.pop("source_result_ids_json"), []
            )
            audits.append(audit)
        return audits

    # ------------------------------------------------------------------
    # Jobs: idempotency + lease claim (CAS)
    # ------------------------------------------------------------------

    async def save_job(self, job: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._save_job_sync, job)

    def _save_job_sync(self, job: Mapping[str, Any]) -> str:
        for key in ("sample_id", "definition_id", "definition_version"):
            if not job.get(key):
                raise ValueError(f"job 需要 {key}")
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            idempotency_key = job.get("idempotency_key")
            if idempotency_key:
                row = conn.execute(
                    "SELECT job_id FROM evaluation_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    # 同一请求幂等键唯一：重试不新增 job。注意：命中幂等键
                    # 时有意不做 payload 一致性比对（与 samples/definitions
                    # 的 hash 比对不同）——幂等键本身就是 job 重试的去重契约。
                    conn.commit()
                    return row["job_id"]
            job_id = job.get("job_id") or uuid4().hex
            try:
                conn.execute(
                    "INSERT INTO evaluation_jobs (job_id, sample_id, definition_id, "
                    "definition_version, experiment_id, replicate, requested_by, "
                    "status, idempotency_key, attempt_count, budget_reserved_usd, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        job["sample_id"],
                        job["definition_id"],
                        job["definition_version"],
                        job.get("experiment_id"),
                        int(job.get("replicate") or 0),
                        job.get("requested_by") or "",
                        job.get("status") or "queued",
                        idempotency_key,
                        int(job.get("attempt_count") or 0),
                        job.get("budget_reserved_usd"),
                        now,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise EvaluationConflictError(
                    f"job 唯一约束冲突（job_id/idempotency_key）: {exc}"
                ) from exc
            return job_id

    async def get_job(self, job_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._get_job_sync, job_id)

    def _get_job_sync(self, job_id: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(f"job not found: {job_id}")
        return _row_to_dict(row)

    async def claim_next_job(
        self,
        claimant: str,
        lease_seconds: int = 180,
        *,
        now: datetime | None = None,
    ) -> Mapping[str, Any] | None:
        """CAS 领取下一个可执行 job（BEGIN IMMEDIATE 串行化）。

        可领取：queued，或 running 且 lease 已过期（重新领取，追加
        attempt，不新建 job）。返回 dict 含最新 lease_token；无可领取
        job 返回 None。状态迁移经 domain state.py 校验。
        """
        return await asyncio.to_thread(
            self._claim_next_job_sync, claimant, lease_seconds, now
        )

    def _claim_next_job_sync(
        self,
        claimant: str,
        lease_seconds: int,
        now: datetime | None,
    ) -> Mapping[str, Any] | None:
        now_dt = now or _utcnow()
        now_str = _iso(now_dt)
        expiry_str = _iso(now_dt + timedelta(seconds=lease_seconds))
        lease_token = uuid4().hex
        attempt_id = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # T18/Mi3 claim 前置门禁：已删主体的 job 不可领取（避免对已
            # purge 快照消耗 Judge 预算；已删样本结果提交亦会被提交门禁
            # 拒绝，领取层先行阻断）
            for row in conn.execute(
                "SELECT * FROM evaluation_jobs "
                "WHERE status = 'queued' "
                "   OR (status = 'running' AND lease_expiry IS NOT NULL "
                "       AND lease_expiry <= ?) "
                "ORDER BY created_at, job_id LIMIT 100",
                (now_str,),
            ).fetchall():
                if row["sample_id"] and self._sample_id_blocked_by_tombstone(
                    conn, row["sample_id"]
                ):
                    continue
                break
            else:
                conn.commit()
                return None
            # 领取即状态迁移，经领域状态机校验（queued->running、
            # running->running 合法）。
            new_status = transition_job_status(row["status"], "running").value
            attempt_index = row["attempt_count"]
            updated = conn.execute(
                "UPDATE evaluation_jobs SET status = ?, lease_owner = ?, "
                "lease_token = ?, lease_expiry = ?, "
                "attempt_count = attempt_count + 1, updated_at = ? "
                "WHERE job_id = ? AND status = ? "
                "AND (lease_expiry IS NULL OR lease_expiry <= ?)",
                (
                    new_status,
                    claimant,
                    lease_token,
                    expiry_str,
                    now_str,
                    row["job_id"],
                    row["status"],
                    now_str,
                ),
            ).rowcount
            if updated != 1:  # pragma: no cover - BEGIN IMMEDIATE 下不可达
                conn.rollback()
                raise EvaluationConflictError(
                    f"job 领取 CAS 失败: {row['job_id']}"
                )
            conn.execute(
                "INSERT INTO evaluation_attempts (attempt_id, job_id, "
                "attempt_index, started_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (attempt_id, row["job_id"], attempt_index, now_str, now_str),
            )
            conn.commit()
        job = _row_to_dict(row)
        job.update(
            status=new_status,
            lease_owner=claimant,
            lease_token=lease_token,
            lease_expiry=expiry_str,
            attempt_count=attempt_index + 1,
            attempt_id=attempt_id,
        )
        return job

    # ------------------------------------------------------------------
    # Attempts (append-only)
    # ------------------------------------------------------------------

    async def append_attempt(self, attempt: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._append_attempt_sync, attempt)

    def _append_attempt_sync(self, attempt: Mapping[str, Any]) -> str:
        job_id = attempt.get("job_id")
        if not job_id:
            raise ValueError("attempt 需要 job_id")
        attempt_id = attempt.get("attempt_id") or uuid4().hex
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO evaluation_attempts (attempt_id, job_id, "
                    "attempt_index, judge_request_json, judge_model, "
                    "usage_known, input_tokens, output_tokens, total_tokens, "
                    "cost_usd, pricing_version, started_at, ended_at, "
                    "error_type, output_ref, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        job_id,
                        int(attempt["attempt_index"]),
                        _json_dumps(attempt.get("judge_request") or {}),
                        attempt.get("judge_model"),
                        1 if attempt.get("usage_known") else 0,
                        attempt.get("input_tokens"),
                        attempt.get("output_tokens"),
                        attempt.get("total_tokens"),
                        attempt.get("cost_usd"),
                        attempt.get("pricing_version"),
                        attempt.get("started_at") or now,
                        attempt.get("ended_at"),
                        attempt.get("error_type"),
                        attempt.get("output_ref"),
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise EvaluationConflictError(
                    f"attempt 追加冲突（append-only）: job={job_id} "
                    f"attempt_index={attempt.get('attempt_index')}: {exc}"
                ) from exc
            return attempt_id

    async def list_attempts(self, job_id: str) -> list[Mapping[str, Any]]:
        return await asyncio.to_thread(self._list_attempts_sync, job_id)

    def _list_attempts_sync(self, job_id: str) -> list[Mapping[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluation_attempts WHERE job_id = ? "
                "ORDER BY attempt_index",
                (job_id,),
            ).fetchall()
        attempts = []
        for row in rows:
            attempt = _row_to_dict(row)
            attempt["judge_request"] = _json_loads(
                attempt.pop("judge_request_json"), {}
            )
            attempts.append(attempt)
        return attempts

    # ------------------------------------------------------------------
    # Results (immutable; lease-verified commit below)
    # ------------------------------------------------------------------

    async def save_result(self, result: Mapping[str, Any]) -> str:
        """持久化评估结果（终态不可变）；唯一约束冲突抛
        EvaluationConflictError，CHECK 违反抛 sqlite3.IntegrityError。

        注意：携带 job_id 的自动结果提交应优先使用
        ``commit_result_with_deliveries``（结果 + 终态 job + deliveries
        单事务，且校验最新 lease token）。
        """
        return await asyncio.to_thread(self._save_result_sync, result)

    def _save_result_sync(self, result: Mapping[str, Any]) -> str:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result_id = self._insert_result(conn, result, _iso())
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "UNIQUE constraint" in str(exc):
                    raise EvaluationConflictError(
                        f"result 唯一约束冲突: {exc}"
                    ) from exc
                raise
            return result_id

    def _insert_result(
        self, conn: sqlite3.Connection, result: Mapping[str, Any], now: str
    ) -> str:
        result_id = result.get("result_id") or uuid4().hex
        typed_value = result.get("typed_value")
        passed = result.get("passed")
        conn.execute(
            "INSERT INTO evaluation_results (result_id, job_id, feedback_id, "
            "provenance, metric_name, metric_version, status, skip_reason, "
            "typed_value_json, passed, explanation, explanation_ref, "
            "evidence_hash, evaluation_trace_id, evaluation_span_id, "
            "evaluated_at, supersedes_result_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result_id,
                result.get("job_id"),
                result.get("feedback_id"),
                result["provenance"],
                result["metric_name"],
                result.get("metric_version") or "",
                result["status"],
                result.get("skip_reason"),
                (
                    _json_dumps(typed_value)
                    if typed_value is not None
                    else None
                ),
                None if passed is None else (1 if passed else 0),
                result.get("explanation"),
                result.get("explanation_ref"),
                result.get("evidence_hash") or "",
                result.get("evaluation_trace_id"),
                result.get("evaluation_span_id"),
                result.get("evaluated_at") or now,
                result.get("supersedes_result_id"),
                now,
            ),
        )
        return result_id

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> dict[str, Any]:
        result = _row_to_dict(row)
        result["typed_value"] = _json_loads(
            result.pop("typed_value_json"), None
        )
        passed = result.get("passed")
        result["passed"] = None if passed is None else bool(passed)
        return result

    async def load_result(self, result_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._load_result_sync, result_id)

    def _load_result_sync(self, result_id: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_results WHERE result_id = ?",
                (result_id,),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(f"result not found: {result_id}")
        return self._row_to_result(row)

    async def commit_result_with_deliveries(
        self,
        result: Mapping[str, Any],
        deliveries: Sequence[Mapping[str, Any]] = (),
        *,
        lease_token: str | None = None,
        job_terminal_status: str | None = None,
        job_error_type: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """结果 + 终态 job + deliveries 在同一事务提交，任一失败整体回滚。

        携带 job_id（自动结果）时必须提供最新 ``lease_token``，且
        ``job_terminal_status`` 迁移经 domain state.py 校验；stale token
        抛 EvaluationLeaseError 且不落任何行。人工结果（feedback_id）
        无 lease，直接单行插入。
        """
        return await asyncio.to_thread(
            self._commit_result_with_deliveries_sync,
            result,
            list(deliveries),
            lease_token,
            job_terminal_status,
            job_error_type,
            now,
        )

    def _commit_result_with_deliveries_sync(
        self,
        result: Mapping[str, Any],
        deliveries: list[Mapping[str, Any]],
        lease_token: str | None,
        job_terminal_status: str | None,
        job_error_type: str | None,
        now: datetime | None,
    ) -> str:
        now_str = _iso(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                job_id = result.get("job_id")
                if job_id is not None:
                    job = conn.execute(
                        "SELECT status, lease_token, sample_id FROM "
                        "evaluation_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if job is None:
                        raise EvaluationNotFoundError(
                            f"job not found: {job_id}"
                        )
                    if not lease_token or job["lease_token"] != lease_token:
                        raise EvaluationLeaseError(
                            f"结果提交 lease token 不匹配最新持有者: "
                            f"job={job_id}"
                        )
                    # T18 删除传播门禁：删除期间运行中 Judge 返回不得提交
                    # 或复活内容（样本 subject 命中 tombstone 时整体拒绝）
                    if job["sample_id"] and self._sample_id_blocked_by_tombstone(
                        conn, job["sample_id"]
                    ):
                        raise EvaluationDeletionConflictError(
                            "job 样本 subject 已删除，拒绝结果提交: "
                            f"job={job_id} sample={job['sample_id']}"
                        )
                    # 终态迁移经领域状态机校验；非法迁移抛
                    # IllegalEvaluationTransition 并整体回滚。
                    new_status = transition_job_status(
                        job["status"], job_terminal_status
                    ).value
                    if job_error_type is not None:
                        conn.execute(
                            "UPDATE evaluation_jobs SET status = ?, "
                            "error_type = ?, lease_expiry = NULL, "
                            "updated_at = ? "
                            "WHERE job_id = ? AND lease_token = ?",
                            (new_status, job_error_type, now_str, job_id,
                             lease_token),
                        )
                    else:
                        conn.execute(
                            "UPDATE evaluation_jobs SET status = ?, "
                            "lease_expiry = NULL, updated_at = ? "
                            "WHERE job_id = ? AND lease_token = ?",
                            (new_status, now_str, job_id, lease_token),
                        )
                result_id = self._insert_result(conn, result, now_str)
                for delivery in deliveries:
                    self._insert_delivery(conn, delivery, result_id, now_str)
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "UNIQUE constraint" in str(exc):
                    raise EvaluationConflictError(
                        f"result/delivery 唯一约束冲突: {exc}"
                    ) from exc
                raise
            except Exception:
                conn.rollback()
                raise
            return result_id

    # ------------------------------------------------------------------
    # Calibration reports (plan T8, spec 10)
    # ------------------------------------------------------------------

    async def save_calibration_report(self, report: Mapping[str, Any]) -> str:
        """持久化校准报告（同 cache_key 重算覆盖，最新结果权威；缓存键
        含 definition hash 与两个数据集 hash，任一变化即新行，历史报告
        保留作审计轨迹）。"""
        return await asyncio.to_thread(self._save_calibration_report_sync, report)

    def _save_calibration_report_sync(
        self, report: Mapping[str, Any]
    ) -> str:
        cache_key = report["cache_key"]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO evaluation_calibrations "
                    "(cache_key, metric_name, definition_id, definition_hash, "
                    "status, report_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        cache_key,
                        report["metric_name"],
                        report["definition_id"],
                        report["definition_hash"],
                        report["status"],
                        _json_dumps(dict(report)),
                        report.get("created_at") or _iso(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return cache_key

    async def get_calibration_report(self, cache_key: str) -> Mapping[str, Any]:
        """按缓存键读取校准报告（返回完整报告映射）；无记录或账本内
        report_json 损坏不可解析均抛 EvaluationNotFoundError（调用方
        视为缓存未命中 = 校准失效/未执行，重算覆盖）。"""
        return await asyncio.to_thread(self._get_calibration_report_sync, cache_key)

    def _get_calibration_report_sync(self, cache_key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM evaluation_calibrations "
                "WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(
                f"calibration report not found: cache_key={cache_key}"
            )
        try:
            return json.loads(row["report_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            # 损坏缓存视为未命中（log + 抛未找到）——绝不永久阻断重校准
            logger.warning(
                "corrupt calibration report_json treated as cache miss: "
                "cache_key=%s: %s",
                cache_key,
                exc,
            )
            raise EvaluationNotFoundError(
                f"corrupt calibration report (treated as cache miss): "
                f"cache_key={cache_key}"
            ) from exc

    async def get_latest_calibration_report(
        self, definition_id: str, definition_hash: str
    ) -> Mapping[str, Any] | None:
        """按 (definition_id, definition_hash) 取最新校准报告；无记录或
        账本内 report_json 损坏不可解析均返回 None（损坏 = 未校准）。"""
        return await asyncio.to_thread(
            self._get_latest_calibration_report_sync,
            definition_id,
            definition_hash,
        )

    def _get_latest_calibration_report_sync(
        self, definition_id: str, definition_hash: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM evaluation_calibrations "
                "WHERE definition_id = ? AND definition_hash = ? "
                "ORDER BY created_at DESC, cache_key DESC LIMIT 1",
                (definition_id, definition_hash),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["report_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "corrupt calibration report_json treated as no report: "
                "definition_id=%s: %s",
                definition_id,
                exc,
            )
            return None

    async def calibration_status(
        self, definition_id: str, definition_hash: str
    ) -> str:
        """definition 的校准状态："passed" / "advisory"。

        无报告（未校准）、definition hash 变化（缓存键失效）、记录损坏
        或状态字段缺失/非法一律 "advisory"——未校准指标仅 advisory，
        必需门禁不得 pass（spec 10）；fail-safe，绝不因坏行放行。"""
        latest = await self.get_latest_calibration_report(
            definition_id, definition_hash
        )
        if isinstance(latest, Mapping) and latest.get("status") == "passed":
            return "passed"
        return "advisory"

    # ------------------------------------------------------------------
    # Deliveries
    # ------------------------------------------------------------------

    def _insert_delivery(
        self,
        conn: sqlite3.Connection,
        delivery: Mapping[str, Any],
        result_id: str,
        now: str,
    ) -> str:
        delivery_id = delivery.get("delivery_id") or uuid4().hex
        payload = delivery.get("payload")
        payload_hash = delivery.get("payload_hash") or (
            _hash_mapping({"payload": payload}) if payload is not None else ""
        )
        conn.execute(
            "INSERT INTO evaluation_deliveries (delivery_id, result_id, sink, "
            "projection_version, external_id, status, attempt_count, "
            "next_retry_at, last_error_type, payload_hash, lease_owner, "
            "lease_token, lease_expiry, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                delivery_id,
                result_id,
                delivery["sink"],
                delivery["projection_version"],
                delivery.get("external_id"),
                delivery.get("status") or "pending",
                int(delivery.get("attempt_count") or 0),
                delivery.get("next_retry_at"),
                delivery.get("last_error_type"),
                payload_hash,
                delivery.get("lease_owner"),
                delivery.get("lease_token"),
                delivery.get("lease_expiry"),
                now,
                now,
            ),
        )
        return delivery_id

    async def get_delivery(self, delivery_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._get_delivery_sync, delivery_id)

    def _get_delivery_sync(self, delivery_id: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(f"delivery not found: {delivery_id}")
        return _row_to_dict(row)

    async def list_deliveries(
        self, result_id: str | None = None
    ) -> list[Mapping[str, Any]]:
        return await asyncio.to_thread(self._list_deliveries_sync, result_id)

    def _list_deliveries_sync(
        self, result_id: str | None
    ) -> list[Mapping[str, Any]]:
        sql = "SELECT * FROM evaluation_deliveries"
        params: list[Any] = []
        if result_id is not None:
            sql += " WHERE result_id = ?"
            params.append(result_id)
        sql += " ORDER BY created_at, delivery_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def record_delivery_transition(
        self,
        delivery_id: str,
        target_status: str,
        *,
        external_id: str | None = None,
        error_type: str | None = None,
        next_retry_at: str | None = None,
        lease_token: str | None = None,
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """投递状态迁移：经 domain ``transition_delivery_status`` 校验，
        非法迁移抛 IllegalEvaluationTransition 且不落任何变更。

        ``lease_token`` 提供时执行 CAS（WHERE lease_token = ?）：投递已被
        其他 worker 重领（token 漂移）时抛 EvaluationConflictError，陈旧
        worker 不得迁移重领后的投递；传 None 表示不带 CAS（管理员操作
        disable/enable/replay 等无租约路径）。

        字段语义（T16/T18 消费注意）：
        - external_id / lease_token 用 COALESCE：传 None 表示保留原值；
        - last_error_type / next_retry_at 无条件覆盖：传 None 即清空；
        - attempt_count 记录的是状态迁移次数，不是投递网络重试次数；
          如需真实重试计数，消费方（T16/T18）需另行拆分计数或在 delivery
          行为上扩展（已在 plan T3 证据行备案）。
        """
        return await asyncio.to_thread(
            self._record_delivery_transition_sync,
            delivery_id,
            target_status,
            external_id,
            error_type,
            next_retry_at,
            lease_token,
            now,
        )

    def _record_delivery_transition_sync(
        self,
        delivery_id: str,
        target_status: str,
        external_id: str | None,
        error_type: str | None,
        next_retry_at: str | None,
        lease_token: str | None,
        now: datetime | None,
    ) -> Mapping[str, Any]:
        now_str = _iso(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM evaluation_deliveries "
                    "WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    raise EvaluationNotFoundError(
                        f"delivery not found: {delivery_id}"
                    )
                new_status = transition_delivery_status(
                    row["status"], target_status
                ).value
                # T18 删除传播门禁（ACK 后复核 tombstone）：发送中删除的
                # 投递不得转 verified——改判 unlinked 隔离上报（结果不再
                # 作为正式投影；已发送内容按预登记 external_id 走外部删除
                # 任务），并标注删除原因供审计
                if new_status == "verified" and self._delivery_blocked_by_tombstone(
                    conn, delivery_id
                ):
                    new_status = "unlinked"
                    error_type = "subject_deleted"
                # lease_token CAS：提供 token 时限定仍持有租约的 worker
                # 才能迁移（陈旧 worker 对已重领投递的写入被拒绝）
                lease_clause = ""
                if lease_token is not None:
                    lease_clause = " AND lease_token = ?"
                cursor = conn.execute(
                    "UPDATE evaluation_deliveries SET status = ?, "
                    "external_id = COALESCE(?, external_id), "
                    "last_error_type = ?, next_retry_at = ?, "
                    "attempt_count = attempt_count + 1, "
                    "lease_token = COALESCE(?, lease_token), updated_at = ? "
                    "WHERE delivery_id = ?" + lease_clause,
                    (
                        new_status,
                        external_id,
                        error_type,
                        next_retry_at,
                        lease_token,
                        now_str,
                        delivery_id,
                        *([lease_token] if lease_token is not None else []),
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise EvaluationConflictError(
                        "delivery lease_token CAS 失败（已被重领或状态"
                        f"漂移）: {delivery_id}"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._get_delivery_sync(delivery_id)

    # ------------------------------------------------------------------
    # T9 delivery scheduling: claim/lease + frozen payload + retry
    # ------------------------------------------------------------------

    async def claim_next_delivery(
        self,
        claimant: str,
        lease_seconds: int = 300,
        *,
        sinks: Sequence[str] | None = None,
        reclaim_accepted_sinks: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> Mapping[str, Any] | None:
        """CAS 领取一条到期可投递的 delivery（BEGIN IMMEDIATE 串行化）。

        可领取：pending 且（无租约或租约已过期）且无未到期的 next_retry_at，
        或 retry_wait 且 next_retry_at 已到期（领取时经状态机迁移回
        pending，清空 next_retry_at），或 accepted 且租约已过期（回读
        窗口内崩溃/租约丢失：经状态机 accepted -> retry_wait(即期) ->
        pending，复用冻结 payload 重投——仅 ``reclaim_accepted_sinks`` 内的
        sink；24 项三方审阅 #11：无回读能力 sink（OTel）的 accepted 是
        投递成功终态，重领会让成功投递周期性重发、队列永不收敛）。
        ``sinks`` 限定可领取的 sink 名
        （未注册适配器的 sink 保持 pending：OTel 投递语义独立，SDK
        enqueue 不由本 store 记 delivered/verified）。返回 dict 含最新
        lease_token；无可领取 delivery 返回 None。
        """
        return await asyncio.to_thread(
            self._claim_next_delivery_sync, claimant, lease_seconds, sinks,
            reclaim_accepted_sinks, now,
        )

    def _claim_next_delivery_sync(
        self,
        claimant: str,
        lease_seconds: int,
        sinks: Sequence[str] | None,
        reclaim_accepted_sinks: Sequence[str] | None,
        now: datetime | None,
    ) -> Mapping[str, Any] | None:
        now_dt = now or _utcnow()
        now_str = _iso(now_dt)
        expiry_str = _iso(now_dt + timedelta(seconds=lease_seconds))
        lease_token = uuid4().hex
        sink_filter = ""
        if sinks:
            sink_filter = (
                f" AND sink IN ({', '.join('?' for _ in sinks)})"
            )
        # accepted 重领仅限可回读 sink（24 项三方审阅 #11）：无回读
        # sink（OTel）的 accepted 是成功终态，租约过期不得重发
        accepted_branch = ""
        reclaim_accepted: tuple[str, ...] = tuple(reclaim_accepted_sinks or ())
        if reclaim_accepted:
            accepted_branch = (
                "  OR (status = 'accepted' "
                "      AND lease_expiry IS NOT NULL AND lease_expiry <= ?"
                "      AND sink IN ("
                + ", ".join("?" for _ in reclaim_accepted)
                + "))"
            )
        sql = (
            "SELECT * FROM evaluation_deliveries WHERE ("
            "  (status = 'pending' "
            "   AND (next_retry_at IS NULL OR next_retry_at <= ?) "
            "   AND (lease_expiry IS NULL OR lease_expiry <= ?))"
            "  OR (status = 'retry_wait' "
            "      AND next_retry_at IS NOT NULL AND next_retry_at <= ?)"
            + accepted_branch +
            ")" + sink_filter + " ORDER BY created_at, delivery_id LIMIT 100"
        )
        # 占位符顺序：pending 分支两个 now 比较 + retry_wait 一个 +
        # accepted 分支（now + 可重领 sink 名单），再接 sink 过滤参数
        params = [now_str, now_str, now_str]
        if reclaim_accepted:
            params.append(now_str)
            params.extend(reclaim_accepted)
        if sinks:
            params.extend(sinks)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute(sql, params).fetchall():
                # T18 删除传播门禁（发布前复核 tombstone）：已删主体的
                # 投递不可领取（pending/retry_wait/accepted 过期恢复路径
                # 一律跳过，不得重发）
                if self._delivery_blocked_by_tombstone(conn, row["delivery_id"]):
                    continue
                if self._claim_delivery_row(
                    conn,
                    row,
                    claimant=claimant,
                    lease_token=lease_token,
                    expiry_str=expiry_str,
                    now_str=now_str,
                ):
                    conn.commit()
                    claimed = _row_to_dict(row)
                    if row["status"] in ("retry_wait", "accepted"):
                        # 领取即迁移：retry_wait/accepted -> pending
                        claimed["status"] = EvaluationDeliveryStatus.pending.value
                        claimed["next_retry_at"] = None
                    claimed.update(
                        lease_owner=claimant,
                        lease_token=lease_token,
                        lease_expiry=expiry_str,
                    )
                    return claimed
            conn.commit()
        return None

    @staticmethod
    def _claim_delivery_row(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        claimant: str,
        lease_token: str,
        expiry_str: str,
        now_str: str,
    ) -> bool:
        """单行 CAS 收租（SELECT 候选与 UPDATE 同在 BEGIN IMMEDIATE 内）。
        retry_wait/accepted 先经状态机迁移回 pending（即期），pending 直
        接收租；返回是否抢占成功（并发 claimer 只有一个赢家）。"""
        delivery_id = row["delivery_id"]
        status = row["status"]
        if status == "retry_wait":
            # retry_wait -> pending（经状态机校验），清空退避时间
            updated = conn.execute(
                "UPDATE evaluation_deliveries SET status = ?, "
                "next_retry_at = NULL, lease_owner = ?, "
                "lease_token = ?, lease_expiry = ?, updated_at = ? "
                "WHERE delivery_id = ? AND status = 'retry_wait' "
                "AND next_retry_at <= ?",
                (
                    EvaluationDeliveryStatus.pending.value,
                    claimant,
                    lease_token,
                    expiry_str,
                    now_str,
                    delivery_id,
                    now_str,
                ),
            ).rowcount
        elif status == "accepted":
            # accepted 且租约过期（回读窗口内崩溃/租约丢失）：
            # 经状态机 accepted -> retry_wait（即期）-> pending，
            # 复用冻结 payload 重投（spec 13 恢复路径；不加计
            # retry_count——这不是一次网络失败）
            transition_delivery_status(
                status, EvaluationDeliveryStatus.retry_wait
            )
            updated = conn.execute(
                "UPDATE evaluation_deliveries SET status = ?, "
                "last_error_type = 'verify_pending', "
                "next_retry_at = ?, lease_owner = ?, "
                "lease_token = ?, lease_expiry = ?, updated_at = ? "
                "WHERE delivery_id = ? AND status = 'accepted' "
                "AND lease_expiry <= ?",
                (
                    EvaluationDeliveryStatus.pending.value,
                    now_str,
                    claimant,
                    lease_token,
                    expiry_str,
                    now_str,
                    delivery_id,
                    now_str,
                ),
            ).rowcount
        else:
            # pending 直接收租（无状态迁移）；CAS 限定租约空闲，
            # 并发 claimer 只有一个赢家
            updated = conn.execute(
                "UPDATE evaluation_deliveries SET "
                "lease_owner = ?, lease_token = ?, lease_expiry = ?, "
                "updated_at = ? WHERE delivery_id = ? "
                "AND status = 'pending' "
                "AND (lease_expiry IS NULL OR lease_expiry <= ?)",
                (
                    claimant,
                    lease_token,
                    expiry_str,
                    now_str,
                    delivery_id,
                    now_str,
                ),
            ).rowcount
        return updated == 1

    async def freeze_delivery_payload(
        self,
        delivery_id: str,
        *,
        external_id: str,
        payload: Mapping[str, Any],
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """发送前冻结 payload（先登记再发送，spec 13）：持久化
        payload_json（canonical JSON 文本）与 payload_hash 并登记
        external_id。

        幂等：已冻结且 external_id/payload_hash 一致时返回原行；已冻结但
        内容不一致抛 EvaluationConflictError（冻结不可变——同 ID 同
        payload 重试是合同，冻结后禁止改写）。"""
        return await asyncio.to_thread(
            self._freeze_delivery_payload_sync,
            delivery_id,
            external_id,
            dict(payload),
            now,
        )

    def _freeze_delivery_payload_sync(
        self,
        delivery_id: str,
        external_id: str,
        payload: dict[str, Any],
        now: datetime | None,
    ) -> Mapping[str, Any]:
        payload_json = canonical_json_bytes(payload).decode("utf-8")
        payload_hash = hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT external_id, payload_hash, payload_json "
                    "FROM evaluation_deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise EvaluationNotFoundError(
                        f"delivery not found: {delivery_id}"
                    )
                # T18 删除传播门禁（发布前复核 tombstone）：已删主体禁止
                # 新发送（先登记再发送的冻结窗口被 tombstone 截断）
                if self._delivery_blocked_by_tombstone(conn, delivery_id):
                    conn.rollback()
                    raise EvaluationDeletionConflictError(
                        "delivery 主体已删除，禁止新发送: "
                        f"delivery_id={delivery_id}"
                    )
                if row["payload_json"] is not None:
                    if (
                        row["external_id"] == external_id
                        and row["payload_hash"] == payload_hash
                    ):
                        conn.commit()
                        return self._get_delivery_sync(delivery_id)
                    conn.rollback()
                    raise EvaluationConflictError(
                        "delivery frozen payload 冲突（冻结不可变）: "
                        f"{delivery_id}"
                    )
                conn.execute(
                    "UPDATE evaluation_deliveries SET payload_json = ?, "
                    "payload_hash = ?, external_id = ?, updated_at = ? "
                    "WHERE delivery_id = ?",
                    (
                        payload_json,
                        payload_hash,
                        external_id,
                        _iso(now),
                        delivery_id,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._get_delivery_sync(delivery_id)

    async def schedule_delivery_retry(
        self,
        delivery_id: str,
        *,
        error_type: str,
        next_retry_at: datetime,
        lease_token: str | None = None,
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """记录一次退避重试：状态 -> retry_wait（经状态机校验）、登记
        last_error_type 与 next_retry_at、retry_count += 1（真实网络重试
        计数，退避指数与 24h dead-letter 窗口的依据；attempt_count 是状态
        迁移次数，不用于退避计算）。``lease_token`` 提供时执行 CAS：投递
        已被其他 worker 重领时抛 EvaluationConflictError（陈旧 worker 的
        重试调度被拒绝）。"""
        return await asyncio.to_thread(
            self._schedule_delivery_retry_sync,
            delivery_id,
            error_type,
            next_retry_at,
            lease_token,
            now,
        )

    def _schedule_delivery_retry_sync(
        self,
        delivery_id: str,
        error_type: str,
        next_retry_at: datetime,
        lease_token: str | None,
        now: datetime | None,
    ) -> Mapping[str, Any]:
        now_str = _iso(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM evaluation_deliveries "
                    "WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise EvaluationNotFoundError(
                        f"delivery not found: {delivery_id}"
                    )
                new_status = transition_delivery_status(
                    row["status"], EvaluationDeliveryStatus.retry_wait
                ).value
                lease_clause = ""
                if lease_token is not None:
                    lease_clause = " AND lease_token = ?"
                cursor = conn.execute(
                    "UPDATE evaluation_deliveries SET status = ?, "
                    "last_error_type = ?, next_retry_at = ?, "
                    "retry_count = retry_count + 1, updated_at = ? "
                    "WHERE delivery_id = ?" + lease_clause,
                    (
                        new_status,
                        error_type,
                        _iso(next_retry_at),
                        now_str,
                        delivery_id,
                        *([lease_token] if lease_token is not None else []),
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise EvaluationConflictError(
                        "delivery lease_token CAS 失败（已被重领或状态"
                        f"漂移）: {delivery_id}"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._get_delivery_sync(delivery_id)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    async def save_feedback(self, feedback: Mapping[str, Any]) -> str:
        """持久化单条 feedback 行。

        ``revision`` 缺省时的 MAX+1 回退仅供直接调用方（测试/运维脚本）
        使用；服务层正常路径必须携带显式 ``revision`` 并使用
        ``save_feedback_with_result`` 单事务提交，本方法仅作为幂等重放
        崩溃恢复路径的组成部分。
        """
        return await asyncio.to_thread(self._save_feedback_sync, feedback)

    def _save_feedback_sync(self, feedback: Mapping[str, Any]) -> str:
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                feedback_id, _revision = self._insert_feedback(conn, feedback, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return feedback_id

    async def save_feedback_with_result(
        self,
        feedback: Mapping[str, Any],
        result: Mapping[str, Any],
        deliveries: Sequence[Mapping[str, Any]] = (),
        *,
        expected_revision: int | None = None,
    ) -> tuple[str, str]:
        """feedback 行 + human result 行 + sink deliveries 单事务提交
        （spec 6.3/145 同事务语义：结果与各 sink delivery 同事务）。

        服务层正常路径使用本方法，关闭"feedback 已落库、result 未落库"的
        双写窗口；任一行失败整体回滚。``feedback`` 必须携带显式
        ``revision``（本方法无 MAX+1 回退）；``result`` 无需携带
        feedback_id（由本方法注入刚插入的 feedback_id）。``deliveries``
        为人评 result 的投递登记（T22：每个 result 都有 sink 投递，无
        真实身份的 unlinked 判定延迟到投递 worker 投影时落）。

        ``expected_revision`` 提供时在 BEGIN IMMEDIATE 写事务内对完整
        线程（七元组线程身份）执行 ``expected == MAX(revision)`` CAS 复查
        （spec 4.4 / plan Task 6 S4：同事务 CAS，唯一索引与降级非唯一索引
        统一生效——降级库上唯一索引不在，仅靠本复查禁止同线程重复
        revision）；不一致抛 EvaluationConflictError。None 时保持现状
        （显式 revision 直插，不做事务内 CAS）。
        """
        return await asyncio.to_thread(
            self._save_feedback_with_result_sync,
            dict(feedback),
            dict(result),
            list(deliveries),
            expected_revision,
        )

    def _save_feedback_with_result_sync(
        self,
        feedback: dict[str, Any],
        result: dict[str, Any],
        deliveries: list[Mapping[str, Any]],
        expected_revision: int | None = None,
    ) -> tuple[str, str]:
        if feedback.get("revision") is None:
            raise ValueError("save_feedback_with_result 需要显式 revision")
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                feedback_id, _revision = self._insert_feedback(
                    conn, feedback, now, expected_revision=expected_revision
                )
                result_payload = dict(result)
                result_payload["feedback_id"] = feedback_id
                result_id = self._insert_result(conn, result_payload, now)
                for delivery in deliveries:
                    self._insert_delivery(conn, delivery, result_id, now)
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "UNIQUE constraint" in str(exc):
                    raise EvaluationConflictError(
                        f"feedback/result 唯一约束冲突（单事务整体回滚）: {exc}"
                    ) from exc
                raise
            except Exception:
                conn.rollback()
                raise
        return feedback_id, result_id

    @staticmethod
    def _thread_revision_max(
        conn: sqlite3.Connection,
        actor_ref: str,
        target: Mapping[str, Any],
        definition_id: str,
        definition_version: str,
    ) -> int:
        """完整线程（七元组线程身份）的当前 MAX(revision)，无线程行为 0。

        revision 自增与事务内 CAS 复查共用同一实现（spec 4.4：唯一索引与
        降级非唯一索引统一生效，禁止两份手工拷贝导致口径分叉）。"""
        row = conn.execute(
            "SELECT MAX(revision) FROM evaluation_feedback "
            "WHERE actor_ref = ? AND target_service = ? "
            "AND target_kind = ? AND target_subject_id = ? "
            "AND target_subject_revision = ? AND definition_id = ? "
            "AND definition_version = ?",
            (
                actor_ref,
                target.get("service") or "",
                target.get("kind") or "",
                target.get("subject_id") or "",
                target.get("subject_revision") or "",
                definition_id,
                definition_version,
            ),
        ).fetchone()
        return int(row[0] or 0)

    def _insert_feedback(
        self,
        conn: sqlite3.Connection,
        feedback: Mapping[str, Any],
        now: str,
        expected_revision: int | None = None,
    ) -> tuple[str, int]:
        """在给定连接上插入 feedback 行，返回 (feedback_id, revision)。

        revision 缺省时按线程 MAX+1 回退（仅直接调用方）；UNIQUE 冲突映射
        EvaluationConflictError 并区分两类语义：feedback_id PK 冲突 =
        request-key 幂等键已绑定不同 payload（服务层映射 409），revision
        唯一冲突 = 同线程并发修订竞态。

        ``expected_revision`` 提供时在当前写事务（调用方 BEGIN IMMEDIATE）
        内对完整线程执行 ``expected == MAX(revision)`` CAS 复查，不一致抛
        EvaluationConflictError（降级非唯一索引库上唯一索引不在，该复查
        是禁止同线程重复 revision 的权威防线）。
        """
        actor_ref = feedback.get("actor_ref")
        definition_id = feedback.get("definition_id")
        if not actor_ref or not definition_id:
            raise ValueError("feedback 需要 actor_ref 与 definition_id")
        target = feedback.get("target") or {}
        # T18/Mi3 迟到人评门禁：目标主体已被删除 tombstone 封禁时拒绝
        # 新反馈（与会话删除级联的收敛语义一致：已删主体不得再累积新内容）
        if self._subject_blocked_by_tombstone(
            conn,
            target.get("kind") or "",
            target.get("subject_id") or "",
            target.get("subject_revision") or "",
        ):
            raise EvaluationDeletionConflictError(
                "feedback 目标主体已删除，拒绝迟到人评: "
                f"kind={target.get('kind')!r} "
                f"subject_id={target.get('subject_id')!r}"
            )
        # value 允许 False/0/"" 等假值（如 feedback.helpful 的否定反馈），
        # 仅 None 视为缺省，禁止 `value or {}` 把假值吞成空对象（T5 修正）
        value_payload = feedback.get("value")
        definition_version = feedback.get("definition_version") or ""
        if expected_revision is not None:
            # 事务内 CAS 复查（spec 4.4 / plan Task 6 S4）：expected ==
            # MAX(revision) 对唯一索引与降级非唯一索引统一生效；调用方
            # BEGIN IMMEDIATE 已串行化写者，本读在写事务内不受并发干扰
            current = self._thread_revision_max(
                conn, actor_ref, target, definition_id, definition_version
            )
            if int(expected_revision) != current:
                raise EvaluationConflictError(
                    "feedback revision 事务内 CAS 复查失败（同线程并发修订"
                    f"竞态，请重载后重试）: {actor_ref}@{definition_id} "
                    f"expected={expected_revision} current={current}"
                )
        revision = feedback.get("revision")
        if revision is None:
            # 同完整线程（七元组，v8 起含 target_subject_revision 与
            # definition_version）递增 revision（人评修订新建行）。
            revision = (
                self._thread_revision_max(
                    conn, actor_ref, target, definition_id, definition_version
                )
                + 1
            )
        supersedes_id = feedback.get("supersedes_id")
        if supersedes_id:
            # v8 写入门禁：supersedes 只允许同线程（七元组线程身份一致），
            # 跨线程取代拒绝（账本线程历史不得交叉串联）
            self._ensure_supersedes_same_thread(
                conn, feedback, target, actor_ref, definition_id, supersedes_id
            )
        feedback_id = feedback.get("feedback_id") or uuid4().hex
        try:
            conn.execute(
                "INSERT INTO evaluation_feedback (feedback_id, actor_ref, "
                "definition_id, definition_version, target_service, "
                "target_kind, target_subject_id, target_subject_revision, "
                "target_json, value_json, comment_ref, revision, "
                "supersedes_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feedback_id,
                    actor_ref,
                    definition_id,
                    feedback.get("definition_version") or "",
                    target.get("service") or "",
                    target.get("kind") or "",
                    target.get("subject_id") or "",
                    target.get("subject_revision") or "",
                    _json_dumps(target),
                    _json_dumps(
                        value_payload if value_payload is not None else {}
                    ),
                    feedback.get("comment_ref"),
                    int(revision),
                    feedback.get("supersedes_id"),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint" in str(exc):
                if "feedback_id" in str(exc):
                    detail = (
                        "feedback_id 冲突（request-key 幂等键已绑定不同 "
                        "payload，或并发同 key 提交竞态）"
                    )
                else:
                    detail = (
                        "feedback revision 冲突（同 actor/target/definition/"
                        "revision 唯一）"
                    )
                raise EvaluationConflictError(
                    f"{detail}: {actor_ref}@{definition_id} "
                    f"revision={revision}: {exc}"
                ) from exc
            raise
        return feedback_id, int(revision)

    @staticmethod
    def _ensure_supersedes_same_thread(
        conn: sqlite3.Connection,
        feedback: Mapping[str, Any],
        target: Mapping[str, Any],
        actor_ref: Any,
        definition_id: Any,
        supersedes_id: str,
    ) -> None:
        """v8 写入门禁：supersedes 只允许同线程（七元组线程身份一致）。

        被取代行与新行必须同属 (actor_ref, target_service, target_kind,
        target_subject_id, target_subject_revision, definition_id,
        definition_version) 线程；跨线程取代抛 EvaluationConflictError。
        被取代行不存在时沿用账本容错语义（不强制外键，放行）。"""
        row = conn.execute(
            "SELECT actor_ref, target_service, target_kind, "
            "target_subject_id, target_subject_revision, definition_id, "
            "definition_version FROM evaluation_feedback "
            "WHERE feedback_id = ?",
            (supersedes_id,),
        ).fetchone()
        if row is None:
            return
        expected = (
            actor_ref,
            target.get("service") or "",
            target.get("kind") or "",
            target.get("subject_id") or "",
            target.get("subject_revision") or "",
            definition_id,
            feedback.get("definition_version") or "",
        )
        actual = (
            row["actor_ref"],
            row["target_service"],
            row["target_kind"],
            row["target_subject_id"],
            row["target_subject_revision"],
            row["definition_id"],
            row["definition_version"],
        )
        if actual != expected:
            raise EvaluationConflictError(
                "supersedes 跨线程拒绝（被取代行与新行线程身份不一致）: "
                f"supersedes_id={supersedes_id} "
                f"actor_ref={expected[0]!r} definition_id={expected[5]!r} "
                f"definition_version={expected[6]!r}"
            )

    @staticmethod
    def _row_to_feedback(row: sqlite3.Row) -> dict[str, Any]:
        feedback = _row_to_dict(row)
        feedback["target"] = _json_loads(feedback.pop("target_json"), {})
        feedback["value"] = _json_loads(feedback.pop("value_json"), {})
        return feedback

    async def get_feedback(self, feedback_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._get_feedback_sync, feedback_id)

    def _get_feedback_sync(self, feedback_id: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_feedback WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(f"feedback not found: {feedback_id}")
        return self._row_to_feedback(row)

    async def list_current_feedback(
        self,
        *,
        definition_id: str | None = None,
        limit: int = 1000,
    ) -> list[Mapping[str, Any]]:
        """人评 current 视图：每完整线程（v8 七元组：actor + target +
        subject_revision + definition_id + definition_version）只保留最新
        revision；删除撤销（tombstone）的行排除；不可变历史保留。"""
        return await asyncio.to_thread(
            self._list_current_feedback_sync, definition_id, limit
        )

    def _list_current_feedback_sync(
        self, definition_id: str | None, limit: int
    ) -> list[Mapping[str, Any]]:
        params: list[Any] = []
        # v8 完整线程（七元组）分组取最新 revision；降级索引下同线程同
        # revision 并存时按 created_at DESC, feedback_id DESC 决定性择一
        # （与 Task 1 dedupe_latest 口径一致，NOT EXISTS 实现；谓词与
        # 聚合/标注队列共用同一模板）。
        newer_row = _FEEDBACK_THREAD_LATEST_PREDICATE
        sql = (
            "SELECT f.* FROM evaluation_feedback f "
            f"WHERE {newer_row} "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'feedback' "
            "    AND t.subject_id = f.feedback_id)"
        )
        if definition_id is not None:
            sql += " AND f.definition_id = ?"
            params.append(definition_id)
        sql += " ORDER BY f.created_at, f.feedback_id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            # 降级非唯一索引存在时输出一次诊断计数日志（受限计数，
            # 不含 feedback 行内容）；进程内 once 标记：仅首次调用探测，
            # 之后跳过 sqlite_master 探测与全表 GROUP BY
            if not self._feedback_degraded_diag_checked:
                self._feedback_degraded_diag_checked = True
                if self._feedback_degraded_index_present(conn):
                    duplicate_rows = (
                        self._feedback_thread_duplicate_rows(conn)
                    )
                    logger.warning(
                        "feedback current 视图运行在降级非唯一索引 %s 上："
                        "检测到同线程同 revision 并存行 duplicate_rows=%d"
                        "（已按 created_at/feedback_id 决定性择一）",
                        _FEEDBACK_THREAD_NONUNIQUE_INDEX,
                        duplicate_rows,
                    )
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_feedback(row) for row in rows]

    async def get_latest_feedback(
        self,
        actor_ref: str,
        target_service: str,
        target_kind: str,
        target_subject_id: str,
        definition_id: str,
        *,
        target_subject_revision: str,
        definition_version: str,
    ) -> Mapping[str, Any] | None:
        """返回该完整线程（v8 七元组：actor + target + subject_revision +
        definition_id + definition_version）的最新 revision 行（无行返回
        None）。T5 服务层扩展：submit_feedback 的 expected_revision CAS
        读取与 supersedes 链构造都以本查询为唯一事实源；v8 起线程身份必
        须完整，keyword-only 参数缺一即 TypeError（禁止静默回退宽线程）。"""
        return await asyncio.to_thread(
            self._get_latest_feedback_sync,
            actor_ref,
            target_service,
            target_kind,
            target_subject_id,
            definition_id,
            target_subject_revision,
            definition_version,
        )

    def _get_latest_feedback_sync(
        self,
        actor_ref: str,
        target_service: str,
        target_kind: str,
        target_subject_id: str,
        definition_id: str,
        target_subject_revision: str,
        definition_version: str,
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_feedback WHERE actor_ref = ? "
                "AND target_service = ? AND target_kind = ? "
                "AND target_subject_id = ? AND definition_id = ? "
                "AND target_subject_revision = ? AND definition_version = ? "
                "ORDER BY revision DESC, created_at DESC, feedback_id DESC "
                "LIMIT 1",
                (
                    actor_ref,
                    target_service,
                    target_kind,
                    target_subject_id,
                    definition_id,
                    target_subject_revision,
                    definition_version,
                ),
            ).fetchone()
        return self._row_to_feedback(row) if row is not None else None

    async def get_result_id_by_feedback_id(self, feedback_id: str) -> str | None:
        """按 feedback_id 查询关联的 human result（UNIQUE(feedback_id) 保证
        至多一行）。T5 服务层扩展：新 revision 的 result 需要以
        ``supersedes_result_id`` 指向旧 feedback 的 result。"""
        return await asyncio.to_thread(
            self._get_result_id_by_feedback_id_sync, feedback_id
        )

    async def get_feedback_by_result_id(
        self, result_id: str
    ) -> Mapping[str, Any] | None:
        """按 human result 反查 feedback 行（T22 人评投递投影输入：target
        与 definition 身份挂在 feedback 上，result 行只有 metric 元数据）。

        返回含 ``tombstoned`` 标志（目标主体命中删除 tombstone 时 True，
        投递 worker 据此把投递改判 unlinked(subject_deleted)，与 T18
        "已删主体内容不新发送"门禁对等）。result 无关联 feedback（非
        human result）返回 None。"""
        return await asyncio.to_thread(
            self._get_feedback_by_result_id_sync, result_id
        )

    def _get_feedback_by_result_id_sync(
        self, result_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT f.* FROM evaluation_feedback f "
                "JOIN evaluation_results r ON r.feedback_id = f.feedback_id "
                "WHERE r.result_id = ?",
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        feedback = _row_to_dict(row)
        target = feedback.pop("target_json", None)
        if isinstance(target, str):
            feedback["target"] = _json_loads(target, {})
        elif isinstance(target, Mapping):
            feedback["target"] = dict(target)
        else:
            feedback["target"] = {}
        feedback["tombstoned"] = self._feedback_tombstoned_sync(feedback)
        return feedback

    def _feedback_tombstoned_sync(self, feedback: Mapping[str, Any]) -> bool:
        target = feedback.get("target") or {}
        with self._connect() as conn:
            return self._subject_blocked_by_tombstone(
                conn,
                str(target.get("kind") or ""),
                str(target.get("subject_id") or ""),
                str(target.get("subject_revision") or ""),
            )

    def _get_result_id_by_feedback_id_sync(
        self, feedback_id: str
    ) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_id FROM evaluation_results WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
        return row["result_id"] if row is not None else None

    # ------------------------------------------------------------------
    # Current view: automatic results
    # ------------------------------------------------------------------

    async def list_current_results(
        self,
        *,
        metric_name: str | None = None,
        limit: int = 1000,
    ) -> list[Mapping[str, Any]]:
        """自动结果 current 视图：排除被 ``supersedes_result_id`` 取代的
        旧值与删除撤销（tombstone）的结果；不可变历史保留，供投影/
        看板/分析共用（plan T10/T14/T21）。"""
        return await asyncio.to_thread(
            self._list_current_results_sync, metric_name, limit
        )

    def _list_current_results_sync(
        self, metric_name: str | None, limit: int
    ) -> list[Mapping[str, Any]]:
        sql = (
            "SELECT r.* FROM evaluation_results r "
            "WHERE r.job_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM evaluation_results s "
            "  WHERE s.supersedes_result_id = r.result_id) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'result' "
            "    AND t.subject_id = r.result_id)"
        )
        params: list[Any] = []
        if metric_name is not None:
            sql += " AND r.metric_name = ?"
            params.append(metric_name)
        sql += " ORDER BY r.created_at, r.result_id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_result(row) for row in rows]

    # ------------------------------------------------------------------
    # T10 gauge snapshot read-only queries（评估遥测 Gauge 数据源）
    # ------------------------------------------------------------------

    async def query_window_snapshot(
        self, *, window_start: str | datetime
    ) -> list[Mapping[str, Any]]:
        """最近窗口（spec 12.3：24h evaluated_at）的质量聚合快照。

        只读；current 视图（排除被 ``supersedes_result_id`` 取代的旧值与
        删除 tombstone 撤销的结果）。返回按
        (target_service, definition_id, definition_version, mode) 分组的
        行，字段：succeeded_count / skipped_count / error_count /
        unlinked_count / numeric_sum / numeric_count / boolean_pass /
        boolean_count。

        口径（文档化判定，plan T10）：
        - succeeded/skipped 来自 automatic result 行（status=value /
          skipped，即 EvaluationValue.status 词汇）；
        - error 不落 result 行（worker 直接终结 job），计入窗口内
          ``j.status='error'`` 的 job（按 updated_at，即终态迁移时间）；
        - unlinked 只统计 succeeded 结果中样本目标缺真实 trace 身份
          （32 位 hex trace_id + 16 位 hex span_id）的计数，与 T9 投递
          unlinked 投影同判；
        - mode：无 experiment_id 为 'online'，否则取实验行的 mode；
        - human（feedback）结果不进入本聚合（无 metric_profile 作业
          形态，质量口径走结果查询 API），文档化留待 T14 按需扩展。
        """
        return await asyncio.to_thread(
            self._query_window_snapshot_sync, window_start
        )

    def _query_window_snapshot_sync(
        self, window_start: str | datetime
    ) -> list[Mapping[str, Any]]:
        if isinstance(window_start, datetime):
            window_start = _iso(window_start)
        mode_expr = (
            "CASE WHEN j.experiment_id IS NULL THEN 'online' "
            "ELSE COALESCE(e.mode, 'online') END"
        )
        unlinked_expr = (
            "(COALESCE(json_extract(s.target_json, '$.trace_id'), '') "
            f"NOT GLOB '{_TRACE_ID_GLOB}' "
            "OR COALESCE(json_extract(s.target_json, '$.span_id'), '') "
            f"NOT GLOB '{_SPAN_ID_GLOB}')"
        )
        current_view = (
            "NOT EXISTS (SELECT 1 FROM evaluation_results sup "
            "WHERE sup.supersedes_result_id = r.result_id) "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t "
            "WHERE t.subject_kind = 'result' "
            "AND t.subject_id = r.result_id)"
        )
        # T18/Mi4：error 分支与 result 分支同口径——已删主体样本的 error
        # job 不计入窗口聚合（经 evidence_targets 反向索引复核 tombstone）
        error_view = (
            "NOT EXISTS (SELECT 1 FROM evaluation_evidence_targets et "
            "LEFT JOIN evaluation_deletion_tombstones t ON "
            + self._TOMBSTONE_MATCH_ON +
            " WHERE et.sample_id = j.sample_id "
            " AND t.tombstone_id IS NOT NULL)"
        )
        sql = (
            "SELECT target_service, definition_id, definition_version, mode, "
            "sampling_cohort, "
            "SUM(is_succeeded) AS succeeded_count, "
            "SUM(is_skipped) AS skipped_count, "
            "SUM(is_error) AS error_count, "
            "SUM(is_unlinked) AS unlinked_count, "
            "SUM(num_value) AS numeric_sum, "
            "SUM(is_numeric) AS numeric_count, "
            "SUM(is_boolean_pass) AS boolean_pass, "
            "SUM(is_boolean) AS boolean_count "
            "FROM ("
            # result 行（succeeded/skipped/unlinked/typed value 统计）
            "SELECT s.service AS target_service, "
            "j.definition_id AS definition_id, "
            "j.definition_version AS definition_version, "
            f"{mode_expr} AS mode, "
            "s.cohort AS sampling_cohort, "
            "CASE WHEN r.status = 'value' THEN 1 ELSE 0 END AS is_succeeded, "
            "CASE WHEN r.status = 'skipped' THEN 1 ELSE 0 END AS is_skipped, "
            "0 AS is_error, "
            f"CASE WHEN r.status = 'value' AND {unlinked_expr} "
            "THEN 1 ELSE 0 END AS is_unlinked, "
            "CASE WHEN json_type(r.typed_value_json) IN ('integer','real') "
            "THEN json_extract(r.typed_value_json, '$') ELSE 0 END AS num_value, "
            "CASE WHEN json_type(r.typed_value_json) IN ('integer','real') "
            "THEN 1 ELSE 0 END AS is_numeric, "
            "CASE WHEN json_type(r.typed_value_json) = 'true' "
            "THEN 1 ELSE 0 END AS is_boolean_pass, "
            "CASE WHEN json_type(r.typed_value_json) IN ('true','false') "
            "THEN 1 ELSE 0 END AS is_boolean "
            "FROM evaluation_results r "
            "JOIN evaluation_jobs j ON r.job_id = j.job_id "
            "JOIN evaluation_samples s ON j.sample_id = s.sample_id "
            "LEFT JOIN evaluation_experiments e "
            "ON j.experiment_id = e.experiment_id "
            f"WHERE r.evaluated_at >= ? AND {current_view} "
            "UNION ALL "
            # error job 行（无 result 行；按 updated_at 终态时间入窗）
            "SELECT s.service, j.definition_id, j.definition_version, "
            f"{mode_expr}, s.cohort, 0, 0, 1, 0, 0, 0, 0, 0 "
            "FROM evaluation_jobs j "
            "JOIN evaluation_samples s ON j.sample_id = s.sample_id "
            "LEFT JOIN evaluation_experiments e "
            "ON j.experiment_id = e.experiment_id "
            f"WHERE j.status = 'error' AND j.updated_at >= ? AND {error_view}"
            ") "
            "GROUP BY target_service, definition_id, definition_version, "
            "mode, sampling_cohort "
            "ORDER BY target_service, definition_id, definition_version, "
            "mode, sampling_cohort"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (window_start, window_start)).fetchall()
        return [
            {
                "target_service": row["target_service"],
                "definition_id": row["definition_id"],
                "definition_version": row["definition_version"],
                "mode": row["mode"],
                # 抽样 cohort（#24 分离维度；旧行/非捕获路径为空串）
                "sampling_cohort": str(row["sampling_cohort"] or ""),
                "succeeded_count": int(row["succeeded_count"] or 0),
                "skipped_count": int(row["skipped_count"] or 0),
                "error_count": int(row["error_count"] or 0),
                "unlinked_count": int(row["unlinked_count"] or 0),
                "numeric_sum": float(row["numeric_sum"] or 0.0),
                "numeric_count": int(row["numeric_count"] or 0),
                "boolean_pass": int(row["boolean_pass"] or 0),
                "boolean_count": int(row["boolean_count"] or 0),
            }
            for row in rows
        ]

    async def query_producer_snapshot(self) -> Mapping[str, Any]:
        """producer 高水位累计计数快照（跨 producer/counter epoch 汇总，
        覆盖率口径为"自当前 counter epoch 起"，spec 12.3）。只读。"""
        return await asyncio.to_thread(self._query_producer_snapshot_sync)

    def _query_producer_snapshot_sync(self) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(eligible_count), 0) AS eligible, "
                "COALESCE(SUM(selected_count), 0) AS selected, "
                "COALESCE(SUM(captured_count), 0) AS captured, "
                "COALESCE(SUM(capture_error_count), 0) AS capture_error, "
                "COALESCE(SUM(queue_full_count), 0) AS queue_full "
                "FROM evaluation_producer_state"
            ).fetchone()
        return {
            "eligible": int(row["eligible"] or 0),
            "selected": int(row["selected"] or 0),
            "captured": int(row["captured"] or 0),
            "capture_error": int(row["capture_error"] or 0),
            "queue_full": int(row["queue_full"] or 0),
        }

    async def experiment_job_status_counts(
        self, experiment_id: str
    ) -> dict[str, int]:
        """实验作业状态计数（CLI 门禁等待 #4：评分作业全部终态后才计算
        门禁，避免排队中作业被静默当作缺失分母）。只读。"""
        return await asyncio.to_thread(
            self._experiment_job_status_counts_sync, experiment_id
        )

    def _experiment_job_status_counts_sync(
        self, experiment_id: str
    ) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM evaluation_jobs "
                "WHERE experiment_id = ? GROUP BY status",
                (experiment_id,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    # ------------------------------------------------------------------
    # 本地 producer 捕获计数持久化（#28：重启不归零 + counter epoch）
    # ------------------------------------------------------------------

    async def load_capture_counters(self) -> Mapping[str, Any] | None:
        """读取本地 producer 捕获计数（单行）；未持久化过返回 None。"""
        return await asyncio.to_thread(self._load_capture_counters_sync)

    def _load_capture_counters_sync(self) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT counter_epoch, values_json FROM "
                "evaluation_capture_counters WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "counter_epoch": str(row["counter_epoch"]),
            "values": json.loads(row["values_json"] or "{}"),
        }

    async def save_capture_counters(
        self, values: Mapping[str, int], counter_epoch: str
    ) -> None:
        """幂等保存本地 producer 捕获计数（单行 upsert，整行覆盖）。"""
        await asyncio.to_thread(
            self._save_capture_counters_sync, dict(values), counter_epoch
        )

    def _save_capture_counters_sync(
        self, values: Mapping[str, int], counter_epoch: str
    ) -> None:
        now_str = _iso(_utcnow())
        values_json = json.dumps(
            {k: int(v) for k, v in values.items()}, sort_keys=True
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evaluation_capture_counters "
                "(id, counter_epoch, values_json, updated_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET counter_epoch = excluded.counter_epoch, "
                "values_json = excluded.values_json, updated_at = excluded.updated_at",
                (counter_epoch, values_json, now_str),
            )

    # ------------------------------------------------------------------
    # Paginated result query (T5 service extension)
    # ------------------------------------------------------------------

    async def aggregate_experiment_deliveries(
        self, experiment_id: str
    ) -> list[Mapping[str, Any]]:
        """实验维度投递聚合（T21 概览 M4：单次 JOIN + GROUP BY 代替逐行
        ``list_deliveries``，消除最坏 5000 次串行查询的 N+1）。

        返回 ``[{sink, status, count}]`` 行；结果行与 query_results_page
        一致地应用 current 语义（排除被 supersedes 取代与 tombstone），
        只统计 automatic 结果（experiment 过滤经 jobs）。"""
        return await asyncio.to_thread(
            self._aggregate_experiment_deliveries_sync, experiment_id
        )

    def _aggregate_experiment_deliveries_sync(
        self, experiment_id: str
    ) -> list[Mapping[str, Any]]:
        sql = (
            "SELECT d.sink AS sink, d.status AS status, COUNT(*) AS count "
            "FROM evaluation_deliveries d "
            "JOIN evaluation_results r ON d.result_id = r.result_id "
            "JOIN evaluation_jobs j ON r.job_id = j.job_id "
            "WHERE j.experiment_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_results sup "
            "  WHERE sup.supersedes_result_id = r.result_id) "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'result' "
            "  AND t.subject_id = r.result_id) "
            "GROUP BY d.sink, d.status "
            "ORDER BY d.sink, d.status"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (experiment_id,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def list_disputed_results(
        self, *, limit: int = 200
    ) -> list[Mapping[str, Any]]:
        """争议结果查询（T21 待复核队列）：同一 (metric, target subject)
        上自动结果与人评结果值冲突的自动结果行。

        值冲突判定：typed_value_json 字符串不等（boolean "true"/"false"
        与数值经同一 canonical JSON 序列化，字符串比较即值比较）。两侧
        均排除被取代与已 tombstone 行；同人多 actor 反馈产生的重复配对
        经 SQL GROUP BY result_id 在查询层去重，LIMIT 作用于去重后行数。"""
        return await asyncio.to_thread(self._list_disputed_results_sync, limit)

    def _list_disputed_results_sync(self, limit: int) -> list[Mapping[str, Any]]:
        # Mi3：去重在 SQL 层完成（GROUP BY a.result_id），LIMIT 作用于
        # 去重后行数，避免"先 LIMIT 再 Python 去重"导致的少返回
        sql = (
            "SELECT a.*, MIN(h.result_id) AS human_result_id "
            "FROM evaluation_results a "
            "JOIN evaluation_jobs j ON a.job_id = j.job_id "
            "JOIN evaluation_samples s ON j.sample_id = s.sample_id "
            "JOIN evaluation_feedback f "
            "  ON f.target_service = s.service "
            " AND f.target_kind = json_extract(s.target_json, '$.kind') "
            " AND f.target_subject_id = json_extract(s.target_json, "
            "      '$.subject_id') "
            "JOIN evaluation_results h ON h.feedback_id = f.feedback_id "
            "WHERE a.provenance = 'automatic' AND h.provenance = 'human' "
            "AND a.metric_name = h.metric_name "
            "AND a.status = 'value' AND h.status = 'value' "
            "AND a.typed_value_json IS NOT NULL "
            "AND h.typed_value_json IS NOT NULL "
            "AND a.typed_value_json != h.typed_value_json "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_results sup "
            "  WHERE sup.supersedes_result_id = a.result_id) "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_results sup "
            "  WHERE sup.supersedes_result_id = h.result_id) "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'result' "
            "  AND t.subject_id = a.result_id) "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'result' "
            "  AND t.subject_id = h.result_id) "
            "GROUP BY a.result_id "
            "ORDER BY a.created_at, a.result_id LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def list_producer_states(
        self, *, limit: int = 50
    ) -> list[Mapping[str, Any]]:
        """producer 状态高水位列表（T21 管理面概览：心跳新鲜度与累计
        计数展示）。按 observed_at 倒序。"""
        return await asyncio.to_thread(self._list_producer_states_sync, limit)

    def _list_producer_states_sync(self, limit: int) -> list[Mapping[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluation_producer_state "
                "ORDER BY observed_at DESC, producer_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def count_results_by_experiment(self) -> dict[str, int]:
        """按实验聚合 current 视图结果数（T21 管理面低覆盖提示）。

        排除被取代与 tombstone 行；experiment_id 为 NULL 的 online 结果
        不参与聚合。"""
        return await asyncio.to_thread(self._count_results_by_experiment_sync)

    def _count_results_by_experiment_sync(self) -> dict[str, int]:
        sql = (
            "SELECT j.experiment_id AS experiment_id, COUNT(*) AS total "
            "FROM evaluation_results r "
            "JOIN evaluation_jobs j ON r.job_id = j.job_id "
            "WHERE j.experiment_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_results sup "
            "  WHERE sup.supersedes_result_id = r.result_id) "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'result' "
            "  AND t.subject_id = r.result_id) "
            "GROUP BY j.experiment_id"
        )
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return {row["experiment_id"]: int(row["total"]) for row in rows}

    async def query_results_page(
        self,
        *,
        target_service: str | None = None,
        target_kind: str | None = None,
        target_subject_id: str | None = None,
        target_subject_revision: str | None = None,
        definition_id: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        definition_version: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
        provenance: str | None = None,
        snapshot_at: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """结果 keyset 分页查询（T5 服务层扩展；Task 4 扩展过滤 + 游标 v2）。

        - 覆盖全部 provenance 结果（current 语义：排除被
          ``supersedes_result_id`` 取代的旧值与删除 tombstone 行）；
        - target 过滤按 provenance 匹配：automatic 结果经 job→sample 的
          target 列，human 结果经 feedback 的 target 列；
        - definition_id / definition_version 过滤双侧匹配（automatic 经
          jobs，human 经 feedback）；experiment 过滤仅命中 automatic；
        - 新增过滤：``created_from``/``created_to``（UTC ISO，半开区间
          ``[from, to)``）、``definition_version``、``provenance``
          （automatic/human/calibration/self_judge），与既有过滤正交组合；
        - 游标：携带任一新增过滤或 v2 游标时启用游标 v2（不透明 token，
          内含过滤 fingerprint、快照上界与 keyset 边界；过滤变更后复用旧
          游标抛 EvaluationValidationError）。无新增过滤时保持旧行为：
          接受旧两段式 ``"{created_at}|{result_id}"`` 游标并输出同格式
          token（兼容分支不宣称完整快照保证）；旧游标 + 新增过滤拒绝；
        - ``snapshot_at`` 可显式固定快照上界（缺省取查询开始 UTC 时刻，
          编码进 v2 token，后续页复用）；
        - ``limit`` 封顶（200）仅作用于游标 v2 分支；兼容分支（无新增
          过滤、旧两段式游标）保持无封顶的旧行为；
        - 返回 ``(rows, next_cursor)``：还有下一页时 next_cursor 非空；
          ``limit`` 必须为正整数（EvaluationValidationError）。
        """
        filters = self._results_filters(
            target_service=target_service,
            target_kind=target_kind,
            target_subject_id=target_subject_id,
            target_subject_revision=target_subject_revision,
            definition_id=definition_id,
            created_from=created_from,
            created_to=created_to,
            definition_version=definition_version,
            experiment_id=experiment_id,
            status=status,
            provenance=provenance,
        )
        rows, next_cursor, _ = await asyncio.to_thread(
            self._query_results_page_sync,
            filters,
            snapshot_at,
            cursor,
            limit,
            True,
        )
        return rows, next_cursor

    async def query_results_page_snapshot(
        self,
        *,
        target_service: str | None = None,
        target_kind: str | None = None,
        target_subject_id: str | None = None,
        target_subject_revision: str | None = None,
        definition_id: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        definition_version: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
        provenance: str | None = None,
        snapshot_at: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[Mapping[str, Any]], str | None, str | None]:
        """带快照上界的结果分页查询（Task 4 新用例入口，游标 v2）。

        与 ``query_results_page`` 同一过滤语义；区别：
        - 返回 ``(rows, next_cursor, snapshot_at)`` 三元组——首次查询
          ``snapshot_at`` 取查询开始 UTC 时刻（``snapshot_at`` 参数可显式
          固定），作为该次分页的 UTC 上界（半开）编码进 v2 游标，后续页
          固定复用（不重取 now），翻页间新写入不漏进当前分页；
        - 仅接受 v2 游标（旧两段式游标拒绝）；fingerprint 绑定全部规范化
          过滤，过滤变更后携带旧游标抛 EvaluationValidationError；
        - ``limit`` 默认 50、最大 200（封顶）。
        """
        filters = self._results_filters(
            target_service=target_service,
            target_kind=target_kind,
            target_subject_id=target_subject_id,
            target_subject_revision=target_subject_revision,
            definition_id=definition_id,
            created_from=created_from,
            created_to=created_to,
            definition_version=definition_version,
            experiment_id=experiment_id,
            status=status,
            provenance=provenance,
        )
        return await asyncio.to_thread(
            self._query_results_page_sync,
            filters,
            snapshot_at,
            cursor,
            limit,
            False,
        )

    @staticmethod
    def _results_filters(
        *,
        target_service: str | None,
        target_kind: str | None,
        target_subject_id: str | None,
        target_subject_revision: str | None,
        definition_id: str | None,
        created_from: str | None,
        created_to: str | None,
        definition_version: str | None,
        experiment_id: str | None,
        status: str | None,
        provenance: str | None,
    ) -> dict[str, Any]:
        """结果分页过滤 dict（11 键与 _RESULTS_FILTER_KEYS 对齐，
        query_results_page / query_results_page_snapshot 共用）。"""
        return {
            "target_service": target_service,
            "target_kind": target_kind,
            "target_subject_id": target_subject_id,
            "target_subject_revision": target_subject_revision,
            "definition_id": definition_id,
            "created_from": created_from,
            "created_to": created_to,
            "definition_version": definition_version,
            "experiment_id": experiment_id,
            "status": status,
            "provenance": provenance,
        }

    def _query_results_page_sync(
        self,
        filters: Mapping[str, Any],
        snapshot_at: str | None,
        cursor: str | None,
        limit: int,
        legacy_compat: bool,
    ) -> tuple[list[Mapping[str, Any]], str | None, str | None]:
        """结果分页调度：兼容分支（旧行为）与游标 v2 分支择一执行。"""
        if limit <= 0:
            raise EvaluationValidationError("limit 必须为正整数")
        new_filtering = (
            filters["created_from"] is not None
            or filters["created_to"] is not None
            or filters["definition_version"] is not None
            or filters["provenance"] is not None
            or snapshot_at is not None
        )
        parsed = (
            _parse_result_cursor(cursor) if cursor is not None else None
        )
        if parsed is not None and parsed["kind"] == "legacy":
            # 旧两段式游标仅允许原结果接口且无新增过滤的兼容分支接受
            if not legacy_compat or new_filtering:
                raise EvaluationValidationError(
                    "旧格式 results 游标仅允许原结果接口且不得携带新增"
                    f"过滤: {cursor!r}"
                )
            return self._query_results_page_legacy_sync(filters, parsed, limit)
        if legacy_compat and not new_filtering and parsed is None:
            # 无新增过滤且未携带 v2 游标：保持旧行为（旧两段式 token，
            # 无快照上界，不宣称完整快照保证）
            return self._query_results_page_legacy_sync(filters, None, limit)
        fingerprint = _results_filter_fingerprint(
            **{key: filters[key] for key in _RESULTS_FILTER_KEYS}
        )
        if parsed is not None:
            if parsed["fp"] != fingerprint:
                raise EvaluationValidationError(
                    "results 游标与当前过滤条件不匹配（fingerprint 不一致），"
                    "请从首页重新分页"
                )
            snap = parsed["snap"]
            if snapshot_at is not None:
                if snap is not None and snap != snapshot_at:
                    raise EvaluationValidationError(
                        "snapshot_at 与游标内快照上界不一致"
                    )
                snap = snapshot_at
        else:
            snap = snapshot_at if snapshot_at is not None else _iso()
        return self._query_results_page_v2_sync(
            filters, fingerprint, snap, parsed, limit
        )

    @staticmethod
    def _results_current_view_conditions() -> list[str]:
        """结果 current 语义（排除被 supersedes 取代与删除 tombstone）。"""
        return [
            "NOT EXISTS (SELECT 1 FROM evaluation_results sup "
            "  WHERE sup.supersedes_result_id = r.result_id)",
            "NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'result' "
            "  AND t.subject_id = r.result_id)",
        ]

    @staticmethod
    def _results_page_filter_conditions(
        filters: Mapping[str, Any],
    ) -> tuple[list[str], list[Any]]:
        """既有过滤条件（status/experiment/definition_id/target 四维）。"""
        where = SQLiteEvaluationStore._results_current_view_conditions()
        params: list[Any] = []
        if filters["status"] is not None:
            where.append("r.status = ?")
            params.append(filters["status"])
        if filters["experiment_id"] is not None:
            where.append("j.experiment_id = ?")
            params.append(filters["experiment_id"])
        if filters["definition_id"] is not None:
            where.append(
                "((r.job_id IS NOT NULL AND j.definition_id = ?) "
                "OR (r.feedback_id IS NOT NULL AND f.definition_id = ?))"
            )
            params.extend([filters["definition_id"]] * 2)
        # sample 侧的 target 维度存于 target_json（无独立列），用 JSON1
        # 提取；feedback 侧为独立列
        target_filters = (
            ("s.service", "target_service"),
            ("json_extract(s.target_json, '$.kind')", "target_kind"),
            (
                "json_extract(s.target_json, '$.subject_id')",
                "target_subject_id",
            ),
            (
                "json_extract(s.target_json, '$.subject_revision')",
                "target_subject_revision",
            ),
        )
        active = [
            (sample_expr, feedback_col)
            for sample_expr, feedback_col in target_filters
            if filters[feedback_col] is not None
        ]
        if active:
            auto_conds = " AND ".join(f"{expr} = ?" for expr, _ in active)
            human_conds = " AND ".join(
                f"f.{col} = ?" for _, col in active
            )
            where.append(
                f"((r.job_id IS NOT NULL AND {auto_conds}) "
                f"OR (r.feedback_id IS NOT NULL AND {human_conds}))"
            )
            # OR 两侧各有一份占位符：先 auto 侧、后 feedback 侧
            active_values = [filters[col] for _, col in active]
            params.extend(active_values)
            params.extend(active_values)
        return where, params

    _RESULTS_PAGE_FROM_SQL = (
        "SELECT r.* FROM evaluation_results r "
        "LEFT JOIN evaluation_jobs j ON r.job_id = j.job_id "
        "LEFT JOIN evaluation_samples s ON j.sample_id = s.sample_id "
        "LEFT JOIN evaluation_feedback f ON r.feedback_id = f.feedback_id "
        "WHERE "
    )

    def _query_results_page_legacy_sync(
        self,
        filters: Mapping[str, Any],
        parsed: Mapping[str, Any] | None,
        limit: int,
    ) -> tuple[list[Mapping[str, Any]], str | None, str | None]:
        """兼容分支：T5 旧行为（旧两段式游标，无快照上界）。"""
        where, params = self._results_page_filter_conditions(filters)
        if parsed is not None:
            where.append("(r.created_at, r.result_id) > (?, ?)")
            params.extend([parsed["created_at"], parsed["result_id"]])
        sql = (
            self._RESULTS_PAGE_FROM_SQL + " AND ".join(where) +
            " ORDER BY r.created_at, r.result_id LIMIT ?"
        )
        params.append(limit + 1)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = f"{last['created_at']}|{last['result_id']}"
        return [self._row_to_result(row) for row in rows], next_cursor, None

    def _query_results_page_v2_sync(
        self,
        filters: Mapping[str, Any],
        fingerprint: str,
        snap: str,
        parsed: Mapping[str, Any] | None,
        limit: int,
    ) -> tuple[list[Mapping[str, Any]], str | None, str | None]:
        """游标 v2 分支：完整过滤 + 快照上界 + fingerprint 绑定。"""
        limit = min(limit, _RESULTS_PAGE_MAX_LIMIT)
        where, params = self._results_page_filter_conditions(filters)
        if filters["definition_version"] is not None:
            # 双侧匹配：automatic 经 jobs.definition_version，human 经
            # feedback.definition_version
            where.append(
                "((r.job_id IS NOT NULL AND j.definition_version = ?) "
                "OR (r.feedback_id IS NOT NULL "
                "AND f.definition_version = ?))"
            )
            params.extend([filters["definition_version"]] * 2)
        if filters["provenance"] is not None:
            where.append("r.provenance = ?")
            params.append(filters["provenance"])
        if filters["created_from"] is not None:
            where.append("r.created_at >= ?")
            params.append(filters["created_from"])
        if filters["created_to"] is not None:
            where.append("r.created_at < ?")
            params.append(filters["created_to"])
        # 快照上界（半开）：绑定该次分页的 UTC 上界，后续页复用 token 内值
        where.append("r.created_at < ?")
        params.append(snap)
        if parsed is not None:
            where.append("(r.created_at, r.result_id) > (?, ?)")
            params.extend([parsed["created_at"], parsed["result_id"]])
        sql = (
            self._RESULTS_PAGE_FROM_SQL + " AND ".join(where) +
            " ORDER BY r.created_at, r.result_id LIMIT ?"
        )
        params.append(limit + 1)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_result_cursor_v2(
                fingerprint, snap, last["created_at"], last["result_id"]
            )
        return [self._row_to_result(row) for row in rows], next_cursor, snap

    async def aggregate_metric_results(
        self,
        *,
        created_from: str | None = None,
        created_to: str | None = None,
        target_kind: str | None = None,
        definition_id: str | None = None,
        definition_version: str | None = None,
        provenance: str | None = None,
        status: str | None = None,
    ) -> list[Mapping[str, Any]]:
        """结果指标聚合（Task 4 统计口径）：服务端完成过滤后聚合，禁止
        依赖前端最近 50 条。

        - 过滤（均为 None 时不过滤）：``created_from``/``created_to``
          （UTC ISO，半开区间 ``[from, to)``，作用于 ``created_at``）、
          ``target_kind``（automatic 经 sample target_json、human 经
          feedback）、``definition_id``/``definition_version``、
          ``provenance``、``status``；
        - current 语义：automatic 分支沿用 supersedes NOT EXISTS +
          result tombstone；human 分支先按完整线程（v8 七元组）取最高
          revision/created_at/feedback_id 的 feedback 再过滤时间与状态
          （窗外新修订不使旧值复活），旧跨线程 supersedes 不能隐藏另一
          线程（不依赖 result supersedes 链），tombstone 规则沿用；
        - 输出按 ``(definition_id, definition_version, provenance,
          output_type)`` 分组（不同 version/provenance 不混键），度量：
          numeric_sum/numeric_count/numeric_min/numeric_max、
          boolean_pass/boolean_count、各 status 计数、UTC+8 逐日分布
          （与 domain ``utc8_day_bucket`` 同口径）、categorical 观测值
          计数与 total_count；
        - SQL 模板与 ``query_window_snapshot`` 同风格（UNION ALL 分支 +
          current 视图谓词）；output_type 经 definitions JOIN 提取。
        """
        filters = {
            "created_from": created_from,
            "created_to": created_to,
            "target_kind": target_kind,
            "definition_id": definition_id,
            "definition_version": definition_version,
            "provenance": provenance,
            "status": status,
        }
        return await asyncio.to_thread(
            self._aggregate_metric_results_sync, filters
        )

    def _aggregate_metric_results_sync(
        self, filters: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        auto_sql, auto_params = self._aggregate_grain_branch_sql(
            "automatic", filters
        )
        human_sql, human_params = self._aggregate_grain_branch_sql(
            "human", filters
        )
        # 聚合在各分支内完成（GROUP BY 用显式表达式，避免 SQLite 对
        # 复合子查询 + 别名 GROUP BY 的展平歧义）；外层只做 definition
        # 过滤与排序，不再二次聚合
        outer_where: list[str] = []
        outer_params: list[Any] = []
        if filters["definition_id"] is not None:
            outer_where.append("definition_id = ?")
            outer_params.append(filters["definition_id"])
        if filters["definition_version"] is not None:
            outer_where.append("definition_version = ?")
            outer_params.append(filters["definition_version"])
        sql = (
            "SELECT * FROM ( " + auto_sql + " UNION ALL " + human_sql + " ) "
        )
        if outer_where:
            sql += "WHERE " + " AND ".join(outer_where) + " "
        sql += (
            "ORDER BY definition_id, definition_version, provenance, "
            "output_type, status, day8"
        )
        with self._connect() as conn:
            rows = conn.execute(
                sql, auto_params + human_params + outer_params
            ).fetchall()

        # 分组聚合：粒度 (definition, version, provenance, output_type,
        # status, UTC+8 日, typed_value_json) -> count，Python 侧合并为
        # 分组度量（值语义判断与 domain summarize 同规则：bool 优先于
        # number 判定）
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["definition_id"],
                row["definition_version"],
                row["provenance"],
                row["output_type"],
            )
            group = groups.get(key)
            if group is None:
                group = {
                    "definition_id": key[0],
                    "definition_version": key[1],
                    "provenance": key[2],
                    "output_type": key[3],
                    "numeric_sum": 0.0,
                    "numeric_count": 0,
                    "numeric_min": None,
                    "numeric_max": None,
                    "boolean_pass": 0,
                    "boolean_count": 0,
                    "status_counts": {"value": 0, "skipped": 0, "error": 0},
                    "daily_counts": {},
                    "categorical_counts": {},
                    "total_count": 0,
                }
                groups[key] = group
            count = int(row["cnt"])
            group["total_count"] += count
            row_status = row["status"]
            if row_status in group["status_counts"]:
                group["status_counts"][row_status] += count
            day = row["day8"]
            if day:
                group["daily_counts"][day] = (
                    group["daily_counts"].get(day, 0) + count
                )
            raw = row["typed_value_json"]
            if raw:
                value = json.loads(raw)
                if isinstance(value, bool):
                    group["boolean_count"] += count
                    if value:
                        group["boolean_pass"] += count
                elif isinstance(value, (int, float)):
                    group["numeric_count"] += count
                    group["numeric_sum"] += float(value) * count
                    if (
                        group["numeric_min"] is None
                        or value < group["numeric_min"]
                    ):
                        group["numeric_min"] = float(value)
                    if (
                        group["numeric_max"] is None
                        or value > group["numeric_max"]
                    ):
                        group["numeric_max"] = float(value)
                elif (
                    row_status == "value"
                    and key[3] == "categorical"
                    and isinstance(value, str)
                ):
                    group["categorical_counts"][value] = (
                        group["categorical_counts"].get(value, 0) + count
                    )
        return [groups[key] for key in sorted(groups)]

    @staticmethod
    def _human_current_view_conditions() -> list[str]:
        """human 分支 current 视图（聚合与标注队列共用）：完整线程先取
        最高 revision（窗外新修订不使旧值复活）；不依赖 result supersedes
        链（旧跨线程 supersedes 不能隐藏另一线程），tombstone 规则沿用
        （feedback + result 双向排除）。"""
        return [
            "r.feedback_id IS NOT NULL",
            _FEEDBACK_THREAD_LATEST_PREDICATE,
            "NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones tf"
            "  WHERE tf.subject_kind = 'feedback' "
            "  AND tf.subject_id = f.feedback_id)",
            "NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t "
            "  WHERE t.subject_kind = 'result' "
            "  AND t.subject_id = r.result_id)",
        ]

    def _aggregate_grain_branch_sql(
        self, branch: str, filters: Mapping[str, Any]
    ) -> tuple[str, list[Any]]:
        """聚合粒度查询分支（automatic/human），返回 (SQL, params)。

        聚合在分支内完成：粒度 (definition_id, definition_version,
        provenance, output_type, status, UTC+8 日, typed_value_json) ->
        COUNT(*)；GROUP BY 使用显式表达式（不用别名），避免 SQLite 对
        复合子查询展平后别名解析的歧义。"""
        params: list[Any] = []
        if branch == "automatic":
            from_sql = (
                "FROM evaluation_results r "
                "JOIN evaluation_jobs j ON r.job_id = j.job_id "
                "JOIN evaluation_samples s ON j.sample_id = s.sample_id "
                "LEFT JOIN evaluation_definitions d "
                "  ON d.definition_id = j.definition_id "
                " AND d.version = j.definition_version "
            )
            where = [
                "r.job_id IS NOT NULL",
                *self._results_current_view_conditions(),
            ]
            target_kind_cond = "json_extract(s.target_json, '$.kind') = ?"
            definition_id_expr = "j.definition_id"
            definition_version_expr = "j.definition_version"
        else:
            from_sql = (
                "FROM evaluation_results r "
                "JOIN evaluation_feedback f ON r.feedback_id = f.feedback_id "
                "LEFT JOIN evaluation_definitions d "
                "  ON d.definition_id = f.definition_id "
                " AND d.version = f.definition_version "
            )
            # human current 视图（与标注队列分支共用同一谓词模板）
            where = [*self._human_current_view_conditions()]
            target_kind_cond = "f.target_kind = ?"
            definition_id_expr = "f.definition_id"
            definition_version_expr = "f.definition_version"
        if filters["status"] is not None:
            where.append("r.status = ?")
            params.append(filters["status"])
        if filters["provenance"] is not None:
            where.append("r.provenance = ?")
            params.append(filters["provenance"])
        if filters["created_from"] is not None:
            where.append("r.created_at >= ?")
            params.append(filters["created_from"])
        if filters["created_to"] is not None:
            where.append("r.created_at < ?")
            params.append(filters["created_to"])
        if filters["target_kind"] is not None:
            where.append(target_kind_cond)
            params.append(filters["target_kind"])
        output_type_expr = (
            "COALESCE(json_extract(d.definition_json, '$.output_type'), '')"
        )
        day8_expr = "substr(datetime(r.created_at, '+8 hours'), 1, 10)"
        select_sql = (
            f"SELECT {definition_id_expr} AS definition_id, "
            f"{definition_version_expr} AS definition_version, "
            "r.provenance AS provenance, "
            f"{output_type_expr} AS output_type, "
            "r.status AS status, "
            # UTC+8 自然日分桶（运营时区），与 domain utc8_day_bucket 同口径
            f"{day8_expr} AS day8, "
            "r.typed_value_json AS typed_value_json, "
            "COUNT(*) AS cnt "
        )
        return (
            select_sql + from_sql + "WHERE " + " AND ".join(where) + " "
            "GROUP BY " + ", ".join((
                definition_id_expr,
                definition_version_expr,
                "r.provenance",
                output_type_expr,
                "r.status",
                day8_expr,
                "r.typed_value_json",
            )),
            params,
        )

    async def list_annotation_queue(
        self,
        *,
        state: str,
        reason: str,
        threshold: float = _ANNOTATION_DEFAULT_THRESHOLD,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """标注队列表查询（Task 4）：state（all/pending/annotated）与
        reason（low_score/disputed/anomalous）必须在分页前应用（SQL 阶段
        完成，不以旧队列最近 50 条作候选集），按原结果 ``created_at DESC,
        result_id`` 稳定排序 keyset 分页。

        - ``low_score``：status=value 且（boolean False 或 numeric <
          ``threshold``，threshold 参数化，参考 evaluation_review 判定）；
        - ``anomalous``：status 为 error/skipped；
        - ``disputed``：同 (metric_name, target) 上自动与人工值冲突
          （复用 ``list_disputed_results`` 查询思路，EXISTS 化）；
        - "已标注"判定：actor_ref='dashboard' 的完整线程 latest 人评存在
          且 ``feedback.created_at >= source_result.created_at``（EXISTS
          子查询；口径与 spec 4.4 一致，Application 层组装最终语义，本
          store 只提供行级事实）；
        - 每行附 source_result_id/target 四元组/definition_id+version/
          output_type/typed_value/status/evaluated_at/
          latest_human_feedback（含 revision, created_at, value）；
        - 候选集为 current 视图（排除被取代与 tombstone；human 侧按完整
          线程取 latest）；游标为 v2 不透明 token（fingerprint 绑定
          state/reason/threshold），过滤变更后复用旧游标抛
          EvaluationValidationError；``limit`` 默认 50、最大 200。
        """
        # threshold 校验在 async 入口完成（bool 拒绝、非数值/非有限浮点
        # 抛 EvaluationValidationError；合法数字字符串如 "0.8" 与 0.8
        # 行为一致），禁止裸 ValueError 泄漏或 bool 静默转 1.0
        if isinstance(threshold, bool):
            raise EvaluationValidationError(
                f"threshold 必须是有限数值: {threshold!r}"
            )
        try:
            threshold_value = float(threshold)
        except (TypeError, ValueError):
            raise EvaluationValidationError(
                f"threshold 必须是有限数值: {threshold!r}"
            ) from None
        if not math.isfinite(threshold_value):
            raise EvaluationValidationError(
                f"threshold 必须是有限数值: {threshold!r}"
            )
        return await asyncio.to_thread(
            self._list_annotation_queue_sync,
            state,
            reason,
            threshold_value,
            limit,
            cursor,
        )

    def _list_annotation_queue_sync(
        self,
        state: str,
        reason: str,
        threshold: float,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        if state not in _ANNOTATION_STATES:
            raise EvaluationValidationError(
                f"非法标注队列 state: {state!r}，仅允许 {_ANNOTATION_STATES}"
            )
        if reason not in _ANNOTATION_REASONS:
            raise EvaluationValidationError(
                f"非法标注队列 reason: {reason!r}，仅允许 "
                f"{_ANNOTATION_REASONS}"
            )
        if isinstance(threshold, bool) or not math.isfinite(threshold):
            raise EvaluationValidationError("threshold 必须是有限数值")
        if limit <= 0:
            raise EvaluationValidationError("limit 必须为正整数")
        limit = min(limit, _RESULTS_PAGE_MAX_LIMIT)
        fingerprint = _results_filter_fingerprint(
            state=state, reason=reason, threshold=threshold
        )
        boundary: tuple[str, str] | None = None
        if cursor is not None:
            parsed = _parse_result_cursor(cursor)
            if parsed["kind"] != "v2":
                raise EvaluationValidationError(
                    f"标注队列游标格式非法: {cursor!r}"
                )
            if parsed["fp"] != fingerprint:
                raise EvaluationValidationError(
                    "标注队列游标与当前 state/reason/threshold 不匹配"
                    "（fingerprint 不一致），请从首页重新分页"
                )
            boundary = (parsed["created_at"], parsed["result_id"])

        auto_sql = self._annotation_candidates_branch_sql("automatic")
        human_sql = self._annotation_candidates_branch_sql("human")
        conditions: list[str] = []
        params: list[Any] = []
        if reason == "low_score":
            conditions.append(
                "c.status = 'value' AND c.typed_value_json IS NOT NULL "
                "AND (json_type(c.typed_value_json) = 'false' "
                "OR (json_type(c.typed_value_json) IN ('integer','real') "
                "AND json_extract(c.typed_value_json, '$') < ?))"
            )
            params.append(threshold)
        elif reason == "anomalous":
            conditions.append("c.status IN ('error', 'skipped')")
        else:  # disputed
            conditions.append(
                "c.provenance = 'automatic' AND EXISTS ("
                "SELECT 1 FROM evaluation_results h "
                "JOIN evaluation_feedback hf ON h.feedback_id = hf.feedback_id "
                "WHERE h.provenance = 'human' "
                "AND h.metric_name = c.metric_name "
                "AND h.status = 'value' AND c.status = 'value' "
                "AND h.typed_value_json IS NOT NULL "
                "AND c.typed_value_json IS NOT NULL "
                "AND h.typed_value_json != c.typed_value_json "
                "AND hf.target_service = c.target_service "
                "AND hf.target_kind = c.target_kind "
                "AND hf.target_subject_id = c.target_subject_id "
                "AND NOT EXISTS (SELECT 1 FROM evaluation_results sup "
                "  WHERE sup.supersedes_result_id = h.result_id) "
                "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones"
                "  ht WHERE ht.subject_kind = 'result' "
                "  AND ht.subject_id = h.result_id))"
            )
        annotated_exists = (
            "EXISTS (SELECT 1 FROM evaluation_feedback df "
            "WHERE df.actor_ref = ? "
            "AND df.target_service = c.target_service "
            "AND df.target_kind = c.target_kind "
            "AND df.target_subject_id = c.target_subject_id "
            "AND df.target_subject_revision = c.target_subject_revision "
            "AND df.definition_id = c.definition_id "
            "AND df.definition_version = c.definition_version "
            "AND df.created_at >= c.created_at "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_deletion_tombstones dt "
            "  WHERE dt.subject_kind = 'feedback' "
            "  AND dt.subject_id = df.feedback_id) "
            "AND " + _feedback_thread_latest_predicate("df") + ")"
        )
        if state == "annotated":
            conditions.append(annotated_exists)
            params.append(_ANNOTATION_DASHBOARD_ACTOR)
        elif state == "pending":
            conditions.append("NOT " + annotated_exists)
            params.append(_ANNOTATION_DASHBOARD_ACTOR)
        if boundary is not None:
            conditions.append("(c.created_at, c.result_id) < (?, ?)")
            params.extend(boundary)
        sql = (
            "SELECT c.*, ("
            + _LATEST_FEEDBACK_JSON_SUBQUERY
            + ") AS latest_feedback_json "
            "FROM ( " + auto_sql + " UNION ALL " + human_sql + " ) AS c "
            "WHERE " + " AND ".join(conditions) +
            " ORDER BY c.created_at DESC, c.result_id DESC LIMIT ?"
        )
        params.append(limit + 1)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_result_cursor_v2(
                fingerprint, None, last["created_at"], last["result_id"]
            )
        items = [
            {
                "source_result_id": row["result_id"],
                "created_at": row["created_at"],
                "provenance": row["provenance"],
                "metric_name": row["metric_name"],
                "status": row["status"],
                "typed_value": _json_loads(row["typed_value_json"], None),
                "evaluated_at": row["evaluated_at"],
                "target_service": row["target_service"],
                "target_kind": row["target_kind"],
                "target_subject_id": row["target_subject_id"],
                "target_subject_revision": row["target_subject_revision"],
                "definition_id": row["definition_id"],
                "definition_version": row["definition_version"],
                "output_type": row["output_type"],
                "latest_human_feedback": (
                    json.loads(row["latest_feedback_json"])
                    if row["latest_feedback_json"]
                    else None
                ),
            }
            for row in rows
        ]
        return items, next_cursor

    def _annotation_candidates_branch_sql(self, branch: str) -> str:
        """标注队列候选集分支（automatic/human），current 视图。"""
        if branch == "automatic":
            from_sql = (
                "FROM evaluation_results r "
                "JOIN evaluation_jobs j ON r.job_id = j.job_id "
                "JOIN evaluation_samples s ON j.sample_id = s.sample_id "
                "LEFT JOIN evaluation_definitions d "
                "  ON d.definition_id = j.definition_id "
                " AND d.version = j.definition_version "
            )
            where = [
                "r.job_id IS NOT NULL",
                *self._results_current_view_conditions(),
            ]
            target_columns = (
                "s.service AS target_service, "
                "json_extract(s.target_json, '$.kind') AS target_kind, "
                "json_extract(s.target_json, '$.subject_id') "
                "  AS target_subject_id, "
                "json_extract(s.target_json, '$.subject_revision') "
                "  AS target_subject_revision, "
            )
            definition_columns = (
                "j.definition_id AS definition_id, "
                "j.definition_version AS definition_version, "
            )
        else:
            from_sql = (
                "FROM evaluation_results r "
                "JOIN evaluation_feedback f ON r.feedback_id = f.feedback_id "
                "LEFT JOIN evaluation_definitions d "
                "  ON d.definition_id = f.definition_id "
                " AND d.version = f.definition_version "
            )
            where = [*self._human_current_view_conditions()]
            target_columns = (
                "f.target_service AS target_service, "
                "f.target_kind AS target_kind, "
                "f.target_subject_id AS target_subject_id, "
                "f.target_subject_revision AS target_subject_revision, "
            )
            definition_columns = (
                "f.definition_id AS definition_id, "
                "f.definition_version AS definition_version, "
            )
        return (
            "SELECT r.result_id AS result_id, "
            "r.created_at AS created_at, "
            "r.provenance AS provenance, "
            "r.metric_name AS metric_name, "
            "r.status AS status, "
            "r.typed_value_json AS typed_value_json, "
            "r.evaluated_at AS evaluated_at, "
            + target_columns
            + definition_columns
            + "COALESCE(json_extract(d.definition_json, '$.output_type'), "
            "'') AS output_type "
            + from_sql
            + "WHERE " + " AND ".join(where)
        )

    # ------------------------------------------------------------------
    # Sink configs
    # ------------------------------------------------------------------

    async def save_sink_config(self, config: Mapping[str, Any]) -> None:
        return await asyncio.to_thread(self._save_sink_config_sync, config)

    def _save_sink_config_sync(self, config: Mapping[str, Any]) -> None:
        for key in ("sink", "project", "definition_version",
                    "external_score_config_id"):
            if not config.get(key):
                raise ValueError(f"sink_config 需要 {key}")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evaluation_sink_configs (sink, project, "
                "definition_version, external_score_config_id, schema_hash, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sink, project, definition_version) DO UPDATE SET "
                "external_score_config_id = excluded.external_score_config_id, "
                "schema_hash = excluded.schema_hash, "
                "updated_at = excluded.updated_at",
                (
                    config["sink"],
                    config["project"],
                    config["definition_version"],
                    config["external_score_config_id"],
                    config.get("schema_hash") or "",
                    _iso(),
                ),
            )
            conn.commit()

    async def get_sink_config(
        self, sink: str, project: str, definition_version: str
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self._get_sink_config_sync, sink, project, definition_version
        )

    def _get_sink_config_sync(
        self, sink: str, project: str, definition_version: str
    ) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_sink_configs WHERE sink = ? "
                "AND project = ? AND definition_version = ?",
                (sink, project, definition_version),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(
                f"sink_config not found: {sink}/{project}/{definition_version}"
            )
        return _row_to_dict(row)

    # ------------------------------------------------------------------
    # Producer state (high-watermark)
    # ------------------------------------------------------------------

    async def save_producer_state(self, record: Mapping[str, Any]) -> None:
        return await asyncio.to_thread(self._save_producer_state_sync, record)

    def _save_producer_state_sync(self, record: Mapping[str, Any]) -> None:
        producer_id = record.get("producer_id")
        counter_epoch = record.get("counter_epoch")
        sequence = record.get("sequence")
        if not producer_id or counter_epoch is None or sequence is None:
            raise ValueError(
                "producer_state 需要 producer_id/counter_epoch/sequence"
            )
        sequence = int(sequence)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT sequence FROM evaluation_producer_state "
                    "WHERE producer_id = ? AND counter_epoch = ?",
                    (producer_id, counter_epoch),
                ).fetchone()
                if row is not None and sequence <= row["sequence"]:
                    conn.rollback()
                    raise EvaluationStaleSequenceError(
                        f"producer_state 只接受更高 sequence: "
                        f"producer={producer_id} epoch={counter_epoch} "
                        f"当前={row['sequence']} 收到={sequence}"
                    )
                conn.execute(
                    "INSERT INTO evaluation_producer_state (producer_id, "
                    "counter_epoch, sequence, eligible_count, selected_count, "
                    "captured_count, capture_error_count, queue_full_count, "
                    "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(producer_id, counter_epoch) DO UPDATE SET "
                    "sequence = excluded.sequence, "
                    "eligible_count = excluded.eligible_count, "
                    "selected_count = excluded.selected_count, "
                    "captured_count = excluded.captured_count, "
                    "capture_error_count = excluded.capture_error_count, "
                    "queue_full_count = excluded.queue_full_count, "
                    "observed_at = excluded.observed_at",
                    (
                        producer_id,
                        counter_epoch,
                        sequence,
                        int(record.get("eligible_count") or 0),
                        int(record.get("selected_count") or 0),
                        int(record.get("captured_count") or 0),
                        int(record.get("capture_error_count") or 0),
                        int(record.get("queue_full_count") or 0),
                        _iso(record.get("observed_at"))
                        if isinstance(record.get("observed_at"), datetime)
                        else (record.get("observed_at") or _iso()),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    async def get_producer_state(
        self, producer_id: str, counter_epoch: str
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self._get_producer_state_sync, producer_id, counter_epoch
        )

    def _get_producer_state_sync(
        self, producer_id: str, counter_epoch: str
    ) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_producer_state "
                "WHERE producer_id = ? AND counter_epoch = ?",
                (producer_id, counter_epoch),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(
                f"producer_state not found: {producer_id}/{counter_epoch}"
            )
        return _row_to_dict(row)

    # ------------------------------------------------------------------
    # Snapshot reference registry (T4 EvidenceStore support)
    # ------------------------------------------------------------------

    async def list_samples_by_snapshot_ref(
        self, snapshot_ref: str
    ) -> list[Mapping[str, Any]]:
        """按 snapshot_ref 查询登记的引用行（T4 回收/到期撤销与读取所需
        的最小查询；行形态与 load_sample 一致，按 created_at, sample_id
        稳定排序）。"""
        return await asyncio.to_thread(
            self._list_samples_by_snapshot_ref_sync, snapshot_ref
        )

    def _list_samples_by_snapshot_ref_sync(
        self, snapshot_ref: str
    ) -> list[Mapping[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluation_samples WHERE snapshot_ref = ? "
                "ORDER BY created_at, sample_id",
                (snapshot_ref,),
            ).fetchall()
        samples = []
        for row in rows:
            sample = _row_to_dict(row)
            sample["target"] = _json_loads(sample.pop("target_json"), {})
            sample.pop("payload_hash", None)
            samples.append(sample)
        return samples

    async def clear_sample_snapshot_ref(
        self, sample_id: str, snapshot_ref: str
    ) -> bool:
        """CAS 清除 sample 行的快照内容引用（T4 到期撤销流程：仅当当前
        snapshot_ref 与传入值一致时清空 ref/hash，返回是否清除）。"""
        return await asyncio.to_thread(
            self._clear_sample_snapshot_ref_sync, sample_id, snapshot_ref
        )

    def _clear_sample_snapshot_ref_sync(
        self, sample_id: str, snapshot_ref: str
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE evaluation_samples SET snapshot_ref = '', "
                "snapshot_hash = '' WHERE sample_id = ? AND snapshot_ref = ?",
                (sample_id, snapshot_ref),
            ).rowcount
            conn.commit()
        return updated == 1

    async def revoke_queued_jobs_for_samples(
        self,
        sample_ids: Sequence[str],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """撤销无法继续执行的排队 job（queued -> cancelled，经 domain
        state.py 校验，CAS 限定 status='queued'），返回被撤销的 job_id。
        running/终态 job 不受影响：running 由 lease 约束可自然收敛，
        终态不可迁移。"""
        return await asyncio.to_thread(
            self._revoke_queued_jobs_for_samples_sync, list(sample_ids), now
        )

    def _revoke_queued_jobs_for_samples_sync(
        self, sample_ids: list[str], now: datetime | None
    ) -> list[str]:
        now_str = _iso(now)
        cancelled = transition_job_status(
            EvaluationJobStatus.queued, EvaluationJobStatus.cancelled
        ).value
        revoked: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sample_id in sample_ids:
                rows = conn.execute(
                    "SELECT job_id FROM evaluation_jobs "
                    "WHERE sample_id = ? AND status = 'queued'",
                    (sample_id,),
                ).fetchall()
                for row in rows:
                    updated = conn.execute(
                        "UPDATE evaluation_jobs SET status = ?, "
                        "lease_expiry = NULL, updated_at = ? "
                        "WHERE job_id = ? AND status = 'queued'",
                        (cancelled, now_str, row["job_id"]),
                    ).rowcount
                    if updated == 1:
                        revoked.append(row["job_id"])
            conn.commit()
        return revoked

    # ------------------------------------------------------------------
    # Deletion tombstones (T18 prerequisite)
    # ------------------------------------------------------------------

    async def record_deletion_tombstone(
        self, tombstone: Mapping[str, Any]
    ) -> str:
        """幂等写入删除 tombstone：同 (kind, id, revision) 重复删除事件
        返回原 tombstone_id，不产生重复行。"""
        return await asyncio.to_thread(
            self._record_deletion_tombstone_sync, tombstone
        )

    def _record_deletion_tombstone_sync(
        self, tombstone: Mapping[str, Any]
    ) -> str:
        subject_kind = tombstone.get("subject_kind")
        subject_id = tombstone.get("subject_id")
        if not subject_kind or not subject_id:
            raise ValueError("tombstone 需要 subject_kind 与 subject_id")
        subject_revision = tombstone.get("subject_revision") or ""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT tombstone_id FROM evaluation_deletion_tombstones "
                "WHERE subject_kind = ? AND subject_id = ? "
                "AND subject_revision = ?",
                (subject_kind, subject_id, subject_revision),
            ).fetchone()
            if row is not None:
                conn.commit()
                return row["tombstone_id"]
            tombstone_id = tombstone.get("tombstone_id") or uuid4().hex
            conn.execute(
                "INSERT INTO evaluation_deletion_tombstones (tombstone_id, "
                "subject_kind, subject_id, subject_revision, reason, "
                "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tombstone_id,
                    subject_kind,
                    subject_id,
                    subject_revision,
                    tombstone.get("reason") or "",
                    _json_dumps(tombstone.get("payload") or {}),
                    _iso(),
                ),
            )
            conn.commit()
            return tombstone_id

    async def record_deletion_tombstones_batch(
        self, tombstones: Sequence[Mapping[str, Any]]
    ) -> list[bool]:
        """批量幂等写入删除 tombstone（T18/N1：级联合并为单事务，消除
        N+1 逐条事务）。同 (kind, id, revision) 已存在的行跳过不重复
        写入；返回与输入对齐的 inserted 标记列表（重复删除事件全 False，
        调用方据此做幂等去重计数）。"""
        return await asyncio.to_thread(
            self._record_deletion_tombstones_batch_sync, list(tombstones)
        )

    def _record_deletion_tombstones_batch_sync(
        self, tombstones: list[Mapping[str, Any]]
    ) -> list[bool]:
        now = _iso()
        inserted: list[bool] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for tombstone in tombstones:
                    subject_kind = tombstone.get("subject_kind")
                    subject_id = tombstone.get("subject_id")
                    if not subject_kind or not subject_id:
                        raise ValueError(
                            "tombstone 需要 subject_kind 与 subject_id"
                        )
                    subject_revision = tombstone.get("subject_revision") or ""
                    row = conn.execute(
                        "SELECT 1 FROM evaluation_deletion_tombstones "
                        "WHERE subject_kind = ? AND subject_id = ? "
                        "AND subject_revision = ?",
                        (subject_kind, subject_id, subject_revision),
                    ).fetchone()
                    if row is not None:
                        inserted.append(False)
                        continue
                    conn.execute(
                        "INSERT INTO evaluation_deletion_tombstones "
                        "(tombstone_id, subject_kind, subject_id, "
                        "subject_revision, reason, payload_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            tombstone.get("tombstone_id") or uuid4().hex,
                            subject_kind,
                            subject_id,
                            subject_revision,
                            tombstone.get("reason") or "",
                            _json_dumps(tombstone.get("payload") or {}),
                            now,
                        ),
                    )
                    inserted.append(True)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return inserted

    async def is_subject_deleted(
        self, subject_kind: str, subject_id: str, subject_revision: str = ""
    ) -> bool:
        return await asyncio.to_thread(
            self._is_subject_deleted_sync,
            subject_kind,
            subject_id,
            subject_revision,
        )

    def _is_subject_deleted_sync(
        self, subject_kind: str, subject_id: str, subject_revision: str
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM evaluation_deletion_tombstones "
                "WHERE subject_kind = ? AND subject_id = ? "
                "AND subject_revision = ?",
                (subject_kind, subject_id, subject_revision),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # T18 deletion propagation gates (tombstone 复核)
    # ------------------------------------------------------------------

    # 文档类 subject_kind 词表变体：T16 线上捕获写 'document'，T3 早期
    # 适配器/夹具可能写 'document_revision'——tombstone 匹配按变体集合
    # 等价（同 subject_id + revision 语义一致）
    _DOCUMENT_KIND_VARIANTS: tuple[str, str] = (
        "document",
        "document_revision",
    )

    # evidence_targets -> tombstone 等值匹配条件（revision 空串 tombstone
    # 表示整个主体被删，匹配任意 revision；document 类按变体集合等价）
    _TOMBSTONE_MATCH_ON = (
        "t.subject_id = et.subject_id "
        "AND (t.subject_kind = et.subject_kind "
        "     OR (t.subject_kind IN ('document', 'document_revision') "
        "         AND et.subject_kind IN ('document', 'document_revision'))) "
        "AND (t.subject_revision = '' "
        "     OR t.subject_revision = et.subject_revision)"
    )

    async def has_deletion_tombstone(
        self,
        subject_kind: str,
        subject_id: str,
        subject_revision: str,
    ) -> bool:
        """tombstone 命中查询（T8 组合根装配：AnnotationService
        tombstone_reader 读侧门禁适配，spec 4.4）。

        匹配语义与写侧门禁共用 _subject_blocked_by_tombstone：document
        类按词表变体等价；空串 revision tombstone 覆盖整个主体。"""
        return await asyncio.to_thread(
            self._has_deletion_tombstone_sync,
            subject_kind,
            subject_id,
            subject_revision,
        )

    def _has_deletion_tombstone_sync(
        self,
        subject_kind: str,
        subject_id: str,
        subject_revision: str,
    ) -> bool:
        with self._connect() as conn:
            return self._subject_blocked_by_tombstone(
                conn, subject_kind, subject_id, subject_revision
            )

    def _subject_blocked_by_tombstone(
        self,
        conn: sqlite3.Connection,
        subject_kind: str,
        subject_id: str,
        subject_revision: str,
    ) -> bool:
        """单主体 (kind, id, revision) 的 tombstone 命中判断（写侧门禁
        共用：样本 evidence_targets 条目、feedback 目标、job 领取前置）。

        document 类按词表变体等价；revision 空串 tombstone 表示整个主体
        被删，匹配任意 revision。"""
        if not subject_kind or not subject_id:
            return False
        variants: tuple[str, ...]
        if subject_kind in self._DOCUMENT_KIND_VARIANTS:
            variants = self._DOCUMENT_KIND_VARIANTS
        else:
            variants = (subject_kind,)
        placeholders = ", ".join("?" for _ in variants)
        row = conn.execute(
            "SELECT 1 FROM evaluation_deletion_tombstones "
            f"WHERE subject_kind IN ({placeholders}) "
            "AND subject_id = ? "
            "AND (subject_revision = '' OR subject_revision = ?) "
            "LIMIT 1",
            (*variants, subject_id, subject_revision or ""),
        ).fetchone()
        return row is not None

    def _sample_blocked_by_tombstone(
        self, conn: sqlite3.Connection, sample: Mapping[str, Any]
    ) -> bool:
        """样本 evidence_targets 任一条目命中 tombstone 即拒绝（写侧门禁）。"""
        for target in sample.get("evidence_targets") or ():
            if not isinstance(target, Mapping):
                continue
            if self._subject_blocked_by_tombstone(
                conn,
                target.get("subject_kind") or "",
                target.get("subject_id") or "",
                target.get("subject_revision") or "",
            ):
                return True
        return False

    def _sample_id_blocked_by_tombstone(
        self, conn: sqlite3.Connection, sample_id: str
    ) -> bool:
        """按 sample_id 经 evidence_targets 反向索引复核 tombstone（提交
        门禁：删除期间运行中 Judge 返回不得提交）。"""
        row = conn.execute(
            "SELECT 1 WHERE EXISTS ("
            "  SELECT 1 FROM evaluation_evidence_targets et "
            "  LEFT JOIN evaluation_deletion_tombstones t ON "
            + self._TOMBSTONE_MATCH_ON +
            "  WHERE et.sample_id = ? AND t.tombstone_id IS NOT NULL"
            ")",
            (sample_id,),
        ).fetchone()
        return row is not None

    def _delivery_blocked_by_tombstone(
        self, conn: sqlite3.Connection, delivery_id: str
    ) -> bool:
        """投递维度 tombstone 复核（claim / freeze / verified 门禁共用）。

        命中条件：投递所属 result 被 result 级 tombstone 删除，或 result
        对应样本的任一 evidence_targets 条目命中 subject 级 tombstone。
        """
        row = conn.execute(
            "SELECT 1 WHERE EXISTS ("
            "  SELECT 1 FROM evaluation_deliveries dd "
            "  JOIN evaluation_results r ON dd.result_id = r.result_id "
            "  LEFT JOIN evaluation_jobs j ON r.job_id = j.job_id "
            "  LEFT JOIN evaluation_evidence_targets et "
            "    ON et.sample_id = j.sample_id "
            "  LEFT JOIN evaluation_deletion_tombstones t ON "
            + self._TOMBSTONE_MATCH_ON +
            "  WHERE dd.delivery_id = ? "
            "    AND (t.tombstone_id IS NOT NULL "
            "         OR EXISTS (SELECT 1 FROM evaluation_deletion_tombstones t2 "
            "                    WHERE t2.subject_kind = 'result' "
            "                    AND t2.subject_id = dd.result_id))"
            ")",
            (delivery_id,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Deletion tasks (persistent; consumer T18 — minimal T3 scope)
    # ------------------------------------------------------------------

    async def save_deletion_task(self, task: Mapping[str, Any]) -> str:
        """幂等登记外部删除任务：同 (sink, subject, external_id) 重复
        登记返回原 task_id。external_id 允许为 NULL（预登记，external_id
        未知）；DB 层 UNIQUE 对 NULL 视为互异，因此 NULL 情形的幂等由本
        方法的显式 SELECT（``external_id IS ?``）保证。"""
        return await asyncio.to_thread(self._save_deletion_task_sync, task)

    def _save_deletion_task_sync(self, task: Mapping[str, Any]) -> str:
        sink = task.get("sink")
        subject_kind = task.get("subject_kind")
        subject_id = task.get("subject_id")
        if not sink or not subject_kind or not subject_id:
            raise ValueError("deletion_task 需要 sink/subject_kind/subject_id")
        subject_revision = task.get("subject_revision") or ""
        external_id = task.get("external_id")
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_id FROM evaluation_deletion_tasks "
                "WHERE sink = ? AND subject_kind = ? AND subject_id = ? "
                "AND subject_revision = ? AND external_id IS ?",
                (sink, subject_kind, subject_id, subject_revision, external_id),
            ).fetchone()
            if row is not None:
                conn.commit()
                return row["task_id"]
            task_id = task.get("task_id") or uuid4().hex
            conn.execute(
                "INSERT INTO evaluation_deletion_tasks (task_id, sink, "
                "external_id, subject_kind, subject_id, subject_revision, "
                "result_id, status, attempt_count, next_retry_at, "
                "last_error_type, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    sink,
                    external_id,
                    subject_kind,
                    subject_id,
                    subject_revision,
                    task.get("result_id"),
                    task.get("status") or "pending",
                    int(task.get("attempt_count") or 0),
                    task.get("next_retry_at"),
                    task.get("last_error_type"),
                    now,
                    now,
                ),
            )
            conn.commit()
            return task_id

    async def list_deletion_tasks(
        self,
        *,
        status: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 1000,
    ) -> list[Mapping[str, Any]]:
        """按状态查询删除任务。

        ``statuses``（24 项三方审阅 #10）：状态集合 SQL 下推——runner
        扫描窗口（LIMIT 截断）不得被历史 done 行挤占，否则累计 1000 条
        完成记录后新 pending 任务永久饥饿。与单值 ``status`` 互斥。"""
        if statuses is not None and status is not None:
            raise EvaluationValidationError(
                "list_deletion_tasks 的 status 与 statuses 不可同时指定"
            )
        return await asyncio.to_thread(
            self._list_deletion_tasks_sync, status, statuses, limit
        )

    async def find_deletion_task(
        self,
        *,
        sink: str,
        subject_kind: str,
        subject_id: str,
        external_id: str,
    ) -> Mapping[str, Any] | None:
        """按 (sink, subject, external_id) 精确存在性查询（T18/Mi5：级联
        去重不受 list_deletion_tasks limit 截断影响）。"""
        return await asyncio.to_thread(
            self._find_deletion_task_sync,
            sink,
            subject_kind,
            subject_id,
            external_id,
        )

    def _find_deletion_task_sync(
        self,
        sink: str,
        subject_kind: str,
        subject_id: str,
        external_id: str,
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_deletion_tasks "
                "WHERE sink = ? AND subject_kind = ? AND subject_id = ? "
                "AND external_id IS ? LIMIT 1",
                (sink, subject_kind, subject_id, external_id),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def _list_deletion_tasks_sync(
        self,
        status: str | None,
        statuses: Sequence[str] | None,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        sql = "SELECT * FROM evaluation_deletion_tasks"
        params: list[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        elif status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at, task_id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def get_deletion_task(self, task_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._get_deletion_task_sync, task_id)

    def _get_deletion_task_sync(self, task_id: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_deletion_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise EvaluationNotFoundError(
                f"deletion_task not found: {task_id}"
            )
        return _row_to_dict(row)

    async def update_deletion_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        external_id: str | None = None,
        error_type: str | None = None,
        next_retry_at: str | None = None,
        bump_attempt: bool = False,
    ) -> Mapping[str, Any]:
        """单行原地更新（无历史表，T18 扩展重试语义）：None 参数保持
        原值；status 走表级 CHECK 封闭集校验，非法值抛
        sqlite3.IntegrityError。"""
        return await asyncio.to_thread(
            self._update_deletion_task_sync,
            task_id,
            status,
            external_id,
            error_type,
            next_retry_at,
            bump_attempt,
        )

    def _update_deletion_task_sync(
        self,
        task_id: str,
        status: str | None,
        external_id: str | None,
        error_type: str | None,
        next_retry_at: str | None,
        bump_attempt: bool,
    ) -> Mapping[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT task_id FROM evaluation_deletion_tasks "
                    "WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise EvaluationNotFoundError(
                        f"deletion_task not found: {task_id}"
                    )
                conn.execute(
                    "UPDATE evaluation_deletion_tasks SET "
                    "status = COALESCE(?, status), "
                    "external_id = COALESCE(?, external_id), "
                    "last_error_type = COALESCE(?, last_error_type), "
                    "next_retry_at = COALESCE(?, next_retry_at), "
                    "attempt_count = attempt_count + ?, updated_at = ? "
                    "WHERE task_id = ?",
                    (
                        status,
                        external_id,
                        error_type,
                        next_retry_at,
                        1 if bump_attempt else 0,
                        _iso(),
                        task_id,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._get_deletion_task_sync(task_id)

    # ------------------------------------------------------------------
    # T18 deletion propagation cascade queries
    # ------------------------------------------------------------------

    async def list_result_ids_for_samples(
        self, sample_ids: Sequence[str]
    ) -> list[str]:
        """按样本集合查询全部关联 result id（删除级联：结果 tombstone）。"""
        return await asyncio.to_thread(
            self._list_result_ids_for_samples_sync, list(sample_ids)
        )

    def _list_result_ids_for_samples_sync(
        self, sample_ids: list[str]
    ) -> list[str]:
        ids = [s for s in sample_ids if s]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT r.result_id FROM evaluation_results r "
                "JOIN evaluation_jobs j ON r.job_id = j.job_id "
                f"WHERE j.sample_id IN ({placeholders}) "
                "ORDER BY r.result_id",
                ids,
            ).fetchall()
        return [r["result_id"] for r in rows]

    async def list_feedback_ids_by_subject(
        self,
        target_subject_id: str,
        *,
        target_kind: str | None = None,
    ) -> list[str]:
        """按反馈目标主体查询 feedback id（删除级联：反馈 tombstone）。

        ``target_kind`` 可选限定（如 'agent_run'）：同 id 跨主体类别不串
        扰（session 与 agent_run 的 id 空间独立，删除级联按类别过滤）。"""
        return await asyncio.to_thread(
            self._list_feedback_ids_by_subject_sync, target_subject_id, target_kind
        )

    def _list_feedback_ids_by_subject_sync(
        self, target_subject_id: str, target_kind: str | None
    ) -> list[str]:
        sql = (
            "SELECT feedback_id FROM evaluation_feedback "
            "WHERE target_subject_id = ?"
        )
        params: list[Any] = [target_subject_id]
        if target_kind is not None:
            sql += " AND target_kind = ?"
            params.append(target_kind)
        sql += " ORDER BY created_at, feedback_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r["feedback_id"] for r in rows]

    async def list_evidence_subject_ids(
        self, sample_ids: Sequence[str], subject_kind: str
    ) -> list[str]:
        """按样本集合提取指定类别的反向索引主体 id（去重）。

        T18 删除级联消费：会话删除时从待级联样本的 evidence_targets 提取
        该会话名下的 agent_run 主体 id，进而定位 target 为这些 agent_run
        的 feedback 一并纳入级联。"""
        return await asyncio.to_thread(
            self._list_evidence_subject_ids_sync, list(sample_ids), subject_kind
        )

    def _list_evidence_subject_ids_sync(
        self, sample_ids: list[str], subject_kind: str
    ) -> list[str]:
        ids = [s for s in sample_ids if s]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT subject_id FROM evaluation_evidence_targets "
                f"WHERE sample_id IN ({placeholders}) "
                "AND subject_kind = ? ORDER BY subject_id",
                (*ids, subject_kind),
            ).fetchall()
        return [r["subject_id"] for r in rows]

    async def list_deliveries_for_samples(
        self, sample_ids: Sequence[str]
    ) -> list[Mapping[str, Any]]:
        """按样本集合查询投递行（删除级联：external_id 删除任务登记）。

        返回最小投影：delivery_id / result_id / sink / external_id /
        status（按 created_at, delivery_id 稳定排序）。"""
        return await asyncio.to_thread(
            self._list_deliveries_for_samples_sync, list(sample_ids)
        )

    def _list_deliveries_for_samples_sync(
        self, sample_ids: list[str]
    ) -> list[Mapping[str, Any]]:
        ids = [s for s in sample_ids if s]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.delivery_id, d.result_id, d.sink, d.external_id, "
                "d.status FROM evaluation_deliveries d "
                "JOIN evaluation_results r ON d.result_id = r.result_id "
                "JOIN evaluation_jobs j ON r.job_id = j.job_id "
                f"WHERE j.sample_id IN ({placeholders}) "
                "ORDER BY d.created_at, d.delivery_id",
                ids,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def unlink_deliveries_for_samples(
        self,
        sample_ids: Sequence[str],
        *,
        error_type: str,
        now: datetime | None = None,
    ) -> int:
        """撤销已删主体样本的无租约在途投递（pending/retry_wait ->
        unlinked，经 domain 状态机校验；T18 删除传播）。

        持有活跃租约（lease_expiry 未过期）的投递不动：其发送状态由
        claim/freeze/verified 三处 tombstone 门禁与外部删除任务收敛。
        返回改判条数。"""
        return await asyncio.to_thread(
            self._unlink_deliveries_for_samples_sync,
            list(sample_ids),
            error_type,
            now,
        )

    def _unlink_deliveries_for_samples_sync(
        self, sample_ids: list[str], error_type: str, now: datetime | None
    ) -> int:
        now_str = _iso(now)
        ids = [s for s in sample_ids if s]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        unlinked = EvaluationDeliveryStatus.unlinked.value
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT d.delivery_id, d.status FROM evaluation_deliveries d "
                    "JOIN evaluation_results r ON d.result_id = r.result_id "
                    "JOIN evaluation_jobs j ON r.job_id = j.job_id "
                    f"WHERE j.sample_id IN ({placeholders}) "
                    "AND d.status IN ('pending', 'retry_wait') "
                    "AND (d.lease_expiry IS NULL OR d.lease_expiry <= ?)",
                    (*ids, now_str),
                ).fetchall()
                count = 0
                for row in rows:
                    # 经状态机校验（pending/retry_wait -> unlinked 合法）
                    new_status = transition_delivery_status(
                        row["status"], EvaluationDeliveryStatus.unlinked
                    ).value
                    updated = conn.execute(
                        "UPDATE evaluation_deliveries SET status = ?, "
                        "last_error_type = ?, next_retry_at = NULL, "
                        "updated_at = ? WHERE delivery_id = ? "
                        "AND status = ?",
                        (
                            new_status,
                            error_type,
                            now_str,
                            row["delivery_id"],
                            row["status"],
                        ),
                    ).rowcount
                    count += updated
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return count

    # ------------------------------------------------------------------
    # T6 worker extensions: heartbeat / cancel / fail / call attempts
    # ------------------------------------------------------------------

    async def heartbeat_job(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """续租：CAS 限定 job 仍为 running 且 lease_token 匹配最新持有者。

        续租成功返回 True；token 过期（被重新领取）或 job 已终结返回
        False，调用方应停止续租并放弃该 job 的后续提交。"""
        return await asyncio.to_thread(
            self._heartbeat_job_sync, job_id, lease_token, lease_seconds, now
        )

    def _heartbeat_job_sync(
        self, job_id: str, lease_token: str, lease_seconds: int, now: datetime | None
    ) -> bool:
        now_dt = now or _utcnow()
        expiry_str = _iso(now_dt + timedelta(seconds=lease_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE evaluation_jobs SET lease_expiry = ?, updated_at = ? "
                "WHERE job_id = ? AND lease_token = ? AND status = 'running'",
                (expiry_str, _iso(now_dt), job_id, lease_token),
            ).rowcount
            conn.commit()
        return updated == 1

    async def cancel_job(self, job_id: str, *, now: datetime | None = None) -> bool:
        """取消 queued 或 running 的 job（经 domain 状态机校验）。

        running 取消会同时清空 lease token：任何携带旧 token 的在途结果
        提交因此 fail closed（EvaluationLeaseError），已发生的费用与
        attempts 保留。终态 job 返回 False。"""
        return await asyncio.to_thread(self._cancel_job_sync, job_id, now)

    def _cancel_job_sync(self, job_id: str, now: datetime | None) -> bool:
        now_str = _iso(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM evaluation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise EvaluationNotFoundError(f"job not found: {job_id}")
            status = row["status"]
            if status not in ("queued", "running"):
                conn.rollback()
                return False
            cancelled = transition_job_status(status, "cancelled").value
            updated = conn.execute(
                "UPDATE evaluation_jobs SET status = ?, lease_owner = NULL, "
                "lease_token = NULL, lease_expiry = NULL, updated_at = ? "
                "WHERE job_id = ? AND status = ?",
                (cancelled, now_str, job_id, status),
            ).rowcount
            conn.commit()
        return updated == 1

    async def fail_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        error_type: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """running -> error 终态迁移（worker 重试耗尽/期限越过/超能力）。

        CAS 限定 lease_token 为最新持有者；token 过期（被重新领取）返回
        False，由新持有者决定终态。error_type 记入 jobs.error_type。"""
        return await asyncio.to_thread(
            self._fail_job_sync, job_id, lease_token, error_type, now
        )

    def _fail_job_sync(
        self, job_id: str, lease_token: str, error_type: str | None, now: datetime | None
    ) -> bool:
        now_str = _iso(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE evaluation_jobs SET status = ?, error_type = ?, "
                "lease_expiry = NULL, updated_at = ? "
                "WHERE job_id = ? AND lease_token = ? AND status = 'running'",
                (
                    transition_job_status("running", "error").value,
                    error_type,
                    now_str,
                    job_id,
                    lease_token,
                ),
            ).rowcount
            conn.commit()
        return updated == 1

    async def append_call_attempt(
        self, job_id: str, **fields: Any
    ) -> Mapping[str, Any]:
        """为一次真实 evaluator 调用追加 attempt 行（spec 6.4 重试留痕）。

        attempt_index 取自 BEGIN IMMEDIATE 事务内读取的
        ``jobs.attempt_count`` 并与计数器 +1 同事务提交：并发 claimer /
        迟到 worker 串行化后各得唯一 index，不会撞
        ``UNIQUE(job_id, attempt_index)``。字段与 attempts 列一一对应
        （judge_request/judge_model/usage_known/input_tokens/output_tokens/
        total_tokens/cost_usd/pricing_version/started_at/ended_at/
        error_type/output_ref）。"""
        return await asyncio.to_thread(self._append_call_attempt_sync, job_id, fields)

    def _append_call_attempt_sync(
        self, job_id: str, fields: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        now = _iso()
        attempt_id = fields.get("attempt_id") or uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT attempt_count FROM evaluation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise EvaluationNotFoundError(f"job not found: {job_id}")
                attempt_index = int(row["attempt_count"])
                conn.execute(
                    "INSERT INTO evaluation_attempts (attempt_id, job_id, "
                    "attempt_index, judge_request_json, judge_model, "
                    "usage_known, input_tokens, output_tokens, total_tokens, "
                    "cost_usd, pricing_version, started_at, ended_at, "
                    "error_type, output_ref, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        job_id,
                        attempt_index,
                        _json_dumps(fields.get("judge_request") or {}),
                        fields.get("judge_model"),
                        1 if fields.get("usage_known") else 0,
                        fields.get("input_tokens"),
                        fields.get("output_tokens"),
                        fields.get("total_tokens"),
                        fields.get("cost_usd"),
                        fields.get("pricing_version"),
                        fields.get("started_at") or now,
                        fields.get("ended_at"),
                        fields.get("error_type"),
                        fields.get("output_ref"),
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE evaluation_jobs SET attempt_count = attempt_count + 1, "
                    "updated_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise EvaluationConflictError(
                    f"call attempt 追加冲突: job={job_id}: {exc}"
                ) from exc
            except Exception:
                conn.rollback()
                raise
        return {
            "attempt_id": attempt_id,
            "job_id": job_id,
            "attempt_index": attempt_index,
        }

    # ------------------------------------------------------------------
    # T6 daily budget ledger (spec 13)
    # ------------------------------------------------------------------

    async def reserve_daily_budget(
        self,
        date_key: str,
        *,
        usd: Decimal | str = Decimal("0"),
        tokens: int = 0,
        calls: int = 0,
        max_usd: Decimal | str | None = None,
        max_tokens: int | None = None,
        max_calls: int | None = None,
    ) -> Mapping[str, Any]:
        """事务预留当日预算（claim 时调用，覆盖该 job 全部重试）。

        任一已配置上限维度上 已预留+已结算+本次预留 超限即抛
        EvaluationBudgetExhaustedError 且不落任何变更；上限为 None 的
        维度不判定。返回预留后的账本累计快照。"""
        return await asyncio.to_thread(
            self._reserve_daily_budget_sync,
            date_key,
            usd,
            tokens,
            calls,
            max_usd,
            max_tokens,
            max_calls,
        )

    def _reserve_daily_budget_sync(
        self,
        date_key: str,
        usd: Decimal | str,
        tokens: int,
        calls: int,
        max_usd: Decimal | str | None,
        max_tokens: int | None,
        max_calls: int | None,
    ) -> Mapping[str, Any]:
        usd_amount = Decimal(str(usd))
        token_amount = int(tokens)
        call_amount = int(calls)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM evaluation_budget_ledger WHERE date_key = ?",
                (date_key,),
            ).fetchone()
            reserved_usd = Decimal(row["reserved_usd"]) if row else Decimal("0")
            settled_usd = Decimal(row["settled_usd"]) if row else Decimal("0")
            reserved_tokens = int(row["reserved_tokens"]) if row else 0
            settled_tokens = int(row["settled_tokens"]) if row else 0
            reserved_calls = int(row["reserved_calls"]) if row else 0
            settled_calls = int(row["settled_calls"]) if row else 0
            if max_usd is not None and (
                reserved_usd + settled_usd + usd_amount > Decimal(str(max_usd))
            ):
                conn.rollback()
                raise EvaluationBudgetExhaustedError(
                    f"daily USD budget exhausted: date={date_key} "
                    f"used={reserved_usd + settled_usd} request={usd_amount} "
                    f"ceiling={max_usd}"
                )
            if max_tokens is not None and (
                reserved_tokens + settled_tokens + token_amount > max_tokens
            ):
                conn.rollback()
                raise EvaluationBudgetExhaustedError(
                    f"daily token budget exhausted: date={date_key} "
                    f"used={reserved_tokens + settled_tokens} "
                    f"request={token_amount} ceiling={max_tokens}"
                )
            if max_calls is not None and (
                reserved_calls + settled_calls + call_amount > max_calls
            ):
                conn.rollback()
                raise EvaluationBudgetExhaustedError(
                    f"daily call budget exhausted: date={date_key} "
                    f"used={reserved_calls + settled_calls} "
                    f"request={call_amount} ceiling={max_calls}"
                )
            conn.execute(
                "INSERT INTO evaluation_budget_ledger (date_key, reserved_usd, "
                "settled_usd, reserved_tokens, settled_tokens, reserved_calls, "
                "settled_calls, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date_key) DO UPDATE SET "
                "reserved_usd = excluded.reserved_usd, "
                "reserved_tokens = excluded.reserved_tokens, "
                "reserved_calls = excluded.reserved_calls, "
                "updated_at = excluded.updated_at",
                (
                    date_key,
                    str(reserved_usd + usd_amount),
                    str(settled_usd),
                    reserved_tokens + token_amount,
                    settled_tokens,
                    reserved_calls + call_amount,
                    settled_calls,
                    _iso(),
                ),
            )
            conn.commit()
        return {
            "date_key": date_key,
            "reserved_usd": str(reserved_usd + usd_amount),
            "settled_usd": str(settled_usd),
            "reserved_tokens": reserved_tokens + token_amount,
            "settled_tokens": settled_tokens,
            "reserved_calls": reserved_calls + call_amount,
            "settled_calls": settled_calls,
        }

    async def settle_daily_budget(
        self,
        date_key: str,
        *,
        usd: Decimal | str = Decimal("0"),
        tokens: int = 0,
        calls: int = 0,
        release_usd: Decimal | str = Decimal("0"),
        release_tokens: int = 0,
        release_calls: int = 0,
    ) -> Mapping[str, Any]:
        """结算当日预算：实际消耗计入 settled，预留释放（reserved 扣减）。

        release 超过当前预留时按 0 封底（防御性，不得出现负预留）。未知
        费用的保守结算由调用方（worker）以 settled=预留全额表达。"""
        return await asyncio.to_thread(
            self._settle_daily_budget_sync,
            date_key,
            usd,
            tokens,
            calls,
            release_usd,
            release_tokens,
            release_calls,
        )

    def _settle_daily_budget_sync(
        self,
        date_key: str,
        usd: Decimal | str,
        tokens: int,
        calls: int,
        release_usd: Decimal | str,
        release_tokens: int,
        release_calls: int,
    ) -> Mapping[str, Any]:
        usd_amount = Decimal(str(usd))
        release_usd_amount = Decimal(str(release_usd))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM evaluation_budget_ledger WHERE date_key = ?",
                (date_key,),
            ).fetchone()
            reserved_usd = Decimal(row["reserved_usd"]) if row else Decimal("0")
            settled_usd = Decimal(row["settled_usd"]) if row else Decimal("0")
            reserved_tokens = int(row["reserved_tokens"]) if row else 0
            settled_tokens = int(row["settled_tokens"]) if row else 0
            reserved_calls = int(row["reserved_calls"]) if row else 0
            settled_calls = int(row["settled_calls"]) if row else 0
            new_reserved_usd = max(Decimal("0"), reserved_usd - release_usd_amount)
            new_reserved_tokens = max(0, reserved_tokens - int(release_tokens))
            new_reserved_calls = max(0, reserved_calls - int(release_calls))
            conn.execute(
                "INSERT INTO evaluation_budget_ledger (date_key, reserved_usd, "
                "settled_usd, reserved_tokens, settled_tokens, reserved_calls, "
                "settled_calls, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date_key) DO UPDATE SET "
                "reserved_usd = excluded.reserved_usd, "
                "settled_usd = excluded.settled_usd, "
                "reserved_tokens = excluded.reserved_tokens, "
                "settled_tokens = excluded.settled_tokens, "
                "reserved_calls = excluded.reserved_calls, "
                "settled_calls = excluded.settled_calls, "
                "updated_at = excluded.updated_at",
                (
                    date_key,
                    str(new_reserved_usd),
                    str(settled_usd + usd_amount),
                    new_reserved_tokens,
                    settled_tokens + int(tokens),
                    new_reserved_calls,
                    settled_calls + int(calls),
                    _iso(),
                ),
            )
            conn.commit()
        return {
            "date_key": date_key,
            "reserved_usd": str(new_reserved_usd),
            "settled_usd": str(settled_usd + usd_amount),
            "reserved_tokens": new_reserved_tokens,
            "settled_tokens": settled_tokens + int(tokens),
            "reserved_calls": new_reserved_calls,
            "settled_calls": settled_calls + int(calls),
        }

    async def get_daily_budget(self, date_key: str) -> Mapping[str, Any]:
        """读取当日账本快照；无行时返回全零（默认禁用预算即无行）。"""
        return await asyncio.to_thread(self._get_daily_budget_sync, date_key)

    def _get_daily_budget_sync(self, date_key: str) -> Mapping[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_budget_ledger WHERE date_key = ?",
                (date_key,),
            ).fetchone()
        if row is None:
            return {
                "date_key": date_key,
                "reserved_usd": "0",
                "settled_usd": "0",
                "reserved_tokens": 0,
                "settled_tokens": 0,
                "reserved_calls": 0,
                "settled_calls": 0,
            }
        return {
            "date_key": row["date_key"],
            "reserved_usd": row["reserved_usd"],
            "settled_usd": row["settled_usd"],
            "reserved_tokens": int(row["reserved_tokens"]),
            "settled_tokens": int(row["settled_tokens"]),
            "reserved_calls": int(row["reserved_calls"]),
            "settled_calls": int(row["settled_calls"]),
        }
