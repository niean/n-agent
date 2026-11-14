"""T4：场景资源账本用例测试（spec 6.1/6.2 生命周期纪律）。

覆盖 plan T4 S1 全部反例：
1. 副作用前 intent 先持久登记；
2. 创建后断网：按唯一幂等业务键回查，不重跑、不因副作用清单为空判无；
3. 租约过期：恢复清理，不自动重跑目标；
4. 重复恢复：幂等收敛；
5. PID 复用：进程身份不符拒杀，cleanup_failed/manual_attention；
6. 用户删除与评分竞态：用户删除优先，保留期即撤销；
7. TTL 到期：按保留期正常清理；
8. 文件 hash 变更拒删；
9. 释放顺序：task/子进程 -> browser/sandbox -> 存储。
"""
from __future__ import annotations

import asyncio

import pytest

from app.application.evaluation_scenario_resources import (
    ScenarioResourceLedger,
)
from app.domain.evaluation.scenario_resources import ResourceIntent
from app.infrastructure.evaluation.scenario_store import (
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


class _Stopper:
    """活跃资源停止端口（task/子进程/browser/sandbox）。"""

    def __init__(self, results=None):
        self.calls = []
        self._results = results or {}

    async def stop(self, resource_kind, external_id, process_identity=None):
        self.calls.append((resource_kind, external_id, process_identity))
        return self._results.get(external_id, "stopped")


class _FileDeleter:
    def __init__(self, results=None):
        self.calls = []
        self._results = results or {}

    async def delete_file(self, ref, expected_sha256):
        self.calls.append((ref, expected_sha256))
        return self._results.get(ref, "deleted")


class _EvidenceDeleter:
    def __init__(self):
        self.calls = []

    async def delete_evidence(self, resource):
        self.calls.append(resource)


class _Prober:
    """按业务幂等键回查端口（只查不建）。"""

    def __init__(self, findings=None):
        self.lookups = []
        self._findings = findings or {}

    async def probe(self, intent):
        self.lookups.append(intent.business_key)
        return self._findings.get(intent.business_key)


def _ledger(store, *, stopper=None, file_deleter=None,
            evidence_deleter=None, prober=None) -> ScenarioResourceLedger:
    return ScenarioResourceLedger(
        store,
        stopper=stopper or _Stopper(),
        file_deleter=file_deleter or _FileDeleter(),
        evidence_deleter=evidence_deleter or _EvidenceDeleter(),
        prober=prober,
        evidence_retention_seconds=3600.0,
        clock=lambda: 1000.0,
    )


def test_intent_persisted_before_side_effect(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    ledger = _ledger(store)
    side_effect_invoked = []

    async def main():
        record = await ledger.register_intent(_intent())
        # intent 已持久化（副作用尚未发生）
        assert (await store.get(record["resource_id"]))["state"] == "intent"
        side_effect_invoked.append(True)
        await ledger.confirm_owned(
            record["business_key"], external_id="sess-1",
        )
        return side_effect_invoked

    assert asyncio.run(main()) == [True]


def test_network_drop_after_creation_recovers_by_business_key(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    prober = _Prober(findings={
        "exec-1:case-a:session:turn-1": {"external_id": "sess-found"},
    })
    ledger = _ledger(store, prober=prober)

    async def main():
        record = await ledger.register_intent(_intent())
        # 创建请求发出后断网：副作用结果未知
        await ledger.mark_unknown(
            record["business_key"], reason="connection dropped after create"
        )
        # 恢复：回查命中真实资源 -> owned；prober 只查不建（未重跑）
        resolved = await ledger.resolve_unknown(record["business_key"])
        assert resolved["state"] == "owned"
        assert resolved["external_id"] == "sess-found"
        assert prober.lookups == ["exec-1:case-a:session:turn-1"]
        # 回查未命中：确认为未创建（released），不得重试创建
        record2 = await ledger.register_intent(
            _intent(business_key="exec-1:case-a:session:turn-2",
                    ref=None)
        )
        await ledger.mark_unknown(record2["business_key"], reason="drop")
        prober._findings[record2["business_key"]] = None
        resolved2 = await ledger.resolve_unknown(record2["business_key"])
        assert resolved2["state"] == "released"

    asyncio.run(main())


def test_lease_expiry_recovers_without_rerunning_target(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    stopper = _Stopper()
    ledger = _ledger(store, stopper=stopper)
    target_invocations = []

    async def main():
        record = await ledger.register_intent(_intent())
        await ledger.confirm_owned(
            record["business_key"], external_id="sess-1",
        )
        target_invocations.append("run")  # 目标只在正常路径发生
        # 租约过期后恢复：收养 + 按保留规则登记 retained；不调用目标、
        # 不停止存储类资源（session 走保留而非停止）
        report = await ledger.recover_interrupted("exec-1", now=9000.0)
        assert report["retained"] == 1
        assert target_invocations == ["run"]
        assert stopper.calls == []

    asyncio.run(main())


def test_repeated_recovery_is_idempotent(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    stopper = _Stopper()
    ledger = _ledger(store, stopper=stopper)

    async def main():
        record = await ledger.register_intent(_intent())
        await ledger.confirm_owned(
            record["business_key"], external_id="sess-1",
        )
        first = await ledger.recover_interrupted("exec-1", now=9000.0)
        second = await ledger.recover_interrupted("exec-1", now=9100.0)
        assert first["retained"] == 1
        # 已 retained 的记录不再重复处理
        assert second == {"released": 0, "retained": 0, "cleanup_failed": 0}

    asyncio.run(main())


async def test_recovery_does_not_release_another_live_lease_in_same_execution(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    stopper = _Stopper()
    ledger = _ledger(store, stopper=stopper)
    stale = await store.register_intent(_intent(resource_kind="task"), now=1000)
    await store.confirm_owned(stale["resource_id"], external_id="old-task",
                              owner_token=stale["owner_token"], now=1000)
    live = await store.register_intent(_intent(business_key="exec-1:case-a:task:live", resource_kind="task"), now=8500)
    await store.confirm_owned(live["resource_id"], external_id="live-task",
                              owner_token=live["owner_token"], now=8500)
    await ledger.recover_interrupted("exec-1", now=9000)
    assert [call[1] for call in stopper.calls] == ["old-task"]
    assert (await store.get(live["resource_id"]))["state"] == "owned"


@pytest.mark.parametrize("unknown", [False, True])
async def test_unresolved_intent_never_counts_as_released_or_retained(tmp_path, unknown):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    stopper = _Stopper()
    ledger = _ledger(store, stopper=stopper)
    row = await ledger.register_intent(_intent(resource_kind="task", external_id="candidate"))
    if unknown:
        await ledger.mark_unknown(row["business_key"], reason="response lost")
    report = await ledger.release_execution("exec-1")
    assert report == {"released": 0, "retained": 0, "cleanup_failed": 1}
    assert stopper.calls == []
    assert (await store.get(row["resource_id"]))["state"] == "unknown"


async def test_failed_process_stop_prevents_workspace_deletion(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    deleter = _FileDeleter()
    ledger = _ledger(store, stopper=_Stopper({"proc": "identity_mismatch"}), file_deleter=deleter)
    for intent, external_id in [
        (_intent(resource_kind="acp_process", process_identity="pid=42:start=1"), "proc"),
        (_intent(business_key="exec-1:case-a:workspace_file:f", resource_kind="workspace_file",
                 ref="out.txt", expected_sha256="a" * 64), "out.txt"),
    ]:
        row = await ledger.register_intent(intent)
        await ledger.confirm_owned(row["business_key"], external_id=external_id)
    report = await ledger.release_execution("exec-1")
    assert report["cleanup_failed"] == 1
    assert deleter.calls == []
    again = await ledger.release_execution("exec-1")
    assert again["cleanup_failed"] == 1


async def test_recovery_never_deletes_stale_file_with_unselected_live_writer(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    deleter = _FileDeleter()
    ledger = _ledger(store, file_deleter=deleter)
    file = await store.register_intent(_intent(resource_kind="workspace_file", ref="out.txt",
        expected_sha256="a" * 64), now=1000)
    await store.confirm_owned(file["resource_id"], external_id="out.txt", owner_token=file["owner_token"], now=1000)
    writer = await store.register_intent(_intent(resource_kind="task", business_key="exec-1:case-a:task:live"), now=8500)
    await store.confirm_owned(writer["resource_id"], external_id="live-task", owner_token=writer["owner_token"], now=8500)
    report = await ledger.recover_interrupted("exec-1", now=9000)
    assert report["cleanup_failed"] == 1
    assert deleter.calls == []
    assert (await store.get(file["resource_id"]))["state"] == "owned"


def test_pid_reuse_refuses_to_kill(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    stopper = _Stopper(results={"acp-1": "identity_mismatch"})
    ledger = _ledger(store, stopper=stopper)

    async def main():
        record = await ledger.register_intent(
            _intent(
                business_key="exec-1:case-a:acp_process:main",
                resource_kind="acp_process",
                process_identity="pid=4242:start=1700000000",
            )
        )
        await ledger.confirm_owned(
            record["business_key"], external_id="acp-1",
        )
        report = await ledger.release_execution("exec-1")
        # PID 复用：身份不符 -> cleanup_failed + manual_attention，不误杀
        assert report["cleanup_failed"] == 1
        assert stopper.calls == [
            ("acp_process", "acp-1", "pid=4242:start=1700000000"),
        ]
        row = await store.get(record["resource_id"])
        assert row["state"] == "cleanup_failed"
        assert row["error_code"] == "manual_attention"

    asyncio.run(main())


def test_user_deletion_overrides_retention_during_scoring(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    evidence_deleter = _EvidenceDeleter()
    ledger = _ledger(store, evidence_deleter=evidence_deleter)

    async def main():
        record = await ledger.register_intent(_intent())
        await ledger.confirm_owned(
            record["business_key"], external_id="sess-synthetic",
        )
        # 评分排队中：证据按保留期登记 retained
        await ledger.register_retention("exec-1", now=1000.0)
        row = await store.get(record["resource_id"])
        assert row["state"] == "retained"
        assert row["retained_until"] == 1000.0 + 3600.0
        # 用户删除源会话：保留期立即撤销（用户删除优先，正在评分也撤销）
        report = await ledger.revoke_sources("session", "sess-src-1")
        assert report["revoked"] == 0  # 未挂源依赖时不误删
        await store.attach_source_ref(
            record["resource_id"], source_kind="session",
            source_id="sess-src-1",
        )
        report = await ledger.revoke_sources("session", "sess-src-1")
        assert report["revoked"] == 1
        assert evidence_deleter.calls, "保留证据被立即删除"
        row = await store.get(record["resource_id"])
        assert row["state"] == "released"

    asyncio.run(main())


def test_retention_ttl_expiry_cleans_up(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    evidence_deleter = _EvidenceDeleter()
    ledger = _ledger(store, evidence_deleter=evidence_deleter)

    async def main():
        record = await ledger.register_intent(_intent())
        await ledger.confirm_owned(
            record["business_key"], external_id="sess-synthetic",
        )
        await ledger.register_retention("exec-1", now=1000.0)
        # 未到期：不清理
        report = await ledger.expire_retention(now=2000.0)
        assert report["expired"] == 0
        assert evidence_deleter.calls == []
        # 到期：正常清理
        report = await ledger.expire_retention(now=5000.0)
        assert report["expired"] == 1
        row = await store.get(record["resource_id"])
        assert row["state"] == "released"

    asyncio.run(main())


def test_file_hash_change_refuses_deletion(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    file_deleter = _FileDeleter(results={"/ws/out.txt": "hash_changed"})
    ledger = _ledger(store, file_deleter=file_deleter)

    async def main():
        record = await ledger.register_intent(
            _intent(
                business_key="exec-1:case-a:workspace_file:out",
                resource_kind="workspace_file",
                ref="/ws/out.txt",
                expected_sha256="a" * 64,
            )
        )
        await ledger.confirm_owned(
            record["business_key"], external_id="/ws/out.txt",
        )
        report = await ledger.release_execution("exec-1")
        # hash 变更：拒删 + cleanup_failed
        assert report["cleanup_failed"] == 1
        assert report["released"] == 0
        row = await store.get(record["resource_id"])
        assert row["state"] == "cleanup_failed"
        assert row["error_code"] == "file_hash_changed"

    asyncio.run(main())


def test_release_order_tasks_before_browser_before_storage(tmp_path):
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    stopper = _Stopper()
    ledger = _ledger(store, stopper=stopper)

    async def main():
        for key, kind in [
            ("exec-1:case-a:session:turn-1", "session"),
            ("exec-1:case-a:task:main", "task"),
            ("exec-1:case-a:browser:main", "browser"),
        ]:
            record = await ledger.register_intent(
                _intent(business_key=key, resource_kind=kind)
            )
            await ledger.confirm_owned(
                record["business_key"], external_id=f"{kind}-1",
            )
        report = await ledger.release_execution("exec-1")
        order = [c[0] for c in stopper.calls]
        # task/子进程最先停，browser/sandbox 其次；存储类（session）走
        # 保留登记，不进停止端口
        assert order == ["task", "browser"]
        assert report["retained"] == 1

    asyncio.run(main())


def test_deletion_propagation_revokes_scenario_retention(tmp_path):
    """用户删除优先走公开删除传播入口：会话删除 -> 挂接该源的场景资源
    保留期立即撤销并清理（spec 6.2，正在评分也撤销）。"""
    from app.application.evaluation_deletion import EvaluationDeletionService
    from app.infrastructure.evaluation.store import SQLiteEvaluationStore

    db = str(tmp_path / "evaluations.db")
    ledger_store = SQLiteScenarioResourceStore(db)
    evidence_deleter = _EvidenceDeleter()
    ledger = _ledger(ledger_store, evidence_deleter=evidence_deleter)
    deletion = EvaluationDeletionService(
        store=SQLiteEvaluationStore(db),
        scenario_resource_ledger=ledger,
    )

    async def main():
        record = await ledger.register_intent(_intent())
        await ledger.confirm_owned(
            record["business_key"], external_id="sess-synthetic",
        )
        await ledger_store.attach_source_ref(
            record["resource_id"], source_kind="session",
            source_id="sess-src-1",
        )
        await ledger.register_retention("exec-1", now=1000.0)
        report = await deletion.handle_session_deleted("sess-src-1")
        assert report["scenario_resources"]["revoked"] == 1
        assert evidence_deleter.calls
        row = await ledger_store.get(record["resource_id"])
        assert row["state"] == "released"

    asyncio.run(main())


def test_recover_stalled_adopts_expired_leases_and_skips_active(tmp_path):
    """T13 S3（D111 接线缺口）：恢复调度编排——按 store 的停滞执行单
    查询逐个 recover_interrupted（收养+按序释放），活跃租约不越权，
    单执行单失败隔离不中断其余收敛。"""
    store = SQLiteScenarioResourceStore(str(tmp_path / "eval.db"))
    stopper = _Stopper()
    evidence_deleter = _EvidenceDeleter()
    ledger = _ledger(store, stopper=stopper, evidence_deleter=evidence_deleter)

    async def main():
        # 停滞执行单：owned 且租约过期（进程资源）
        record = await store.register_intent(
            _intent(business_key="exec-stale:case-a:acp_process:conv-1",
                    execution_id="exec-stale", case_id="case-a",
                    resource_kind="acp_process",
                    external_id="1234",
                    process_identity="pid=1234:start=999.0"),
            now=1000.0,
        )
        await store.confirm_owned(
            record["resource_id"], external_id="1234",
            owner_token=record["owner_token"], now=1001.0,
        )
        # 活跃执行单：租约有效（晚注册，lease 至 5500），不得收养
        active = await store.register_intent(
            _intent(business_key="exec-live:case-a:session:turn-1",
                    execution_id="exec-live", case_id="case-a"),
            now=4900.0,
        )
        await store.confirm_owned(
            active["resource_id"], external_id="sess-live",
            owner_token=active["owner_token"], now=4901.0,
        )
        report = await ledger.recover_stalled(now=5000.0)
        assert report["recovered_executions"] == 1
        assert report["released"] == 1
        assert report["failed_executions"] == 0
        # 停滞记录被收养并释放（进程停止调用在案）
        assert ("acp_process", "1234", "pid=1234:start=999.0") in stopper.calls
        after = await store.get(record["resource_id"])
        assert after["state"] == "released"
        # 活跃记录不动
        assert (await store.get(active["resource_id"]))["state"] == "owned"
        replay = await ledger.recover_stalled(now=5001.0)
        assert replay["recovered_executions"] == 0

    import asyncio

    asyncio.run(main())
