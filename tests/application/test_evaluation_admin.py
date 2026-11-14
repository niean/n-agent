# app.application.evaluation_admin 用例测试（plan-261112 Task 5 S1）。
#
# 覆盖评估器管理用例：
# - capabilities()：能力目录（deterministic 五指标固定组合 / llm_judge 就绪
#   门控 / human online_forbidden），未注册组合不得宣称可执行
# - create_definition：服务端构造 EvaluatorDefinition（白名单、未知参数拒
#   绝、material_hash 服务端生成）、unsupported_capability、幂等与冲突
# - list_definitions：定义 + usage 联合投影、method/target_kind 过滤、
#   cursor 分页、脱敏
# - get_definition_detail：完整契约 + usage + 只读校准摘要（passed/advisory/
#   未校准 None）
# - set_definition_usage：human 禁启用、CAS 经 store、返回新 usage
from __future__ import annotations

import hashlib

import pytest

from app.application.evaluation_admin import EvaluationAdminService
from app.application.evaluation_service import definition_from_mapping
from app.application.evaluation_calibration import calibration_definition_hash
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationNotFoundError,
)
from app.domain.evaluation.models import EvaluationValidationError
from app.infrastructure.evaluation.store import SQLiteEvaluationStore


UTC_TS = "2026-11-12T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    return SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))


@pytest.fixture
def admin(store):
    return EvaluationAdminService(
        store, judge_ready=lambda: False, service="dashboard"
    )


def deterministic_payload(**overrides):
    payload = {
        "definition_id": "kb.recall_at_5",
        "version": "v1",
        "metric_name": "kb.recall_at_5",
        "target_kind": "retrieval",
        "method": "deterministic",
        "output_type": "number",
        "range_or_categories": [0.0, 1.0],
    }
    payload.update(overrides)
    return payload


def judge_payload(**overrides):
    payload = {
        "definition_id": "agent.answer_relevance",
        "version": "v1",
        "metric_name": "agent.answer_relevance",
        "target_kind": "agent_run",
        "method": "llm_judge",
        "output_type": "number",
        "judge_model": "judge-model-a",
        "judge_params": {"temperature": 0},
        "required_fields": ["request", "answer"],
    }
    payload.update(overrides)
    return payload


def human_payload(**overrides):
    payload = {
        "definition_id": "review.manual_score",
        "version": "v1",
        "metric_name": "review.manual_score",
        "target_kind": "agent_run",
        "method": "human",
        "output_type": "number",
        "range_or_categories": [0.0, 5.0],
    }
    payload.update(overrides)
    return payload


async def _enable_usage(store, definition_id, version):
    """显式启用 usage（注册已写 revision=1 的 disabled 行，CAS 用当前 revision）。"""
    usage = await store.get_definition_usage(definition_id, version)
    return await store.upsert_definition_usage(
        definition_id,
        version,
        online_enabled=True,
        expected_revision=usage["revision"] if usage else 0,
        updated_by="test",
    )


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_deterministic_entries_fixed_combos(self, admin):
        catalog = admin.capabilities()
        deterministic = [
            e for e in catalog["entries"] if e["method"] == "deterministic"
        ]
        got = {
            (e["metric_name"], e["target_kind"], e["output_type"])
            for e in deterministic
        }
        # metrics_deterministic 六指标的目标适配（kb.* -> retrieval /
        # agent.* -> agent_run），与 worker _DETERMINISTIC_HANDLERS 一致
        # （plan T3 新增 agent.scenario_assertions）
        assert got == {
            ("kb.recall_at_5", "retrieval", "number"),
            ("kb.ndcg_at_5", "retrieval", "number"),
            ("kb.mrr_at_5", "retrieval", "number"),
            ("agent.task_success", "agent_run", "boolean"),
            ("agent.tool_contract", "agent_run", "boolean"),
            ("agent.scenario_assertions", "agent_run", "boolean"),
        }
        assert all(
            e["executable"] is True and e["judge_required"] is False
            for e in deterministic
        )

    def test_judge_entries_gated_on_judge_ready(self, store):
        admin_off = EvaluationAdminService(
            store, judge_ready=lambda: False, service="dashboard"
        )
        catalog = admin_off.capabilities()
        judge = [e for e in catalog["entries"] if e["method"] == "llm_judge"]
        assert judge
        assert all(e["judge_required"] is True for e in judge)
        assert all(e["executable"] is False for e in judge)
        assert catalog["judge_configured"] is False

        admin_on = EvaluationAdminService(
            store, judge_ready=lambda: True, service="dashboard"
        )
        catalog_on = admin_on.capabilities()
        judge_on = [
            e for e in catalog_on["entries"] if e["method"] == "llm_judge"
        ]
        assert all(e["executable"] is True for e in judge_on)
        assert catalog_on["judge_configured"] is True

    def test_human_entries_forbidden_online(self, admin):
        catalog = admin.capabilities()
        human = [e for e in catalog["entries"] if e["method"] == "human"]
        assert human
        assert all(e["online_forbidden"] is True for e in human)
        assert all(e["executable"] is False for e in human)
        # human 支持三种 output_type
        assert {e["output_type"] for e in human} == {
            "number",
            "boolean",
            "categorical",
        }

    def test_no_unregistered_combo_claimed_executable(self, admin):
        catalog = admin.capabilities()
        executable = {
            (e["metric_name"], e["method"], e["target_kind"])
            for e in catalog["entries"]
            if e["executable"]
        }
        # judge_ready=False：可执行组合只有 deterministic 六指标
        assert executable == {
            ("kb.recall_at_5", "deterministic", "retrieval"),
            ("kb.ndcg_at_5", "deterministic", "retrieval"),
            ("kb.mrr_at_5", "deterministic", "retrieval"),
            ("agent.task_success", "deterministic", "agent_run"),
            ("agent.tool_contract", "deterministic", "agent_run"),
            ("agent.scenario_assertions", "deterministic", "agent_run"),
        }


# ---------------------------------------------------------------------------
# create_definition
# ---------------------------------------------------------------------------


class TestCreateDefinition:
    async def test_creates_definition_with_disabled_usage(self, admin, store):
        outcome = await admin.create_definition(deterministic_payload())

        definition = outcome["definition"]
        assert definition["definition_id"] == "kb.recall_at_5"
        assert definition["version"] == "v1"
        assert definition["method"] == "deterministic"
        assert definition["target_kind"] == "retrieval"
        assert definition["output_type"] == "number"
        # 服务端生成 material_hash（deterministic 无外部材料 -> 空）
        assert definition["material_hash"] == {}

        usage = outcome["usage"]
        assert usage["definition_id"] == "kb.recall_at_5"
        assert usage["definition_version"] == "v1"
        assert usage["online_enabled"] is False
        assert usage["revision"] == 1
        assert usage["updated_by"] == "dashboard"
        stored = await store.get_definition_usage("kb.recall_at_5", "v1")
        assert stored is not None
        assert stored["online_enabled"] is False

    async def test_unknown_params_rejected(self, admin):
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                deterministic_payload(code="print('hi')")
            )

    async def test_material_hash_server_side_for_judge_rubric(self, admin):
        rubric = "回答必须直接解决请求"
        outcome = await admin.create_definition(
            judge_payload(judge_params={"temperature": 0, "rubric": rubric})
        )
        expected = hashlib.sha256(rubric.encode("utf-8")).hexdigest()
        assert outcome["definition"]["material_hash"] == {"rubric": expected}
        # 客户端伪造 material_hash 属未知参数
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(material_hash={"rubric": "f" * 64})
            )

    async def test_deterministic_only_builtin_combos(self, admin):
        # 组合不在能力目录：metric 不认识
        with pytest.raises(EvaluationValidationError) as exc:
            await admin.create_definition(
                deterministic_payload(
                    definition_id="custom.metric",
                    version="v1",
                    metric_name="custom.metric",
                )
            )
        assert getattr(exc.value, "code", None) == "unsupported_capability"
        # 组合不在能力目录：目标错配
        with pytest.raises(EvaluationValidationError) as exc:
            await admin.create_definition(
                deterministic_payload(target_kind="agent_run")
            )
        assert getattr(exc.value, "code", None) == "unsupported_capability"
        # deterministic 不接收 judge 材料 / 额外 required_fields
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                deterministic_payload(judge_model="judge-model-a")
            )
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                deterministic_payload(required_fields=["query"])
            )

    async def test_llm_judge_validations(self, admin):
        # 缺 judge_model（domain 不变量）
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(judge_payload(judge_model=None))
        # 未知 judge 参数拒绝
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(judge_params={"temperature": 0, "foo": 1})
            )
        # temperature 越界
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(judge_params={"temperature": 3})
            )
        # 组合不在能力目录（metric 不认识 / 目标错配）
        with pytest.raises(EvaluationValidationError) as exc:
            await admin.create_definition(
                judge_payload(
                    definition_id="custom.judge",
                    metric_name="custom.judge",
                )
            )
        assert getattr(exc.value, "code", None) == "unsupported_capability"
        with pytest.raises(EvaluationValidationError) as exc:
            await admin.create_definition(
                judge_payload(
                    definition_id="kb.context_relevance",
                    metric_name="kb.context_relevance",
                    target_kind="agent_run",
                )
            )
        assert getattr(exc.value, "code", None) == "unsupported_capability"
        # 合法创建
        outcome = await admin.create_definition(judge_payload())
        assert outcome["definition"]["judge_model"] == "judge-model-a"

    async def test_judge_params_boundaries(self, admin):
        # temperature=2 为合法上界
        outcome = await admin.create_definition(
            judge_payload(version="v-t2", judge_params={"temperature": 2})
        )
        assert outcome["definition"]["judge_params"] == {"temperature": 2}
        # temperature=True 拒绝（bool 不是数值）
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(judge_params={"temperature": True})
            )
        # rubric 空白串拒绝
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(judge_params={"rubric": "  "})
            )
        # self_judge 非布尔拒绝
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(judge_params={"self_judge": "yes"})
            )

    async def test_human_evidence_policy_strict(self, admin):
        """human evidence_policy：缺省 actor_submission；显式传入必须为
        非空字符串，非字符串与空串/空白串显式拒绝（不静默强转或映射默认）。"""
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(human_payload(evidence_policy=123))
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(human_payload(evidence_policy=""))
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(human_payload(evidence_policy="  "))
        outcome = await admin.create_definition(
            human_payload(
                version="v-ep",
                evidence_policy="actor_submission",
                range_or_categories=[0.0, 5.0],
            )
        )
        assert outcome["definition"]["evidence_policy"] == "actor_submission"

    async def test_human_allows_three_output_types(self, admin):
        for output_type, extra in (
            ("number", {"range_or_categories": [0.0, 5.0]}),
            ("boolean", {"range_or_categories": None}),
            ("categorical", {"range_or_categories": ["good", "bad"]}),
        ):
            outcome = await admin.create_definition(
                human_payload(
                    version=f"v-{output_type}",
                    output_type=output_type,
                    **extra,
                )
            )
            assert outcome["definition"]["output_type"] == output_type
        # human 不接收 judge 材料
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                human_payload(judge_params={"temperature": 0})
            )

    async def test_ci_admissible_registered_as_declared(self, admin):
        outcome = await admin.create_definition(
            deterministic_payload(ci_admissible=True)
        )
        assert outcome["definition"]["ci_admissible"] is True

    async def test_idempotent_same_content_returns_existing_usage(self, admin, store):
        first = await admin.create_definition(deterministic_payload())
        # 用户显式启用后重放注册：停用状态不被复活
        await _enable_usage(store, "kb.recall_at_5", "v1")
        replay = await admin.create_definition(deterministic_payload())
        assert replay["definition"]["definition_id"] == "kb.recall_at_5"
        assert replay["definition"]["version"] == "v1"
        assert replay["usage"]["online_enabled"] is True
        assert replay["usage"]["revision"] == 2
        assert first["definition"]["timeout_seconds"]

    async def test_same_version_different_content_conflicts(self, admin):
        await admin.create_definition(deterministic_payload())
        with pytest.raises(EvaluationConflictError):
            await admin.create_definition(
                deterministic_payload(timeout_seconds=99)
            )

    async def test_fixed_branches_reject_ignored_fields(self, admin):
        """deterministic/llm_judge 的 evidence_policy/range_or_categories 为
        服务端固定值：非空传入显式拒绝，不静默忽略（白名单允许仅为 human
        传参）；显式 None 与"未传"等价，仍可创建。"""
        # deterministic：evidence_policy 固定 snapshot
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                deterministic_payload(evidence_policy="actor_submission")
            )
        # llm_judge：evidence_policy 固定 snapshot、range_or_categories 固定 None
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(evidence_policy="actor_submission")
            )
        with pytest.raises(EvaluationValidationError):
            await admin.create_definition(
                judge_payload(range_or_categories=[0.0, 1.0])
            )
        # 显式 None 仍允许
        outcome = await admin.create_definition(
            judge_payload(evidence_policy=None, range_or_categories=None)
        )
        assert outcome["definition"]["evidence_policy"] == "snapshot"
        assert outcome["definition"]["range_or_categories"] is None


# ---------------------------------------------------------------------------
# list_definitions
# ---------------------------------------------------------------------------


class TestListDefinitions:
    async def test_filters_method_and_kind_with_usage_projection(self, admin):
        await admin.create_definition(deterministic_payload())
        await admin.create_definition(
            judge_payload(
                definition_id="rag.faithfulness",
                metric_name="rag.faithfulness",
            )
        )
        await admin.create_definition(human_payload())

        page = await admin.list_definitions()
        assert len(page["items"]) == 3
        assert page["next_cursor"] is None
        by_id = {i["definition_id"]: i for i in page["items"]}
        # 定义 + usage 联合投影
        assert by_id["kb.recall_at_5"]["usage"]["online_enabled"] is False
        assert by_id["kb.recall_at_5"]["method"] == "deterministic"

        retrieval = await admin.list_definitions(target_kind="retrieval")
        assert {i["definition_id"] for i in retrieval["items"]} == {
            "kb.recall_at_5"
        }
        judged = await admin.list_definitions(method="llm_judge")
        assert {i["definition_id"] for i in judged["items"]} == {
            "rag.faithfulness"
        }

    async def test_invalid_cursor_rejected(self, admin):
        with pytest.raises(EvaluationValidationError):
            await admin.list_definitions(cursor="@@@")

    async def test_cursor_pagination(self, admin):
        for idx, metric in enumerate(
            ("kb.recall_at_5", "kb.ndcg_at_5", "kb.mrr_at_5")
        ):
            await admin.create_definition(
                deterministic_payload(
                    definition_id=metric,
                    version="v1",
                    metric_name=metric,
                )
            )
        first = await admin.list_definitions(limit=2)
        assert len(first["items"]) == 2
        assert first["next_cursor"] is not None
        second = await admin.list_definitions(limit=2, cursor=first["next_cursor"])
        assert len(second["items"]) == 1
        assert second["next_cursor"] is None
        all_ids = [i["definition_id"] for i in first["items"]] + [
            i["definition_id"] for i in second["items"]
        ]
        assert all_ids == ["kb.mrr_at_5", "kb.ndcg_at_5", "kb.recall_at_5"]

    async def test_projection_is_sanitized(self, admin):
        await admin.create_definition(deterministic_payload())
        page = await admin.list_definitions()
        item = page["items"][0]
        allowed_keys = {
            "definition_id",
            "version",
            "metric_name",
            "target_kind",
            "method",
            "usage",
        }
        assert set(item.keys()) <= allowed_keys
        assert set(item["usage"].keys()) == {
            "online_enabled",
            "revision",
            "updated_at",
            "updated_by",
        }


# ---------------------------------------------------------------------------
# get_definition_detail
# ---------------------------------------------------------------------------


class TestGetDefinitionDetail:
    async def test_detail_contract_usage_calibration_none(self, admin):
        await admin.create_definition(judge_payload())
        detail = await admin.get_definition_detail(
            "agent.answer_relevance", "v1"
        )
        definition = detail["definition"]
        assert definition["judge_model"] == "judge-model-a"
        assert definition["judge_params"] == {"temperature": 0}
        assert definition["required_fields"] == ["request", "answer"]
        assert detail["usage"]["online_enabled"] is False
        # 未校准：calibration 为 None
        assert detail["calibration"] is None

    async def test_detail_calibration_passed(self, admin, store):
        await admin.create_definition(judge_payload())
        row = await store.get_definition("agent.answer_relevance", "v1")
        definition = definition_from_mapping(row["definition"])
        definition_hash = calibration_definition_hash(definition)
        await store.save_calibration_report(
            {
                "cache_key": "ck-1",
                "metric_name": "agent.answer_relevance",
                "definition_id": "agent.answer_relevance",
                "definition_hash": definition_hash,
                "status": "passed",
                "failures": [],
                "calibration_set_id": "cal-1",
                "calibration_set_hash": "c" * 64,
                "regression_set_id": "reg-1",
                "regression_set_hash": "d" * 64,
                "sample_count": 8,
                "consistency": {},
                "repeatability": {},
                "repeatability_samples": 2,
                "repeatability_runs": 2,
                "created_at": UTC_TS,
            }
        )
        detail = await admin.get_definition_detail(
            "agent.answer_relevance", "v1"
        )
        calibration = detail["calibration"]
        assert calibration is not None
        assert calibration["effective_state"] == "passed"
        assert calibration["status"] == "passed"
        assert calibration["definition_hash"] == definition_hash
        assert calibration["calibration_set_hash"] == "c" * 64
        assert calibration["regression_set_hash"] == "d" * 64
        assert calibration["created_at"] == UTC_TS
        assert "message" not in calibration

    async def test_detail_calibration_advisory_shows_message(self, admin, store):
        await admin.create_definition(judge_payload())
        row = await store.get_definition("agent.answer_relevance", "v1")
        definition = definition_from_mapping(row["definition"])
        definition_hash = calibration_definition_hash(definition)
        await store.save_calibration_report(
            {
                "cache_key": "ck-2",
                "metric_name": "agent.answer_relevance",
                "definition_id": "agent.answer_relevance",
                "definition_hash": definition_hash,
                "status": "advisory",
                "failures": ["consistency_below_threshold"],
                "calibration_set_id": "cal-1",
                "calibration_set_hash": "c" * 64,
                "regression_set_id": "reg-1",
                "regression_set_hash": "d" * 64,
                "sample_count": 8,
                "consistency": {},
                "repeatability": {},
                "repeatability_samples": 2,
                "repeatability_runs": 2,
                "created_at": UTC_TS,
            }
        )
        detail = await admin.get_definition_detail(
            "agent.answer_relevance", "v1"
        )
        calibration = detail["calibration"]
        assert calibration["effective_state"] == "advisory"
        assert calibration["status"] == "advisory"
        assert "未校准" in calibration["message"]
        assert "材料已变更" in calibration["message"]
        assert calibration["failures"] == ["consistency_below_threshold"]

    async def test_detail_calibration_miss_after_hash_change(self, admin, store):
        """definition hash 变化（rubric/模型等材料变更）后旧报告按缓存键
        失效：未命中返回 calibration None（不显示"已校准"）。"""
        await admin.create_definition(judge_payload())
        await store.save_calibration_report(
            {
                "cache_key": "ck-3",
                "metric_name": "agent.answer_relevance",
                "definition_id": "agent.answer_relevance",
                "definition_hash": "a" * 64,  # 与当前定义 hash 不一致
                "status": "passed",
                "failures": [],
                "calibration_set_id": "cal-1",
                "calibration_set_hash": "c" * 64,
                "regression_set_id": "reg-1",
                "regression_set_hash": "d" * 64,
                "sample_count": 8,
                "consistency": {},
                "repeatability": {},
                "repeatability_samples": 2,
                "repeatability_runs": 2,
                "created_at": UTC_TS,
            }
        )
        detail = await admin.get_definition_detail(
            "agent.answer_relevance", "v1"
        )
        # hash 不匹配 -> 报告未命中（视为未校准）
        assert detail["calibration"] is None

    async def test_detail_not_found(self, admin):
        with pytest.raises(EvaluationNotFoundError):
            await admin.get_definition_detail("nope.metric", "v1")


# ---------------------------------------------------------------------------
# set_definition_usage
# ---------------------------------------------------------------------------


class TestSetDefinitionUsage:
    async def test_enable_then_disable_with_revision_cas(self, admin, store):
        await admin.create_definition(deterministic_payload())
        # 注册已写 revision=1 的 disabled usage 行：启停 CAS 从 1 起
        usage = await admin.set_definition_usage(
            "kb.recall_at_5", "v1", online_enabled=True, expected_revision=1
        )
        assert usage["online_enabled"] is True
        assert usage["revision"] == 2
        usage = await admin.set_definition_usage(
            "kb.recall_at_5", "v1", online_enabled=False, expected_revision=2
        )
        assert usage["online_enabled"] is False
        assert usage["revision"] == 3
        stored = await store.get_definition_usage("kb.recall_at_5", "v1")
        assert stored["online_enabled"] is False

    async def test_human_enable_rejected(self, admin):
        await admin.create_definition(human_payload())
        with pytest.raises(EvaluationValidationError):
            await admin.set_definition_usage(
                "review.manual_score", "v1",
                online_enabled=True, expected_revision=0,
            )

    async def test_stale_revision_conflicts(self, admin):
        await admin.create_definition(deterministic_payload())
        with pytest.raises(EvaluationConflictError):
            await admin.set_definition_usage(
                "kb.recall_at_5", "v1",
                online_enabled=True, expected_revision=5,
            )

    async def test_unknown_definition_not_found(self, admin):
        with pytest.raises(EvaluationNotFoundError):
            await admin.set_definition_usage(
                "ghost.metric", "v1",
                online_enabled=True, expected_revision=0,
            )
