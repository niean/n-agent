# app/application/evaluation_annotation.py
"""人工标注用例（plan Task 6，spec 4.4）。

AnnotationService 是人工标注（评估工作台）的 Application 用例入口，与
EvaluationService 并列、不改变其既有 submit_feedback 显式 CAS 重试行为：
- 上下文（get_annotation_context）：服务端从原结果（source_result_id）
  解析目标四元组与 DefinitionKey，不接受浏览器自报目标身份；提供受控
  证据状态、评分契约摘要、当前 actor 完整线程 latest 人评与 revision。
- 提交（submit_annotation）：request_key 重放优先（同 payload 返回原
  成功 + is_current 标记，异 payload 409），未命中走 expected_revision
  CAS；同事务落 feedback + human result + sink deliveries（复用
  EvaluationService 的 ``_delivery_sink_specs`` 机制）。原自动结果不变，
  provenance 固定 human；本期不引入自由文本评论存储（comment_ref 恒
  None，保留既有语义）。

依赖注入（DDD：Application 不 import Infrastructure）：
- store：EvaluationLedgerStore 形状（真实 SQLiteEvaluationStore 在
  组合根注入）；
- evaluation_service：EvaluationService 实例，仅复用其公开投影
  （delivery_sink_specs 属性与模块级纯函数）；
- evidence_reader：LocalEvidenceStore 形状（load_snapshot），未注入或
  读取失败时证据按 unavailable 处理（fail-closed，spec 4.4：证据到期/
  脱敏后不足禁止提交新标注）；
- agent_run_lookup：async (subject_id) -> Mapping | None，agent_run
  目标核验真实 Run（复用 routes 现有 target_not_found 通道语义；未注入
  时跳过核验，与既有 /feedback 行为一致）；
- tombstone_reader：async (kind, subject_id, subject_revision) -> bool，
  目标主体 tombstone 命中判断（组合根经 store 的 tombstone 查询适配）。

并发语义：单事务原子性由 store 的 ``save_feedback_with_result``
（BEGIN IMMEDIATE）保证；权威 revision CAS 经 ``expected_revision``
参数传入写事务（spec 4.4 / plan Task 6 S4：同事务 expected ==
MAX(revision) 复查，唯一索引与降级非唯一索引统一生效，降级库禁止
重复 revision）；完整线程唯一索引（uq_feedback_thread_revision_key，
八列含 revision）在事务内继续兜底。Application 层预读 CAS 仅作
fast-fail。并发落败方收到 EvaluationConflictError 后按 request_key
重放语义复核（同 key 同 payload 返回原成功）或上抛
EvaluationRevisionConflictError（409，前端要求显式重载）。

错误映射（Interfaces 层 Task 9 接线）：EvaluationTargetDeletedError ->
410；EvaluationTargetMissingError -> 422 target_not_found；
EvidenceUnavailableError -> 422 evidence_unavailable；
EvaluationRevisionConflictError / EvaluationConflictError -> 409；
EvaluationValidationError -> 422。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

# 同源复用（plan Task 6）：幂等键/payload hash/目标投影与人评 result
# payload/deliveries 构造均为 evaluation_service 模块级纯函数，
# AnnotationService 直接导入同一实现，禁止两份手工拷贝导致算法或形状
# 分叉。两条提交流程语义不同（服务端解析 vs 客户端自报、重放顺序），
# 流程各自编排，仅 payload/deliveries 构造收敛。
from app.application.evaluation_service import (
    EvaluationRevisionConflictError,
    EvaluationServiceError,
    definition_from_mapping,
    feedback_id_from_request_key,
    feedback_payload_hash,
    feedback_payload_hash_from_row,
    human_result_deliveries,
    human_result_payload,
    target_to_mapping,
)
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationDeletionConflictError,
    EvaluationNotFoundError,
)
from app.domain.evaluation.models import (
    EvaluationTarget,
    EvaluationValidationError,
    validate_evaluation_value,
)

# 标注队列固定 actor（spec 4.4"已标注"口径）；与 store 标注队列查询的
# dashboard actor 常量同源（store 常量为 Infrastructure 私有，此处按
# 契约值声明并在组合根共用本常量）。
ANNOTATION_DASHBOARD_ACTOR = "dashboard"

AgentRunLookup = Callable[[str], Awaitable[Mapping[str, Any] | None]]
TombstoneReader = Callable[[str, str, str], Awaitable[bool]]


class EvaluationTargetDeletedError(EvaluationServiceError):
    """标注目标主体已被删除 tombstone 封禁（Interfaces 层映射 410 Gone）。"""


class EvaluationTargetMissingError(EvaluationServiceError):
    """标注目标核验失败：真实 Run 不存在（Interfaces 层映射 422
    target_not_found）。"""


class EvidenceUnavailableError(EvaluationServiceError):
    """标注证据缺失/过期/脱敏不足（Interfaces 层映射 422
    evidence_unavailable）。spec 4.4：证据到期或脱敏后不足时禁止提交
    新标注、说明原因，不补造正文。"""


@dataclass(frozen=True)
class AnnotationSubmitResult:
    """标注提交结果。``duplicate=True`` 表示 request_key 重放命中返回原
    成功（不新建 revision）；``is_current`` 表示该 feedback 行是否仍为
    完整线程 latest（False 时前端刷新 latest，不复活旧值）。"""

    feedback_id: str
    result_id: str
    revision: int
    is_current: bool
    duplicate: bool
    superseded_feedback_id: str | None


@dataclass(frozen=True)
class _ResolvedSource:
    """从原结果服务端解析出的目标与定义身份（浏览器不自报）。"""

    result: Mapping[str, Any]
    target_mapping: Mapping[str, Any]
    # 仅 automatic + agent_run 目标构造领域 VO（其余目标类型本期不可标注）
    target: EvaluationTarget | None
    definition_row: Mapping[str, Any]
    definition_id: str
    definition_version: str
    sample: Mapping[str, Any] | None


class AnnotationService:
    """人工标注用例入口（spec 4.4：标注上下文 + 受约束提交）。"""

    def __init__(
        self,
        store: Any,
        evaluation_service: Any,
        *,
        evidence_reader: Any = None,
        agent_run_lookup: AgentRunLookup | None = None,
        tombstone_reader: TombstoneReader | None = None,
    ) -> None:
        self._store = store
        self._evaluation_service = evaluation_service
        self._evidence_reader = evidence_reader
        self._agent_run_lookup = agent_run_lookup
        self._tombstone_reader = tombstone_reader

    # ------------------------------------------------------------------
    # 标注队列（Task 9 审查修复：只读投影经 Application 用例直通）
    # ------------------------------------------------------------------

    async def list_queue(
        self,
        *,
        state: str,
        reason: str,
        threshold: float = 0.5,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Mapping[str, Any]], str | None, str]:
        """标注队列只读投影（GET /chat/evaluations/annotations 用例包装）。

        直通 ``store.list_annotation_queue``：参数校验由账本端口实现承担
        （EvaluationValidationError -> 422）。返回三元组
        ``(rows, next_cursor, snapshot_at)``：snapshot_at 口径为
        "响应生成时刻"（用例层生成，标注队列为 keyset 分页、无快照
        上界要求），Interfaces 路由层不自造时间戳。
        """
        rows, next_cursor = await self._store.list_annotation_queue(
            state=state,
            reason=reason,
            threshold=threshold,
            limit=limit,
            cursor=cursor,
        )
        snapshot_at = datetime.now(timezone.utc).isoformat()
        return rows, next_cursor, snapshot_at

    # ------------------------------------------------------------------
    # 标注上下文
    # ------------------------------------------------------------------

    async def get_annotation_context(
        self, source_result_id: str, actor: str = ANNOTATION_DASHBOARD_ACTOR
    ) -> Mapping[str, Any]:
        """标注上下文（spec 4.4：受控证据 + 评分契约 + latest 人评）。

        - source_result 不存在 -> EvaluationNotFoundError（404）；
        - 目标已 tombstone -> EvaluationTargetDeletedError（410）；
        - 仅 automatic + agent_run 的来源结果可标注；其他目标类型返回
          上下文但 can_submit=False, reject_reason="target_kind_not_supported"；
        - 证据缺失/过期 -> evidence_status="unavailable"，can_submit=False
          （reject_reason="evidence_unavailable"），不补造正文；证据可用
          时随附受控投影 evidence（仅 request/answer 两字段脱敏正文，
          spec 6.2），不可用时上下文缺省 evidence 字段；
        - latest_human_feedback 为当前 actor 完整线程 latest（无则
          current_revision=0；旧线程迁移后取既有最大 revision，不重编号）。
        """
        resolved = await self._resolve_source_result(source_result_id)
        target_mapping = resolved.target_mapping
        target_quadruple = {
            "service": str(target_mapping.get("service") or ""),
            "kind": str(target_mapping.get("kind") or ""),
            "subject_id": str(target_mapping.get("subject_id") or ""),
            "subject_revision": str(target_mapping.get("subject_revision") or ""),
        }
        definition = definition_from_mapping(
            resolved.definition_row["definition"]
        )
        rubric = definition.judge_params.get("rubric")
        # 证据一次性加载：状态与受控投影（request/answer 脱敏正文）同源；
        # 不可用时投影为 None，上下文缺省 evidence 字段（不补造正文）
        evidence_status, evidence = await self._load_evidence(resolved.sample)
        context: dict[str, Any] = {
            "source_result_id": source_result_id,
            "target": target_quadruple,
            "definition": {
                "definition_id": definition.definition_id,
                "version": definition.version,
                "metric_name": definition.metric_name,
                "output_type": definition.output_type,
                "range_or_categories": (
                    list(definition.range_or_categories)
                    if definition.range_or_categories is not None
                    else None
                ),
                "rubric_summary": rubric if isinstance(rubric, str) else "",
            },
            "evidence_status": evidence_status,
            "latest_human_feedback": None,
            "current_revision": 0,
            "can_submit": False,
            "reject_reason": None,
        }
        if evidence is not None:
            context["evidence"] = evidence

        # tombstone 先于一切展示门禁：已删主体内容不可见（410）
        if self._tombstone_reader is not None and await self._tombstone_reader(
            target_quadruple["kind"],
            target_quadruple["subject_id"],
            target_quadruple["subject_revision"],
        ):
            raise EvaluationTargetDeletedError(
                "标注目标主体已删除: "
                f"kind={target_quadruple['kind']!r} "
                f"subject_id={target_quadruple['subject_id']!r}"
            )

        # 当前 actor 完整线程 latest 人评（含不可提交场景，供前端展示）
        if resolved.target is not None:
            latest = await self._store.get_latest_feedback(
                actor,
                resolved.target.service,
                resolved.target.kind,
                resolved.target.subject_id,
                resolved.definition_id,
                target_subject_revision=resolved.target.subject_revision,
                definition_version=resolved.definition_version,
            )
            if latest is not None:
                context["latest_human_feedback"] = {
                    "feedback_id": latest["feedback_id"],
                    "result_id": await self._store.get_result_id_by_feedback_id(
                        latest["feedback_id"]
                    ),
                    "revision": int(latest["revision"]),
                    "value": latest.get("value"),
                    "created_at": latest.get("created_at"),
                }
                context["current_revision"] = int(latest["revision"])

        if resolved.target is None:
            context["reject_reason"] = "target_kind_not_supported"
            return context

        # 真实 Run 核验（lookup 未注入时跳过，与既有 /feedback 行为一致）
        if self._agent_run_lookup is not None:
            run = await self._lookup_run_safe(resolved.target.subject_id)
            if run is None:
                context["reject_reason"] = "target_not_found"
                return context
            end_reason = str(run.get("end_reason") or "")
            if end_reason and end_reason != resolved.target.subject_revision:
                context["reject_reason"] = "target_revision_mismatch"
                return context

        if context["evidence_status"] != "available":
            context["reject_reason"] = "evidence_unavailable"
            return context

        context["can_submit"] = True
        return context

    # ------------------------------------------------------------------
    # 提交（S5 实现）
    # ------------------------------------------------------------------

    async def submit_annotation(
        self,
        source_result_id: str,
        *,
        value: Any,
        request_key: str,
        expected_revision: int,
        actor: str = ANNOTATION_DASHBOARD_ACTOR,
    ) -> AnnotationSubmitResult:
        """人工标注提交（spec 4.4：request_key 重放优先 + 完整线程 CAS）。

        重试顺序（spec 4.4）：
        1. 先按 request_key 派生 feedback_id 查已有行（与
           ``feedback_id_from_request_key`` 同源算法）：
           - 命中且 payload hash（完整目标身份 + 定义身份 + typed value）
             一致 -> 返回原成功（duplicate=True）；该行已非线程 latest 时
             is_current=False，不创建新 revision、不复活旧值；
           - 命中但 payload 不一致 -> EvaluationConflictError（409）。
        2. 未命中：重新读取证据并核验有效性（不依赖先前 GET can_submit；
           证据不足 -> EvidenceUnavailableError），然后 expected_revision
           CAS：预读不一致 -> EvaluationRevisionConflictError（fast-fail）；
           通过后在同一事务（store ``save_feedback_with_result`` BEGIN
           IMMEDIATE）落 feedback + human result + sink deliveries，权威
           revision CAS 经 store ``expected_revision`` 参数在写事务内复查
           （expected == MAX(revision)，spec 4.4 / plan Task 6 S4 同事务
           CAS：唯一索引与降级非唯一索引统一生效）；事务内 CAS 冲突抛
           EvaluationConflictError，落败方按 request_key 重放语义复核
           （同 key 同 payload 返回原成功）或上抛
           EvaluationRevisionConflictError。

        目标核验：tombstone -> EvaluationTargetDeletedError；真实 Run 不
        存在 -> EvaluationTargetMissingError；subject_revision 与真实 Run
        不匹配 -> EvaluationRevisionConflictError。actor 由服务端注入，
        不从请求载荷读取（请求体传 actor 属协议校验，Interfaces 层 Task 9
        按 422 protocol_invalid 拒绝）。
        """
        if not isinstance(actor, str) or not actor:
            raise EvaluationValidationError(
                "actor 必须由服务端注入（非空字符串）"
            )
        if not isinstance(request_key, str) or not request_key:
            raise EvaluationValidationError(
                "request_key 必须是非空字符串（spec 4.4 提交契约）"
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise EvaluationValidationError(
                f"expected_revision 必须是非负整数: {expected_revision!r}"
            )
        resolved = await self._resolve_source_result(source_result_id)
        if resolved.target is None:
            raise EvaluationValidationError(
                "该来源结果不支持标注（仅 automatic + agent_run 可提交）: "
                f"kind={resolved.target_mapping.get('kind')!r} "
                f"provenance={resolved.result.get('provenance')!r}"
            )
        target = resolved.target
        definition = definition_from_mapping(
            resolved.definition_row["definition"]
        )
        validate_evaluation_value(definition, value)

        # 目标核验（写侧同样执行，不依赖先前 GET）。
        # 顺序取舍（fail-closed，spec 4.4 重试语义的已知收窄）：目标核验
        # 先于 request_key 重放——目标主体在成功提交后才被删除 tombstone
        # 封禁时，携带旧 request_key 的重试将收到 410（EvaluationTarget
        # DeletedError）而非原成功响应；重放成功路径仅适用于目标未删除
        # 场景，组合根/前端刷新对重放成功的依赖在该前提下成立。不做调换：
        # 已删主体不应再被任何写路径（含重放复核）放行。
        if self._tombstone_reader is not None and await self._tombstone_reader(
            target.kind, target.subject_id, target.subject_revision
        ):
            raise EvaluationTargetDeletedError(
                f"标注目标主体已删除: kind={target.kind!r} "
                f"subject_id={target.subject_id!r}"
            )
        if self._agent_run_lookup is not None:
            run = await self._lookup_run_safe(target.subject_id)
            if run is None:
                raise EvaluationTargetMissingError(
                    f"agent run not found: {target.subject_id}"
                )
            end_reason = str(run.get("end_reason") or "")
            if end_reason and end_reason != target.subject_revision:
                raise EvaluationRevisionConflictError(
                    "目标 subject_revision 与真实 Run 不匹配（原结果已过期，"
                    f"请重载）: expected={target.subject_revision!r} "
                    f"actual={end_reason!r}"
                )

        payload_hash = feedback_payload_hash(
            actor,
            target,
            resolved.definition_id,
            resolved.definition_version,
            value,
            None,  # 本期不引入自由文本评论存储（spec 4.4）
        )
        candidate_id = feedback_id_from_request_key(request_key)

        # 1) request_key 重放优先（先于 CAS：旧键重放即使 expected_revision
        #    已过期也返回原成功并标 is_current，前端据此刷新 latest）
        existing = await self._get_feedback_optional(candidate_id)
        if existing is not None:
            if feedback_payload_hash_from_row(existing) != payload_hash:
                raise EvaluationConflictError(
                    "标注 request_key 已绑定不同 payload: "
                    f"feedback_id={existing['feedback_id']}"
                )
            return await self._replay_success(existing)

        # 2) 未命中：证据重读核验（到期/脱敏不足禁止提交，不补造正文）
        if await self._evidence_status(resolved.sample) != "available":
            raise EvidenceUnavailableError(
                f"标注证据不可用（缺失/过期/脱敏不足）: {source_result_id}"
            )

        # expected_revision 预读 fast-fail（权威 CAS 在下方写事务内经
        # store expected_revision 参数复查，见模块 docstring 并发语义）
        latest = await self._store.get_latest_feedback(
            actor,
            target.service,
            target.kind,
            target.subject_id,
            resolved.definition_id,
            target_subject_revision=target.subject_revision,
            definition_version=resolved.definition_version,
        )
        current_revision = int(latest["revision"]) if latest is not None else 0
        if expected_revision != current_revision:
            raise EvaluationRevisionConflictError(
                "标注 revision CAS 失败（请显式重载后重试）: "
                f"expected={expected_revision} current={current_revision}"
            )
        previous_result_id = (
            await self._store.get_result_id_by_feedback_id(
                latest["feedback_id"]
            )
            if latest is not None
            else None
        )
        new_revision = current_revision + 1
        feedback_payload: dict[str, Any] = {
            "feedback_id": candidate_id,
            "actor_ref": actor,
            "definition_id": resolved.definition_id,
            "definition_version": resolved.definition_version,
            "target": target_to_mapping(target),
            "value": value,
            "comment_ref": None,
            "revision": new_revision,
            "supersedes_id": (
                latest["feedback_id"] if latest is not None else None
            ),
        }
        # 人评 result payload/deliveries 经 evaluation_service 模块级纯
        # 函数构造（与 submit_feedback 单一权威源共用，见该函数 docstring）
        result_payload = human_result_payload(
            resolved.definition_row, value, previous_result_id
        )
        # sink 投递与 submit_feedback 共用同一 _delivery_sink_specs 机制
        deliveries = human_result_deliveries(
            self._evaluation_service.delivery_sink_specs,
            resolved.definition_row,
            value,
        )
        try:
            # 权威 CAS：expected_revision 传入 store，在 BEGIN IMMEDIATE
            # 写事务内对完整线程执行 expected == MAX(revision) 复查
            # （spec 4.4 / plan Task 6 S4：唯一索引与降级非唯一索引统一
            # 生效，降级库禁止重复 revision）；冲突抛 EvaluationConflict
            # Error 由下方 except 映射（重放复核或 409）
            feedback_id, result_id = await self._store.save_feedback_with_result(
                feedback_payload, result_payload, deliveries,
                expected_revision=expected_revision,
            )
        except EvaluationDeletionConflictError as exc:
            # store 写侧 tombstone 门禁（_insert_feedback）：已删主体拒绝
            # 迟到人评，映射 410（与上下文口径一致）
            raise EvaluationTargetDeletedError(
                f"标注目标主体已删除（store 写侧门禁）: {target.subject_id!r}"
            ) from exc
        except EvaluationConflictError as exc:
            # 并发竞态落败方（UNIQUE(feedback_id) PK 或线程 revision 唯一
            # 索引）：同 key 同 payload 的赢方按重放语义返回原成功；同 key
            # 异 payload 或异 key revision 互斥落败方上抛 409（前端显式重载）
            winner = await self._get_feedback_optional(candidate_id)
            if winner is not None:
                if feedback_payload_hash_from_row(winner) == payload_hash:
                    return await self._replay_success(winner)
                raise EvaluationConflictError(
                    "标注 request_key 已绑定不同 payload: "
                    f"feedback_id={winner['feedback_id']}"
                ) from exc
            raise EvaluationRevisionConflictError(
                "标注 revision 并发冲突（请显式重载后重试）: "
                f"actor={actor!r} definition={resolved.definition_id} "
                f"revision={new_revision}"
            ) from exc
        return AnnotationSubmitResult(
            feedback_id=feedback_id,
            result_id=result_id,
            revision=new_revision,
            is_current=True,
            duplicate=False,
            superseded_feedback_id=(
                latest["feedback_id"] if latest is not None else None
            ),
        )

    async def _get_feedback_optional(
        self, feedback_id: str
    ) -> Mapping[str, Any] | None:
        try:
            return await self._store.get_feedback(feedback_id)
        except EvaluationNotFoundError:
            return None

    async def _replay_success(
        self, existing: Mapping[str, Any]
    ) -> AnnotationSubmitResult:
        """request_key 重放命中：返回原成功记录，不创建新 revision。

        崩溃残留补齐（feedback 行已存在、human result 缺失）与
        ``evaluation_service._ensure_human_result`` 同源语义：UNIQUE
        (feedback_id) 兜底并发重复创建；payload 经
        ``human_result_payload`` 单一权威源构造。"""
        feedback_id = existing["feedback_id"]
        result_id = await self._store.get_result_id_by_feedback_id(feedback_id)
        if result_id is None:
            definition_row = await self._store.get_definition(
                existing["definition_id"], existing["definition_version"]
            )
            previous_result_id = None
            if existing.get("supersedes_id"):
                previous_result_id = await self._store.get_result_id_by_feedback_id(
                    existing["supersedes_id"]
                )
            payload = human_result_payload(
                definition_row,
                existing.get("value"),
                previous_result_id,
                feedback_id=feedback_id,
            )
            try:
                result_id = await self._store.save_result(payload)
            except EvaluationConflictError:
                result_id = await self._store.get_result_id_by_feedback_id(
                    feedback_id
                )
                if result_id is None:
                    raise
        latest = await self._store.get_latest_feedback(
            existing["actor_ref"],
            str((existing.get("target") or {}).get("service") or ""),
            str((existing.get("target") or {}).get("kind") or ""),
            str((existing.get("target") or {}).get("subject_id") or ""),
            existing["definition_id"],
            target_subject_revision=str(
                (existing.get("target") or {}).get("subject_revision") or ""
            ),
            definition_version=str(existing.get("definition_version") or ""),
        )
        is_current = (
            latest is not None and latest["feedback_id"] == feedback_id
        )
        return AnnotationSubmitResult(
            feedback_id=feedback_id,
            result_id=result_id,
            revision=int(existing["revision"]),
            is_current=is_current,
            duplicate=True,
            superseded_feedback_id=existing.get("supersedes_id"),
        )

    # ------------------------------------------------------------------
    # 内部：来源解析与证据核验
    # ------------------------------------------------------------------

    async def _resolve_source_result(self, source_result_id: str) -> _ResolvedSource:
        """从原结果服务端解析目标四元组与 DefinitionKey（spec 4.4）。

        automatic 结果经 job -> sample 解析（sample target_json 携带真实
        run_id/session_id，供投递投影关联）；human 结果经 feedback 行解析。
        definition 缺失（404）按 EvaluationNotFoundError 上抛。"""
        result = await self._store.load_result(source_result_id)
        provenance = str(result.get("provenance") or "")
        sample: Mapping[str, Any] | None = None
        if provenance == "human":
            feedback_id = result.get("feedback_id")
            if not feedback_id:
                raise EvaluationNotFoundError(
                    f"human result 缺少 feedback 关联: {source_result_id}"
                )
            feedback = await self._store.get_feedback(feedback_id)
            target_mapping = dict(feedback.get("target") or {})
            definition_id = str(feedback.get("definition_id") or "")
            definition_version = str(feedback.get("definition_version") or "")
        else:
            job_id = result.get("job_id")
            if not job_id:
                raise EvaluationNotFoundError(
                    f"automatic result 缺少 job 关联: {source_result_id}"
                )
            job = await self._store.get_job(job_id)
            sample = await self._store.load_sample(job["sample_id"])
            target_mapping = dict(sample.get("target") or {})
            definition_id = str(job.get("definition_id") or "")
            definition_version = str(job.get("definition_version") or "")
        definition_row = await self._store.get_definition(
            definition_id, definition_version
        )
        target: EvaluationTarget | None = None
        if provenance == "automatic" and target_mapping.get("kind") == "agent_run":
            # 领域 VO 构造期校验四元组非空；trace 身份缺失按 None 传
            target = EvaluationTarget(
                service=str(target_mapping.get("service") or ""),
                kind="agent_run",
                subject_id=str(target_mapping.get("subject_id") or ""),
                subject_revision=str(target_mapping.get("subject_revision") or ""),
                trace_id=target_mapping.get("trace_id") or None,
                span_id=target_mapping.get("span_id") or None,
                run_id=target_mapping.get("run_id") or None,
                session_id=target_mapping.get("session_id") or None,
            )
        return _ResolvedSource(
            result=result,
            target_mapping=target_mapping,
            target=target,
            definition_row=definition_row,
            definition_id=definition_id,
            definition_version=definition_version,
            sample=sample,
        )

    async def _load_evidence(
        self, sample: Mapping[str, Any] | None
    ) -> tuple[str, Mapping[str, Any] | None]:
        """加载证据快照并返回 (evidence_status, 受控投影 evidence)。

        一次性读取脱敏快照（构造期已 hash 校验、冻结正文），status 与
        正文同源不二读。受控投影仅含 request/answer 两字段（spec 6.2
        脱敏证据正文随标注上下文返回：详情并排展示 + 导出取真实样本
        query）；T22 快照正文 request 与 query 同值，T22 之前旧快照仅
        有 query 别名时 request 回退取 query（同属脱敏正文，不补造）。
        充分性判定：request/query 与 answer 至少一项存在，否则视为正文
        不足；任何读取失败/正文不足按 (unavailable, None) 处理，上下文
        不携带 evidence 字段。
        """
        if sample is None:
            return "unavailable", None
        ref = str(sample.get("snapshot_ref") or "")
        if not ref or self._evidence_reader is None:
            return "unavailable", None
        try:
            snapshot = await self._evidence_reader.load_snapshot(ref)
        except Exception:
            return "unavailable", None
        content = getattr(snapshot, "content", None)
        if not isinstance(content, Mapping) or not content:
            return "unavailable", None
        request = content.get("request")
        if request is None:
            request = content.get("query")
        answer = content.get("answer")
        # 充分性判定（投影前）：request/query 与 answer 至少一项存在，
        # 否则按"正文不足"fail-closed（unavailable），不返回空投影
        if request is None and answer is None:
            return "unavailable", None
        return "available", {
            "request": request,
            "answer": answer,
        }

    async def _evidence_status(self, sample: Mapping[str, Any] | None) -> str:
        """证据状态：available / unavailable（缺失、过期、脱敏后不足）。

        充分性判定：快照正文为非空 Mapping（EvidenceSnapshot 构造期已
        冻结脱敏后正文），且 request/query 与 answer 至少一项存在；任何
        读取失败按 unavailable 处理，不补造正文。
        """
        return (await self._load_evidence(sample))[0]

    async def _lookup_run_safe(self, subject_id: str) -> Mapping[str, Any] | None:
        """真实 Run 核验（lookup 异常按 not found 处理，routes 通道语义）。"""
        try:
            return await self._agent_run_lookup(subject_id)
        except Exception:
            return None
