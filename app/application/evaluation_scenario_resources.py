"""场景资源账本用例（plan T4，spec 6.1/6.2 资源生命周期纪律）。

编排 intent 登记、副作用确认、未知归属回查、两阶段释放、保留期登记
与到期清理、用户删除优先撤销。全部通过注入端口操作真实资源，禁止：
- 未知副作用直接重跑目标（先按唯一业务幂等键回查）；
- 宽泛前缀删除（只处理 manifest 内精确 refs）；
- PID 复用误杀（进程身份不符 -> cleanup_failed/manual_attention）；
- 文件 hash 变更后继续删除（拒删 -> cleanup_failed）；
- 把"仍留有证据资源"伪装成全部删除（retained 是独立账目状态）。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.domain.evaluation.scenario_resources import (
    RETENTION_ELIGIBLE_KINDS,
    ResourceIntent,
    contract_identifier,
)


class ActiveResourceStopper(Protocol):
    """活跃资源停止端口（task/子进程取消回读终态、browser/sandbox 关闭）。"""

    async def stop(
        self, resource_kind: str, external_id: str,
        process_identity: str | None = None,
    ) -> str: ...


class FileDeleterPort(Protocol):
    """文件删除端口：删除前复核根/类型/hash；hash 变更返回 hash_changed。"""

    async def delete_file(self, ref: str, expected_sha256: str) -> str: ...


class EvidenceDeleterPort(Protocol):
    """既有删除端口（spec 6.2：到期清理按依赖顺序调用既有删除端口，
    触发正常 tombstone/结果撤销）。"""

    async def delete_evidence(self, resource: Mapping[str, Any]) -> None: ...


class ResourceProber(Protocol):
    """按唯一业务幂等键回查副作用端口（只查不建，禁止重跑目标）。"""

    async def probe(self, intent: ResourceIntent) -> Mapping[str, Any] | None: ...


class ScenarioResourceLedger:
    """场景资源账目用例（独立于执行 phase 的 release/retention 状态）。"""

    def __init__(
        self,
        store,
        *,
        stopper: ActiveResourceStopper,
        file_deleter: FileDeleterPort,
        evidence_deleter: EvidenceDeleterPort,
        prober: ResourceProber | None = None,
        evidence_retention_seconds: float = 0.0,
        clock=None,
    ) -> None:
        self._store = store
        self._stopper = stopper
        self._file_deleter = file_deleter
        self._evidence_deleter = evidence_deleter
        self._prober = prober
        self._evidence_retention_seconds = float(evidence_retention_seconds)
        import time as _time

        self._clock = clock or _time.time
        # 可重入守卫：删除传播入口（handle_session_deleted）会再次回调
        # revoke_sources；同一资源正在撤销时跳过内层，防递归
        self._revoking: set[str] = set()

    # ------------------------------------------------------------------
    # intent / 确认 / 未知归属
    # ------------------------------------------------------------------

    async def register_intent(self, intent: ResourceIntent) -> dict:
        """副作用前持久登记 intent（调用方必须先登记再触发副作用）。"""
        return await self._store.register_intent(intent, now=self._clock())

    async def confirm_owned(
        self, business_key: str, *, external_id: str, ref: str | None = None,
    ) -> dict:
        """副作用成功创建后补齐真实 ID/路径。"""
        record = await self._store.get_by_business_key(business_key)
        if record is None:
            raise KeyError(f"resource intent not registered: {business_key!r}")
        return await self._store.confirm_owned(
            record["resource_id"], external_id=external_id, ref=ref,
            owner_token=record["owner_token"], now=self._clock(),
        )

    async def mark_unknown(self, business_key: str, *, reason: str) -> dict:
        """副作用结果未知（断网/崩溃）：登记 unknown，等待回查收敛。"""
        record = await self._store.get_by_business_key(business_key)
        if record is None:
            raise KeyError(f"resource intent not registered: {business_key!r}")
        return await self._store.transition(
            record["resource_id"], record["owner_token"], state="unknown",
            error_code="side_effect_outcome_unknown",
            now=self._clock(),
        )

    async def resolve_unknown(self, business_key: str) -> dict:
        """未知归属回查：命中 -> owned；未命中 -> released（确认未创建）。

        只回查不重跑（spec 6.1）；未接线 prober 时保持 unknown 并标记
        manual_attention，禁止以 side_effects=[] 判定无副作用。"""
        record = await self._store.get_by_business_key(business_key)
        if record is None:
            raise KeyError(f"resource intent not registered: {business_key!r}")
        if record["state"] != "unknown":
            return record
        if self._prober is None:
            return await self._store.transition(
                record["resource_id"], record["owner_token"], state="unknown",
                error_code="manual_attention", now=self._clock(),
            )
        finding = await self._prober.probe(
            self._intent_of(record)
        )
        if finding is None:
            return await self._store.transition(
                record["resource_id"], record["owner_token"], state="released",
                error_code=None, now=self._clock(),
            )
        return await self._store.confirm_owned(
            record["resource_id"],
            external_id=str(finding["external_id"]),
            ref=finding.get("ref"),
            owner_token=record["owner_token"], now=self._clock(),
        )

    @staticmethod
    def _intent_of(record: Mapping[str, Any]) -> ResourceIntent:
        return ResourceIntent(
            business_key=record["business_key"],
            execution_id=record["execution_id"],
            case_id=record["case_id"],
            resource_kind=record["resource_kind"],
            release_rule=record["release_rule"],
            retention_rule=record["retention_rule"],
            external_id=record["external_id"],
            ref=record["ref"],
            expected_sha256=record["expected_sha256"],
            process_identity=record["process_identity"],
        )

    # ------------------------------------------------------------------
    # 两阶段释放与保留
    # ------------------------------------------------------------------

    async def release_execution(self, execution_id: str, *, resource_ids: set[str] | None = None) -> dict[str, int]:
        """按依赖顺序释放执行的活跃资源并登记保留（spec 6.1/6.2）。

        顺序（release_order 升序）：task/子进程 -> browser/sandbox ->
        存储数据。存储类按保留规则登记 retained_until；活跃资源及时
        停止释放。返回 {released, retained, cleanup_failed}。"""
        report = {"released": 0, "retained": 0, "cleanup_failed": 0}
        now = self._clock()
        records = await self._store.list_for_execution(execution_id)
        unselected_writer = resource_ids is not None and any(
            record["resource_id"] not in resource_ids and record["release_order"] < 2
            and record["state"] != "released" for record in records
        )
        active_release_failed = unselected_writer
        for record in records:
            if resource_ids is not None and record["resource_id"] not in resource_ids:
                continue
            state = record["state"]
            if state in ("released", "retained"):
                continue
            if state == "cleanup_failed":
                report["cleanup_failed"] += 1
                active_release_failed = True
                continue
            # No storage deletion while an earlier writer may still be alive.
            if active_release_failed and record["release_order"] >= 2:
                if unselected_writer:
                    report["cleanup_failed"] += 1
                continue
            lease_until = record.get("lease_until")
            if lease_until is not None and lease_until <= now:
                record = await self._store.adopt_expired(record["resource_id"], now=now)
            if state == "intent":
                record = await self.mark_unknown(record["business_key"], reason="unconfirmed resource intent")
                state = "unknown"
            if state == "unknown":
                resolved = await self.resolve_unknown(record["business_key"])
                if resolved["state"] in ("released", "cleanup_failed"):
                    report[
                        "released" if resolved["state"] == "released"
                        else "cleanup_failed"
                    ] += 1
                    continue
                if resolved["state"] != "owned":
                    report["cleanup_failed"] += 1
                    active_release_failed = True
                    continue
                record = resolved
            # 租约过期（执行超过租约期或跨进程重建后 settle）：本账本
            # 是该执行单释放的合法归属方，先收养换发 token 再释放，
            # 不让 lease lost 使执行单整体 failed（T9 S4 实测缺口）
            lease_until = record.get("lease_until")
            if lease_until is not None and lease_until <= now:
                record = await self._store.adopt_expired(
                    record["resource_id"], now=self._clock()
                )
            outcome = await self._release_record(record)
            report[outcome] += 1
            if outcome == "cleanup_failed":
                active_release_failed = True
        return report

    async def _release_record(self, record: Mapping[str, Any]) -> str:
        kind = record["resource_kind"]
        if kind in ("task", "acp_process", "browser", "sandbox"):
            result = await self._stopper.stop(
                kind, record["external_id"] or "",
                process_identity=record["process_identity"],
            )
            if result == "identity_mismatch":
                await self._store.transition(
                    record["resource_id"], record["owner_token"],
                    state="cleanup_failed", error_code="manual_attention",
                    now=self._clock(),
                )
                return "cleanup_failed"
            await self._store.transition(
                record["resource_id"], record["owner_token"],
                state="released", now=self._clock(),
            )
            return "released"
        if kind == "workspace_file":
            result = await self._file_deleter.delete_file(
                record["ref"] or "", record["expected_sha256"] or ""
            )
            if result == "hash_changed":
                await self._store.transition(
                    record["resource_id"], record["owner_token"],
                    state="cleanup_failed", error_code="file_hash_changed",
                    now=self._clock(),
                )
                return "cleanup_failed"
            await self._store.transition(
                record["resource_id"], record["owner_token"],
                state="released", now=self._clock(),
            )
            return "released"
        # 存储类（session/cli_conversation/artifact/memory_binding）：
        # 按保留规则登记 retained_until，不伪装成全部删除
        if kind in RETENTION_ELIGIBLE_KINDS and self._evidence_retention_seconds > 0:
            await self._store.transition(
                record["resource_id"], record["owner_token"], state="retained",
                retained_until=self._clock() + self._evidence_retention_seconds,
                now=self._clock(),
            )
            return "retained"
        await self._store.transition(
            record["resource_id"], record["owner_token"], state="released",
            now=self._clock(),
        )
        return "released"

    async def observe_owned(
        self,
        *,
        execution_id: str,
        case_id: str,
        resource_kind: str,
        external_id: str | None,
        ref: str | None = None,
        expected_sha256: str | None = None,
        business_key: str | None = None,
    ) -> str:
        """通用 owned 回填登记（T10）：资源在目标执行中产生、无法在
        副作用前预知 ID 时，证据读取后按记录登记 owned，释放仍走账本
        两阶段纪律。幂等：同 business_key 重复登记返回既有状态；无
        external_id 返回 skipped 不登记。"""
        if not external_id:
            return "skipped"
        # external_id 可能不满足标识符契约（真实制品 ID 是 base64url
        # 含大写）：确定性编码进 business_key，本体保持原样入账
        key = business_key or (
            f"{execution_id}:{case_id}:{resource_kind}:"
            f"{contract_identifier(str(external_id))}"
        )
        existing = await self._store.get_by_business_key(key)
        if existing is not None:
            return str(existing["state"])
        await self.register_intent(
            ResourceIntent(
                business_key=key,
                execution_id=execution_id,
                case_id=case_id,
                resource_kind=resource_kind,
                release_rule="release_active_resources",
                retention_rule="retain_evidence_default",
                external_id=str(external_id),
                ref=ref,
                expected_sha256=expected_sha256,
                process_identity=None,
            )
        )
        await self.confirm_owned(
            key, external_id=str(external_id), ref=ref,
        )
        return "owned"

    async def observe_browser_session(
        self,
        *,
        execution_id: str,
        case_id: str,
        browser_session_id: str | None,
        ref: str | None = None,
    ) -> str:
        """浏览器会话账目回填登记（plan T9）。

        浏览器会话由目标 agent 在轮次中创建，无法在副作用前预知 ID：
        证据读取后按会话记录登记 owned，释放仍走账本两阶段纪律
        （stopper 真实关闭）。幂等：同 (execution, case, browser_session)
        重复观察返回既有状态；无 browser_session_id（unknown 记录，
        读取失败）返回 skipped 不登记。"""
        return await self.observe_owned(
            execution_id=execution_id,
            case_id=case_id,
            resource_kind="browser",
            external_id=browser_session_id,
            ref=ref,
        )

    async def register_retention(
        self, execution_id: str, *, now: float | None = None,
    ) -> int:
        """对已 owned 的存储类资源登记保留期（评分排队后立即调用）。"""
        now = self._clock() if now is None else now
        count = 0
        for record in await self._store.list_for_execution(execution_id):
            if record["state"] != "owned":
                continue
            # 租约过期（长执行/跨重建后 settle）：先收养再登记保留
            # （同 release_execution 收养语义，T9 S4 实测缺口）
            lease_until = record.get("lease_until")
            if lease_until is not None and lease_until <= now:
                record = await self._store.adopt_expired(
                    record["resource_id"], now=self._clock()
                )
            if record["resource_kind"] not in RETENTION_ELIGIBLE_KINDS:
                continue
            if self._evidence_retention_seconds <= 0:
                continue
            await self._store.transition(
                record["resource_id"], record["owner_token"], state="retained",
                retained_until=now + self._evidence_retention_seconds, now=now,
            )
            count += 1
        return count

    async def expire_retention(self, *, now: float | None = None) -> dict[str, int]:
        """TTL 到期清理：按既有删除端口收敛（tombstone/撤销语义不变）。"""
        now = self._clock() if now is None else now
        report = {"expired": 0, "cleanup_failed": 0}
        for record in await self._store.list_expired_retention(now=now):
            try:
                await self._evidence_deleter.delete_evidence(record)
            except Exception:
                await self._store.transition(
                    record["resource_id"], record["owner_token"] or "",
                    state="retained", error_code="retention_cleanup_failed",
                    retained_until=now + self._evidence_retention_seconds,
                    now=now,
                )
                report["cleanup_failed"] += 1
                continue
            await self._store.force_release(record["resource_id"], now=now)
            report["expired"] += 1
        return report

    # ------------------------------------------------------------------
    # 恢复与用户删除优先
    # ------------------------------------------------------------------

    async def recover_interrupted(
        self, execution_id: str, *, now: float | None = None,
    ) -> dict[str, int]:
        """租约过期/崩溃/SIGTERM 后的资源恢复（不重跑目标）。

        收养过期记录 -> 未知归属回查 -> 按序释放。幂等可重入：已终态
        记录不再处理。"""
        now = self._clock() if now is None else now
        adopted_ids: set[str] = set()
        for record in await self._store.list_for_execution(execution_id):
            if record["state"] in ("released", "cleanup_failed", "retained"):
                continue
            lease_valid = (
                record["lease_until"] is not None
                and record["lease_until"] > now
            )
            if lease_valid:
                continue  # 活跃执行仍持有租约，不越权收养
            await self._store.adopt_expired(record["resource_id"], now=now)
            adopted_ids.add(record["resource_id"])
        return await self.release_execution(execution_id, resource_ids=adopted_ids)

    async def recover_stalled(self, *, now: float | None = None) -> dict[str, int]:
        """恢复调度编排（D111）：对 store 报告的停滞执行单（租约过期的
        未收敛账目）逐个 recover_interrupted。

        生产调度方是组合根周期维护任务；单执行单失败隔离（计数
        failed_executions，不中断其余收敛），活跃租约由
        recover_interrupted 的租约门与 store 查询双重排除。"""
        now = self._clock() if now is None else now
        report = {
            "recovered_executions": 0,
            "released": 0,
            "retained": 0,
            "cleanup_failed": 0,
            "failed_executions": 0,
        }
        for execution_id in await self._store.list_stalled_executions(now=now):
            try:
                outcome = await self.recover_interrupted(
                    execution_id, now=now,
                )
            except Exception:
                report["failed_executions"] += 1
                continue
            report["recovered_executions"] += 1
            for key in ("released", "retained", "cleanup_failed"):
                report[key] += int(outcome.get(key, 0) or 0)
        return report

    async def revoke_sources(
        self, source_kind: str, source_id: str, *, revision: str | None = None,
    ) -> dict[str, int]:
        """用户删除优先（spec 6.2）：撤销保留期并立即清理证据资源。

        正在评分也照常撤销——清理走既有删除端口，触发正常 tombstone
        与结果撤销，不绕过删除政策。"""
        resource_ids = await self._store.list_resource_ids_by_source(
            source_kind, source_id, revision=revision,
        )
        report = {"revoked": 0}
        for resource_id in resource_ids:
            record = await self._store.get(resource_id)
            if record["state"] in ("released", "cleanup_failed"):
                continue
            if resource_id in self._revoking:
                # 删除传播内层重入：外层正在撤销，跳过
                continue
            self._revoking.add(resource_id)
            try:
                await self._evidence_deleter.delete_evidence(record)
                await self._store.force_release(resource_id, now=self._clock())
            finally:
                self._revoking.discard(resource_id)
            report["revoked"] += 1
        return report
