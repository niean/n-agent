# Evaluation 契约模型测试：对照 spec 6.1-6.3 校验 EvaluationTarget、
# EvidenceSnapshot、EvaluatorDefinition、DatasetManifest、Experiment 的
# 不变量与校验函数行为。
from __future__ import annotations

import dataclasses
import math
from decimal import Decimal

import pytest

from app.domain.evaluation.models import (
    EVIDENCE_SNAPSHOT_MAX_BYTES,
    DefinitionUsage,
    EvaluationValidationError,
    DatasetCase,
    DatasetManifest,
    EvidenceSnapshot,
    EvaluationTarget,
    EvaluatorDefinition,
    Experiment,
    validate_evaluation_value,
)


# ---------------------------------------------------------------------------
# EvaluationTarget（spec 6.1 身份与目标）
# ---------------------------------------------------------------------------


def _valid_target_kwargs() -> dict:
    return {
        "service": "n-agent",
        "kind": "agent_run",
        "subject_id": "run-123",
        "subject_revision": "rev-7",
    }


class TestEvaluationTarget:
    def test_valid_minimal_construction(self):
        target = EvaluationTarget(**_valid_target_kwargs())
        assert target.service == "n-agent"
        assert target.kind == "agent_run"
        assert target.subject_id == "run-123"
        assert target.subject_revision == "rev-7"
        # 可选身份字段默认全部为 None
        assert target.trace_id is None
        assert target.span_id is None
        assert target.run_id is None
        assert target.session_id is None
        assert target.retrieval_id is None

    def test_valid_full_construction(self):
        target = EvaluationTarget(
            trace_id="a" * 32,
            span_id="b" * 16,
            run_id="run-1",
            session_id="sess-1",
            retrieval_id="r-1",
            **_valid_target_kwargs(),
        )
        assert target.trace_id == "a" * 32
        assert target.span_id == "b" * 16
        assert target.run_id == "run-1"
        assert target.session_id == "sess-1"
        assert target.retrieval_id == "r-1"

    @pytest.mark.parametrize("missing", ["service", "subject_id", "subject_revision"])
    def test_required_fields_must_be_non_empty(self, missing: str):
        kwargs = _valid_target_kwargs()
        kwargs[missing] = ""
        with pytest.raises(EvaluationValidationError):
            EvaluationTarget(**kwargs)

    @pytest.mark.parametrize("missing", ["service", "subject_id", "subject_revision"])
    def test_required_fields_cannot_be_omitted(self, missing: str):
        kwargs = _valid_target_kwargs()
        del kwargs[missing]
        with pytest.raises(TypeError):
            EvaluationTarget(**kwargs)

    @pytest.mark.parametrize(
        "kind",
        ["agent_run", "retrieval", "experiment_item", "session_feedback"],
    )
    def test_allowed_kinds(self, kind: str):
        kwargs = _valid_target_kwargs()
        kwargs["kind"] = kind
        target = EvaluationTarget(**kwargs)
        assert target.kind == kind

    @pytest.mark.parametrize("kind", ["agent", "trace", "http_request", "", "AGENT_RUN"])
    def test_out_of_range_kind_raises(self, kind: str):
        kwargs = _valid_target_kwargs()
        kwargs["kind"] = kind
        with pytest.raises(EvaluationValidationError):
            EvaluationTarget(**kwargs)

    def test_identity_fields_are_stored_independently(self):
        # run_id/session_id 不冒充 trace_id：五个身份字段是独立字段，
        # 各自保存、互不别名、互不合并。
        target = EvaluationTarget(
            run_id="run-1", session_id="sess-1", **_valid_target_kwargs()
        )
        field_names = {f.name for f in dataclasses.fields(target)}
        assert {"trace_id", "span_id", "run_id", "session_id", "retrieval_id"} <= field_names
        # run_id/session_id 有值时 trace_id/span_id 保持为空
        assert target.trace_id is None
        assert target.span_id is None
        assert target.run_id == "run-1"
        assert target.session_id == "sess-1"
        # 反向同理：trace_id 有值不产生 run_id/session_id
        target2 = EvaluationTarget(trace_id="c" * 32, **_valid_target_kwargs())
        assert target2.trace_id == "c" * 32
        assert target2.run_id is None
        assert target2.session_id is None

    def test_trace_id_must_be_32_lowercase_hex(self):
        # spec 6.1：真实 trace_id 为 32 位 OTel hex 身份
        kwargs = _valid_target_kwargs()
        kwargs["trace_id"] = "a" * 32
        assert EvaluationTarget(**kwargs).trace_id == "a" * 32
        for bad in ("A" * 32, "a" * 31, "a" * 33, "z" * 32, "a" * 31 + "g"):
            kwargs["trace_id"] = bad
            with pytest.raises(EvaluationValidationError):
                EvaluationTarget(**kwargs)

    def test_span_id_must_be_16_lowercase_hex(self):
        # spec 6.1：真实 span_id 为 16 位 OTel hex 身份
        kwargs = _valid_target_kwargs()
        kwargs["span_id"] = "b" * 16
        assert EvaluationTarget(**kwargs).span_id == "b" * 16
        for bad in ("B" * 16, "b" * 15, "b" * 17, "b" * 15 + "g"):
            kwargs["span_id"] = bad
            with pytest.raises(EvaluationValidationError):
                EvaluationTarget(**kwargs)

    def test_frozen(self):
        target = EvaluationTarget(**_valid_target_kwargs())
        with pytest.raises(dataclasses.FrozenInstanceError):
            target.service = "other"


# ---------------------------------------------------------------------------
# EvidenceSnapshot（spec 6.3：规范化 JSON + SHA-256 + 2MiB 上限 + 脱敏版本）
# ---------------------------------------------------------------------------


class TestEvidenceSnapshot:
    def test_sha256_over_canonical_json(self):
        snapshot = EvidenceSnapshot(
            content={"b": 1, "a": "中文"},
            redaction_version="redact-v1",
        )
        assert len(snapshot.content_sha256) == 64
        int(snapshot.content_sha256, 16)  # 必须是合法十六进制
        # 规范化编码：固定键序、紧凑分隔符、UTF-8
        expected = '{"a":"中文","b":1}'.encode("utf-8")
        assert snapshot.canonical_bytes == expected
        import hashlib

        assert snapshot.content_sha256 == hashlib.sha256(expected).hexdigest()

    def test_key_order_does_not_change_hash(self):
        s1 = EvidenceSnapshot(
            content={"x": 1, "y": {"b": 2, "a": 3}}, redaction_version="v1"
        )
        s2 = EvidenceSnapshot(
            content={"y": {"a": 3, "b": 2}, "x": 1}, redaction_version="v1"
        )
        assert s1.content_sha256 == s2.content_sha256
        assert s1.canonical_bytes == s2.canonical_bytes

    def test_redaction_version_required(self):
        with pytest.raises(EvaluationValidationError):
            EvidenceSnapshot(content={"a": 1}, redaction_version="")
        with pytest.raises(EvaluationValidationError):
            EvidenceSnapshot(content={"a": 1}, redaction_version=None)

    def test_oversized_content_rejected(self):
        big = {"blob": "x" * (EVIDENCE_SNAPSHOT_MAX_BYTES + 1)}
        with pytest.raises(EvaluationValidationError):
            EvidenceSnapshot(content=big, redaction_version="v1")

    def test_content_at_limit_accepted(self):
        # 恰好等于 2MiB 上限的内容可接受：按 JSON 框架字节精确补齐
        framing = len(b'{"blob":""}')
        payload = "x" * (EVIDENCE_SNAPSHOT_MAX_BYTES - framing)
        snapshot = EvidenceSnapshot(content={"blob": payload}, redaction_version="v1")
        assert len(snapshot.canonical_bytes) == EVIDENCE_SNAPSHOT_MAX_BYTES

    def test_non_json_serializable_content_rejected(self):
        class SdkObject:
            pass

        with pytest.raises(EvaluationValidationError):
            EvidenceSnapshot(content={"obj": SdkObject()}, redaction_version="v1")

    def test_non_finite_floats_rejected(self):
        with pytest.raises(EvaluationValidationError):
            EvidenceSnapshot(content={"v": float("nan")}, redaction_version="v1")
        with pytest.raises(EvaluationValidationError):
            EvidenceSnapshot(content={"v": float("inf")}, redaction_version="v1")

    def test_source_dict_mutation_after_construction_does_not_diverge(self):
        # 构造后修改外部字典不得影响快照正文与 hash（防别名）
        source = {"a": 1}
        snapshot = EvidenceSnapshot(content=source, redaction_version="v1")
        before = snapshot.content_sha256
        source["a"] = 999
        source["b"] = "new"
        assert snapshot.content["a"] == 1
        assert "b" not in snapshot.content
        assert snapshot.content_sha256 == before
        assert snapshot.canonical_bytes == b'{"a":1}'

    def test_content_is_readonly_mapping(self):
        snapshot = EvidenceSnapshot(content={"a": 1}, redaction_version="v1")
        with pytest.raises(TypeError):
            snapshot.content["a"] = 2

    def test_content_must_be_mapping(self):
        with pytest.raises(EvaluationValidationError):
            EvidenceSnapshot(content=[1, 2, 3], redaction_version="v1")

    def test_frozen(self):
        snapshot = EvidenceSnapshot(content={"a": 1}, redaction_version="v1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.redaction_version = "v2"


# ---------------------------------------------------------------------------
# EvaluatorDefinition（spec 6.2）
# ---------------------------------------------------------------------------


def _valid_definition_kwargs() -> dict:
    return {
        "definition_id": "kb.recall_at_5",
        "version": "1",
        "metric_name": "kb.recall_at_5",
        "target_kind": "retrieval",
        "method": "deterministic",
        "required_fields": ("retrieved_ids", "qrels"),
        "evidence_policy": "require_frozen_corpus",
        "output_type": "number",
        "range_or_categories": (0.0, 1.0),
        "direction": "higher_is_better",
        "material_hash": {"code": "a" * 64},
        "judge_model": None,
        "judge_params": {},
        "timeout_seconds": 60,
        "budget": None,
        "ci_admissible": True,
    }


class TestEvaluatorDefinition:
    def test_valid_deterministic_definition(self):
        definition = EvaluatorDefinition(**_valid_definition_kwargs())
        assert definition.definition_id == "kb.recall_at_5"
        assert definition.method == "deterministic"
        assert definition.ci_admissible is True

    def test_valid_llm_judge_definition(self):
        kwargs = _valid_definition_kwargs()
        kwargs.update(
            {
                "method": "llm_judge",
                "judge_model": "gpt-4o-mini",
                "judge_params": {"temperature": 0},
                "material_hash": {"rubric": "b" * 64, "prompt": "c" * 64},
            }
        )
        definition = EvaluatorDefinition(**kwargs)
        assert definition.judge_model == "gpt-4o-mini"

    def test_llm_judge_requires_judge_model(self):
        kwargs = _valid_definition_kwargs()
        kwargs["method"] = "llm_judge"
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    @pytest.mark.parametrize(
        "field, bad",
        [
            ("definition_id", ""),
            ("version", ""),
            ("metric_name", ""),
            ("evidence_policy", ""),
        ],
    )
    def test_required_strings_non_empty(self, field: str, bad: str):
        kwargs = _valid_definition_kwargs()
        kwargs[field] = bad
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    @pytest.mark.parametrize("method", ["auto", "judge", "", "DETERMINISTIC"])
    def test_out_of_range_method_raises(self, method: str):
        kwargs = _valid_definition_kwargs()
        kwargs["method"] = method
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    @pytest.mark.parametrize("target_kind", ["agent", "", "kb"])
    def test_out_of_range_target_kind_raises(self, target_kind: str):
        kwargs = _valid_definition_kwargs()
        kwargs["target_kind"] = target_kind
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    @pytest.mark.parametrize("output_type", ["text", "float", ""])
    def test_out_of_range_output_type_raises(self, output_type: str):
        kwargs = _valid_definition_kwargs()
        kwargs["output_type"] = output_type
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    @pytest.mark.parametrize("direction", ["both", "", "lower", "lower_is_better"])
    def test_out_of_range_direction_raises(self, direction: str):
        kwargs = _valid_definition_kwargs()
        kwargs["direction"] = direction
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    def test_number_output_requires_two_tuple_range(self):
        kwargs = _valid_definition_kwargs()
        kwargs["range_or_categories"] = (0.0,)
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        kwargs["range_or_categories"] = ("a", "b")
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        # bool 不能充当范围端点
        kwargs["range_or_categories"] = (False, True)
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    def test_number_output_range_low_le_high(self):
        kwargs = _valid_definition_kwargs()
        kwargs["range_or_categories"] = (1.0, 0.0)
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    def test_categorical_output_requires_non_empty_unique_categories(self):
        kwargs = _valid_definition_kwargs()
        kwargs["output_type"] = "categorical"
        kwargs["range_or_categories"] = ()
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        kwargs["range_or_categories"] = ("pass", "pass")
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        kwargs["range_or_categories"] = ("pass", "fail", "partial")
        definition = EvaluatorDefinition(**kwargs)
        assert definition.range_or_categories == ("pass", "fail", "partial")

    def test_boolean_output_requires_empty_range(self):
        kwargs = _valid_definition_kwargs()
        kwargs["output_type"] = "boolean"
        kwargs["range_or_categories"] = ()
        definition = EvaluatorDefinition(**kwargs)
        assert definition.output_type == "boolean"
        kwargs["range_or_categories"] = (0.0, 1.0)
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    def test_material_hash_keys_and_values_validated(self):
        kwargs = _valid_definition_kwargs()
        kwargs["material_hash"] = {"recipe": "a" * 64}
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        kwargs["material_hash"] = {"code": "nothex"}
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    def test_timeout_seconds_must_be_positive_int(self):
        kwargs = _valid_definition_kwargs()
        kwargs["timeout_seconds"] = 0
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        kwargs["timeout_seconds"] = -5
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        kwargs["timeout_seconds"] = 60.5
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    def test_budget_must_be_finite_non_negative(self):
        kwargs = _valid_definition_kwargs()
        kwargs["budget"] = Decimal("5.00")
        definition = EvaluatorDefinition(**kwargs)
        assert definition.budget == Decimal("5.00")
        kwargs["budget"] = Decimal("-1")
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)
        kwargs["budget"] = Decimal("NaN")
        with pytest.raises(EvaluationValidationError):
            EvaluatorDefinition(**kwargs)

    def test_frozen(self):
        definition = EvaluatorDefinition(**_valid_definition_kwargs())
        with pytest.raises(dataclasses.FrozenInstanceError):
            definition.version = "2"


# ---------------------------------------------------------------------------
# validate_evaluation_value（spec 6.2 输出类型校验）
# ---------------------------------------------------------------------------


class TestValidateEvaluationValue:
    def _definition(self, **overrides) -> EvaluatorDefinition:
        kwargs = _valid_definition_kwargs()
        kwargs.update(overrides)
        return EvaluatorDefinition(**kwargs)

    def test_number_accepts_finite_in_range(self):
        definition = self._definition()
        validate_evaluation_value(definition, 0.0)
        validate_evaluation_value(definition, 0.5)
        validate_evaluation_value(definition, 1)
        validate_evaluation_value(definition, 1.0)

    def test_number_rejects_bool(self):
        definition = self._definition()
        with pytest.raises(EvaluationValidationError):
            validate_evaluation_value(definition, True)
        with pytest.raises(EvaluationValidationError):
            validate_evaluation_value(definition, False)

    def test_number_rejects_nan_and_inf(self):
        definition = self._definition()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(EvaluationValidationError):
                validate_evaluation_value(definition, bad)

    def test_number_rejects_out_of_range(self):
        definition = self._definition()
        for bad in (-0.1, 1.01, 100):
            with pytest.raises(EvaluationValidationError):
                validate_evaluation_value(definition, bad)

    def test_number_rejects_non_numeric(self):
        definition = self._definition()
        for bad in ("0.5", None, [1], Decimal("0.5")):
            with pytest.raises(EvaluationValidationError):
                validate_evaluation_value(definition, bad)

    def test_number_without_range_accepts_any_finite(self):
        definition = self._definition(
            output_type="number", range_or_categories=None
        )
        validate_evaluation_value(definition, -3.5)
        with pytest.raises(EvaluationValidationError):
            validate_evaluation_value(definition, float("inf"))

    def test_boolean_accepts_only_real_bool(self):
        definition = self._definition(
            output_type="boolean", range_or_categories=()
        )
        validate_evaluation_value(definition, True)
        validate_evaluation_value(definition, False)
        for bad in (1, 0, "true", None):
            with pytest.raises(EvaluationValidationError):
                validate_evaluation_value(definition, bad)

    def test_categorical_accepts_only_listed(self):
        definition = self._definition(
            output_type="categorical",
            range_or_categories=("pass", "fail", "partial"),
        )
        validate_evaluation_value(definition, "pass")
        validate_evaluation_value(definition, "partial")
        for bad in ("Pass", "unknown", 1, None):
            with pytest.raises(EvaluationValidationError):
                validate_evaluation_value(definition, bad)


# ---------------------------------------------------------------------------
# DatasetManifest / Experiment（spec 9.1 / 6.3）
# ---------------------------------------------------------------------------


def _valid_case_kwargs() -> dict:
    return {
        "case_id": "case-001",
        "input": {"query": "如何部署 N-Agent"},
        "target_kind": "retrieval",
    }


def _valid_manifest_kwargs() -> dict:
    return {
        "schema_version": "evaluation.dataset.v1",
        "dataset_id": "smoke-agent-20",
        "dataset_version": "1",
        "dataset_hash": "a" * 64,
        "target_kind": "agent_run",
        "cases": (DatasetCase(**_valid_case_kwargs()),),
    }


class TestDatasetManifest:
    def test_valid_manifest(self):
        manifest = DatasetManifest(**_valid_manifest_kwargs())
        assert manifest.dataset_id == "smoke-agent-20"
        assert manifest.dataset_hash == "a" * 64
        assert len(manifest.cases) == 1

    def test_case_with_expected_output_and_qrels(self):
        kwargs = _valid_case_kwargs()
        kwargs["expected_output"] = {"answer": "使用 docker/restart.sh"}
        case = DatasetCase(**kwargs)
        assert case.expected_output["answer"].startswith("使用")
        # Mapping 型真值冻结：事后修改外部字典不影响实例
        kwargs["expected_output"]["answer"] = "被篡改"
        assert case.expected_output["answer"].startswith("使用")
        with pytest.raises(TypeError):
            case.expected_output["answer"] = "再篡改"
        kwargs2 = _valid_case_kwargs()
        kwargs2["qrels"] = {"doc1#chunk1": 2}
        case2 = DatasetCase(**kwargs2)
        assert case2.qrels == {"doc1#chunk1": 2}

    def test_case_invalid_target_kind_raises(self):
        kwargs = _valid_case_kwargs()
        kwargs["target_kind"] = "http"
        with pytest.raises(EvaluationValidationError):
            DatasetCase(**kwargs)

    def test_case_fixture_optional(self):
        # spec 9.1：fixture 是 case 的可选附件引用，缺省为 None
        case = DatasetCase(**_valid_case_kwargs())
        assert case.fixture is None
        kwargs = _valid_case_kwargs()
        kwargs["fixture"] = "evaluations/datasets/fixtures/case-001.json"
        case = DatasetCase(**kwargs)
        assert case.fixture == "evaluations/datasets/fixtures/case-001.json"
        # 给定时不得为空串占位
        kwargs["fixture"] = ""
        with pytest.raises(EvaluationValidationError):
            DatasetCase(**kwargs)

    def test_manifest_requires_non_empty_ids_and_hash(self):
        for field in ("schema_version", "dataset_id", "dataset_version"):
            kwargs = _valid_manifest_kwargs()
            kwargs[field] = ""
            with pytest.raises(EvaluationValidationError):
                DatasetManifest(**kwargs)
        kwargs = _valid_manifest_kwargs()
        kwargs["dataset_hash"] = "xyz"
        with pytest.raises(EvaluationValidationError):
            DatasetManifest(**kwargs)

    def test_manifest_requires_at_least_one_case(self):
        kwargs = _valid_manifest_kwargs()
        kwargs["cases"] = ()
        with pytest.raises(EvaluationValidationError):
            DatasetManifest(**kwargs)

    def test_manifest_invalid_target_kind_raises(self):
        kwargs = _valid_manifest_kwargs()
        kwargs["target_kind"] = "session"
        with pytest.raises(EvaluationValidationError):
            DatasetManifest(**kwargs)


def _valid_experiment_kwargs() -> dict:
    return {
        "experiment_id": "exp-001",
        "dataset_id": "smoke-agent-20",
        "dataset_version": "1",
        "dataset_hash": "a" * 64,
        "target_config_hash": "b" * 64,
        "evaluator_set_hash": "c" * 64,
        "mode": "execute",
        "status": "created",
    }


class TestExperiment:
    def test_valid_experiment(self):
        experiment = Experiment(**_valid_experiment_kwargs())
        assert experiment.experiment_id == "exp-001"
        assert experiment.baseline_id is None
        assert experiment.gate_report_ref is None

    def test_execute_and_replay_modes_only(self):
        for mode in ("execute", "replay"):
            kwargs = _valid_experiment_kwargs()
            kwargs["mode"] = mode
            assert Experiment(**kwargs).mode == mode
        kwargs = _valid_experiment_kwargs()
        kwargs["mode"] = "dry_run"
        with pytest.raises(EvaluationValidationError):
            Experiment(**kwargs)

    def test_hash_fields_validated(self):
        for field in ("dataset_hash", "target_config_hash", "evaluator_set_hash"):
            kwargs = _valid_experiment_kwargs()
            kwargs[field] = "nothex"
            with pytest.raises(EvaluationValidationError):
                Experiment(**kwargs)

    def test_baseline_optional_but_non_empty_when_given(self):
        kwargs = _valid_experiment_kwargs()
        kwargs["baseline_id"] = "exp-baseline"
        assert Experiment(**kwargs).baseline_id == "exp-baseline"
        kwargs["baseline_id"] = ""
        with pytest.raises(EvaluationValidationError):
            Experiment(**kwargs)

    def test_status_non_empty(self):
        kwargs = _valid_experiment_kwargs()
        kwargs["status"] = ""
        with pytest.raises(EvaluationValidationError):
            Experiment(**kwargs)

    def test_frozen(self):
        experiment = Experiment(**_valid_experiment_kwargs())
        with pytest.raises(dataclasses.FrozenInstanceError):
            experiment.status = "running"


# ---------------------------------------------------------------------------
# DefinitionUsage（定义线上使用状态 VO）
# ---------------------------------------------------------------------------


def _valid_usage_kwargs() -> dict:
    return {
        "definition_id": "kb.recall_at_5",
        "definition_version": "1",
        "online_enabled": True,
        "revision": 1,
        "updated_at": "2026-11-12T00:00:00+00:00",
        "updated_by": "dashboard",
    }


class TestDefinitionUsage:
    def test_valid_construction(self):
        usage = DefinitionUsage(**_valid_usage_kwargs())
        assert usage.definition_id == "kb.recall_at_5"
        assert usage.definition_version == "1"
        assert usage.online_enabled is True
        assert usage.revision == 1
        assert usage.updated_at == "2026-11-12T00:00:00+00:00"
        assert usage.updated_by == "dashboard"

    def test_for_definition_projects_identity(self):
        definition = EvaluatorDefinition(**_valid_definition_kwargs())
        usage = DefinitionUsage.for_definition(
            definition,
            online_enabled=True,
            revision=3,
            updated_at="2026-11-12T08:00:00+00:00",
            updated_by="dashboard",
        )
        assert usage.definition_id == definition.definition_id
        assert usage.definition_version == definition.version
        assert usage.online_enabled is True
        assert usage.revision == 3

    def test_human_definition_forbids_online_enable(self):
        kwargs = _valid_definition_kwargs()
        kwargs["method"] = "human"
        definition = EvaluatorDefinition(**kwargs)
        with pytest.raises(EvaluationValidationError):
            DefinitionUsage.for_definition(
                definition,
                online_enabled=True,
                revision=1,
                updated_at="2026-11-12T08:00:00+00:00",
                updated_by="dashboard",
            )
        # human 定义关闭线上开关是合法状态
        usage = DefinitionUsage.for_definition(
            definition,
            online_enabled=False,
            revision=1,
            updated_at="2026-11-12T08:00:00+00:00",
            updated_by="dashboard",
        )
        assert usage.online_enabled is False

    @pytest.mark.parametrize("revision", [0, -1, 1.5, True, "2", None])
    def test_revision_must_be_positive_int(self, revision):
        kwargs = _valid_usage_kwargs()
        kwargs["revision"] = revision
        with pytest.raises(EvaluationValidationError):
            DefinitionUsage(**kwargs)

    @pytest.mark.parametrize("online_enabled", [0, 1, "true", None])
    def test_online_enabled_must_be_bool(self, online_enabled):
        kwargs = _valid_usage_kwargs()
        kwargs["online_enabled"] = online_enabled
        with pytest.raises(EvaluationValidationError):
            DefinitionUsage(**kwargs)

    def test_updated_at_must_be_tz_aware_iso(self):
        kwargs = _valid_usage_kwargs()
        kwargs["updated_at"] = "not-a-time"
        with pytest.raises(EvaluationValidationError):
            DefinitionUsage(**kwargs)
        kwargs["updated_at"] = "2026-11-12T00:00:00"
        with pytest.raises(EvaluationValidationError):
            DefinitionUsage(**kwargs)

    def test_required_strings_non_empty(self):
        for field in ("definition_id", "definition_version", "updated_by"):
            kwargs = _valid_usage_kwargs()
            kwargs[field] = ""
            with pytest.raises(EvaluationValidationError):
                DefinitionUsage(**kwargs)

    def test_to_mapping_from_mapping_roundtrip(self):
        usage = DefinitionUsage(**_valid_usage_kwargs())
        mapping = usage.to_mapping()
        assert mapping == _valid_usage_kwargs()
        restored = DefinitionUsage.from_mapping(mapping)
        assert restored == usage

    def test_from_mapping_rejects_missing_fields_and_non_mapping(self):
        with pytest.raises(EvaluationValidationError):
            DefinitionUsage.from_mapping({"definition_id": "x"})
        with pytest.raises(EvaluationValidationError):
            DefinitionUsage.from_mapping(["not", "a", "mapping"])

    def test_frozen(self):
        usage = DefinitionUsage(**_valid_usage_kwargs())
        with pytest.raises(dataclasses.FrozenInstanceError):
            usage.online_enabled = False
