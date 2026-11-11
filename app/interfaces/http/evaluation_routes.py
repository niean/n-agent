"""Evaluation producer 接收端路由 -- POST /evaluation/producer/messages.

T17：N-KB 样本入口。单一 producer 通道接受三种封闭消息（spec 7.2）：

- ``evaluation.sample.v1``：检索样本；正文快照经 evidence store 落盘
  （T16 既有双写模式：引用行 + submit_sample 样本行），样本幂等复用
  ``EvaluationService.submit_sample`` 既有语义（同 hash 返回原
  sample_id，异 hash 冲突），路由层不重复实现幂等；
- ``evaluation.producer-state.v1``：producer 高水位累计计数，经
  ``EvaluationService.record_producer_state`` 落库（低 sequence 409）；
- ``evaluation.target-deleted.v1``：远端文档删除传播，落
  ``EvaluationDeletionService.handle_document_deleted``（tombstone 写
  成功才 ACK，写失败 500 不 ACK）。

认证为服务专属 B 类凭据（启动期 env-only，默认 ``""`` = 通道关闭），
凭据绑定 service 身份（默认 n-kb）；载荷声明 service 与绑定不符 403，
客户端不得自报受信 service。路由层只做认证/校验/映射，业务语义全部
在 application 层。

T21 接缝：管理面路由（Dashboard 只读查询等）将在本文件扩展，复用
统一错误响应 ``_err`` 与封闭校验工具函数；producer 端点不挂
Dashboard 同源保护，凭据独立。
"""
from __future__ import annotations

import base64
import functools
import hmac
import json
import logging
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Any, Mapping, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.application.evaluation_analytics import IncomparableExperimentsError
from app.application.evaluation_annotation import (
    EvaluationTargetDeletedError,
    EvaluationTargetMissingError,
    EvidenceUnavailableError,
)
from app.application.evaluation_review import (
    DEFAULT_LOW_SCORE_THRESHOLD,
    EvaluationIdentityLeakError,
)
from app.application.evaluation_service import (
    DefinitionKey,
    EvaluationCapacityError,
    EvaluationRevisionConflictError,
    EvaluationService,
    StartExperimentRequest,
    SubmitFeedbackRequest,
    SubmitSampleRequest,
)
from app.domain.evaluation.config import (
    EvaluationConfigConflictError,
    EvaluationConfigValidationError,
)
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationDeletionConflictError,
    EvaluationNotFoundError,
    EvaluationStaleSequenceError,
)
from app.domain.evaluation.models import (
    EvidenceSnapshot,
    EvaluationTarget,
    EvaluationValidationError,
    PRODUCER_STATE_COUNT_KEYS,
    UnsupportedCapabilityError,
    require_non_negative_int,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 封闭消息枚举（spec 7.2；不接受任意 source / 脚本路径 / Judge URL）
# ---------------------------------------------------------------------------

SAMPLE_MESSAGE_TYPE = "evaluation.sample.v1"
PRODUCER_STATE_MESSAGE_TYPE = "evaluation.producer-state.v1"
TARGET_DELETED_MESSAGE_TYPE = "evaluation.target-deleted.v1"

CLOSED_MESSAGE_TYPES: tuple[str, ...] = (
    SAMPLE_MESSAGE_TYPE,
    PRODUCER_STATE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
)

# 单条 producer 消息默认字节上限（与证据快照 2MiB 上限对齐；组合根
# 可经 Settings evaluation_producer_max_bytes 覆盖）
DEFAULT_MAX_MESSAGE_BYTES: int = 2 * 1024 * 1024

# producer 消息顶层允许的键（按消息类型白名单校验，未知键 422——
# 开放字段语义会引入任意 source/URL 注入面）
_SAMPLE_ALLOWED_KEYS = frozenset({
    "type",
    "schema_version",
    "producer_event_id",
    "service",
    "retrieval_id",
    "captured_at",
    "query",
    "top_k",
    "filters",
    "results",
    "corpus_revision",
    "trace_ref",
    "integrity",
})
_STATE_ALLOWED_KEYS = frozenset({
    "type",
    "schema_version",
    "service",
    "producer_id",
    "counter_epoch",
    "sequence",
    # 五个累计计数键（权威定义在 domain PRODUCER_STATE_COUNT_KEYS，直接
    # 引用避免键名漂移）
    *PRODUCER_STATE_COUNT_KEYS,
    "observed_at",
})
_DELETED_ALLOWED_KEYS = frozenset({
    "type",
    "schema_version",
    "service",
    "document_id",
    "document_revision",
    "reason",
})

# 结果条目稳定身份必需键（spec 7.2 results 契约；qrels 与删除传播
# 都依赖 document_id/document_revision/chunk_id/chunk_content_hash）
_RESULT_REQUIRED_KEYS = (
    "document_id",
    "document_revision",
    "chunk_id",
    "chunk_content_hash",
    "rank",
)

# producer-state 五个累计计数字段（权威键表在 domain
# PRODUCER_STATE_COUNT_KEYS；此处不再重复定义）


# ---------------------------------------------------------------------------
# 隔离计数（409 冲突等接收端健康信号；T20 经 Gauge 暴露）
# ---------------------------------------------------------------------------

PRODUCER_INGRESS_COUNTERS: dict[str, int] = {}

_COUNTER_KEYS = (
    "unauthorized",
    "forbidden",
    "payload_too_large",
    "protocol_invalid",
    "event_conflict",
    "stale_sequence",
    "deletion_conflict",
    "capacity_rejected",
    "internal_error",
    "sample_accepted",
    "producer_state_accepted",
    "target_deleted_accepted",
)


def _bump_counter(key: str) -> None:
    PRODUCER_INGRESS_COUNTERS[key] = PRODUCER_INGRESS_COUNTERS.get(key, 0) + 1


def producer_ingress_counters() -> Mapping[str, int]:
    """producer 接收端计数快照（只读投影，供测试与 T20 Gauge 接线）。"""
    return {key: PRODUCER_INGRESS_COUNTERS.get(key, 0) for key in _COUNTER_KEYS}


def reset_producer_ingress_counters() -> None:
    """清零全部隔离计数（测试隔离与 T20 Gauge 接线测试复位用）。"""
    PRODUCER_INGRESS_COUNTERS.clear()


# ---------------------------------------------------------------------------
# 业务端口的最小形状（N3：鸭子类型协议注解，供类型检查与文档）
# ---------------------------------------------------------------------------


class DocumentDeletionHandler(Protocol):
    """target-deleted 消息的落点（EvaluationDeletionService 形状）。"""

    async def handle_document_deleted(
        self,
        document_id: str,
        document_revision: str,
        *,
        reason: str = ...,
    ) -> Mapping[str, Any]: ...


class SnapshotEvidenceStore(Protocol):
    """快照正文落盘端口（LocalEvidenceStore 形状；可选依赖）。"""

    async def save_snapshot(
        self, target: EvaluationTarget, snapshot: EvidenceSnapshot
    ) -> str: ...


# ---------------------------------------------------------------------------
# 认证器（B 类凭据；测试注入 fake）
# ---------------------------------------------------------------------------


class ProducerAuthenticator(Protocol):
    """producer 凭据认证器端口：返回绑定的受信 service 身份或 None。"""

    def authenticate(self, authorization: str | None) -> str | None: ...


class BearerTokenProducerAuthenticator:
    """服务专属 B 类凭据认证器（启动期注入，绑定单一 service 身份）。

    token 为空表示 producer 通道关闭（一律 401）；比较使用常量时间
    等值（hmac.compare_digest）。凭据不落库、不进日志。"""

    def __init__(self, token: str, *, service: str = "n-kb") -> None:
        self._token = token
        self._service = service

    def authenticate(self, authorization: str | None) -> str | None:
        if not self._token:
            # B 类配置未提供凭据：通道关闭，所有请求 401
            return None
        # scheme 严格大小写（"Bearer ..." 精确匹配）：RFC 7235 允许
        # scheme 大小写不敏感，但收紧匹配可消除 "bearer"/"BEARER" 变体
        # 的意外放行面；producer 客户端协议固定用 "Bearer"，无兼容负担
        expected = f"Bearer {self._token}"
        try:
            matched = isinstance(authorization, str) and hmac.compare_digest(
                authorization, expected
            )
        except TypeError:
            # uvicorn 按 latin-1 解码头部，非 ASCII 字节可达此处；
            # compare_digest 对非 ASCII str 抛 TypeError——一律按无效
            # 凭据处理（401），绝不以 500 泄露编码边界
            return None
        if matched:
            return self._service
        return None


# ---------------------------------------------------------------------------
# 校验错误（路由内部；不向调用方回显载荷内容）
# ---------------------------------------------------------------------------


class _ProtocolInvalid(Exception):
    """producer 消息协议校验失败（映射 422）。"""


def _require_non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _ProtocolInvalid(f"{label} 必须是非空字符串")
    return value


def _require_present(mapping: Mapping[str, Any], key: str) -> Any:
    if key not in mapping:
        raise _ProtocolInvalid(f"缺少必需字段: {key}")
    return mapping[key]


# ---------------------------------------------------------------------------
# 各消息类型的载荷校验（422；只校验结构与封闭契约，不信任内容）
# ---------------------------------------------------------------------------


def _check_closed_envelope(msg: Mapping[str, Any]) -> str:
    msg_type = _require_present(msg, "type")
    if msg_type not in CLOSED_MESSAGE_TYPES:
        raise _ProtocolInvalid(
            "未知消息 type（封闭枚举）: 仅接受 "
            f"{', '.join(CLOSED_MESSAGE_TYPES)}"
        )
    schema_version = _require_present(msg, "schema_version")
    if schema_version != msg_type:
        raise _ProtocolInvalid(
            "schema_version 必须与 type 一致的协议版本"
        )
    return msg_type


class _ServiceMismatch(Exception):
    """声明 service 与凭据绑定身份不匹配（映射 403）。"""


def _check_declared_service(
    msg: Mapping[str, Any], bound_service: str
) -> None:
    declared = msg.get("service")
    if declared is not None and declared != bound_service:
        # 凭据有效但声明 service 与绑定身份不匹配（映射 403）
        raise _ServiceMismatch()


def _validate_sample_message(msg: Mapping[str, Any]) -> None:
    for key in msg:
        if key not in _SAMPLE_ALLOWED_KEYS:
            # 客户端可控键名不回显（避免 2MiB 键名反射进 422 响应）
            raise _ProtocolInvalid(
                "消息包含未知顶层字段（封闭白名单）"
            )
    _require_non_empty_str(
        _require_present(msg, "producer_event_id"), "producer_event_id"
    )
    _require_non_empty_str(_require_present(msg, "retrieval_id"), "retrieval_id")
    captured_at = _require_non_empty_str(
        _require_present(msg, "captured_at"), "captured_at"
    )
    try:
        captured = datetime.fromisoformat(captured_at)
    except ValueError:
        raise _ProtocolInvalid("captured_at 必须是 ISO-8601 时间戳")
    if captured.tzinfo is None:
        # spec 7.2：captured_at 是 UTC 时间；naive 时间戳歧义拒绝
        # （T16 捕获侧生成 astimezone(timezone.utc).isoformat()，带偏移）
        raise _ProtocolInvalid("captured_at 必须携带时区偏移（UTC）")
    _require_non_empty_str(
        _require_present(msg, "corpus_revision"), "corpus_revision"
    )
    # spec 7.2：失败、空结果同样可形成样本（query 允许空串）
    if not isinstance(msg.get("query"), str):
        raise _ProtocolInvalid("query 必须是字符串")
    results = _require_present(msg, "results")
    if not isinstance(results, list):
        raise _ProtocolInvalid("results 必须是数组")
    for entry in results:
        # 内层键（results 条目/integrity）不做白名单封闭：边界决策——
        # 内层内容只随快照正文进入 content hash 与证据文件（不可解释
        # 执行、无自由字段消费方），服务器代码只读取稳定身份键
        # （document_id/document_revision/chunk_id/chunk_content_hash/
        # rank）与 integrity.redaction_version；宽松键随正文原样持久化
        if not isinstance(entry, dict):
            raise _ProtocolInvalid("results 条目必须是对象")
        for key in _RESULT_REQUIRED_KEYS:
            value = _require_present(entry, key)
            if key == "rank":
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise _ProtocolInvalid("results.rank 必须是正整数")
            else:
                _require_non_empty_str(value, f"results.{key}")
        snippet = entry.get("snippet")
        if snippet is not None and not isinstance(snippet, str):
            raise _ProtocolInvalid("results.snippet 必须是字符串")
        score = entry.get("similarity_score")
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, (int, float))
        ):
            raise _ProtocolInvalid("results.similarity_score 必须是数值")
    top_k = msg.get("top_k")
    if top_k is not None and (
        isinstance(top_k, bool) or not isinstance(top_k, int)
    ):
        raise _ProtocolInvalid("top_k 必须是整数")
    filters = msg.get("filters")
    if filters is not None and not isinstance(filters, dict):
        raise _ProtocolInvalid("filters 必须是对象")
    trace_ref = _require_present(msg, "trace_ref")
    if trace_ref is not None and not isinstance(trace_ref, dict):
        raise _ProtocolInvalid("trace_ref 必须是对象或 null")
    integrity = _require_present(msg, "integrity")
    if not isinstance(integrity, dict):
        raise _ProtocolInvalid("integrity 必须是对象")
    # integrity 内层键同样不封闭：只消费 redaction_version（快照还原
    # 契约），其余字段随正文进入 hash 与证据文件（见 results 处边界注释）
    _require_non_empty_str(
        _require_present(integrity, "redaction_version"),
        "integrity.redaction_version",
    )


def _validate_state_message(msg: Mapping[str, Any]) -> None:
    for key in msg:
        if key not in _STATE_ALLOWED_KEYS:
            # 客户端可控键名不回显（避免键名反射进 422 响应）
            raise _ProtocolInvalid(
                "消息包含未知顶层字段（封闭白名单）"
            )
    _require_non_empty_str(_require_present(msg, "producer_id"), "producer_id")
    _require_non_empty_str(
        _require_present(msg, "counter_epoch"), "counter_epoch"
    )
    require_non_negative_int(_require_present(msg, "sequence"), "sequence")
    for key in PRODUCER_STATE_COUNT_KEYS:
        require_non_negative_int(_require_present(msg, key), key)
    observed_at = _require_non_empty_str(
        _require_present(msg, "observed_at"), "observed_at"
    )
    try:
        datetime.fromisoformat(observed_at)
    except ValueError:
        raise _ProtocolInvalid("observed_at 必须是 ISO-8601 时间戳")


def _validate_deleted_message(msg: Mapping[str, Any]) -> None:
    for key in msg:
        if key not in _DELETED_ALLOWED_KEYS:
            # 客户端可控键名不回显（避免键名反射进 422 响应）
            raise _ProtocolInvalid(
                "消息包含未知顶层字段（封闭白名单）"
            )
    _require_non_empty_str(_require_present(msg, "document_id"), "document_id")
    _require_non_empty_str(
        _require_present(msg, "document_revision"), "document_revision"
    )
    reason = msg.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise _ProtocolInvalid("reason 必须是字符串")


# ---------------------------------------------------------------------------
# sample 消息 -> application 提交（复用 T16 双写模式与既有幂等）
# ---------------------------------------------------------------------------


def _build_sample_target(
    msg: Mapping[str, Any], bound_service: str
) -> EvaluationTarget:
    trace_ref = msg.get("trace_ref") or {}
    return EvaluationTarget(
        service=bound_service,
        kind="retrieval",
        subject_id=msg["retrieval_id"],
        subject_revision=msg["corpus_revision"],
        trace_id=trace_ref.get("trace_id"),
        span_id=trace_ref.get("span_id"),
        retrieval_id=msg["retrieval_id"],
    )


def _build_evidence_targets(
    msg: Mapping[str, Any], bound_service: str
) -> tuple[dict[str, str], ...]:
    """从 results 展开文档证据目标（去重），对齐 T16 evidence_targets 语义，
    供 T18 删除传播按 (n-kb, document, revision) 反向索引定位样本。"""
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in msg.get("results") or ():
        key = (entry["document_id"], entry["document_revision"])
        if key in seen:
            continue
        seen.add(key)
        targets.append({
            "source_service": bound_service,
            "subject_kind": "document",
            "subject_id": entry["document_id"],
            "subject_revision": entry["document_revision"],
        })
    return tuple(targets)


def _build_snapshot_content(msg: Mapping[str, Any]) -> dict[str, Any]:
    """经允许释放的快照正文（进入 hash 与证据文件；spec 7.2 契约字段）。"""
    # 证据槽投影（24 项三方审阅 #23）：Judge kb.context_relevance 按
    # chunks 槽位取证据，此前 producer 快照只有 results 别名键 -> 恒
    # skipped(evidence_missing)。chunk 文本取 snippet（与 results 同源）
    content: dict[str, Any] = {
        "query": msg.get("query", ""),
        "results": msg["results"],
        "chunks": [
            str(entry.get("snippet") or "")
            for entry in msg["results"]
            if isinstance(entry, Mapping) and entry.get("snippet")
        ],
        "corpus_revision": msg["corpus_revision"],
        "trace_ref": msg.get("trace_ref"),
        "integrity": msg["integrity"],
    }
    if msg.get("top_k") is not None:
        content["top_k"] = msg["top_k"]
    if msg.get("filters") is not None:
        content["filters"] = msg["filters"]
    return content


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------


def register_evaluation_producer_routes(
    app: APIRouter,
    *,
    authenticator: ProducerAuthenticator,
    evaluation_service: EvaluationService,
    deletion_service: DocumentDeletionHandler,
    evidence_store: SnapshotEvidenceStore | None = None,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> None:
    """注册 POST /evaluation/producer/messages（T17 producer 端点）。

    authenticator 经依赖注入（组合根传入 BearerTokenProducerAuthenticator，
    测试注入 fake）；deletion_service 满足 DocumentDeletionHandler 协议
    （EvaluationDeletionService 形状）；evidence_store 满足
    SnapshotEvidenceStore 协议，可选（None 时样本行 snapshot_ref 为空，
    正文不落盘仅计入幂等 hash）。
    """
    if max_message_bytes <= 0:
        raise ValueError("max_message_bytes 必须为正")

    @app.post("/evaluation/producer/messages")
    async def post_producer_message(request: Request) -> JSONResponse:
        # 1) 认证（匿名/错误凭据 401；通道关闭一律 401，不暴露配置状态）
        service_bound = authenticator.authenticate(
            request.headers.get("authorization")
        )
        if service_bound is None:
            _bump_counter("unauthorized")
            return _err(401, "invalid_credentials", "producer credential required")
        # 2) 请求大小上限：Content-Length 快路径预检（畸形值同样拒绝），
        #    随后经 request.stream() 增量消费实际字节并累计复核——chunked
        #    请求不落全量缓冲，超限立即 413 并停止读取（内存耗尽 DoS 面）
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_message_bytes:
                    raise ValueError
            except ValueError:
                _bump_counter("payload_too_large")
                return _err(413, "payload_too_large", "message exceeds size limit")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            if not isinstance(chunk, bytes):
                chunk = bytes(chunk)
            total += len(chunk)
            if total > max_message_bytes:
                # 超限立即返回：后续分片不再消费
                _bump_counter("payload_too_large")
                return _err(413, "payload_too_large", "message exceeds size limit")
            chunks.append(chunk)
        body = b"".join(chunks)
        # 3) JSON 解析（统一 422 错误契约）
        try:
            msg = json.loads(body)
        except Exception:
            _bump_counter("protocol_invalid")
            return _err(422, "protocol_invalid", "body must be valid JSON")
        if not isinstance(msg, dict):
            _bump_counter("protocol_invalid")
            return _err(422, "protocol_invalid", "body must be a JSON object")
        try:
            # 4) 封闭信封：type 白名单 + schema_version 一致性
            msg_type = _check_closed_envelope(msg)
            # 5) 声明 service 与凭据绑定身份一致性（403 先于字段级 422）
            _check_declared_service(msg, service_bound)
            # 6) 分类型封闭校验
            if msg_type == SAMPLE_MESSAGE_TYPE:
                _validate_sample_message(msg)
            elif msg_type == PRODUCER_STATE_MESSAGE_TYPE:
                _validate_state_message(msg)
            else:
                _validate_deleted_message(msg)
        except _ServiceMismatch:
            _bump_counter("forbidden")
            return _err(
                403, "service_mismatch", "declared service does not match credential"
            )
        except EvaluationValidationError as exc:
            # domain 共用校验助手（require_non_negative_int 等）的失败
            # 与路由本地 _ProtocolInvalid 同为协议错误（映射 422）
            _bump_counter("protocol_invalid")
            return _err(422, "protocol_invalid", str(exc))
        except _ProtocolInvalid as exc:
            _bump_counter("protocol_invalid")
            return _err(422, "protocol_invalid", str(exc))
        # 7) 分类型业务分发（application 层语义）
        if msg_type == SAMPLE_MESSAGE_TYPE:
            return await _handle_sample(
                msg, service_bound, evaluation_service, evidence_store
            )
        if msg_type == PRODUCER_STATE_MESSAGE_TYPE:
            return await _handle_state(msg, evaluation_service)
        return await _handle_deleted(msg, deletion_service)

    # T21 接缝：管理面路由在同文件扩展，复用 _err 与封闭校验工具。


async def _handle_sample(
    msg: Mapping[str, Any],
    bound_service: str,
    service: EvaluationService,
    evidence_store: SnapshotEvidenceStore | None,
) -> JSONResponse:
    """sample 消息：快照正文落盘（T16 双写模式）+ submit_sample 既有幂等。"""
    try:
        target = _build_sample_target(msg, bound_service)
        snapshot = EvidenceSnapshot(
            content=_build_snapshot_content(msg),
            redaction_version=msg["integrity"]["redaction_version"],
        )
        snapshot_ref = ""
        if evidence_store is not None:
            snapshot_ref = await evidence_store.save_snapshot(target, snapshot)
        # 线上样本作业展开（#5）：按目标种类解析已注册的自动评估定义，
        # 生产样本携带 definitions 进入 submit_sample——缺省空元组会让
        # 样本只落库不开作业，自动 evaluator 永不执行
        definitions = await service.definitions_for_target(target.kind)
        result = await service.submit_sample(
            _build_submit_request(
                msg, bound_service, target, snapshot, snapshot_ref,
                definitions=definitions,
            )
        )
    except EvaluationCapacityError:
        _bump_counter("capacity_rejected")
        return _err(503, "capacity_full", "evaluation capacity reached; retry later")
    except EvaluationValidationError as exc:
        _bump_counter("protocol_invalid")
        return _err(422, "protocol_invalid", str(exc))
    except EvaluationDeletionConflictError:
        # 迟到样本携带已删文档版本：tombstone 门禁拒绝（新 retrieval_id
        # 不绕过），producer 停发该主体样本
        _bump_counter("deletion_conflict")
        return _err(
            409,
            "deletion_conflict",
            "sample subject deleted; late write rejected",
        )
    except EvaluationConflictError:
        _bump_counter("event_conflict")
        return _err(
            409,
            "event_conflict",
            "producer_event_id already used with different content",
        )
    except Exception:
        logger.warning("producer sample ingest failed", exc_info=True)
        _bump_counter("internal_error")
        return _err(500, "producer_ingest_failed", "sample could not be persisted")
    _bump_counter("sample_accepted")
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "sample_id": result.sample_id,
            "job_ids": list(result.job_ids),
            "snapshot_ref": snapshot_ref,
        },
        headers={"Cache-Control": "no-store"},
    )


def _build_submit_request(
    msg: Mapping[str, Any],
    bound_service: str,
    target: EvaluationTarget,
    snapshot: EvidenceSnapshot,
    snapshot_ref: str,
    *,
    definitions: tuple[DefinitionKey, ...] = (),
) -> SubmitSampleRequest:
    return SubmitSampleRequest(
        producer_event_id=msg["producer_event_id"],
        service=bound_service,
        target=target,
        evidence_targets=_build_evidence_targets(msg, bound_service),
        snapshot_ref=snapshot_ref,
        snapshot_hash=snapshot.content_sha256,
        schema_version=SAMPLE_MESSAGE_TYPE,
        captured_at=msg["captured_at"],
        definitions=definitions,
    )


async def _handle_state(
    msg: Mapping[str, Any], service: EvaluationService
) -> JSONResponse:
    """producer-state 消息：高水位幂等落库 + 心跳新鲜度判定。"""
    try:
        result = await service.record_producer_state(msg)
    except EvaluationStaleSequenceError:
        _bump_counter("stale_sequence")
        return _err(
            409, "stale_sequence", "sequence is lower than current high watermark"
        )
    except EvaluationValidationError as exc:
        _bump_counter("protocol_invalid")
        return _err(422, "protocol_invalid", str(exc))
    except Exception:
        logger.warning("producer state ingest failed", exc_info=True)
        _bump_counter("internal_error")
        return _err(500, "producer_ingest_failed", "producer state could not be persisted")
    _bump_counter("producer_state_accepted")
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "applied": result.applied,
            "heartbeat_stale": result.heartbeat_stale,
            "coverage": "unknown" if result.heartbeat_stale else "current",
        },
        headers={"Cache-Control": "no-store"},
    )


async def _handle_deleted(
    msg: Mapping[str, Any], deletion_service: DocumentDeletionHandler
) -> JSONResponse:
    """target-deleted 消息：写 tombstone 成功才 ACK；写失败 500 不 ACK。"""
    try:
        report = await deletion_service.handle_document_deleted(
            msg["document_id"],
            msg["document_revision"],
            reason=msg.get("reason") or "document_deleted",
        )
    except Exception:
        logger.warning("producer target-deleted ingest failed", exc_info=True)
        _bump_counter("internal_error")
        return _err(
            500, "tombstone_write_failed", "tombstone could not be persisted"
        )
    _bump_counter("target_deleted_accepted")
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "tombstone": True,
            "cascade": {
                "samples": report.get("samples", 0),
                "revoked_job_ids": list(report.get("revoked_job_ids") or []),
                "result_tombstones": report.get("result_tombstones", 0),
                "deletion_tasks": report.get("deletion_tasks", 0),
                "unlinked_deliveries": report.get("unlinked_deliveries", 0),
            },
        },
        headers={"Cache-Control": "no-store"},
    )


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# T21 管理面路由（/chat/evaluations/*，Dashboard 同源调用，无认证头，
# 同源保护沿用既有 Dashboard 约束：无 CORS 宽松、错误契约统一 _err）
# ---------------------------------------------------------------------------

# 管理面 feedback 的 actor：服务端可信上下文（Dashboard 同源会话），
# 不从请求载荷读取（spec 10：actor 不由调用方自报）
_MANAGEMENT_ACTOR = "dashboard"


def _guarded(handler):
    """Mi2：管理面端点统一异常兜底——错误映射契约之外的意外异常一律
    500 internal_error（对齐 _err 契约与 producer 端点 Exception→500
    兜底模式），不裸抛未处理异常导致 FastAPI 默认 500 体泄漏堆栈细节。
    functools.wraps 保留原签名，FastAPI 路由参数注入不受影响。"""

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> JSONResponse:
        try:
            return await handler(*args, **kwargs)
        except Exception:
            logger.exception("evaluation management endpoint failed")
            return _err(500, "internal_error", "unexpected internal error")

    return wrapper


def register_evaluation_management_routes(
    app: APIRouter,
    *,
    evaluation_service: EvaluationService,
    config_service: Any,
    review_service: Any,
    retention_days: int = 7,
    agent_run_lookup: Any = None,
    evaluation_admin_service: Any = None,
    evaluation_annotation_service: Any = None,
    evaluation_analytics_service: Any = None,
) -> None:
    """注册 T21 管理面端点（组合根在 evaluation 配置服务就绪时调用）。

    错误映射契约：EvaluationValidationError / EvaluationConfigValidationError
    / DatasetManifestError -> 422；EvaluationNotFoundError -> 404；
    EvaluationConflictError / EvaluationRevisionConflictError /
    EvaluationConfigConflictError -> 409（m1：管理面无容量判定，无
    503 映射——503 仅 producer 入口容量拒绝语义）。
    Task 9（评估工作台）补充映射：UnsupportedCapabilityError ->
    422 unsupported_capability；EvaluationTargetDeletedError ->
    410 target_deleted；EvaluationTargetMissingError ->
    422 target_not_found；EvidenceUnavailableError ->
    422 evidence_unavailable；IncomparableExperimentsError ->
    422 incomparable_experiments（附可公开差异维度）。
    统一经 _err 输出，不泄漏内部细节（堆栈/SQL/绝对路径/sha 不进响应）。

    ``agent_run_lookup``：async (subject_id) -> Mapping | None，人评
    agent_run 目标的服务端核验（#15：目标必须对应真实 Run，None ->
    422 target_not_found）。未注入时跳过核验（组件未装配场景）。

    Task 9 新增注入（评估器/人工标注/分析路由，逐服务可选注册——
    组合根 gating 与管理面总闸一致）：
    - ``evaluation_admin_service`` / ``evaluation_annotation_service`` /
      ``evaluation_analytics_service``：Application 正式用例入口；标注
      队列（``AnnotationService.list_queue``）与结果快照分页
      （``EvaluationService.list_results_snapshot``）等只读投影同样经
      Application 用例编排（spec 7：新接口由 Application 正式用例编排，
      Interfaces 不 import Infrastructure、不触 SQLite、不直调账本端口）。
    """

    @app.get("/chat/evaluations/datasets")
    @_guarded
    async def get_datasets() -> JSONResponse:
        return _json_ok({"items": await review_service.list_datasets()})

    @app.get("/chat/evaluations/datasets/{dataset_id}/{version}")
    @_guarded
    async def get_dataset(dataset_id: str, version: str) -> JSONResponse:
        try:
            return _json_ok(await review_service.get_dataset(dataset_id, version))
        except EvaluationNotFoundError:
            return _err(404, "not_found", "dataset not found")
        except EvaluationValidationError as exc:
            return _err(422, "dataset_invalid", str(exc))

    @app.post("/chat/evaluations/datasets")
    @_guarded
    async def post_dataset(request: Request) -> JSONResponse:
        body, err = await _read_json_body(request)
        if err is not None:
            return err
        try:
            return _json_ok(await review_service.import_dataset(body))
        except EvaluationConflictError:
            return _err(409, "conflict", "dataset version already exists")
        except EvaluationValidationError as exc:
            return _err(422, "dataset_invalid", str(exc))

    @app.get("/chat/evaluations/overview")
    @_guarded
    async def get_overview() -> JSONResponse:
        """管理面概览只读聚合（T21/M3a-e）：容量积压、日预算用量、
        producer 心跳、低覆盖实验、证据保留天数。"""
        capacity = await evaluation_service.capacity_usage()
        budget = await evaluation_service.daily_budget_usage()
        producer_states = await evaluation_service.list_producer_states()
        experiment_results = (
            await evaluation_service.experiment_result_counts()
        )
        resolved = await config_service.get_resolved()
        return _json_ok({
            "capacity": capacity,
            "budget": {
                "date_key": budget.get("date_key"),
                "reserved_calls": budget.get("reserved_calls", 0),
                "settled_calls": budget.get("settled_calls", 0),
                "daily_call_ceiling": (
                    resolved.config.evaluation_daily_call_ceiling
                ),
            },
            "producer_states": [dict(row) for row in producer_states],
            "experiment_results": experiment_results,
            "evidence_retention_days": retention_days,
        })

    @app.post("/chat/evaluations/experiments")
    @_guarded
    async def post_experiment(request: Request) -> JSONResponse:
        body, err = await _read_json_body(request)
        if err is not None:
            return err
        try:
            evaluator_set = tuple(
                DefinitionKey(
                    definition_id=entry["definition_id"],
                    definition_version=entry["definition_version"],
                )
                for entry in body["evaluator_set"]
            )
            result = await evaluation_service.start_experiment(
                StartExperimentRequest(
                    dataset_id=body["dataset_id"],
                    dataset_version=body["dataset_version"],
                    dataset_hash=body["dataset_hash"],
                    target_config=body.get("target_config") or {},
                    evaluator_set=evaluator_set,
                    mode=body.get("mode") or "execute",
                    baseline_id=body.get("baseline_id"),
                    manifest_json=body.get("manifest_json") or "",
                )
            )
        except (KeyError, TypeError) as exc:
            return _err(422, "protocol_invalid", f"missing or invalid field: {exc}")
        except EvaluationNotFoundError as exc:
            return _err(404, "not_found", str(exc))
        except EvaluationValidationError as exc:
            return _err(422, "protocol_invalid", str(exc))
        except EvaluationConflictError:
            return _err(409, "conflict", "experiment conflict")
        return JSONResponse(
            status_code=202,
            content={
                "experiment_id": result.experiment_id,
                "status": "created",
                "dataset_hash": result.dataset_hash,
                # #19 执行语义显式化（诚实降级）：HTTP 创建的实验保持
                # created 状态，业务执行与评分经 CLI evaluate run 驱动
                # （server 进程不承载实验编排），不再让调用方误以为创建
                # 即执行
                "execution": "cli: n-agent evaluate run --dataset ...",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/chat/evaluations/experiments")
    @_guarded
    async def get_experiments() -> JSONResponse:
        """实验列表（Mi1：剥离 manifest_json——manifest 正文只在详情
        接口透出，列表行不携带大正文降低响应体积）。"""
        rows = await evaluation_service.list_experiments()
        items = []
        for row in rows:
            item = dict(row)
            item.pop("manifest_json", None)
            items.append(item)
        return _json_ok({"items": items})

    # -- Task 9：只读实验对比 + 概览分析（spec 4.5/4.6）。字面 compare
    #    路由先于动态 /experiments/{experiment_id} 注册（不同方法本就
    #    互不遮蔽，注册顺序仍按 spec 7 固定，防后续演化回归）---------
    if evaluation_analytics_service is not None:

        @app.post("/chat/evaluations/experiments/compare")
        @_guarded
        async def post_experiments_compare(request: Request) -> JSONResponse:
            body, err = await _read_json_body(request)
            if err is not None:
                return err
            err = _reject_unknown_fields(
                body, frozenset({"candidate_id", "baseline_id", "policy"})
            )
            if err is not None:
                return err
            candidate_id = body.get("candidate_id")
            baseline_id = body.get("baseline_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                return _err(422, "protocol_invalid", "candidate_id is required")
            if not isinstance(baseline_id, str) or not baseline_id:
                return _err(422, "protocol_invalid", "baseline_id is required")
            policy = body.get("policy")
            if policy is not None and not isinstance(policy, dict):
                return _err(422, "protocol_invalid", "policy must be an object")
            try:
                comparison = (
                    await evaluation_analytics_service.compare_experiments(
                        candidate_id, baseline_id, policy=policy
                    )
                )
            except EvaluationNotFoundError:
                return _err(404, "not_found", "experiment not found")
            except IncomparableExperimentsError as exc:
                # spec 9：列出可公开的差异维度（不携带内部路径/凭据）
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "incomparable_experiments",
                            "message": str(exc),
                            "diff_dimensions": dict(exc.diff_dimensions),
                        }
                    },
                    headers={"Cache-Control": "no-store"},
                )
            except EvaluationValidationError as exc:
                return _err(422, "protocol_invalid", str(exc))
            return _json_ok(dict(comparison))

        @app.get("/chat/evaluations/analytics")
        @_guarded
        async def get_analytics(request: Request) -> JSONResponse:
            """概览分析聚合（spec 4.5）：服务端完成全量过滤后统计，
            时间窗归一（默认最近 7 天、上限 90 天）在 Application 层。"""
            params = request.query_params
            try:
                overview = (
                    await evaluation_analytics_service.overview_analytics(
                        created_from=params.get("created_from"),
                        created_to=params.get("created_to"),
                        target_kind=params.get("target_kind"),
                        definition_id=params.get("definition_id"),
                        definition_version=params.get("definition_version"),
                        provenance=params.get("provenance"),
                        status=params.get("status"),
                    )
                )
            except EvaluationValidationError as exc:
                return _err(422, "protocol_invalid", str(exc))
            return _json_ok(dict(overview))

    @app.get("/chat/evaluations/experiments/{experiment_id}")
    @_guarded
    async def get_experiment_detail(experiment_id: str) -> JSONResponse:
        try:
            overview = await evaluation_service.experiment_overview(experiment_id)
        except EvaluationNotFoundError:
            return _err(404, "not_found", "experiment not found")
        return _json_ok(overview)

    @app.get("/chat/evaluations/results")
    @_guarded
    async def get_results(request: Request) -> JSONResponse:
        """结果分页查询（Task 9 扩展）。

        - 保留既有过滤与旧 b64 ``{"c","r"}`` 两段式游标兼容（旧游标 +
          无新增过滤走既有用例路径，响应保持 items/next_cursor 原形状，
          不含 snapshot_at——兼容分支刻意维持旧形状，历史客户端不受
          新字段影响）；
        - 新增过滤 created_from/created_to/definition_version/provenance
          或游标 v2 token 时走快照分页用例
          ``evaluation_service.list_results_snapshot``（直通账本端口
          ``query_results_page_snapshot`` 三元组返回形状，响应追加
          snapshot_at，后续页固定复用首次 UTC 上界）；旧格式游标携带
          新增过滤拒绝（422 cursor_invalid，store v2 分支同样拒绝，
          路由层先行判定以输出专有错误码）。
        """
        params = request.query_params
        limit, limit_err = _parse_limit(params)
        if limit_err is not None:
            return limit_err
        created_from = params.get("created_from")
        created_to = params.get("created_to")
        definition_version = params.get("definition_version")
        provenance = params.get("provenance")
        cursor = params.get("cursor")
        cursor_kind, cursor_err = _classify_result_cursor(cursor)
        if cursor_err is not None:
            return cursor_err
        has_new_filters = any(
            value is not None
            for value in (
                created_from,
                created_to,
                definition_version,
                provenance,
            )
        )
        if cursor_kind == "legacy" and has_new_filters:
            return _err(
                422,
                "cursor_invalid",
                "legacy results cursor is not allowed with extended filters",
            )
        try:
            if cursor_kind == "legacy":
                # 兼容路径：既有用例（旧两段式游标语义，无快照上界）
                page = await evaluation_service.list_results(
                    target_service=params.get("target_service"),
                    target_kind=params.get("target_kind"),
                    target_subject_id=params.get("target_subject_id"),
                    target_subject_revision=params.get(
                        "target_subject_revision"
                    ),
                    definition_id=params.get("definition_id"),
                    experiment_id=params.get("experiment_id"),
                    status=params.get("status"),
                    cursor=cursor,
                    limit=limit,
                )
                items = []
                for row in page.items:
                    item = dict(row)
                    item.pop("explanation", None)
                    items.append(item)
                return _json_ok({
                    "items": items,
                    "next_cursor": page.next_cursor,
                })
            # 快照路径：Application 快照分页用例（直通账本端口游标 v2
            # 分支：首次查询 UTC 上界编码进 token，后续页固定复用；
            # 过滤变更后复用旧游标被拒绝）
            rows, next_token, snapshot_at = (
                await evaluation_service.list_results_snapshot(
                    target_service=params.get("target_service"),
                    target_kind=params.get("target_kind"),
                    target_subject_id=params.get("target_subject_id"),
                    target_subject_revision=params.get(
                        "target_subject_revision"
                    ),
                    definition_id=params.get("definition_id"),
                    created_from=created_from,
                    created_to=created_to,
                    definition_version=definition_version,
                    experiment_id=params.get("experiment_id"),
                    status=params.get("status"),
                    provenance=provenance,
                    cursor=cursor,
                    limit=limit,
                )
            )
        except EvaluationValidationError as exc:
            return _err(422, "protocol_invalid", str(exc))
        # 不默认返回正文：列表条目剥离 Judge 解释正文（正文只在详情）
        items = []
        for row in rows:
            item = dict(row)
            item.pop("explanation", None)
            items.append(item)
        return _json_ok({
            "items": items,
            "next_cursor": next_token,
            "snapshot_at": snapshot_at,
        })

    @app.get("/chat/evaluations/results/{result_id}")
    @_guarded
    async def get_result_detail(result_id: str) -> JSONResponse:
        try:
            detail = await evaluation_service.get_result_detail(result_id)
        except EvaluationNotFoundError:
            return _err(404, "not_found", "result not found")
        return _json_ok({
            "result": dict(detail.result),
            "job": dict(detail.job) if detail.job is not None else None,
            "feedback": (
                dict(detail.feedback) if detail.feedback is not None else None
            ),
            "sample": (
                dict(detail.sample) if detail.sample is not None else None
            ),
            "evidence_summary": dict(detail.evidence_summary),
            "attempts": [dict(row) for row in detail.attempts],
            "deliveries": [dict(row) for row in detail.deliveries],
        })

    @app.post("/chat/evaluations/feedback")
    @_guarded
    async def post_feedback(request: Request) -> JSONResponse:
        body, err = await _read_json_body(request)
        if err is not None:
            return err
        target_body = body.get("target")
        if not isinstance(target_body, dict):
            return _err(422, "protocol_invalid", "target must be an object")
        # #15 服务端目标核验：agent_run 目标必须对应真实 Run——客户端
        # 自报 subject_id 不校验会让人评挂在不存在的运行上（Score 无从
        # 关联，观测栈死数据）。lookup 未接线（组件未装配）时跳过核验，
        # 保持向后兼容。
        subject_id = str(target_body.get("subject_id") or "")
        target_kind = str(target_body.get("kind") or "")
        if (
            target_kind == "agent_run"
            and agent_run_lookup is not None
            and subject_id
        ):
            try:
                run = await agent_run_lookup(subject_id)
            except Exception:
                logger.warning(
                    "feedback agent run lookup failed", exc_info=True
                )
                run = None
            if run is None:
                return _err(
                    422,
                    "target_not_found",
                    f"agent run not found: {subject_id}",
                )
            # #21 关联恢复（T21/AC18）：真实 Run 的身份回到 target——此前
            # lookup 只判空，target 仍由客户端四字段构造，真实 Run 的人评
            # 无 session_id，Langfuse 投影恒 unlinked。service 归属核验：
            # agent_run 目标只能来自 n-agent，声明不一致拒绝。
            declared_service = str(target_body.get("service") or "")
            if declared_service and declared_service != "n-agent":
                return _err(
                    422,
                    "service_mismatch",
                    f"agent run target service must be 'n-agent': "
                    f"{declared_service}",
                )
            enriched = dict(target_body)
            enriched["service"] = "n-agent"
            enriched["run_id"] = subject_id
            session_id = str(run.get("session_id") or "")
            if session_id:
                enriched["session_id"] = session_id
            target_body = enriched
        try:
            result = await evaluation_service.submit_feedback(
                _MANAGEMENT_ACTOR,
                SubmitFeedbackRequest(
                    target=_build_feedback_target(target_body),
                    definition_id=body["definition_id"],
                    definition_version=body["definition_version"],
                    value=body["value"],
                    comment_ref=body.get("comment_ref"),
                    request_key=body.get("request_key"),
                    expected_revision=body.get("expected_revision"),
                ),
            )
        except (KeyError, TypeError) as exc:
            return _err(422, "protocol_invalid", f"missing or invalid field: {exc}")
        except EvaluationNotFoundError as exc:
            return _err(404, "not_found", str(exc))
        except EvaluationValidationError as exc:
            return _err(422, "protocol_invalid", str(exc))
        except (EvaluationRevisionConflictError, EvaluationConflictError):
            return _err(
                409,
                "conflict",
                "feedback revision/idempotency conflict",
            )
        return _json_ok({
            "feedback_id": result.feedback_id,
            "result_id": result.result_id,
            "revision": result.revision,
            "superseded_feedback_id": result.superseded_feedback_id,
            "reused": result.reused,
        })

    @app.get("/chat/evaluations/config")
    @_guarded
    async def get_config() -> JSONResponse:
        resolved = await config_service.get_resolved()
        return _json_ok(_config_payload(resolved))

    @app.patch("/chat/evaluations/config")
    @_guarded
    async def patch_config(request: Request) -> JSONResponse:
        body, err = await _read_json_body(request)
        if err is not None:
            return err
        if not isinstance(body.get("patch"), dict):
            return _err(422, "protocol_invalid", "patch must be an object")
        expected_version = body.get("expected_version")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            return _err(
                422, "protocol_invalid", "expected_version must be a non-negative int"
            )
        try:
            resolved = await config_service.update(
                body["patch"], expected_version, updated_by=_MANAGEMENT_ACTOR
            )
        except EvaluationConfigValidationError as exc:
            return _err(422, "config_invalid", str(exc))
        except EvaluationConfigConflictError as exc:
            return _err(409, "conflict", str(exc))
        return _json_ok(_config_payload(resolved))

    @app.post("/chat/evaluations/deliveries/retry")
    @_guarded
    async def post_delivery_retry(request: Request) -> JSONResponse:
        body, err = await _read_json_body(request)
        if err is not None:
            return err
        sink = body.get("sink")
        result_id = body.get("result_id")
        if not isinstance(sink, str) or not sink:
            return _err(422, "protocol_invalid", "sink is required")
        if not isinstance(result_id, str) or not result_id:
            return _err(422, "protocol_invalid", "result_id is required")
        try:
            delivery = await evaluation_service.retry_delivery(sink, result_id)
        except EvaluationNotFoundError as exc:
            return _err(404, "not_found", str(exc))
        except EvaluationConflictError:
            return _err(
                409, "conflict", "delivery is terminal and cannot be retried"
            )
        return _json_ok({"delivery": dict(delivery)})

    @app.get("/chat/evaluations/review-queue")
    @_guarded
    async def get_review_queue(request: Request) -> JSONResponse:
        params = request.query_params
        reason = params.get("reason") or ""
        threshold_raw = params.get("threshold")
        limit_raw = params.get("limit")
        try:
            threshold = (
                float(threshold_raw) if threshold_raw is not None
                else DEFAULT_LOW_SCORE_THRESHOLD
            )
            limit = int(limit_raw) if limit_raw is not None else 50
        except ValueError:
            return _err(422, "protocol_invalid", "threshold/limit must be numeric")
        try:
            items = await review_service.review_queue(
                reason=reason, threshold=threshold, limit=limit
            )
        except EvaluationValidationError as exc:
            return _err(422, "protocol_invalid", str(exc))
        return _json_ok({"items": [dict(item) for item in items]})

    @app.post("/chat/evaluations/review-queue/export")
    @_guarded
    async def post_review_export(request: Request) -> JSONResponse:
        body, err = await _read_json_body(request)
        if err is not None:
            return err
        try:
            result = await review_service.export_reviewed_dataset(body)
        except EvaluationConflictError:
            return _err(409, "conflict", "dataset version already exported")
        except EvaluationIdentityLeakError as exc:
            return _err(422, "identity_leak", str(exc))
        except EvaluationValidationError as exc:
            return _err(422, "export_invalid", str(exc))
        return _json_ok(dict(result))

    # -- Task 9：评估器管理路由（spec 4.2/4.3；Application 正式用例，
    #    Interfaces 不触 SQLite）----------------------------------------
    if evaluation_admin_service is not None:

        @app.get("/chat/evaluations/evaluators/capabilities")
        @_guarded
        async def get_evaluator_capabilities() -> JSONResponse:
            """能力目录（spec 4.2）：只宣称当前执行器真实支持的组合。"""
            return _json_ok(evaluation_admin_service.capabilities())

        @app.get("/chat/evaluations/evaluators")
        @_guarded
        async def get_evaluators(request: Request) -> JSONResponse:
            params = request.query_params
            limit, limit_err = _parse_limit(params)
            if limit_err is not None:
                return limit_err
            try:
                page = await evaluation_admin_service.list_definitions(
                    target_kind=params.get("target_kind"),
                    method=params.get("method"),
                    limit=limit,
                    cursor=params.get("cursor"),
                )
            except EvaluationValidationError as exc:
                return _err(422, "protocol_invalid", str(exc))
            return _json_ok(page)

        @app.get("/chat/evaluations/evaluators/{definition_id}/{version}")
        @_guarded
        async def get_evaluator_detail(
            definition_id: str, version: str
        ) -> JSONResponse:
            try:
                detail = await evaluation_admin_service.get_definition_detail(
                    definition_id, version
                )
            except EvaluationNotFoundError:
                return _err(404, "not_found", "evaluator definition not found")
            return _json_ok(dict(detail))

        @app.post("/chat/evaluations/evaluators")
        @_guarded
        async def post_evaluator(request: Request) -> JSONResponse:
            body, err = await _read_json_body(request)
            if err is not None:
                return err
            try:
                result = await evaluation_admin_service.create_definition(body)
            except (KeyError, TypeError) as exc:
                return _err(
                    422, "protocol_invalid", f"missing or invalid field: {exc}"
                )
            except UnsupportedCapabilityError as exc:
                # 能力目录不含的组合（spec 4.2）：类型化错误码，不按
                # 消息文本分类
                return _err(422, exc.code, str(exc))
            except EvaluationValidationError as exc:
                return _err(422, "protocol_invalid", str(exc))
            except EvaluationConflictError as exc:
                return _err(409, "conflict", str(exc))
            return _json_ok(dict(result))

        @app.patch("/chat/evaluations/evaluators/{definition_id}/{version}/usage")
        @_guarded
        async def patch_evaluator_usage(
            definition_id: str, version: str, request: Request
        ) -> JSONResponse:
            body, err = await _read_json_body(request)
            if err is not None:
                return err
            err = _reject_unknown_fields(
                body, frozenset({"online_enabled", "expected_revision"})
            )
            if err is not None:
                return err
            online_enabled = body.get("online_enabled")
            if not isinstance(online_enabled, bool):
                return _err(
                    422, "protocol_invalid", "online_enabled must be a boolean"
                )
            err = _check_expected_revision(body)
            if err is not None:
                return err
            expected_revision = body.get("expected_revision")
            try:
                usage = await evaluation_admin_service.set_definition_usage(
                    definition_id,
                    version,
                    online_enabled=online_enabled,
                    expected_revision=expected_revision,
                )
            except EvaluationNotFoundError as exc:
                return _err(404, "not_found", str(exc))
            except EvaluationValidationError as exc:
                # 含 human 方法禁线上启用（spec 6.2/4.3）
                return _err(422, "protocol_invalid", str(exc))
            except EvaluationConflictError as exc:
                return _err(409, "conflict", str(exc))
            return _json_ok({"usage": dict(usage)})

    # -- Task 9：人工标注路由（spec 4.4）。队列列表经
    #    AnnotationService.list_queue 只读投影用例（审查修复：不再直调
    #    账本端口）；上下文/提交走 AnnotationService 正式用例--------------
    if evaluation_annotation_service is not None:

        @app.get("/chat/evaluations/annotations")
        @_guarded
        async def get_annotations(request: Request) -> JSONResponse:
            """标注队列（spec 4.4）：经 AnnotationService.list_queue 用例
            返回三元组 (rows, next_cursor, snapshot_at)；snapshot_at 口径
            为"响应生成时刻"（用例层生成——标注队列为 keyset 分页，无
            快照上界要求），Interfaces 不在路由层自造时间戳。"""
            params = request.query_params
            state = params.get("state") or "all"
            reason = params.get("reason") or "low_score"
            limit, limit_err = _parse_limit(params)
            if limit_err is not None:
                return limit_err
            threshold_raw = params.get("threshold")
            try:
                threshold = (
                    float(threshold_raw)
                    if threshold_raw is not None
                    else DEFAULT_LOW_SCORE_THRESHOLD
                )
            except ValueError:
                return _err(422, "protocol_invalid", "threshold must be numeric")
            try:
                rows, next_cursor, snapshot_at = (
                    await evaluation_annotation_service.list_queue(
                        state=state,
                        reason=reason,
                        threshold=threshold,
                        limit=limit,
                        cursor=params.get("cursor"),
                    )
                )
            except EvaluationValidationError as exc:
                return _err(422, "protocol_invalid", str(exc))
            return _json_ok({
                "items": [dict(row) for row in rows],
                "next_cursor": next_cursor,
                "snapshot_at": snapshot_at,
            })

        @app.get("/chat/evaluations/annotations/{source_result_id}")
        @_guarded
        async def get_annotation(source_result_id: str) -> JSONResponse:
            """标注上下文（spec 4.4）：服务端核验目标/证据/latest 人评。"""
            try:
                context = (
                    await evaluation_annotation_service.get_annotation_context(
                        source_result_id
                    )
                )
            except EvaluationNotFoundError:
                return _err(404, "not_found", "source result not found")
            except EvaluationTargetDeletedError:
                return _target_deleted_err()
            return _json_ok(dict(context))

        @app.post("/chat/evaluations/annotations/{source_result_id}")
        @_guarded
        async def post_annotation(
            source_result_id: str, request: Request
        ) -> JSONResponse:
            """标注提交（spec 4.4）：字段白名单（actor 由服务端注入，
            不从载荷读取）；目标类型先行核验，不支持的类型不创建评分。"""
            body, err = await _read_json_body(request)
            if err is not None:
                return err
            err = _reject_unknown_fields(
                body, frozenset({"value", "request_key", "expected_revision"})
            )
            if err is not None:
                return err
            request_key = body.get("request_key")
            if not isinstance(request_key, str) or not request_key:
                return _err(
                    422,
                    "protocol_invalid",
                    "request_key must be a non-empty string",
                )
            err = _check_expected_revision(body)
            if err is not None:
                return err
            expected_revision = body.get("expected_revision")
            if "value" not in body:
                return _err(422, "protocol_invalid", "value is required")
            try:
                context = (
                    await evaluation_annotation_service.get_annotation_context(
                        source_result_id
                    )
                )
            except EvaluationNotFoundError:
                return _err(404, "not_found", "source result not found")
            except EvaluationTargetDeletedError:
                return _target_deleted_err()
            except EvaluationValidationError as exc:
                return _err(422, "protocol_invalid", str(exc))
            if context.get("reject_reason") == "target_kind_not_supported":
                return _err(
                    422,
                    "target_kind_not_supported",
                    "only automatic agent_run results support annotation",
                )
            try:
                result = await evaluation_annotation_service.submit_annotation(
                    source_result_id,
                    value=body["value"],
                    request_key=request_key,
                    expected_revision=expected_revision,
                )
            except EvaluationTargetDeletedError:
                return _target_deleted_err()
            except EvaluationTargetMissingError as exc:
                return _err(422, "target_not_found", str(exc))
            except EvidenceUnavailableError as exc:
                return _err(422, "evidence_unavailable", str(exc))
            except EvaluationRevisionConflictError as exc:
                return _err(409, "conflict", str(exc))
            except EvaluationConflictError as exc:
                return _err(409, "conflict", str(exc))
            except EvaluationValidationError as exc:
                return _err(422, "protocol_invalid", str(exc))
            except EvaluationNotFoundError as exc:
                return _err(404, "not_found", str(exc))
            return _json_ok({
                "feedback_id": result.feedback_id,
                "result_id": result.result_id,
                "revision": result.revision,
                "is_current": result.is_current,
                "duplicate": result.duplicate,
                "superseded_feedback_id": result.superseded_feedback_id,
            })


def _parse_limit(params: Any) -> tuple[int, JSONResponse | None]:
    """管理面 GET 列表路由共用的 ``limit`` 查询参数解析（默认 50）。

    与 Application 层校验冗余、刻意前置（Interfaces 快速失败），错误
    契约稳定："limit must be an integer"（422 protocol_invalid）。"""
    limit_raw = params.get("limit")
    if limit_raw is None:
        return 50, None
    try:
        return int(limit_raw), None
    except ValueError:
        return 0, _err(422, "protocol_invalid", "limit must be an integer")


def _reject_unknown_fields(
    body: Mapping[str, Any], allowed: frozenset[str]
) -> JSONResponse | None:
    """管理面请求体顶层键白名单校验（未知键 422 protocol_invalid）。

    与 Application 校验冗余、刻意前置（Interfaces 快速失败），错误契约
    稳定："unknown fields: [...]"（键名排序回显，避免顺序抖动）。"""
    unknown = sorted(set(body.keys()) - set(allowed))
    if unknown:
        return _err(422, "protocol_invalid", f"unknown fields: {unknown}")
    return None


def _check_expected_revision(body: Mapping[str, Any]) -> JSONResponse | None:
    """``expected_revision`` 前置类型校验（非负 int，bool 排除）。

    与 Application 校验冗余、刻意前置（Interfaces 快速失败），错误契约
    稳定："expected_revision must be a non-negative int"。"""
    expected_revision = body.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        return _err(
            422,
            "protocol_invalid",
            "expected_revision must be a non-negative int",
        )
    return None


def _target_deleted_err() -> JSONResponse:
    """标注目标主体已删除的统一 410 映射（get/post annotation 共用）。"""
    return _err(
        410,
        "target_deleted",
        "annotation target subject has been deleted",
    )


def _reject_json_constant(value: str) -> Any:
    """json parse_constant 钩子：NaN/Infinity 等 JSON 标准外字面量一律
    拒绝（Python json 默认接受，非有限数值流入 Domain VO 会造成非法
    值落库；spec 7：NaN/Infinity 拒绝，不依赖异常字符串识别）。"""
    raise ValueError(f"non-standard JSON constant rejected: {value}")


async def _read_json_body(request: Request) -> tuple[Any, JSONResponse | None]:
    """管理面 JSON 请求体解析：非法 body 统一 422 错误契约。

    经 parse_constant 钩子拒绝 NaN/Infinity（覆盖全部 POST/PATCH
    管理面请求体，Task 9 起生效）。"""
    raw = await request.body()
    try:
        body = json.loads(
            raw.decode("utf-8"), parse_constant=_reject_json_constant
        )
    except ValueError as exc:
        # parse_constant 钩子（_reject_json_constant，原文案见其 docstring）
        # 拒绝的 NaN/Infinity 等标准外常量：错误信息补括注说明，便于
        # 调用方定位；其余解析失败维持原文案（JSONDecodeError 亦为
        # ValueError 子类，按消息判别不命中括注）
        message = "body must be valid JSON"
        if "non-standard JSON constant" in str(exc):
            message += (
                " (non-standard JSON constants like NaN/Infinity are"
                " rejected)"
            )
        return None, _err(422, "protocol_invalid", message)
    except Exception:
        return None, _err(422, "protocol_invalid", "body must be valid JSON")
    if not isinstance(body, dict):
        return None, _err(422, "protocol_invalid", "body must be a JSON object")
    return body, None


def _classify_result_cursor(
    cursor: str | None,
) -> tuple[str, JSONResponse | None]:
    """分类 results 游标：``""``=无游标 / ``"legacy"``=历史 b64
    ``{"c","r"}`` 两段式 / ``"v2"``=游标 v2 token。

    无法解析返回 422 cursor_invalid 响应；裸两段式（含 ``|``，非本
    接口历史签发格式）按 legacy 放行，由下游既有校验拒绝。"""
    if not cursor:
        return "", None
    if "|" in cursor:
        return "legacy", None
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception:
        return "", _err(422, "cursor_invalid", "results cursor is invalid")
    if isinstance(raw, dict) and raw.get("v") == 2:
        return "v2", None
    if isinstance(raw, dict) and set(raw.keys()) == {"c", "r"}:
        return "legacy", None
    return "", _err(422, "cursor_invalid", "results cursor is invalid")


def _json_ok(content: Mapping[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=content,
        headers={"Cache-Control": "no-store"},
    )


def _build_feedback_target(payload: Mapping[str, Any]) -> EvaluationTarget:
    return EvaluationTarget(
        service=payload["service"],
        kind=payload["kind"],
        subject_id=payload["subject_id"],
        subject_revision=payload["subject_revision"],
        # 真实身份关联（#21）：路由层从真实 Run 恢复后注入
        session_id=payload.get("session_id"),
        run_id=payload.get("run_id"),
        trace_id=payload.get("trace_id"),
        span_id=payload.get("span_id"),
    )


def _config_payload(resolved: Any) -> dict[str, Any]:
    return {
        "config": asdict(resolved.config),
        "version": resolved.version,
        "overridden_fields": list(resolved.overridden_fields),
        "updated_at": resolved.updated_at,
        "updated_by": resolved.updated_by,
    }
