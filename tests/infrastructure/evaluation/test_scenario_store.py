"""T4：场景资源持久化 store 测试（迁移占号、幂等键、CAS、反向依赖）。

覆盖 plan T4：
- scenario_store 在共享 evaluation_schema_migrations 登记新版本号
  （1001/1002 已被 usage 回填与 dashboard-execution 占用，不得复用），
  且不持有 PRAGMA user_version（唯一所有者是 SQLiteEvaluationStore）；
- business_key 唯一业务幂等键：同键同内容幂等返回，同键异内容冲突；
- 受租约/CAS 管理的状态迁移：intent -> owned/unknown -> released/
  retained/cleanup_failed；token 不符或租约过期拒绝；
- 源会话/worker runs/文档版本反向依赖索引。
"""
from __future__ import annotations

import pytest

from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationLeaseError,
)
from app.domain.evaluation.models import EvaluationValidationError
from app.domain.evaluation.scenario_resources import ResourceIntent
from app.infrastructure.evaluation.scenario_store import (
    SCHEMA_MIGRATION_VERSION,
    SQLiteScenarioResourceStore,
)


def _intent(**overrides) -> ResourceIntent:
    fields = {
        "business_key": "exec-1:case-a:session:turn-1",
        "execution_id": "exec-1",
        "case_id": "case-a",
        "resource_kind": "session",
        "release_rule": "release_active_resources",
        "retention_rule": "retain_evidence_default",
        "external_id": None,
        "ref": None,
        "expected_sha256": None,
        "process_identity": None,
    }
    fields.update(overrides)
    return ResourceIntent(**fields)


def test_migration_row_registered_and_user_version_untouched(tmp_path):
    db = tmp_path / "eval.db"
    store = SQLiteScenarioResourceStore(str(db))
    import sqlite3

    conn = sqlite3.connect(db)
    versions = {
        row[0] for row in conn.execute(
            "SELECT version FROM evaluation_schema_migrations"
        ).fetchall()
    }
    conn.close()
    # 1001/1002 已占用，新表迁移不得复用既有段
    assert SCHEMA_MIGRATION_VERSION >= 1003
    assert SCHEMA_MIGRATION_VERSION in versions
    # scenario_store 不持有 PRAGMA user_version（唯一所有者是账本 store）
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def test_register_intent_idempotent_on_business_key(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))

    async def main():
        first = await store.register_intent(_intent(), now=1000.0)
        second = await store.register_intent(_intent(), now=1001.0)
        assert first["resource_id"] == second["resource_id"]
        assert first["state"] == "intent"
        # 同键异内容：冲突拒绝（防指纹漂移静默复用）
        with pytest.raises(EvaluationConflictError):
            await store.register_intent(
                _intent(resource_kind="task"), now=1002.0
            )

    import asyncio

    asyncio.run(main())


def test_confirm_owned_requires_valid_token_and_lease(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))

    async def main():
        record = await store.register_intent(_intent(), now=1000.0)
        token = record["owner_token"]
        # token 不符：拒绝
        with pytest.raises(EvaluationLeaseError):
            await store.confirm_owned(
                record["resource_id"], external_id="sess-1",
                owner_token="wrong", now=1001.0,
            )
        owned = await store.confirm_owned(
            record["resource_id"], external_id="sess-1",
            owner_token=token, now=1001.0,
        )
        assert owned["state"] == "owned"
        assert owned["external_id"] == "sess-1"
        # 租约过期：CAS 拒绝
        with pytest.raises(EvaluationLeaseError):
            await store.transition(
                record["resource_id"], token, state="retained",
                retained_until=2000.0, now=1000.0 + 10_000.0,
            )

    import asyncio

    asyncio.run(main())


def test_state_transitions_closed_vocabulary(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))

    async def main():
        record = await store.register_intent(_intent(), now=1000.0)
        token = record["owner_token"]
        rid = record["resource_id"]
        with pytest.raises(EvaluationValidationError):
            await store.transition(
                rid, token, state="made_up_state", now=1001.0
            )
        # intent -> unknown（副作用结果未知）
        unknown = await store.transition(
            rid, token, state="unknown", error_code="side_effect_outcome_unknown",
            now=1001.0,
        )
        assert unknown["state"] == "unknown"
        # unknown -> owned（回查命中）
        owned = await store.confirm_owned(
            rid, external_id="sess-9", owner_token=token, now=1002.0,
        )
        assert owned["state"] == "owned"

    import asyncio

    asyncio.run(main())


def test_source_reverse_dependency_index(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))

    async def main():
        record = await store.register_intent(_intent(), now=1000.0)
        await store.attach_source_ref(
            record["resource_id"], source_kind="session",
            source_id="sess-src-1",
        )
        await store.attach_source_ref(
            record["resource_id"], source_kind="document",
            source_id="doc-1", revision="r2",
        )
        ids = await store.list_resource_ids_by_source("session", "sess-src-1")
        assert ids == [record["resource_id"]]
        ids_doc = await store.list_resource_ids_by_source(
            "document", "doc-1", revision="r2"
        )
        assert ids_doc == [record["resource_id"]]
        # 其他版本不受影响
        assert await store.list_resource_ids_by_source(
            "document", "doc-1", revision="r3"
        ) == []

    import asyncio

    asyncio.run(main())


def test_expired_retention_query_and_lease_adoption(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))

    async def main():
        record = await store.register_intent(_intent(), now=1000.0)
        token = record["owner_token"]
        rid = record["resource_id"]
        await store.confirm_owned(
            rid, external_id="sess-1", owner_token=token, now=1001.0,
        )
        await store.transition(
            rid, token, state="retained", retained_until=1500.0, now=1002.0,
        )
        expired = await store.list_expired_retention(now=1600.0)
        assert [r["resource_id"] for r in expired] == [rid]
        assert await store.list_expired_retention(now=1400.0) == []
        # 租约过期后可被恢复方收养（收养换发新 token）
        adopted = await store.adopt_expired(rid, now=5000.0)
        assert adopted["owner_token"] not in (None, token)
        # 收养后旧 token CAS 失效
        with pytest.raises(EvaluationLeaseError):
            await store.transition(
                rid, token, state="released", now=5001.0,
            )

    import asyncio

    asyncio.run(main())


def test_list_stalled_executions_returns_only_expired_unsettled(tmp_path):
    """T13 S3：恢复调度需要"哪些执行单遗留未收敛账目"查询——只含租约
    已过期的 intent/owned/unknown 记录；活跃租约与已终态（released/
    retained/cleanup_failed）不出现，同执行单去重。"""
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))

    async def main():
        # exec-stalled：intent 且租约已过期
        stalled = await store.register_intent(
            _intent(business_key="exec-stalled:case-a:session:turn-1",
                    execution_id="exec-stalled"),
            now=1000.0,
        )
        # exec-stalled 第二条 owned 记录（同执行单去重）
        owned = await store.register_intent(
            _intent(business_key="exec-stalled:case-a:session:turn-2",
                    execution_id="exec-stalled"),
            now=1000.0,
        )
        await store.confirm_owned(
            owned["resource_id"], external_id="sess-2",
            owner_token=owned["owner_token"], now=1001.0,
        )
        # exec-active：租约仍有效（晚注册，lease 至 5500）
        await store.register_intent(
            _intent(business_key="exec-active:case-a:session:turn-1",
                    execution_id="exec-active"),
            now=4900.0,
        )
        # exec-settled：记录已 released 终态
        settled = await store.register_intent(
            _intent(business_key="exec-settled:case-a:session:turn-1",
                    execution_id="exec-settled"),
            now=1000.0,
        )
        await store.confirm_owned(
            settled["resource_id"], external_id="sess-3",
            owner_token=settled["owner_token"], now=1001.0,
        )
        await store.transition(
            settled["resource_id"], settled["owner_token"],
            state="released", now=1002.0,
        )
        # exec-retained：保留期未到（终态，不收敛）
        retained = await store.register_intent(
            _intent(business_key="exec-retained:case-a:session:turn-1",
                    execution_id="exec-retained"),
            now=1000.0,
        )
        await store.transition(
            retained["resource_id"], retained["owner_token"],
            state="retained", retained_until=9000.0, now=1002.0,
        )
        stalled_ids = await store.list_stalled_executions(now=5000.0)
        assert stalled_ids == ["exec-stalled"]
        # 活跃期不应返回任何执行单
        assert await store.list_stalled_executions(now=1100.0) == []
        assert stalled["lease_until"] <= 5000.0

    import asyncio

    asyncio.run(main())


async def test_active_execution_heartbeat_protects_its_older_resource_leases(tmp_path):
    from app.infrastructure.evaluation.execution_store import SQLiteExecutionStore
    path = str(tmp_path / "eval.db")
    executions = SQLiteExecutionStore(path)
    execution = await executions.submit({"request_key": "live", "request_fingerprint": "fp",
        "name": "active", "dataset_id": "d", "dataset_version": "1", "dataset_hash": "a" * 64,
        "payload": {}}, now=1000)
    await executions.claim(now=1001)
    store = SQLiteScenarioResourceStore(path)
    await store.register_intent(_intent(execution_id=execution["execution_id"]), now=100)
    assert await store.list_stalled_executions(now=1010) == []
    assert await store.list_stalled_executions(now=1100) == [execution["execution_id"]]
