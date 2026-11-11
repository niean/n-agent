# app/application/evaluation_capture.py
"""线上评估捕获接线（spec 7.1，plan T16）。

在 AgentRun 最终业务结果可用的边界（agent_graph.finalize 之后）形成受控
评估快照，经确定性哈希抽样决策后，非阻塞放入生命周期托管的有界后台队列；
后台消费者把快照持久化（EvidenceStore）并登记样本（EvaluationService）
后由 Evaluation Worker 恢复执行。本模块只依赖 application/domain 层。

核心边界（spec 7.1）：
- 抽样：按 ``H(policy_version, service, subject_id) / 2^256 < p`` 决策，
  默认 p=0（关闭）；不依赖 Trace 是否 sampled，也不随重试重新抽签
  （同 subject 同决策，subject 由 session + 输入请求摘要确定性派生）。
  故障定向采样（failure_rate）使用同一哈希的另一阈值，独立记 cohort
  （failure 优先标记），不与随机样本混算总体质量。
- 快照内容（spec 6.3/7.1）：只包含允许字段——query/最终回答/允许的工具
  轨迹与副作用证明/真实消费的检索片段与稳定身份/真值引用/模型与目标
  版本/采集完整性/脱敏版本。按构造排除 SDK 对象、凭据、完整 system
  prompt 与受禁截图；脱敏先于入队，脱敏后的内容即实际评估输入。
- 捕获/入队异常不回滚业务成功：所有异常隔离为 capture_error 计数 +
  warning 日志；业务 finalize 路径永不等待网络或 Judge（入队为同步
  put_nowait，消费者在后台任务执行持久化）。
- 有界后台队列：asyncio.Queue(maxsize) 满时拒绝并计 queue_full，禁止
  每次捕获创建无界 task；消费者为单一生命周期任务（start/stop）。
- 覆盖率：eligible/selected/captured/capture_error/queue_full 累计计数
  （字段集与 T10 ``PRODUCER_MEASURES`` 对齐，经组合根喂给
  EvaluationSnapshotHolder 的 producer Gauge；T20 接线）。
- 证据缓冲淘汰不入 producer gauge 口径：buffer 淘汰（每键记录数/键数
  上限）只递增 buffer 自身的 ``dropped_total`` 并反映在快照
  ``capture_completeness.evidence_drops``，不计入 capture_error——
  capture_error 仅表示"本应捕获却失败"，与抽样率解耦；producer 五字段
  集合锁定为 T10 契约，不扩字段。

真实消费的检索上下文来自 KnowledgeService 在真实检索结果返回处的就地
登记（KnowledgeSearchResult 可选 evaluation_context / 稳定版本字段，
向后兼容），工具轨迹来自 ToolService 执行后的就地登记；两者汇入
EvaluationCaptureBuffer（有界、按会话键），finalize 时 drain 汇聚为
快照内容。禁止再次查询 KB 重建上下文。

组合根接线（T20）：EvaluationCaptureService 实例注入 AgentGraphRunner
（构造参数 evaluation_capture）、ToolService 与 KnowledgeService（各自
的可选 evaluation_capture 参数，三者仅使用 ``buffer`` 属性的鸭子类型）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from fractions import Fraction
from typing import Any, Callable, Mapping

from app.application.evaluation_service import SubmitSampleRequest
from app.domain.evaluation.models import (
    EvaluationTarget,
    EvaluationValidationError,
    EvidenceSnapshot,
)
from app.domain.agent_run import current_run
from app.domain.knowledge import KnowledgeSearchResult
from app.domain.tool import ToolResultStatus

logger = logging.getLogger(__name__)

# 快照 schema 版本（spec 6.3 evaluation_samples.schema_version）
CAPTURE_SNAPSHOT_SCHEMA_VERSION = "evaluation.snapshot.v1"
# 脱敏版本：脱敏是快照可复现性的一部分（spec 6.3）。v2（T20 质量复审
# m5 回归修复）：快照期固定叠加内建凭据形态正则（注入 redactor 之后
# 兜底），脱敏语义变化 bump 版本；该值为纯元数据（快照正文与引用行
# 透传，无条件分支消费方），与 sink redact_comment 独立、兼容
REDACTION_VERSION = "capture-redaction-v2"
# 抽样策略默认版本
SAMPLING_POLICY_VERSION = "capture-sampling-v1"

DEFAULT_QUEUE_MAXSIZE = 1000
DEFAULT_BUFFER_MAX_KEYS = 256
DEFAULT_BUFFER_MAX_RECORDS = 50

# 单条字符串在快照内的上限（UTF-8 字符数）；整快照 2MiB 上限由
# EvidenceSnapshot 构造期校验兜底
MAX_SNAPSHOT_FIELD_CHARS = 8192
# 记录登记时的单字符串截断上限（先截断、快照期再脱敏）
MAX_RECORD_FIELD_CHARS = 2048
# 快照内嵌套容器上限（键数/长度/深度）
MAX_SNAPSHOT_ITEMS = 64
MAX_SNAPSHOT_DEPTH = 8

_COHORT_RANDOM = "random"
_COHORT_FAILURE = "failure"

# 覆盖率计数字段集（与 T10 PRODUCER_MEASURES 对齐）
CAPTURE_COUNTERS: tuple[str, ...] = (
    "eligible", "selected", "captured", "capture_error", "queue_full",
)

# 快照禁含键（大小写不敏感的片段匹配；防御性排除，正文按允许字段构造）
_FORBIDDEN_KEY_FRAGMENTS: frozenset[str] = frozenset({
    "credential", "api_key", "apikey", "secret", "password", "passwd",
    "token", "authorization", "cookie", "system_prompt", "sdk",
    "screenshot", "session_key", "private_key",
})

# 内建凭据形态脱敏（组合根可注入 redactor 覆盖）。T20 覆盖面确认补充：
# JWT（eyJ 三段式）、Slack xox* token、Google OAuth ya29. 凭据
_DEFAULT_REDACT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    # JWT：header.payload.signature 三段 base64url（header 以 eyJ 开头）
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Slack：xoxb/xoxp/xoxa/xoxs 等前缀 token
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    # Google OAuth 2.0 access token
    re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),
    # AWS secret access key（40 位 base64-ish；AKIA access key id 已在上）。
    # 保守方向：40 位通配形态存在误伤面（如任意 40 位 base64-ish 串），
    # 宁可误脱敏不漏脱敏；后续可优化为带上下文提示（如前置
    # aws_secret_access_key/= 线索）的定向匹配
    re.compile(
        r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"
    ),
)

_REDACTED = "[redacted]"


def _default_redact(text: str) -> str:
    for pattern in _DEFAULT_REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def _identity_from_trace_carrier(
    carrier: Mapping[str, str] | None,
) -> tuple[str | None, str | None]:
    """从 W3C traceparent 载体解析真实 trace_id/span_id（无/非法返回 None）。

    评估目标只允许携带真实 Trace 身份，绝不构造假 ID（spec 7.2）。
    """
    if not isinstance(carrier, Mapping):
        return None, None
    traceparent = carrier.get("traceparent")
    if not isinstance(traceparent, str):
        return None, None
    match = _TRACEPARENT_RE.fullmatch(traceparent)
    if match is None:
        return None, None
    return match.group(1), match.group(2)


def _truncate_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _truncate_record_text(text: str) -> str:
    return _truncate_chars(text, MAX_RECORD_FIELD_CHARS)


def _stable_default(value: Any) -> str:
    """json.dumps default 钩子的跨进程稳定投影：不带内存地址。

    datetime/date 投影 isoformat（工具结果常见且信息承载）；其余对象投影
    类型名（无 repr 地址，跨进程幂等；详细内容不作恢复承诺——摘要只是
    副作用证明的辅助线索）。
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return f"<{type(value).__name__}>"


def jsonable_excerpt(value: Any) -> str:
    """工具结果的短摘要（服务端可信证据）。

    脱敏先于限长（与快照期同一原则）：先应用内建凭据形态正则（组合根
    注入的 redactor 在快照期再次兜底），再按 ``MAX_RECORD_FIELD_CHARS``
    UTF-8 字符截断——若先截断后脱敏，跨截断边界的凭据会被劈开而逃过
    正则匹配。
    """
    try:
        text = json.dumps(value, ensure_ascii=False, default=_stable_default)
    except Exception:
        text = _stable_default(value)
    return _truncate_record_text(_default_redact(text))


# ---------------------------------------------------------------------------
# 抽样
# ---------------------------------------------------------------------------


def _fraction_less(h: int, rate: float) -> bool:
    """精确判定 h / 2^256 < rate（不经 float 乘法损失精度）。"""
    return Fraction(h, 2**256) < Fraction(rate)


@dataclass(frozen=True)
class EvaluationSamplingPolicy:
    """确定性哈希抽样策略（spec 7.1）。

    rate 是随机 cohort 抽样率，failure_rate 是故障定向 cohort 抽样率；
    两者使用同一 H(policy_version, service, subject_id)，阈值独立，
    cohort 独立标记（failure 优先）。默认全 0（捕获关闭）。

    哈希输入用 ``|`` 分隔符拼接：理论上存在分隔符碰撞（如
    policy_version 含 ``|``），但 service 为服务端常量、policy_version
    由配置管理，实际无风险；变更哈希键序等同更换 policy_version。
    """

    policy_version: str = SAMPLING_POLICY_VERSION
    rate: float = 0.0
    failure_rate: float = 0.0

    def _hash(self, service: str, subject_id: str) -> int:
        digest = hashlib.sha256(
            f"{self.policy_version}|{service}|{subject_id}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest, "big")

    def cohort_for(
        self, service: str, subject_id: str, *, failed: bool = False,
    ) -> str | None:
        """返回命中的 cohort（random/failure），未命中返回 None。

        同 (policy_version, service, subject_id) 永远同决策：重试不重抽。
        """
        h = self._hash(service, subject_id)
        if failed and self.failure_rate > 0 and _fraction_less(h, self.failure_rate):
            return _COHORT_FAILURE
        if self.rate > 0 and _fraction_less(h, self.rate):
            return _COHORT_RANDOM
        return None

    def probability_for(self, cohort: str) -> float:
        return self.failure_rate if cohort == _COHORT_FAILURE else self.rate


def derive_agent_run_subject(session_id: str, input_messages: Any) -> str:
    """派生跨重试稳定的抽样 subject：session + 输入请求内容摘要。

    重试（同 session 同输入）得到同一 subject，因此不随重试重新抽签。
    """
    try:
        payload = json.dumps(
            [
                message.get("content") if isinstance(message, Mapping) else message
                for message in (input_messages or ())
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=_stable_default,
        )
    except Exception:
        payload = ""
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{session_id}:{digest}"


# ---------------------------------------------------------------------------
# 覆盖率计数
# ---------------------------------------------------------------------------


class EvaluationCaptureCounters:
    """累计捕获计数（eligible/selected/captured/capture_error/queue_full）。

    字段集与 T10 ``PRODUCER_MEASURES`` 一致；组合根经
    ``snapshot()`` 喂给 EvaluationSnapshotHolder 的 producer Gauge
    （nagent.evaluation.producer.*，spec 12.3）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {name: 0 for name in CAPTURE_COUNTERS}

    def bump(self, name: str) -> None:
        if name not in CAPTURE_COUNTERS:
            raise ValueError(f"未知捕获计数字段: {name}")
        with self._lock:
            self._values[name] += 1

    def set_values(self, values: Mapping[str, int]) -> None:
        """恢复持久化计数（#28：重启不归零；仅接受已知字段，单调取大）。"""
        with self._lock:
            for name, value in (values or {}).items():
                if name in CAPTURE_COUNTERS:
                    self._values[name] = max(
                        self._values[name], int(value)
                    )

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


class PersistingCaptureCounters(EvaluationCaptureCounters):
    """持久化捕获计数（#28）：bump 后异步落盘，重启经 ``load()`` 恢复。

    - 计数不再随进程重启归零（producer 高水位累计口径）；
    - counter epoch 首次持久化时生成并稳定复用（Gauge 覆盖率口径
      "自当前 counter epoch 起"跨重启一致）；
    - flush 节流：同事件循环内已有在途 flush 时合并（dirty 标记）；
      无运行事件循环时跳过（stop() 的同步 flush 兜底）。"""

    def __init__(
        self,
        persistence: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        self._persistence = persistence
        self._epoch: str | None = None
        self._dirty = False
        self._flush_task: asyncio.Task | None = None

    @property
    def counter_epoch(self) -> str | None:
        return self._epoch

    async def load(self) -> None:
        """启动恢复：读取持久化计数合并进内存（单调取大）。"""
        try:
            row = await self._persistence.load_capture_counters()
        except Exception:
            logger.warning(
                "evaluation capture counters load failed", exc_info=True
            )
            row = None
        if row:
            self._epoch = str(row.get("counter_epoch") or "") or None
            self.set_values(row.get("values") or {})
        if self._epoch is None:
            self._epoch = uuid.uuid4().hex

    def bump(self, name: str) -> None:
        super().bump(name)
        self._dirty = True
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环：flush 由 stop()/显式 flush 兜底
        if self._flush_task is not None and not self._flush_task.done():
            return  # 在途 flush 合并（dirty 标记）
        self._flush_task = loop.create_task(self._flush())

    async def _flush(self) -> None:
        self._dirty = False
        values = self.snapshot()
        try:
            await self._persistence.save_capture_counters(
                values, self._epoch or ""
            )
        except Exception:
            self._dirty = True  # 下次 bump 重试
            logger.warning(
                "evaluation capture counters flush failed", exc_info=True
            )

    async def flush(self) -> None:
        """显式落盘（stop 时调用）：合并在途 flush 任务后强制写一次。"""
        if self._flush_task is not None and not self._flush_task.done():
            try:
                await self._flush_task
            except Exception:
                pass
        await self._flush()


# ---------------------------------------------------------------------------
# 有界证据缓冲（检索片段与工具轨迹的登记）
# ---------------------------------------------------------------------------


class _BufferBucket:
    """单个会话键的登记桶（检索/工具双队列 + 本桶淘汰计数）。"""

    __slots__ = ("retrievals", "tools", "drops")

    def __init__(self, max_records: int) -> None:
        self.retrievals: deque = deque(maxlen=max_records)
        self.tools: deque = deque(maxlen=max_records)
        self.drops: int = 0


class EvaluationCaptureBuffer:
    """按会话键的有界检索/工具证据缓冲。

    KnowledgeService（真实检索结果返回处）与 ToolService（工具执行后）
    就地登记；finalize 时 ``drain`` 汇聚为快照内容。有界：每键记录数
    （淘汰最旧）与键数（LRU 淘汰）受上限约束。

    淘汰观测与 producer gauge 解耦（M1 修复）：淘汰只递增本缓冲的
    ``dropped_total`` 并经 ``on_drop`` 通知（组合根可接日志/自定义观测），
    不计入 capture_error——capture_error 仅表示"本应捕获却失败"，与
    抽样率解耦；未抽样会话（默认 p=0）的登记/淘汰不影响 producer 五
    字段口径。``drain`` 返回本桶登记期淘汰数（快照
    ``capture_completeness.evidence_drops``）；键淘汰整桶丢弃，只计入
    ``dropped_total``。只接受 Mapping 记录（浅拷贝存储）；非 Mapping
    直接丢弃并通知。
    """

    def __init__(
        self,
        *,
        max_keys: int = DEFAULT_BUFFER_MAX_KEYS,
        max_records: int = DEFAULT_BUFFER_MAX_RECORDS,
        on_drop: Callable[[Any], None] | None = None,
    ) -> None:
        self._max_keys = max(1, max_keys)
        self._max_records = max(1, max_records)
        self._on_drop = on_drop
        self._lock = threading.Lock()
        self._dropped_total = 0
        self._keys: OrderedDict[str, _BufferBucket] = OrderedDict()

    @property
    def dropped_total(self) -> int:
        """累计淘汰记录数（含键淘汰整桶丢弃；不入 producer gauge 口径）。"""
        with self._lock:
            return self._dropped_total

    def _notify_drop(self, record: Any) -> None:
        """淘汰通知回调（不得回调缓冲自身；调用方负责递增 dropped_total）。"""
        if self._on_drop is not None:
            try:
                self._on_drop(record)
            except Exception:
                # Phase 5 扫描修复（Warning）：回调隔离语义保留（淘汰
                # 通知失败绝不阻塞采集），但必须留日志线索
                logger.debug("evaluation capture drop-notify callback failed", exc_info=True)

    def _record(self, session_id: str, kind: str, record: Any) -> None:
        if not isinstance(record, Mapping):
            with self._lock:
                self._dropped_total += 1
            self._notify_drop(record)
            return
        evicted: list[Any] = []
        try:
            with self._lock:
                bucket = self._keys.get(session_id)
                if bucket is None:
                    while len(self._keys) >= self._max_keys:
                        _, evicted_bucket = self._keys.popitem(last=False)
                        for queued in (evicted_bucket.retrievals, evicted_bucket.tools):
                            self._dropped_total += len(queued)
                            evicted.extend(queued)
                    bucket = _BufferBucket(self._max_records)
                    self._keys[session_id] = bucket
                else:
                    self._keys.move_to_end(session_id)
                queued = bucket.retrievals if kind == "retrievals" else bucket.tools
                if len(queued) >= self._max_records:
                    evicted.append(queued[0])
                    self._dropped_total += 1
                    bucket.drops += 1
                queued.append(dict(record))
        except Exception:
            with self._lock:
                self._dropped_total += 1
            self._notify_drop(record)
            return
        # 通知在锁外执行（回调不得回调缓冲自身）
        for item in evicted:
            self._notify_drop(item)

    def record_retrieval(self, session_id: str, record: Mapping[str, Any]) -> None:
        self._record(session_id, "retrievals", record)

    def record_tool(self, session_id: str, record: Mapping[str, Any]) -> None:
        self._record(session_id, "tools", record)

    def drain(self, session_id: str) -> dict[str, Any]:
        """取出并清空该会话的全部登记记录，返回 {"retrievals","tools","drops"}。"""
        with self._lock:
            bucket = self._keys.pop(session_id, None)
        result: dict[str, Any] = {"retrievals": [], "tools": [], "drops": 0}
        if bucket is None:
            return result
        result["retrievals"] = list(bucket.retrievals)
        result["tools"] = list(bucket.tools)
        result["drops"] = int(bucket.drops)
        return result


def capture_session_key(explicit: str | None = None) -> str | None:
    """登记键归一化（Mi5）：显式会话 ID 优先，回退当前 Run 绑定。

    ToolService 传 ToolExecutionContext.session_id（生产路径由
    chat_service 注入），KnowledgeService 不传（仅运行绑定可用）；
    finalize 按同一会话 ID drain，键双轨在此处归一为单一来源。
    """
    if explicit:
        return explicit
    binding = current_run.get()
    if binding is not None:
        return binding.session_id
    return None


def retrieval_record_from_result(result: KnowledgeSearchResult) -> dict[str, Any]:
    """把真实检索结果归一为证据记录（稳定身份；内容截断待脱敏）。

    只使用返回结果自身携带的信息，绝不再次查询 KB 重建上下文。
    """
    context = result.evaluation_context
    chunks = []
    for rank, snippet in enumerate(result.results, start=1):
        chunks.append({
            "rank": rank,
            "chunk_id": snippet.chunk_id or snippet.id,
            "document_id": snippet.document_id,
            "document_revision": snippet.document_revision,
            "content_hash": snippet.chunk_content_hash,
            "score": snippet.score,
            "excerpt": _truncate_record_text(snippet.content),
        })
    return {
        "kb_id": result.kb_id,
        "query": result.query,
        "retrieval_id": context.retrieval_id if context is not None else None,
        "corpus_revision": context.corpus_revision if context is not None else None,
        "chunks": chunks,
    }


# ---------------------------------------------------------------------------
# 有界后台队列
# ---------------------------------------------------------------------------


class EvaluationCaptureQueue:
    """生命周期托管的有界捕获队列。

    ``put_nowait`` 仅供业务完成钩子同步调用（满即拒绝，绝不阻塞业务）；
    消费者是 start/stop 管理的单一后台任务，逐条调用注入的 handler
    （持久化登记），禁止每次捕获创建无界 task。队列未 start 时允许
    put_nowait（条目滞留在有界队列中，等待 start 后消费）。
    """

    def __init__(
        self,
        *,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        handler: Callable[[Any], Any] | None = None,
    ) -> None:
        self._maxsize = max(1, maxsize)
        self._handler = handler
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # 仅吞消费者自身的取消；调用方（外层 task）被取消时不吞，
                # 原样上抛保持取消语义
                if not task.cancelled():
                    raise
            except Exception:
                pass

    def put_nowait(self, item: Any) -> bool:
        """同步非阻塞入队；满返回 False（由调用方计 queue_full）。"""
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            return False
        return True

    def queued_items(self) -> list[Any]:
        """调试/测试视图：当前队列内条目（快照副本）。"""
        if self._queue is None:
            return []
        return list(self._queue._queue)  # noqa: SLF001 有界队列的测试观测面

    def depth(self) -> int:
        if self._queue is None:
            return 0
        return self._queue.qsize()

    @property
    def done(self) -> bool:
        task = self._task
        return task is None or task.done()

    @property
    def consumer_task(self) -> asyncio.Task | None:
        """当前消费者任务（测试观测 start/stop 不泄漏任务）。"""
        return self._task

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if self._handler is not None:
                    await self._handler(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # handler 自身负责计数；此处兜底保证消费者不因单条失败停摆
                logger.warning(
                    "evaluation capture consumer item failed", exc_info=True,
                )
            finally:
                self._queue.task_done()


# ---------------------------------------------------------------------------
# 捕获服务
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureEnvelope:
    """入队条目：持久化所需的全部材料。"""

    target: EvaluationTarget
    snapshot: EvidenceSnapshot
    producer_event_id: str
    sample_probability: float
    sampling_policy_version: str
    captured_at: str
    cohort: str
    evidence_targets: tuple[Mapping[str, Any], ...] = ()


def _redact_capture_value(
    value: Any,
    redactor: Callable[[str], str],
    *,
    depth: int = 0,
) -> Any:
    """递归脱敏并排除禁含键；输出保证 JSON 原生类型。

    非有限 float（NaN/Inf）投影为 None：domain ``canonical_json_bytes``
    以 allow_nan=False 拒绝非有限数，直通会让整条快照作废。
    """
    if depth > MAX_SNAPSHOT_DEPTH:
        return _truncate_chars(_stable_default(value), MAX_SNAPSHOT_FIELD_CHARS)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _truncate_chars(redactor(value), MAX_SNAPSHOT_FIELD_CHARS)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            # 非 str 键加类型前缀，避免 str(key) 碰撞静默覆盖
            key_text = (
                key if isinstance(key, str) else f"{type(key).__name__}:{key}"
            )
            lowered = key_text.lower()
            if any(fragment in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                continue
            if count >= MAX_SNAPSHOT_ITEMS:
                break
            count += 1
            redacted[key_text] = _redact_capture_value(
                item, redactor, depth=depth + 1,
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [
            _redact_capture_value(item, redactor, depth=depth + 1)
            for item in list(value)[:MAX_SNAPSHOT_ITEMS]
        ]
    # 非 JSON 原生（SDK 对象等）只保留稳定的类型名投影（无内存地址，
    # 跨进程幂等；str() 会带 0x 地址破坏可复现性），绝不原样保存
    return _truncate_chars(
        redactor(f"<{type(value).__name__}>"), MAX_SNAPSHOT_FIELD_CHARS,
    )


class EvaluationCaptureService:
    """线上评估捕获编排：抽样 -> 快照 -> 有界入队 -> 后台持久登记。"""

    def __init__(
        self,
        *,
        policy: EvaluationSamplingPolicy | None = None,
        policy_provider: Callable[[], EvaluationSamplingPolicy | None] | None = None,
        counters: EvaluationCaptureCounters | None = None,
        buffer: EvaluationCaptureBuffer | None = None,
        queue: EvaluationCaptureQueue | None = None,
        evidence_store: Any = None,
        evaluation_service: Any = None,
        service: str = "n-agent",
        redaction_version: str = REDACTION_VERSION,
        redactor: Callable[[str], str] | None = None,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = (
            EvaluationSamplingPolicy() if policy is None else policy
        )
        # T20 组合根接线：policy_provider 提供 C 类热重载抽样策略；返回
        # None 表示 C enabled=False（捕获总闸关闭）。未注入时退回构造期
        # 固定 policy（向后兼容 T16 直测路径）。
        self._policy_provider = policy_provider
        self.counters = counters or EvaluationCaptureCounters()
        # 缓冲淘汰不入 producer gauge 口径（M1）：on_drop 仅做 debug 日志，
        # 淘汰量经 buffer.dropped_total 与快照 evidence_drops 单独观测
        self.buffer = buffer or EvaluationCaptureBuffer(
            on_drop=lambda _record: logger.debug(
                "evaluation capture buffer record dropped"),
        )
        self.queue = queue or EvaluationCaptureQueue(
            maxsize=queue_maxsize, handler=self._persist,
        )
        self.evidence_store = evidence_store
        self.evaluation_service = evaluation_service
        self._service = service
        self._redaction_version = redaction_version
        self._redactor = redactor or _default_redact
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- 生命周期 -----------------------------------------------------------

    def _current_policy(self) -> EvaluationSamplingPolicy | None:
        """C 类热重载抽样策略：注入 policy_provider 时逐次捕获查询；
        未注入退回构造期固定 policy。provider 抛错 fail-safe 回退固定
        policy（捕获绝不上抛）。"""
        if self._policy_provider is None:
            return self._policy
        try:
            return self._policy_provider()
        except Exception:
            logger.warning(
                "evaluation policy provider failed; using fallback policy",
                exc_info=True,
            )
            return self._policy

    async def start(self) -> None:
        # #28：持久化计数启动恢复（无 load 能力的计数器跳过）
        load = getattr(self.counters, "load", None)
        if load is not None:
            await load()
        await self.queue.start()

    async def stop(self) -> None:
        await self.queue.stop()
        # #28：停止前同步落盘计数（有界，失败不阻断停止）
        flush = getattr(self.counters, "flush", None)
        if flush is not None:
            try:
                await flush()
            except Exception:
                logger.warning(
                    "evaluation capture counters flush on stop failed",
                    exc_info=True,
                )

    # -- 捕获入口（业务完成钩子；同步、非阻塞、绝不抛错） ---------------------

    def capture_agent_run(
        self,
        *,
        subject_id: str,
        session_id: str,
        query: str,
        final_answer: str,
        model: str | None = None,
        target_versions: Mapping[str, Any] | None = None,
        truth_refs: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        trace_carrier: Mapping[str, str] | None = None,
        failed: bool = False,
        error: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        """AgentRun 业务完成后的 best-effort 捕获。

        同步返回、绝不抛错、绝不等待网络或 Judge：抽样不命中直接返回；
        命中则构建脱敏快照并 put_nowait 入队；任何异常隔离为
        capture_error 计数 + warning 日志，不回滚业务成功。
        """
        try:
            self.counters.bump("eligible")
            policy = self._current_policy()
            if policy is None:
                # C 类 enabled=False：捕获总闸关闭，静默跳过（eligible 已
                # 计数反映探针活性，不产生 selected/captured）
                return
            cohort = policy.cohort_for(
                self._service, subject_id, failed=failed,
            )
            if cohort is None:
                return
            self.counters.bump("selected")
            evidence = self.buffer.drain(session_id)
            content = self._build_snapshot_content(
                subject_id=subject_id,
                query=query,
                final_answer=final_answer,
                model=model,
                target_versions=target_versions,
                truth_refs=truth_refs,
                failed=failed,
                error=error,
                finish_reason=finish_reason,
                evidence=evidence,
                cohort=cohort,
            )
            snapshot = EvidenceSnapshot(
                content=content, redaction_version=self._redaction_version,
            )
            trace_id, span_id = _identity_from_trace_carrier(trace_carrier)
            target = EvaluationTarget(
                service=self._service,
                kind="agent_run",
                subject_id=subject_id,
                subject_revision=finish_reason or ("error" if failed else "unknown"),
                run_id=run_id or None,
                session_id=session_id or None,
                trace_id=trace_id,
                span_id=span_id,
            )
            captured_at = self._clock().astimezone(timezone.utc).isoformat()
            envelope = CaptureEnvelope(
                target=target,
                snapshot=snapshot,
                producer_event_id=(
                    f"agent-run:{subject_id}:{snapshot.content_sha256[:16]}"
                ),
                sample_probability=policy.probability_for(cohort),
                sampling_policy_version=policy.policy_version,
                captured_at=captured_at,
                cohort=cohort,
                evidence_targets=self._build_evidence_targets(
                    session_id=session_id,
                    run_id=run_id,
                    fallback_subject_id=subject_id,
                    evidence=evidence,
                ),
            )
            if self.queue.put_nowait(envelope):
                self.counters.bump("captured")
            else:
                self.counters.bump("queue_full")
                logger.warning(
                    "evaluation capture queue full; sample rejected "
                    "session=%s subject=%s",
                    session_id, subject_id,
                )
        except Exception:
            self._isolate_capture_error(
                f"session={session_id} subject={subject_id}")

    def _isolate_capture_error(self, context: str) -> None:
        try:
            self.counters.bump("capture_error")
        except Exception:
            pass
        try:
            logger.warning("evaluation capture failed (%s)", context, exc_info=True)
        except Exception:
            pass

    # -- 快照构建 ------------------------------------------------------------

    def _build_snapshot_content(
        self,
        *,
        subject_id: str,
        query: str,
        final_answer: str,
        model: str | None,
        target_versions: Mapping[str, Any] | None,
        truth_refs: Mapping[str, Any] | None,
        failed: bool,
        error: str | None,
        finish_reason: str | None,
        evidence: Mapping[str, Any],
        cohort: str,
    ) -> dict[str, Any]:
        # 只按允许字段构造正文（spec 6.3：不含 SDK 对象/凭据/完整 system
        # prompt/受禁截图）；再经统一脱敏遍历兜底
        raw: dict[str, Any] = {
            "schema_version": CAPTURE_SNAPSHOT_SCHEMA_VERSION,
            "kind": "agent_run",
            "subject_id": subject_id,
            "query": query,
            "final_answer": final_answer,
            # T22 收口（T16/T9 证据槽接缝）：Judge 适配器按 request/answer/
            # chunks 槽位取证据（judge_provider _build_evidence_slots），
            # 此前快照只有 query/final_answer/retrieval_chunks 别名键，
            # 线上 LLM Judge 指标全部 skipped(evidence_missing) 死点。
            # chunk 槽位用检索记录 excerpt（与 retrieval_chunks 同源）。
            "request": query,
            "answer": final_answer,
            "chunks": [
                str(chunk.get("excerpt") or chunk.get("chunk_id") or "")
                for record in (evidence.get("retrievals") or [])
                if isinstance(record, Mapping)
                for chunk in (record.get("chunks") or [])
                if isinstance(chunk, Mapping)
            ],
            # 证据槽别名（24 项三方审阅 #23）：faithfulness 按 contexts
            # 槽位取证据，缺别名 -> 线上 faithfulness 恒
            # skipped(evidence_missing)；与 chunks 同源同值
            "contexts": [
                str(chunk.get("excerpt") or chunk.get("chunk_id") or "")
                for record in (evidence.get("retrievals") or [])
                if isinstance(record, Mapping)
                for chunk in (record.get("chunks") or [])
                if isinstance(chunk, Mapping)
            ],
            "failed": failed,
            "error": error,
            "finish_reason": finish_reason,
            # 工具契约投影（24 项三方审阅 #17）：tool_traces 原始轨迹
            # 投影为 worker agent.tool_contract 消费的契约形状——缺字段
            # 会被默认成空集合，评分对工具违规虚假通过。已执行调用必然
            # 通过 schema 校验与权限门（否则不会执行）；权限拒绝调用
            # permission_granted=False；allowed_tools = 出现过的工具集
            # （契约按实际轨迹判违规，空集语义不适用）
            "tool_calls": [
                {
                    "tool": str(trace.get("tool") or ""),
                    "schema_valid": True,
                    "permission_granted": (
                        str(trace.get("status") or "")
                        != "permission_denied"
                    ),
                    "side_effects": [],
                }
                for trace in (evidence.get("tools") or [])
                if isinstance(trace, Mapping) and trace.get("tool")
            ],
            "allowed_tools": sorted(
                {
                    str(trace.get("tool"))
                    for trace in (evidence.get("tools") or [])
                    if isinstance(trace, Mapping) and trace.get("tool")
                }
            ),
            "tool_traces": list(evidence.get("tools") or []),
            "retrieval_chunks": list(evidence.get("retrievals") or []),
            "truth_refs": dict(truth_refs or {}),
            # #25：self-judge 判定读 target_model 键（线上自动识别"被评
            # 模型 == Judge 模型"）；model 键保留兼容既有消费方
            "model": model,
            "target_model": model,
            "target_versions": dict(target_versions or {}),
            "capture_completeness": {
                "tool_trace_count": len(evidence.get("tools") or []),
                "retrieval_count": len(evidence.get("retrievals") or []),
                "evidence_drops": int(evidence.get("drops") or 0),
            },
            "redaction_version": self._redaction_version,
            "sampling": {
                "policy_version": self._policy.policy_version,
                "cohort": cohort,
                "probability": self._policy.probability_for(cohort),
            },
        }
        # 快照期脱敏双层（m5 回归修复）：先注入 redactor（组合根注入的
        # LogSanitizer——已知 secrets 目录精确值匹配 + 固定敏感规则），
        # 后内建 _default_redact 兜底——精确值匹配必须先于通配形态规则，
        # 否则通配规则（如 40 位 raw 形态）可能破坏精确匹配的替换边界；
        # 内建层覆盖 LogSanitizer 固定规则之外的裸凭据形态（ghp_/
        # github_pat_/sk-/xox*/ya29./AKIA/40 位 raw），不可被注入关闭。
        # 注意 query/final_answer/error 三个直接字段不经 jsonable_excerpt
        # 的登记层内建正则，此快照期兜底是其唯一内建脱敏防线
        def _snapshot_redactor(text: str) -> str:
            return _default_redact(self._redactor(text))

        redacted = _redact_capture_value(raw, _snapshot_redactor)
        if not isinstance(redacted, dict):  # pragma: no cover - 防御
            raise EvaluationValidationError("快照正文脱敏后退化失败")
        return redacted

    # -- 后台持久化（消费者 handler） -----------------------------------------

    def _build_evidence_targets(
        self,
        *,
        session_id: str,
        run_id: str | None,
        fallback_subject_id: str,
        evidence: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        """构建 evaluation_evidence_targets 反向索引条目（spec 6.3）。

        覆盖原会话（session）、本次 Run（agent_run）与快照内真实消费的
        N-KB 文档版本（document，仅带 retrieval_id 的检索记录——即来自
        N-KB evaluation_context 的记录；其余检索后端不经此索引，T18 删除
        传播对非 N-KB 文档不适用）。T18 删除传播依赖本索引撤销待执行
        job 与 pending delivery，缺失即静默范围缩减，故 agent_run 线上
        样本必须携带。
        """
        targets: list[Mapping[str, Any]] = []
        if session_id:
            targets.append({
                "source_service": self._service,
                "subject_kind": "session",
                "subject_id": session_id,
                "subject_revision": "",
            })
        targets.append({
            "source_service": self._service,
            "subject_kind": "agent_run",
            "subject_id": run_id or fallback_subject_id,
            "subject_revision": "",
        })
        seen_documents: set[tuple[str, str]] = set()
        for record in evidence.get("retrievals") or ():
            if not isinstance(record, Mapping) or not record.get("retrieval_id"):
                continue
            for chunk in record.get("chunks") or ():
                if not isinstance(chunk, Mapping):
                    continue
                document_id = chunk.get("document_id")
                if not document_id:
                    continue
                revision = chunk.get("document_revision") or ""
                key = (str(document_id), str(revision))
                if key in seen_documents:
                    continue
                seen_documents.add(key)
                targets.append({
                    "source_service": "n-kb",
                    "subject_kind": "document",
                    "subject_id": str(document_id),
                    "subject_revision": str(revision),
                })
        return tuple(targets)

    async def _persist(self, envelope: CaptureEnvelope) -> None:
        """快照落盘 + 样本登记（Evaluation Worker 随后按队列恢复执行）。

        任何失败只计 capture_error + warning，不向业务传播。
        """
        try:
            snapshot_ref = ""
            if self.evidence_store is not None:
                snapshot_ref = await self.evidence_store.save_snapshot(
                    envelope.target, envelope.snapshot,
                )
            if self.evaluation_service is not None:
                # T22：线上样本按目标种类展开当前已注册的自动评估定义
                #（human 方法在解析层过滤；未注册定义时保持仅登记语义）
                definitions = (
                    await self.evaluation_service.definitions_for_target(
                        envelope.target.kind
                    )
                )
                await self.evaluation_service.submit_sample(SubmitSampleRequest(
                    producer_event_id=envelope.producer_event_id,
                    service=self._service,
                    target=envelope.target,
                    definitions=definitions,
                    evidence_targets=envelope.evidence_targets,
                    snapshot_ref=snapshot_ref,
                    snapshot_hash=envelope.snapshot.content_sha256,
                    schema_version=CAPTURE_SNAPSHOT_SCHEMA_VERSION,
                    captured_at=envelope.captured_at,
                    sample_probability=envelope.sample_probability,
                    sampling_policy_version=envelope.sampling_policy_version,
                    cohort=envelope.cohort,
                ))
        except Exception:
            self._isolate_capture_error(
                f"persist producer_event_id={envelope.producer_event_id}")
