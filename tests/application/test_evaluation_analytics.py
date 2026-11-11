# app.application.evaluation_analytics 用例测试（plan Task 7，spec 4.5 / 4.6）。
#
# TDD 严格 RED -> GREEN。S1（analytics 部分）覆盖：
# - EvaluationAnalyticsService.overview_analytics：Task 4 聚合查询
#   （store.aggregate_metric_results）+ Domain analytics.py 纯函数组装；
# - 响应含 snapshot_at、window {from,to}、UTC+8 日期桶（含零记录空日）、
#   按 (DefinitionKey, provenance, output_type) 分组的统计；
# - 无值分组 count=0 其余 null；error/skipped 分列；不生成未定义阈值的
#   通过率；无总体分母 coverage: unknown 附原因"缺少应评估目标总数"；
# - 过滤校验复用 Task 1 normalize_analytics_filters（from>=to / 超 90 天
#   拒绝）。
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

from app.application.evaluation_analytics import (
    EvaluationAnalyticsService,
    IncomparableExperimentsError,
)
from app.application.evaluation_gate import (
    gate_policy_content,
    gate_policy_from_content,
)
from app.application.evaluation_service import (
    DefinitionKey,
    EvaluationService,
    SubmitSampleRequest,
)
from app.domain.evaluation.analytics import (
    normalize_analytics_filters,
    utc8_day_bucket,
)
from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import (
    EvaluationTarget,
    EvaluationValidationError,
    canonical_json_bytes,
)
from app.infrastructure.evaluation.store import SQLiteEvaluationStore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 11, 12, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))


@pytest.fixture
def evaluation_service(store):
    return EvaluationService(store, enabled=True)


def _definition_payload(
    definition_id: str,
    *,
    output_type: str,
    range_or_categories=None,
    version: str = "v1",
) -> dict:
    return {
        "definition_id": definition_id,
        "version": version,
        "metric_name": definition_id,
        "target_kind": "experiment_item",
        "method": "llm_judge",
        "required_fields": [],
        "evidence_policy": "snapshot_complete",
        "output_type": output_type,
        "range_or_categories": (
            [] if range_or_categories is None else range_or_categories
        ),
        "direction": "higher_is_better",
        "material_hash": {},
        "judge_model": "judge-model",
        "judge_params": {"temperature": 0},
        "timeout_seconds": 30,
        "budget": None,
        "ci_admissible": True,
    }


async def _seed_definition(
    store, definition_id, *, output_type, range_or_categories=None
):
    await store.save_definition(
        _definition_payload(
            definition_id,
            output_type=output_type,
            range_or_categories=range_or_categories,
        )
    )


async def _seed_auto_result(
    store,
    service,
    definition_id,
    value,
    *,
    status: str = "value",
    provenance: str = "automatic",
    experiment_id: str = "exp-analytics",
    seq: int = 0,
    version: str = "v1",
):
    submitted = await service.submit_sample(
        SubmitSampleRequest(
            producer_event_id=(
                f"experiment:{experiment_id}:case-{seq:04d}#r0:root"
            ),
            service="n-agent",
            target=EvaluationTarget(
                service="n-agent",
                kind="experiment_item",
                subject_id=f"case-{seq:04d}#r0",
                subject_revision="v1",
            ),
            definitions=(
                DefinitionKey(
                    definition_id=definition_id,
                    definition_version=version,
                ),
            ),
            experiment_id=experiment_id,
            mode="execute",
            replicate=0,
        )
    )
    await store.save_result(
        {
            "job_id": submitted.job_ids[0],
            "provenance": provenance,
            "metric_name": definition_id,
            "metric_version": version,
            "status": status,
            "skip_reason": (
                "evidence_unavailable" if status == "skipped" else None
            ),
            "typed_value": value,
            "passed": None,
            "explanation": "",
            "evidence_hash": "",
        }
    )


def _expected_bucket_dates(window_from: datetime, window_to: datetime):
    """与实现同口径的期望 UTC+8 日期桶序列（[from, to) 覆盖的自然日）。"""
    start = date.fromisoformat(utc8_day_bucket(window_from.isoformat()))
    end = date.fromisoformat(
        utc8_day_bucket((window_to - timedelta(microseconds=1)).isoformat())
    )
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# S1: overview_analytics 响应形状与过滤校验
# ---------------------------------------------------------------------------


class TestOverviewAnalyticsShape:
    async def test_empty_ledger_shape(self, store):
        service = EvaluationAnalyticsService(store)
        result = await service.overview_analytics(now=FIXED_NOW)
        assert result["snapshot_at"] == FIXED_NOW.isoformat()
        expected_from, expected_to = normalize_analytics_filters(
            created_from=None, created_to=None, now=FIXED_NOW
        )
        assert result["window"]["from"] == expected_from.isoformat()
        assert result["window"]["to"] == expected_to.isoformat()
        assert result["groups"] == []
        # UTC+8 日期桶含零记录空日：默认 7 天窗覆盖的自然日逐日齐全
        assert [b["date"] for b in result["buckets"]] == _expected_bucket_dates(
            expected_from, expected_to
        )
        assert all(bucket["total"] == 0 for bucket in result["buckets"])
        assert result["coverage"] == {
            "status": "unknown",
            "reason": "缺少应评估目标总数",
        }

    async def test_window_past_has_buckets_but_no_groups(self, store):
        service = EvaluationAnalyticsService(store)
        # 完全位于过去的窗口：无分组，但日期桶仍逐日生成
        result = await service.overview_analytics(
            created_from="2026-01-01T00:00:00+00:00",
            created_to="2026-01-08T00:00:00+00:00",
            now=FIXED_NOW,
        )
        assert result["groups"] == []
        assert [b["date"] for b in result["buckets"]] == _expected_bucket_dates(
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 8, tzinfo=timezone.utc),
        )
        assert len(result["buckets"]) == 8  # 含 1/1 与 1/8 边界空日
        # 手写日期字面量锚定边界（to-1µs 的 UTC+8 桶是 1/8，共 8 桶），
        # 不依赖 _expected_bucket_dates 与实现的同口径镜像
        assert [b["date"] for b in result["buckets"]] == [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-04",
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
        ]

    async def test_invalid_time_range_rejected(self, store):
        service = EvaluationAnalyticsService(store)
        with pytest.raises(EvaluationValidationError):
            await service.overview_analytics(
                created_from="2026-11-12T00:00:00+00:00",
                created_to="2026-11-11T00:00:00+00:00",
                now=FIXED_NOW,
            )

    async def test_over_max_range_rejected(self, store):
        service = EvaluationAnalyticsService(store)
        with pytest.raises(EvaluationValidationError):
            await service.overview_analytics(
                created_from="2026-01-01T00:00:00+00:00",
                created_to="2026-11-12T00:00:00+00:00",
                now=FIXED_NOW,
            )


class TestOverviewAnalyticsGrouping:
    async def test_groups_by_definition_provenance_output_type(self, store, evaluation_service):
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_definition(
            store, "agent.task_success", output_type="boolean"
        )
        # number：2 个有效值 + 1 skipped + 1 error（error/skipped 分列）
        await _seed_auto_result(store, evaluation_service, "rag.faithfulness", 1.0, seq=0)
        await _seed_auto_result(store, evaluation_service, "rag.faithfulness", 0.5, seq=1)
        await _seed_auto_result(
            store, evaluation_service, "rag.faithfulness", None,
            status="skipped", seq=2,
        )
        await _seed_auto_result(
            store, evaluation_service, "rag.faithfulness", None,
            status="error", seq=3,
        )
        # boolean：2 true + 1 false
        await _seed_auto_result(store, evaluation_service, "agent.task_success", True, seq=4)
        await _seed_auto_result(store, evaluation_service, "agent.task_success", True, seq=5)
        await _seed_auto_result(store, evaluation_service, "agent.task_success", False, seq=6)

        service = EvaluationAnalyticsService(store)
        result = await service.overview_analytics()

        assert result["coverage"] == {
            "status": "unknown",
            "reason": "缺少应评估目标总数",
        }
        assert result["snapshot_at"] == result["window"]["to"]

        groups = {(g["definition_id"], g["provenance"], g["output_type"]): g for g in result["groups"]}
        assert set(groups) == {
            ("agent.task_success", "automatic", "boolean"),
            ("rag.faithfulness", "automatic", "number"),
        }

        number = groups[("rag.faithfulness", "automatic", "number")]
        assert number["definition_version"] == "v1"
        assert number["total_count"] == 4
        assert number["status_counts"] == {"value": 2, "skipped": 1, "error": 1}
        assert number["count"] == 2
        assert number["mean"] == pytest.approx(0.75)
        assert number["min"] == pytest.approx(0.5)
        assert number["max"] == pytest.approx(1.0)
        # 不生成未定义阈值的通过率：number 分组不得出现通过率/阈值字段
        assert "pass_rate" not in number
        assert "threshold" not in number

        boolean = groups[("agent.task_success", "automatic", "boolean")]
        assert boolean["true_count"] == 2
        assert boolean["false_count"] == 1
        assert boolean["true_ratio"] == pytest.approx(2 / 3)
        assert "pass_rate" not in boolean

    async def test_empty_value_group_stats_are_null(self, store, evaluation_service):
        """仅 skipped/error 行的分组：count=0，其余统计为 null。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_auto_result(
            store, evaluation_service, "rag.faithfulness", None,
            status="skipped", seq=0,
        )
        await _seed_auto_result(
            store, evaluation_service, "rag.faithfulness", None,
            status="error", seq=1,
        )
        service = EvaluationAnalyticsService(store)
        result = await service.overview_analytics()
        assert len(result["groups"]) == 1
        group = result["groups"][0]
        assert group["count"] == 0
        assert group["mean"] is None
        assert group["min"] is None
        assert group["max"] is None
        assert group["status_counts"] == {"value": 0, "skipped": 1, "error": 1}

    async def test_buckets_count_today_and_zero_days(self, store, evaluation_service):
        """当日记录进当日 UTC+8 桶；其余日期 total=0 且逐日连续。"""
        await _seed_definition(store, "rag.faithfulness", output_type="number")
        await _seed_auto_result(store, evaluation_service, "rag.faithfulness", 1.0, seq=0)
        await _seed_auto_result(store, evaluation_service, "rag.faithfulness", 0.5, seq=1)

        now = datetime.now(timezone.utc)
        service = EvaluationAnalyticsService(store)
        result = await service.overview_analytics(now=now)

        dates = [b["date"] for b in result["buckets"]]
        expected_from, expected_to = normalize_analytics_filters(
            created_from=None, created_to=None, now=now
        )
        assert dates == _expected_bucket_dates(expected_from, expected_to)
        today_bucket = utc8_day_bucket(now.isoformat())
        totals = {b["date"]: b["total"] for b in result["buckets"]}
        assert totals[today_bucket] == 2
        assert sum(totals.values()) == 2
        # 分组内 daily_counts 与桶同口径
        assert result["groups"][0]["daily_counts"] == {today_bucket: 2}


# ---------------------------------------------------------------------------
# 404（compare 前置：实验必须存在；Task 9 注册路由映射）
# ---------------------------------------------------------------------------


class TestCompareNotFound:
    async def test_candidate_not_found(self, store):
        """candidate 实验不存在 -> EvaluationNotFoundError（404），
        且无 policy 的描述性路径同样先做存在性检查。"""
        service = EvaluationAnalyticsService(store)
        with pytest.raises(EvaluationNotFoundError):
            await service.compare_experiments(
                "exp-missing", "exp-b", policy=None
            )

    async def test_baseline_not_found(self, store, evaluation_service):
        """baseline 实验不存在 -> EvaluationNotFoundError（404）。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0]
        )
        service = EvaluationAnalyticsService(store)
        with pytest.raises(EvaluationNotFoundError):
            await service.compare_experiments("exp-c", "exp-b-missing")


# ---------------------------------------------------------------------------
# S4: compare_experiments —— 只读实验对比（spec 4.6）
# ---------------------------------------------------------------------------

DATASET_HASH_A = "a" * 64
DATASET_HASH_B = "b" * 64


def _manifest_json_str(
    case_ids,
    *,
    dataset_id: str = "ds-1",
    dataset_version: str = "v1",
    dataset_hash: str = DATASET_HASH_A,
) -> str:
    """experiment 冻结 manifest_json（与 execute 路径同一字段集）。"""
    return canonical_json_bytes(
        {
            "schema_version": "1",
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_hash": dataset_hash,
            "target_kind": "experiment_item",
            "case_ids": list(case_ids),
        }
    ).decode("utf-8")


async def _seed_experiment(
    store,
    service,
    exp_id,
    *,
    values,
    metric: str = "rag.faithfulness",
    dataset_id: str = "ds-1",
    dataset_version: str = "v1",
    dataset_hash: str = DATASET_HASH_A,
    manifest_json: str | None = None,
    evaluator_set_hash: str = "e" * 64,
    target_config_hash: str = "t" * 64,
):
    """种子一个实验：experiment 记录（冻结身份/manifest）+ 自动结果行。"""
    case_ids = [f"case-{i:04d}" for i in range(len(values))]
    if manifest_json is None:
        manifest_json = _manifest_json_str(
            case_ids,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_hash=dataset_hash,
        )
    await store.save_experiment(
        {
            "experiment_id": exp_id,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_hash": dataset_hash,
            "target_config_hash": target_config_hash,
            "evaluator_set_hash": evaluator_set_hash,
            "manifest_json": manifest_json,
            "mode": "execute",
            "status": "completed",
        }
    )
    for seq, value in enumerate(values):
        await _seed_auto_result(
            store, service, metric, value, experiment_id=exp_id, seq=seq
        )


def _policy_content(**overrides) -> dict:
    """compare 用窄策略（小样本/少 bootstrap 轮次，保持确定性）。"""
    content = {
        "schema_version": "1",
        "policy_version": "compare-v1",
        "required_metrics": {"rag.faithfulness": 0.9},
        "coverage_metrics": ["rag.faithfulness"],
        "safety_slices": [],
        "key_slices": [],
        "non_inferiority_margin": -0.02,
        "min_cases": 2,
        "min_paired_cases": 2,
        "min_key_slice_cases": 1,
        "bootstrap_replicates": 50,
        "bootstrap_seed": 20261112,
    }
    content.update(overrides)
    return content


def _expected_policy_fingerprint(content: dict) -> str:
    policy = gate_policy_from_content(content)
    return hashlib.sha256(
        canonical_json_bytes(gate_policy_content(policy))
    ).hexdigest()


class FakeRunner:
    """只读断言 fake：记录 execute/replay/judge 调用次数（spec 4.6：
    对比只读，不重跑目标、不启动 Judge）。"""

    def __init__(self):
        self.calls = {"execute": 0, "replay": 0, "judge": 0}

    async def execute_experiment(self, *args, **kwargs):
        self.calls["execute"] += 1
        raise AssertionError("compare 不得触发实验执行")

    async def replay_experiment(self, *args, **kwargs):
        self.calls["replay"] += 1
        raise AssertionError("compare 不得触发实验重放")

    async def judge(self, *args, **kwargs):
        self.calls["judge"] += 1
        raise AssertionError("compare 不得启动 Judge")


class TestCompareIncomparable:
    async def test_dataset_hash_mismatch_incomparable_without_policy(
        self, store, evaluation_service
    ):
        """无 policy 的描述性对比同样执行数据集身份核验（spec 4.6：
        不同数据集返回明确错误，不能只凭名称强行对比）。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0, 1.0]
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[1.0, 1.0],
            dataset_hash=DATASET_HASH_B,
        )
        service = EvaluationAnalyticsService(store)
        with pytest.raises(IncomparableExperimentsError) as excinfo:
            await service.compare_experiments("exp-c", "exp-b", policy=None)
        assert excinfo.value.diff_dimensions == {
            "dataset_id": False,
            "dataset_version": False,
            "dataset_hash": True,
        }

    async def test_dataset_id_mismatch_lists_dimension(
        self, store, evaluation_service
    ):
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0, 1.0]
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[1.0, 1.0],
            dataset_id="ds-2",
        )
        service = EvaluationAnalyticsService(store)
        with pytest.raises(IncomparableExperimentsError) as excinfo:
            await service.compare_experiments("exp-c", "exp-b")
        assert excinfo.value.diff_dimensions == {
            "dataset_id": True,
            "dataset_version": False,
            "dataset_hash": False,
        }

    async def test_definition_key_mismatch_incomparable(
        self, store, evaluation_service
    ):
        """DefinitionKey 集不一致 -> 不可比（正式只读校验入口，无 policy
        同样执行）。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_definition(
            store, "agent.task_success", output_type="boolean"
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0, 1.0],
            metric="rag.faithfulness",
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[True, True],
            metric="agent.task_success",
        )
        service = EvaluationAnalyticsService(store)
        with pytest.raises(IncomparableExperimentsError) as excinfo:
            await service.compare_experiments("exp-c", "exp-b", policy=None)
        assert excinfo.value.diff_dimensions["definition_keys"] is True

    async def test_invalid_policy_rejected(self, store, evaluation_service):
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0, 1.0]
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[1.0, 1.0]
        )
        service = EvaluationAnalyticsService(store)
        # 缺字段 / 非对象 -> gate_policy_from_content 拒绝，统一 422 通道
        with pytest.raises(EvaluationValidationError):
            await service.compare_experiments(
                "exp-c", "exp-b", policy={"schema_version": "1"}
            )
        with pytest.raises(EvaluationValidationError):
            await service.compare_experiments("exp-c", "exp-b", policy="nope")


class TestCompareDescriptive:
    async def test_descriptive_compare_without_policy(
        self, store, evaluation_service
    ):
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c",
            values=[1.0, 0.9, 0.8, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b",
            values=[0.5, 0.5, 0.5, 0.5],
        )
        service = EvaluationAnalyticsService(store)
        result = await service.compare_experiments("exp-c", "exp-b", policy=None)

        assert result["evidence_basis"] == "ledger_reconstruction"
        assert result["denominator_basis"] == "pre_declared"
        assert result["candidate"]["experiment_id"] == "exp-c"
        assert result["candidate"]["dataset_id"] == "ds-1"
        assert result["candidate"]["dataset_hash"] == DATASET_HASH_A
        assert result["baseline"]["experiment_id"] == "exp-b"
        # 描述性输出：无策略指纹与四态结论，不生成 gate_pass/gate_fail
        assert "policy_fingerprint" not in result
        assert "conclusion" not in result
        assert "gate_pass" not in str(result)
        assert "gate_fail" not in str(result)
        assert "delivery_status_counts" in result

        assert len(result["metrics"]) == 1
        row = result["metrics"][0]
        assert row["metric_name"] == "rag.faithfulness"
        assert row["candidate"]["value_count"] == 4
        assert row["candidate"]["macro_value"] == pytest.approx(0.925)
        assert row["candidate"]["missing_count"] == 0
        assert row["candidate"]["error_count"] == 0
        assert row["baseline"]["value_count"] == 4
        assert row["baseline"]["macro_value"] == pytest.approx(0.5)
        assert row["paired_cases"] == 4
        assert row["diff"] == pytest.approx(0.425)
        # 既有置信区间（case 配对 bootstrap 下界）随描述性对比输出
        assert row["paired_ci_lower"] is not None

    async def test_non_finite_number_value_excluded(
        self, store, evaluation_service
    ):
        """含 NaN 值 case 的描述性对比：非有限值不计入统计（与
        EvaluationGate._case_value 的 isfinite 口径一致），diff /
        macro_value 不被 NaN 污染，置信区间不静默降级为 None。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c",
            values=[1.0, 1.0, float("nan")],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b",
            values=[0.5, 0.5, 0.5],
        )
        service = EvaluationAnalyticsService(store)
        result = await service.compare_experiments(
            "exp-c", "exp-b", policy=None
        )
        row = result["metrics"][0]
        # NaN case 被过滤：候选侧只统计 2 个有限值 case
        assert row["candidate"]["value_count"] == 2
        assert row["candidate"]["macro_value"] == pytest.approx(1.0)
        # 基线侧 3 个有限值 case 照常统计
        assert row["baseline"]["value_count"] == 3
        assert row["baseline"]["macro_value"] == pytest.approx(0.5)
        # 配对只在有限值 case 上进行，diff 干净
        assert row["paired_cases"] == 2
        assert row["diff"] == pytest.approx(0.5)
        # 非静默降级：有限配对差上 CI 照常产出（None 只在无配对时出现）
        assert row["paired_ci_lower"] is not None

    async def test_boolean_mapping_and_error_count(
        self, store, evaluation_service
    ):
        """boolean 指标按 1/0 映射参与配对宏平均；error 行不计入值
        统计，单列 error_count（不把执行失败当 0）。"""
        await _seed_definition(
            store, "agent.task_success", output_type="boolean"
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c",
            values=[True, True], metric="agent.task_success",
        )
        await _seed_auto_result(
            store, evaluation_service, "agent.task_success", None,
            status="error", experiment_id="exp-c", seq=2,
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b",
            values=[False, True, True], metric="agent.task_success",
        )
        service = EvaluationAnalyticsService(store)
        result = await service.compare_experiments(
            "exp-c", "exp-b", policy=None
        )
        row = result["metrics"][0]
        assert row["metric_name"] == "agent.task_success"
        # boolean 1/0 映射：候选 (1+1)/2 = 1.0；基线 (0+1+1)/3 = 2/3
        assert row["candidate"]["macro_value"] == pytest.approx(1.0)
        assert row["baseline"]["macro_value"] == pytest.approx(2 / 3)
        # error 行不计入值统计，单列 error_count
        assert row["candidate"]["value_count"] == 2
        assert row["candidate"]["error_count"] == 1
        assert row["baseline"]["error_count"] == 0
        # 配对差按 1/0 映射：case-0000: 1-0=1；case-0001: 1-1=0 -> 0.5
        assert row["paired_cases"] == 2
        assert row["diff"] == pytest.approx(0.5)
        assert row["paired_ci_lower"] is not None


class TestCompareGate:
    async def test_policy_gate_pass_downgraded_inconclusive(
        self, store, evaluation_service
    ):
        """账本重建缺完整预声明证据（仅 case_ids、缺逐指标适用性）：
        即使分母口径标 pre_declared，gate_pass 也必须降为 inconclusive
        （analytics 层新增保护，不改原门禁算法）。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0, 1.0]
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[1.0, 1.0]
        )
        policy = _policy_content()
        service = EvaluationAnalyticsService(store)
        result = await service.compare_experiments(
            "exp-c", "exp-b", policy=policy
        )
        assert result["evidence_basis"] == "ledger_reconstruction"
        assert result["denominator_basis"] == "pre_declared"
        assert result["policy_fingerprint"] == _expected_policy_fingerprint(
            policy
        )
        conclusion = result["conclusion"]
        assert conclusion["policy_version"] == "compare-v1"
        assert conclusion["denominator_basis"] == "pre_declared"
        # 投递状态单列，不改变质量结论
        assert "delivery_status_counts" in conclusion
        assert conclusion["state"] == "inconclusive"
        assert any(
            reason.startswith("ledger_evidence_incomplete")
            for reason in conclusion["reasons"]
        )

    async def test_policy_gate_fail_preserved(self, store, evaluation_service):
        """原 gate_fail 保留：analytics 保护只降 gate_pass，不改门禁失败。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[0.1, 0.1]
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[0.1, 0.1]
        )
        service = EvaluationAnalyticsService(store)
        result = await service.compare_experiments(
            "exp-c", "exp-b", policy=_policy_content()
        )
        assert result["conclusion"]["state"] == "gate_fail"

    async def test_observed_denominator_gate_pass_downgraded(
        self, store, evaluation_service
    ):
        """无 manifest 冻结（denominator_basis=observed）：结论至多
        inconclusive。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0, 1.0],
            manifest_json="",
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[1.0, 1.0],
            manifest_json="",
        )
        service = EvaluationAnalyticsService(store)
        result = await service.compare_experiments(
            "exp-c", "exp-b", policy=_policy_content()
        )
        assert result["denominator_basis"] == "observed"
        assert result["conclusion"]["state"] == "inconclusive"
        assert any(
            reason.startswith("ledger_evidence_incomplete")
            for reason in result["conclusion"]["reasons"]
        )


class TestCompareReadOnly:
    async def test_compare_never_invokes_runner(self, store, evaluation_service):
        """只读断言（spec 4.6）：compare 过程零 execute/replay/judge 调用。"""
        await _seed_definition(
            store, "rag.faithfulness", output_type="number",
            range_or_categories=[0.0, 1.0],
        )
        await _seed_experiment(
            store, evaluation_service, "exp-c", values=[1.0, 1.0]
        )
        await _seed_experiment(
            store, evaluation_service, "exp-b", values=[1.0, 1.0]
        )
        runner = FakeRunner()
        service = EvaluationAnalyticsService(store, runner=runner)
        await service.compare_experiments("exp-c", "exp-b", policy=None)
        await service.compare_experiments(
            "exp-c", "exp-b", policy=_policy_content()
        )
        assert runner.calls == {"execute": 0, "replay": 0, "judge": 0}

