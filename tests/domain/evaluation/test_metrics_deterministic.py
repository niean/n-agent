# Deterministic 指标纯函数测试：对照 spec 8 表与 8 节注释逐条校验
# kb.recall_at_5 / kb.ndcg_at_5 / kb.mrr_at_5 / agent.task_success /
# agent.tool_contract 的口径，以及 EvaluationValue 统一输出形态与 T1
# validate_evaluation_value 的联通。
from __future__ import annotations

import dataclasses
import inspect
import math
from dataclasses import dataclass
from typing import Mapping

import pytest

from app.domain.evaluation.metrics_deterministic import (
    METRIC_AGENT_TASK_SUCCESS,
    METRIC_AGENT_TOOL_CONTRACT,
    METRIC_KB_NDCG_AT_5,
    METRIC_KB_MRR_AT_5,
    METRIC_KB_RECALL_AT_5,
    ChunkIdentity,
    SideEffectEvidence,
    TaskSuccessInput,
    ToolCallRecord,
    ToolContractInput,
    agent_task_success,
    agent_tool_contract,
    kb_mrr_at_5,
    kb_ndcg_at_5,
    kb_recall_at_5,
)
from app.domain.evaluation.models import (
    EvaluationValue,
    EvaluationValidationError,
    EvaluatorDefinition,
    validate_evaluation_value,
)
from app.domain.evaluation.state import SkipReason


def _chunk(name: str) -> ChunkIdentity:
    return ChunkIdentity(
        corpus_revision="corpus-r1",
        document_revision="doc-r1",
        chunk_id=name,
    )


def _definition(
    metric_name: str,
    target_kind: str,
    output_type: str,
    range_or_categories,
) -> EvaluatorDefinition:
    return EvaluatorDefinition(
        definition_id=f"def-{metric_name}",
        version="v1",
        metric_name=metric_name,
        target_kind=target_kind,
        method="deterministic",
        required_fields=(),
        evidence_policy="offline_benchmark",
        output_type=output_type,
        range_or_categories=range_or_categories,
        direction="higher_is_better",
        material_hash={},
        judge_model=None,
        judge_params={},
        timeout_seconds=30,
        budget=None,
        ci_admissible=True,
    )


# ---------------------------------------------------------------------------
# kb.recall_at_5（spec 8：前 5 个去重稳定 ID 中相关项数 / 全部相关项数，
# rel>=1；无相关项查询 skipped；身份 = corpus_revision + document_revision
# + chunk_id，按身份去重）
# ---------------------------------------------------------------------------


class TestKbRecallAt5:
    def test_basic_partial_recall(self):
        qrels = {_chunk("a"): 1, _chunk("b"): 1, _chunk("c"): 1}
        results = (_chunk("a"), _chunk("x"), _chunk("b"))
        value = kb_recall_at_5(results, qrels)
        assert value.status == "value"
        assert value.typed_value == pytest.approx(2 / 3)

    def test_dedup_counts_duplicate_identity_once(self):
        qrels = {_chunk("a"): 1}
        results = (_chunk("a"), _chunk("a"), _chunk("a"))
        value = kb_recall_at_5(results, qrels)
        assert value.typed_value == pytest.approx(1.0)

    def test_dedup_gives_relevant_items_real_slots(self):
        # 重复 ID 若不去重会挤掉 top-5 中的相关项：去重后 4 个相关全部命中
        qrels = {_chunk(n): 1 for n in ("r1", "r2", "r3", "r4")}
        results = (
            _chunk("x"),
            _chunk("x"),
            _chunk("r1"),
            _chunk("r2"),
            _chunk("r3"),
            _chunk("r4"),
        )
        value = kb_recall_at_5(results, qrels)
        assert value.typed_value == pytest.approx(1.0)

    def test_boundary_rank5_counts_rank6_not(self):
        qrels = {_chunk("hit5"): 1, _chunk("hit6"): 1}
        results = (
            _chunk("n1"),
            _chunk("n2"),
            _chunk("n3"),
            _chunk("n4"),
            _chunk("hit5"),
            _chunk("hit6"),
        )
        value = kb_recall_at_5(results, qrels)
        assert value.typed_value == pytest.approx(0.5)

    def test_rel_zero_not_relevant(self):
        qrels = {_chunk("a"): 0, _chunk("b"): 1}
        assert kb_recall_at_5((_chunk("a"), _chunk("b")), qrels).typed_value == pytest.approx(1.0)
        assert kb_recall_at_5((_chunk("a"),), qrels).typed_value == pytest.approx(0.0)

    def test_no_relevant_query_skipped_no_answer(self):
        value = kb_recall_at_5((_chunk("a"),), {_chunk("a"): 0})
        assert value.status == "skipped"
        assert value.typed_value is None
        assert value.skip_reason is SkipReason.no_answer

    def test_empty_results_with_relevant_qrels_is_zero(self):
        qrels = {_chunk("a"): 1, _chunk("b"): 2}
        value = kb_recall_at_5((), qrels)
        assert value.status == "value"
        assert value.typed_value == pytest.approx(0.0)

    def test_identity_matches_by_value_not_instance(self):
        # 两处独立构造的同身份 ChunkIdentity 必须匹配（frozen dataclass 值语义）
        qrels = {
            ChunkIdentity(corpus_revision="c1", document_revision="d1", chunk_id="k"): 1
        }
        results = (
            ChunkIdentity(corpus_revision="c1", document_revision="d1", chunk_id="k"),
        )
        assert kb_recall_at_5(results, qrels).typed_value == pytest.approx(1.0)

    @pytest.mark.parametrize("grade", [3, -1, True])
    def test_invalid_grade_rejected(self, grade):
        with pytest.raises(EvaluationValidationError):
            kb_recall_at_5((_chunk("a"),), {_chunk("a"): grade})

    def test_non_identity_result_rejected(self):
        with pytest.raises(EvaluationValidationError):
            kb_recall_at_5(("chunk-a",), {_chunk("a"): 1})


# ---------------------------------------------------------------------------
# kb.ndcg_at_5（spec 8：DCG@5 / IDCG@5，gain=2^rel-1，discount=1/log2(rank+1)；
# 空相关集 skipped；精确浮点，指标本身不做舍入）
# ---------------------------------------------------------------------------


class TestKbNdcgAt5:
    def test_perfect_ordering_is_one(self):
        qrels = {_chunk("a"): 2, _chunk("b"): 1}
        results = (_chunk("a"), _chunk("b"))
        value = kb_ndcg_at_5(results, qrels)
        assert value.status == "value"
        assert value.typed_value == pytest.approx(1.0)

    def test_suboptimal_ordering_penalized(self):
        qrels = {_chunk("a"): 2, _chunk("b"): 1}
        results = (_chunk("b"), _chunk("a"))
        value = kb_ndcg_at_5(results, qrels)
        expected = (1.0 + 3 / math.log2(3)) / (3.0 + 1 / math.log2(3))
        assert value.typed_value == pytest.approx(expected)
        assert value.typed_value < 1.0

    def test_only_top5_contribute(self):
        # rel=2 的项排在第 6 位不贡献 DCG；换到第 1 位则为满分
        qrels = {_chunk("gold"): 2}
        tail = tuple(_chunk(f"n{i}") for i in range(1, 6))
        assert kb_ndcg_at_5(tail + (_chunk("gold"),), qrels).typed_value == pytest.approx(0.0)
        assert kb_ndcg_at_5((_chunk("gold"),) + tail, qrels).typed_value == pytest.approx(1.0)

    def test_grade_zero_items_contribute_nothing(self):
        qrels = {_chunk("a"): 2, _chunk("z"): 0}
        with_zero = kb_ndcg_at_5((_chunk("a"), _chunk("z")), qrels)
        assert with_zero.typed_value == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "qrels", [{}, {_chunk("a"): 0}, {_chunk("a"): 0, _chunk("b"): 0}]
    )
    def test_empty_relevant_set_skipped_no_answer(self, qrels):
        value = kb_ndcg_at_5((_chunk("a"),), qrels)
        assert value.status == "skipped"
        assert value.typed_value is None
        assert value.skip_reason is SkipReason.no_answer

    def test_ideal_dcg_uses_qrels_not_results(self):
        # 相关项从未被返回：DCG=0，IDCG 仍按 qrels 计算，结果为 0 而非 skipped
        value = kb_ndcg_at_5((), {_chunk("a"): 2, _chunk("b"): 1})
        assert value.status == "value"
        assert value.typed_value == pytest.approx(0.0)

    def test_exact_float_no_rounding(self):
        # rel=2 在 rank2：DCG=3/log2(3)，IDCG=3 → 值为 1/log2(3) 的完整浮点精度
        value = kb_ndcg_at_5((_chunk("x"), _chunk("a")), {_chunk("a"): 2})
        assert value.typed_value == pytest.approx(1 / math.log2(3), rel=1e-12)
        assert value.typed_value != round(value.typed_value, 4)

    def test_invalid_grade_rejected(self):
        with pytest.raises(EvaluationValidationError):
            kb_ndcg_at_5((_chunk("a"),), {_chunk("a"): 5})


# ---------------------------------------------------------------------------
# kb.mrr_at_5（spec 8：前 5 个中首个相关结果排名的倒数；"没有相关返回为 0"
# 仅指有 qrels 但 top-5 未命中；query 无相关项为 skipped）
# ---------------------------------------------------------------------------


class TestKbMrrAt5:
    @pytest.mark.parametrize(
        "position,expected",
        [(1, 1.0), (3, 1 / 3), (5, 0.2)],
    )
    def test_reciprocal_rank_of_first_relevant(self, position, expected):
        qrels = {_chunk("rel"): 1}
        results = tuple(_chunk(f"n{i}") for i in range(1, position)) + (_chunk("rel"),)
        value = kb_mrr_at_5(results, qrels)
        assert value.status == "value"
        assert value.typed_value == pytest.approx(expected)

    def test_relevant_beyond_top5_is_zero(self):
        qrels = {_chunk("rel"): 1}
        results = tuple(_chunk(f"n{i}") for i in range(1, 6)) + (_chunk("rel"),)
        assert kb_mrr_at_5(results, qrels).typed_value == pytest.approx(0.0)

    def test_relevant_qrels_but_no_hit_is_zero_not_skipped(self):
        value = kb_mrr_at_5(
            (_chunk("x"), _chunk("y")), {_chunk("a"): 1, _chunk("b"): 2}
        )
        assert value.status == "value"
        assert value.typed_value == pytest.approx(0.0)

    def test_no_relevant_query_skipped_no_answer(self):
        value = kb_mrr_at_5((_chunk("a"),), {_chunk("a"): 0})
        assert value.status == "skipped"
        assert value.skip_reason is SkipReason.no_answer

    def test_dedup_before_ranking(self):
        # 重复 ID 去重后首个相关排名提前：[x, x, rel] 中 rel 实际 rank=2
        qrels = {_chunk("rel"): 1}
        results = (_chunk("x"), _chunk("x"), _chunk("rel"))
        assert kb_mrr_at_5(results, qrels).typed_value == pytest.approx(0.5)

    def test_uses_first_relevant_only(self):
        qrels = {_chunk("a"): 1, _chunk("b"): 1}
        results = (_chunk("x"), _chunk("b"), _chunk("a"))
        assert kb_mrr_at_5(results, qrels).typed_value == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# agent.task_success（spec 8：boolean；优先检查实际制品/状态，不能仅相信
# 完成自述；证据缺失为 skipped 而非 False）
# ---------------------------------------------------------------------------


class TestAgentTaskSuccess:
    def test_missing_evidence_skipped_not_false(self):
        value = agent_task_success(
            TaskSuccessInput(completion_reported=True, side_effect_evidence=None)
        )
        assert value.status == "skipped"
        assert value.typed_value is None
        assert value.skip_reason is SkipReason.evidence_missing

    def test_side_effect_proof_overrides_false_self_report(self):
        # 副作用证明优先于完成自述：自述未完成但证明副作用已发生 → True
        value = agent_task_success(
            TaskSuccessInput(
                completion_reported=False,
                side_effect_evidence=SideEffectEvidence(
                    expected=("artifact.created",),
                    verified=("artifact.created",),
                ),
            )
        )
        assert value.status == "value"
        assert value.typed_value is True

    def test_side_effect_proof_overrides_true_self_report(self):
        # 自称完成但可核实副作用缺失 → False（不能仅相信完成自述）
        value = agent_task_success(
            TaskSuccessInput(
                completion_reported=True,
                side_effect_evidence=SideEffectEvidence(
                    expected=("artifact.created",),
                    verified=(),
                ),
            )
        )
        assert value.status == "value"
        assert value.typed_value is False

    def test_all_expected_side_effects_verified_true(self):
        value = agent_task_success(
            TaskSuccessInput(
                completion_reported=True,
                side_effect_evidence=SideEffectEvidence(
                    expected=("file.written", "artifact.published"),
                    verified=("file.written", "artifact.published", "extra"),
                ),
            )
        )
        assert value.typed_value is True

    def test_no_expected_side_effects_falls_back_to_self_report(self):
        evidence = SideEffectEvidence(expected=(), verified=())
        assert (
            agent_task_success(
                TaskSuccessInput(completion_reported=True, side_effect_evidence=evidence)
            ).typed_value
            is True
        )
        assert (
            agent_task_success(
                TaskSuccessInput(completion_reported=False, side_effect_evidence=evidence)
            ).typed_value
            is False
        )


# ---------------------------------------------------------------------------
# agent.tool_contract（spec 8：boolean；实际调用、参数 schema、允许工具和
# 预期副作用全部满足；不以固定调用顺序限制合法解法；权限拒绝为 False）
# ---------------------------------------------------------------------------


def _contract_input(calls, allowed=("search", "writer")) -> ToolContractInput:
    return ToolContractInput(
        calls=calls,
        allowed_tools=allowed,
        expected_side_effects=("artifact.created",),
    )


class TestAgentToolContract:
    def test_all_constraints_satisfied_true(self):
        calls = (
            ToolCallRecord(
                tool="search",
                schema_valid=True,
                permission_granted=True,
                side_effects=(),
            ),
            ToolCallRecord(
                tool="writer",
                schema_valid=True,
                permission_granted=True,
                side_effects=("artifact.created",),
            ),
        )
        value = agent_tool_contract(_contract_input(calls))
        assert value.status == "value"
        assert value.typed_value is True

    def test_disallowed_tool_false(self):
        calls = (
            ToolCallRecord(
                tool="shell",
                schema_valid=True,
                permission_granted=True,
                side_effects=("artifact.created",),
            ),
        )
        assert agent_tool_contract(_contract_input(calls)).typed_value is False

    def test_invalid_arguments_schema_false(self):
        calls = (
            ToolCallRecord(
                tool="writer",
                schema_valid=False,
                permission_granted=True,
                side_effects=("artifact.created",),
            ),
        )
        assert agent_tool_contract(_contract_input(calls)).typed_value is False

    def test_permission_denial_counts_false(self):
        calls = (
            ToolCallRecord(
                tool="writer",
                schema_valid=True,
                permission_granted=False,
                side_effects=("artifact.created",),
            ),
        )
        assert agent_tool_contract(_contract_input(calls)).typed_value is False

    def test_missing_expected_side_effect_false(self):
        calls = (
            ToolCallRecord(tool="search", schema_valid=True, side_effects=()),
        )
        assert agent_tool_contract(_contract_input(calls)).typed_value is False

    def test_call_order_not_required(self):
        ordered = (
            ToolCallRecord(tool="search", schema_valid=True, side_effects=()),
            ToolCallRecord(
                tool="writer",
                schema_valid=True,
                side_effects=("artifact.created",),
            ),
        )
        assert agent_tool_contract(_contract_input(ordered)).typed_value is True
        assert agent_tool_contract(_contract_input(tuple(reversed(ordered)))).typed_value is True

    def test_empty_calls_with_no_expected_side_effects_true(self):
        value = agent_tool_contract(
            ToolContractInput(calls=(), allowed_tools=("search",), expected_side_effects=())
        )
        assert value.typed_value is True

    def test_extra_side_effect_declared_outside_expected_still_contract_true(self):
        # 预期副作用只要求覆盖，额外声明的副作用不否定契约
        calls = (
            ToolCallRecord(
                tool="writer",
                schema_valid=True,
                side_effects=("artifact.created", "cache.warmed"),
            ),
        )
        assert agent_tool_contract(_contract_input(calls)).typed_value is True


# ---------------------------------------------------------------------------
# 相似度分数隔离：recall / ndcg / mrr 输入路径只接受身份 ID 与 qrels
# ---------------------------------------------------------------------------


class TestSimilarityIsolation:
    def test_kb_signatures_accept_only_identity_and_qrels(self):
        for func in (kb_recall_at_5, kb_ndcg_at_5, kb_mrr_at_5):
            params = inspect.signature(func).parameters
            assert set(params) == {"results", "qrels"}, func.__name__
            results_annotation = params["results"].annotation
            qrels_annotation = params["qrels"].annotation
            assert "ChunkIdentity" in results_annotation, func.__name__
            assert "ChunkIdentity" in qrels_annotation, func.__name__
            for annotation in (results_annotation, qrels_annotation):
                lowered = annotation.lower()
                assert "similarity" not in lowered, func.__name__
                assert "score" not in lowered, func.__name__

    def test_chunk_identity_has_no_similarity_field(self):
        field_names = {f.name for f in dataclasses.fields(ChunkIdentity)}
        assert field_names == {"corpus_revision", "document_revision", "chunk_id"}

    def test_scored_hit_dataclass_rejected_on_identity_path(self):
        @dataclass(frozen=True)
        class ScoredHit:
            identity: ChunkIdentity
            similarity: float

        hit = ScoredHit(identity=_chunk("a"), similarity=0.87)
        with pytest.raises(EvaluationValidationError):
            kb_recall_at_5((hit,), {_chunk("a"): 1})


# ---------------------------------------------------------------------------
# EvaluationValue 统一输出形态与 T1 validate_evaluation_value 联通
# ---------------------------------------------------------------------------


class TestEvaluationValueWiring:
    def test_metric_names_match_spec_catalog(self):
        assert METRIC_KB_RECALL_AT_5 == "kb.recall_at_5"
        assert METRIC_KB_NDCG_AT_5 == "kb.ndcg_at_5"
        assert METRIC_KB_MRR_AT_5 == "kb.mrr_at_5"
        assert METRIC_AGENT_TASK_SUCCESS == "agent.task_success"
        assert METRIC_AGENT_TOOL_CONTRACT == "agent.tool_contract"

    def test_kb_metric_values_pass_number_definition_validation(self):
        definition = _definition(
            METRIC_KB_RECALL_AT_5, "retrieval", "number", (0.0, 1.0)
        )
        for func in (kb_recall_at_5, kb_ndcg_at_5, kb_mrr_at_5):
            value = func((_chunk("a"),), {_chunk("a"): 1})
            validate_evaluation_value(definition, value.typed_value)

    def test_agent_metric_values_pass_boolean_definition_validation(self):
        for metric_name in (METRIC_AGENT_TASK_SUCCESS, METRIC_AGENT_TOOL_CONTRACT):
            definition = _definition(metric_name, "agent_run", "boolean", None)
            if metric_name == METRIC_AGENT_TASK_SUCCESS:
                value = agent_task_success(
                    TaskSuccessInput(
                        completion_reported=True,
                        side_effect_evidence=SideEffectEvidence(
                            expected=(), verified=()
                        ),
                    )
                )
            else:
                value = agent_tool_contract(
                    ToolContractInput(
                        calls=(), allowed_tools=(), expected_side_effects=()
                    )
                )
            assert value.typed_value is True
            validate_evaluation_value(definition, value.typed_value)

    def test_skipped_values_carry_reason_and_no_value(self):
        skipped = kb_recall_at_5((_chunk("a"),), {_chunk("a"): 0})
        assert skipped.typed_value is None
        assert isinstance(skipped.skip_reason, SkipReason)
        assert skipped.skip_reason is SkipReason.no_answer

    def test_evaluation_value_value_status_invariants(self):
        with pytest.raises(EvaluationValidationError):
            EvaluationValue(typed_value=None, status="value")
        with pytest.raises(EvaluationValidationError):
            EvaluationValue(
                typed_value=0.5,
                status="value",
                skip_reason=SkipReason.no_answer,
            )

    def test_evaluation_value_skipped_status_invariants(self):
        with pytest.raises(EvaluationValidationError):
            EvaluationValue(typed_value=0.5, status="skipped")
        with pytest.raises(EvaluationValidationError):
            EvaluationValue(typed_value=None, status="skipped", skip_reason=None)

    def test_evaluation_value_is_frozen(self):
        value = kb_recall_at_5((_chunk("a"),), {_chunk("a"): 1})
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.typed_value = 0.0  # type: ignore[misc]

    def test_value_status_accepts_scalar_types(self):
        for typed_value in (True, False, 1, 0, 0.5, "pass"):
            value = EvaluationValue(typed_value=typed_value, status="value")
            assert value.typed_value == typed_value

    @pytest.mark.parametrize("typed_value", [float("nan"), float("inf"), float("-inf")])
    def test_value_status_rejects_non_finite_float(self, typed_value):
        with pytest.raises(EvaluationValidationError):
            EvaluationValue(typed_value=typed_value, status="value")

    @pytest.mark.parametrize(
        "typed_value",
        [
            {"verdict": "pass"},  # Mapping 载荷禁止按引用携带
            ["pass"],
            ("pass",),
            {"verdict"},
            None,
            [0.5, {"similarity": 0.9}],
        ],
    )
    def test_value_status_rejects_mutable_and_non_scalar(self, typed_value):
        with pytest.raises(EvaluationValidationError):
            EvaluationValue(typed_value=typed_value, status="value")


# ---------------------------------------------------------------------------
# 序列输入防呆：字符串不得被 tuple() 炸裂成字符，非序列不得抛裸 TypeError
# ---------------------------------------------------------------------------


class TestInputSequenceGuards:
    def test_side_effect_evidence_rejects_string_not_char_exploded(self):
        with pytest.raises(EvaluationValidationError):
            SideEffectEvidence(expected="artifact.created", verified=())

    def test_side_effect_evidence_rejects_non_sequence(self):
        with pytest.raises(EvaluationValidationError):
            SideEffectEvidence(expected=7, verified=())

    def test_tool_call_record_rejects_string_side_effects(self):
        with pytest.raises(EvaluationValidationError):
            ToolCallRecord(tool="writer", schema_valid=True, side_effects="a.b")

    def test_tool_call_record_rejects_non_sequence_side_effects(self):
        with pytest.raises(EvaluationValidationError):
            ToolCallRecord(tool="writer", schema_valid=True, side_effects=3)

    def test_tool_contract_input_rejects_string_calls(self):
        with pytest.raises(EvaluationValidationError):
            ToolContractInput(calls="writer", allowed_tools=("writer",))

    def test_tool_contract_input_rejects_string_allowed_tools(self):
        with pytest.raises(EvaluationValidationError):
            ToolContractInput(calls=(), allowed_tools="writer")

    def test_tool_contract_input_rejects_non_sequence_expected(self):
        with pytest.raises(EvaluationValidationError):
            ToolContractInput(calls=(), allowed_tools=(), expected_side_effects=99)
