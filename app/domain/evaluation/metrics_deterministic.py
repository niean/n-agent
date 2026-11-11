# app/domain/evaluation/metrics_deterministic.py
"""Deterministic 指标纯函数（spec 8 Score 目录）。

覆盖正式目录中 method=deterministic 的五个指标：kb.recall_at_5、
kb.ndcg_at_5、kb.mrr_at_5、agent.task_success、agent.tool_contract。
全部为纯函数：stdlib only、零 IO、零 SDK 依赖，输入是身份 ID 与 qrels，
绝不接受 similarity / rerank / embedding 分数（spec 8：检索底层 score
永远保留为输入特征，不写成 kb.recall）。

检索身份 = corpus_revision + document_revision + chunk_id（spec 8 注释），
以 frozen dataclass 的值语义去重。输出统一为 EvaluationValue：
skipped 不是 0——无相关项查询为 skipped(no_answer)，task_success 缺证据
为 skipped(evidence_missing)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.domain.evaluation.models import EvaluationValue, EvaluationValidationError
from app.domain.evaluation.state import SkipReason

# 正式 metric 名称（spec 8 表）
METRIC_KB_RECALL_AT_5 = "kb.recall_at_5"
METRIC_KB_NDCG_AT_5 = "kb.ndcg_at_5"
METRIC_KB_MRR_AT_5 = "kb.mrr_at_5"
METRIC_AGENT_TASK_SUCCESS = "agent.task_success"
METRIC_AGENT_TOOL_CONTRACT = "agent.tool_contract"

# 三个检索指标统一 top-5 截断（spec 8 表）
_TOP_K = 5

# qrels 相关性分级词汇（spec 8：0/1/2 分级 qrels；Recall / MRR 阈值 rel>=1）
_VALID_GRADES = (0, 1, 2)


@dataclass(frozen=True)
class ChunkIdentity:
    """检索片段稳定身份（spec 8 注释：corpus_revision + document_revision
    + chunk_id），值语义相等即同一身份，frozen 保证可哈希去重。"""

    corpus_revision: str
    document_revision: str
    chunk_id: str

    def __post_init__(self) -> None:
        for label in ("corpus_revision", "document_revision", "chunk_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value:
                raise EvaluationValidationError(
                    f"{label} 必须是非空字符串: {value!r}"
                )


def _validate_qrels(qrels: Mapping[ChunkIdentity, int]) -> None:
    if not isinstance(qrels, Mapping):
        raise EvaluationValidationError("qrels 必须是 Mapping")
    for identity, grade in qrels.items():
        if not isinstance(identity, ChunkIdentity):
            raise EvaluationValidationError(
                f"qrels 键必须是 ChunkIdentity: {identity!r}"
            )
        if isinstance(grade, bool) or not isinstance(grade, int):
            raise EvaluationValidationError(
                f"qrels 相关性必须是 int 分级: {grade!r}"
            )
        if grade not in _VALID_GRADES:
            raise EvaluationValidationError(
                f"qrels 相关性分级仅允许 {_VALID_GRADES}: {grade!r}"
            )


def _validate_results(results: Sequence[ChunkIdentity]) -> tuple[ChunkIdentity, ...]:
    if isinstance(results, (str, bytes)) or not isinstance(results, Sequence):
        raise EvaluationValidationError("results 必须是身份 ID 有序序列")
    ordered = tuple(results)
    for identity in ordered:
        if not isinstance(identity, ChunkIdentity):
            raise EvaluationValidationError(
                f"results 条目必须是 ChunkIdentity（禁止带分数的命中对象）: {identity!r}"
            )
    return ordered


def _dedup_top_k(
    results: tuple[ChunkIdentity, ...], k: int
) -> tuple[ChunkIdentity, ...]:
    """按身份去重并保持首次出现顺序，截取前 k 个。"""
    seen: set[ChunkIdentity] = set()
    ordered: list[ChunkIdentity] = []
    for identity in results:
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(identity)
        if len(ordered) == k:
            break
    return tuple(ordered)


def _frozen_str_tuple(label: str, value: Any) -> tuple[str, ...]:
    """校验字符串有序序列并冻结为 tuple：拒绝 str/bytes（防字符炸裂）
    与非 Sequence 输入，条目必须是非空字符串。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EvaluationValidationError(f"{label} 必须是字符串有序序列: {value!r}")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str) or not item:
            raise EvaluationValidationError(
                f"{label} 条目必须是非空字符串: {item!r}"
            )
    return items


def _relevant_set(qrels: Mapping[ChunkIdentity, int]) -> frozenset[ChunkIdentity]:
    """rel>=1 的相关身份集合（spec 8 注释：Recall / MRR 相关阈值 rel>=1）。"""
    return frozenset(
        identity for identity, grade in qrels.items() if grade >= 1
    )


def _no_answer() -> EvaluationValue:
    return EvaluationValue(
        typed_value=None,
        status="skipped",
        skip_reason=SkipReason.no_answer,
        explanation="query 在 qrels 中没有相关项，无法评估检索指标",
    )


def _kb_value(metric: float | None, explanation: str) -> EvaluationValue:
    if metric is None:
        return _no_answer()
    return EvaluationValue(
        typed_value=metric, status="value", explanation=explanation
    )


def kb_recall_at_5(
    results: Sequence[ChunkIdentity], qrels: Mapping[ChunkIdentity, int]
) -> EvaluationValue:
    """kb.recall_at_5：前 5 个去重稳定 ID 中相关项数 / 全部相关项数。

    qrels rel>=1 计入相关；query 无相关项（含空 qrels、全 0 分级）为
    skipped(no_answer)，不是 0。
    """
    _validate_qrels(qrels)
    ordered = _validate_results(results)
    relevant = _relevant_set(qrels)
    if not relevant:
        return _no_answer()
    top = _dedup_top_k(ordered, _TOP_K)
    hits = sum(1 for identity in top if identity in relevant)
    return _kb_value(hits / len(relevant), f"{hits}/{len(relevant)} relevant in top-5")


def kb_ndcg_at_5(
    results: Sequence[ChunkIdentity], qrels: Mapping[ChunkIdentity, int]
) -> EvaluationValue:
    """kb.ndcg_at_5：DCG@5 / IDCG@5，gain=2^rel-1，discount=1/log2(rank+1)。

    相关集为空（无 rel>=1 项）为 skipped(no_answer)；IDCG 按 qrels 分级
    降序取前 5 计算，与实际返回无关；结果未返回相关项时为 0 而非 skipped。
    精确浮点计算，指标本身不做舍入。
    """
    _validate_qrels(qrels)
    ordered = _validate_results(results)
    relevant = _relevant_set(qrels)
    if not relevant:
        return _no_answer()
    top = _dedup_top_k(ordered, _TOP_K)

    def dcg(pairs: list[tuple[int, int]]) -> float:
        return sum(
            (2**grade - 1) / math.log2(rank + 1)
            for rank, grade in pairs
        )

    actual_grades = [
        (rank, qrels.get(identity, 0)) for rank, identity in enumerate(top, start=1)
    ]
    ideal_grades = [
        (rank, grade)
        for rank, grade in enumerate(
            sorted(qrels.values(), reverse=True)[:_TOP_K], start=1
        )
    ]
    ideal_dcg = dcg(ideal_grades)
    if ideal_dcg <= 0:
        # 相关集非空但全为 0 gain 在校验后不可能发生，防御性兜底
        return _no_answer()
    return _kb_value(
        dcg(actual_grades) / ideal_dcg,
        "DCG@5/IDCG@5, gain=2^rel-1, discount=1/log2(rank+1)",
    )


def kb_mrr_at_5(
    results: Sequence[ChunkIdentity], qrels: Mapping[ChunkIdentity, int]
) -> EvaluationValue:
    """kb.mrr_at_5：前 5 个去重结果中首个相关项排名的倒数。

    "没有相关返回为 0" 仅指 qrels 有相关项但 top-5 未命中；query 无相关项
    为 skipped(no_answer)。
    """
    _validate_qrels(qrels)
    ordered = _validate_results(results)
    relevant = _relevant_set(qrels)
    if not relevant:
        return _no_answer()
    top = _dedup_top_k(ordered, _TOP_K)
    for rank, identity in enumerate(top, start=1):
        if identity in relevant:
            return _kb_value(1.0 / rank, f"first relevant at rank {rank}")
    return _kb_value(0.0, "no relevant item in top-5")


@dataclass(frozen=True)
class SideEffectEvidence:
    """可核实副作用证据（spec 8：agent.task_success 优先检查实际制品/状态）。

    expected 为该任务声明的预期副作用标识；verified 为证据中实际核实
    发生的副作用标识。expected 为空表示任务无副作用要求。
    """

    expected: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "expected", _frozen_str_tuple("side_effect expected", self.expected)
        )
        object.__setattr__(
            self, "verified", _frozen_str_tuple("side_effect verified", self.verified)
        )


@dataclass(frozen=True)
class TaskSuccessInput:
    """agent.task_success 输入：完成自述 + 可核实副作用证据。

    side_effect_evidence 为 None 表示证据缺失（skipped(evidence_missing)，
    不是 False）；有证据时副作用证明优先于完成自述。
    """

    completion_reported: bool
    side_effect_evidence: SideEffectEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.completion_reported, bool):
            raise EvaluationValidationError(
                f"completion_reported 必须是 bool: {self.completion_reported!r}"
            )


def agent_task_success(task_input: TaskSuccessInput) -> EvaluationValue:
    """agent.task_success：boolean；副作用证明优先于完成自述。

    证据缺失 → skipped(evidence_missing)，不是 False。有证据时以
    expected ⊆ verified 判定（证明覆盖自述两个方向）；无预期副作用时
    回落为完成自述。
    """
    if not isinstance(task_input, TaskSuccessInput):
        raise EvaluationValidationError(
            f"task_input 必须是 TaskSuccessInput: {task_input!r}"
        )
    if task_input.side_effect_evidence is None:
        return EvaluationValue(
            typed_value=None,
            status="skipped",
            skip_reason=SkipReason.evidence_missing,
            explanation="缺少可核实的副作用证据，不能仅凭完成自述判定",
        )
    evidence = task_input.side_effect_evidence
    if evidence.expected:
        success = set(evidence.expected) <= set(evidence.verified)
    else:
        success = task_input.completion_reported
    return EvaluationValue(
        typed_value=success,
        status="value",
        explanation="verified side effects cover expected" if evidence.expected
        else "no expected side effects; completion self-report used",
    )


@dataclass(frozen=True)
class ToolCallRecord:
    """单次工具调用记录：调用工具、参数 schema 校验结论、权限结论与
    该调用声明的副作用标识。不含调用顺序语义（spec 8：不以固定调用顺序
    限制合法解法）。"""

    tool: str
    schema_valid: bool
    permission_granted: bool = True
    side_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tool, str) or not self.tool:
            raise EvaluationValidationError(f"tool 必须是非空字符串: {self.tool!r}")
        for label in ("schema_valid", "permission_granted"):
            if not isinstance(getattr(self, label), bool):
                raise EvaluationValidationError(f"{label} 必须是 bool")
        object.__setattr__(
            self, "side_effects", _frozen_str_tuple("side_effects", self.side_effects)
        )


@dataclass(frozen=True)
class ToolContractInput:
    """agent.tool_contract 输入：实际调用、允许工具与预期副作用。"""

    calls: tuple[ToolCallRecord, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    expected_side_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.calls, (str, bytes)) or not isinstance(self.calls, Sequence):
            raise EvaluationValidationError(
                f"calls 必须是 ToolCallRecord 有序序列: {self.calls!r}"
            )
        object.__setattr__(self, "calls", tuple(self.calls))
        object.__setattr__(
            self,
            "allowed_tools",
            _frozen_str_tuple("allowed_tools", self.allowed_tools),
        )
        object.__setattr__(
            self,
            "expected_side_effects",
            _frozen_str_tuple("expected_side_effects", self.expected_side_effects),
        )
        for call in self.calls:
            if not isinstance(call, ToolCallRecord):
                raise EvaluationValidationError(
                    f"calls 条目必须是 ToolCallRecord: {call!r}"
                )


def agent_tool_contract(contract_input: ToolContractInput) -> EvaluationValue:
    """agent.tool_contract：boolean；实际调用、参数 schema、允许工具和
    预期副作用全部满足才 True。不校验固定调用顺序；权限拒绝计 False；
    预期副作用只要求被覆盖，额外副作用不否定契约。"""
    if not isinstance(contract_input, ToolContractInput):
        raise EvaluationValidationError(
            f"contract_input 必须是 ToolContractInput: {contract_input!r}"
        )
    allowed = set(contract_input.allowed_tools)
    covered: set[str] = set()
    for call in contract_input.calls:
        if call.tool not in allowed:
            return EvaluationValue(
                typed_value=False,
                status="value",
                explanation=f"disallowed tool: {call.tool}",
            )
        if not call.schema_valid:
            return EvaluationValue(
                typed_value=False,
                status="value",
                explanation=f"arguments schema violated: {call.tool}",
            )
        if not call.permission_granted:
            return EvaluationValue(
                typed_value=False,
                status="value",
                explanation=f"permission denied: {call.tool}",
            )
        covered.update(call.side_effects)
    missing = set(contract_input.expected_side_effects) - covered
    if missing:
        return EvaluationValue(
            typed_value=False,
            status="value",
            explanation=f"expected side effects not satisfied: {sorted(missing)}",
        )
    return EvaluationValue(
        typed_value=True, status="value", explanation="tool contract satisfied"
    )
