# app.application.evaluation_experiment 用例测试（plan T13，spec 9.1 / 11.1 / 11.2）。
#
# TDD 严格 RED -> GREEN；本文件覆盖 plan T13 S1 全部要点：
# - execute：逐 case 冻结配置调用目标（经注入的 EvaluationTargetRunner 端口，
#   N-Agent/N-KB 适配属 Infrastructure target_runner，本文件全部用 fake）；
#   每 case 独立会话与隔离 namespace；多轮 case 共享会话；replicate 独立执行
#   （默认 1，显式 3）；实验项 ID = case_id + replicate；所有 attempt 记录保留；
#   业务 error 计 task_success 失败分母，Judge error 单列。
# - replay：仅对现有 EvidenceSnapshot 重新评分；绝不调用业务目标；生成新评分
#   实验项并 Link 原目标；名称标 replay。
# - 冻结快照：代码 commit、目标模型请求/响应版本、system prompt hash、
#   工具/Skill/MCP 清单 hash、KB/corpus 配置、evaluator 版本集、Judge 配置、
#   dataset hash、seed 与重复次数；无法冻结项标 external_unpinned。
# - 副作用：清理失败独立失败上报，不影响已完成评分。
# - 失败重试边界：质量门禁按首次业务执行；无副作用传输失败才可重试。
# - 实验项根采样（spec 11.1）：OTel 禁用时不采样、实验分析标 unavailable
#   （infrastructure 受信 sampler 本体见 tests/infrastructure/evaluation/
#   test_target_runner.py）。
# - Application 层纯净：本模块禁止 import app.infrastructure 与 httpx
#   （N-KB 直调适配属 Infrastructure，AST 静态断言锁定）。
from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.application.evaluation_dataset import qrels_identity
from app.application.evaluation_experiment import (
    AttemptRecord,
    ExecuteExperimentRequest,
    ExperimentRootHandle,
    ReplayExperimentRequest,
    summarize_quality,
    EvaluationExperimentRunner,
)
from app.application.evaluation_service import EvaluationService
from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import (
    DatasetCase,
    DatasetManifest,
    EvaluatorDefinition,
    EvaluationValidationError,
    EXTERNAL_UNPINNED,
    FROZEN_ENVIRONMENT_FIELDS,
)
from app.domain.evaluation.state import SkipReason
from app.infrastructure.evaluation.store import SQLiteEvaluationStore


REPO_ROOT = Path(__file__).resolve().parents[2]

HEX32 = "a" * 32
HEX16 = "b" * 16


# ---------------------------------------------------------------------------
# 测试材料
# ---------------------------------------------------------------------------


def _definition(
    metric_name: str,
    target_kind: str,
    *,
    method: str = "deterministic",
    judge_model: str | None = None,
) -> EvaluatorDefinition:
    is_judge = method == "llm_judge"
    return EvaluatorDefinition(
        definition_id=metric_name,
        version="v1",
        metric_name=metric_name,
        target_kind=target_kind,
        method=method,
        required_fields=(),
        evidence_policy="snapshot_complete",
        output_type="number" if is_judge else "boolean",
        range_or_categories=(0.0, 1.0) if is_judge else (),
        direction="higher_is_better",
        material_hash={},
        judge_model=judge_model,
        judge_params={"temperature": 0} if is_judge else {},
        timeout_seconds=30,
        budget=None,
        ci_admissible=True,
    )


def _agent_case(case_id: str = "case-1", *, multi_turn: bool = False) -> DatasetCase:
    case_input = (
        {"turns": [{"message": "turn-1"}, {"message": "turn-2"}]}
        if multi_turn
        else {"message": "hello"}
    )
    return DatasetCase(
        case_id=case_id,
        input=case_input,
        target_kind="agent_run",
        expected_output="ok",
    )


def _kb_case(case_id: str = "kb-1", *, no_answer: bool = False) -> DatasetCase:
    grade = 0 if no_answer else 2
    qrels = {
        qrels_identity("corp-1", "doc-1", "chunk-1"): grade,
        qrels_identity("corp-1", "doc-2", "chunk-2"): 0 if no_answer else 1,
    }
    return DatasetCase(
        case_id=case_id,
        input={"query": f"query-{case_id}"},
        target_kind="retrieval",
        qrels=qrels,
        corpus_revision="corp-1",
        no_answer=no_answer,
        metric_applicability=(
            {"kb.recall_at_5": False, "kb.no_answer_hit": True}
            if no_answer
            else None
        ),
    )


def _manifest(*cases: DatasetCase, target_kind: str = "agent_run") -> DatasetManifest:
    return DatasetManifest(
        schema_version="1",
        dataset_id="ds-1",
        dataset_version="v1",
        dataset_hash="c" * 64,
        target_kind=target_kind,
        cases=cases,
    )


def _agent_output() -> dict:
    return {
        "completion_reported": True,
        "final_answer": "answer",
        "tool_calls": [],
        "side_effect_evidence": None,
        "allowed_tools": [],
    }


def _kb_results() -> list[dict]:
    return [
        {
            "corpus_revision": "corp-1",
            "document_revision": "doc-1",
            "chunk_id": "chunk-1",
            "rank": 1,
        },
        {
            "corpus_revision": "corp-1",
            "document_revision": "doc-2",
            "chunk_id": "chunk-2",
            "rank": 2,
        },
    ]


def _retrieval_entry() -> dict:
    return {
        "retrieval_id": "ret-1",
        "trace_id": HEX32,
        "span_id": HEX16,
        "corpus_revision": "corp-1",
        "results": _kb_results(),
    }


class FakeTargetRunner:
    """按序弹出预置结果的目标执行 fake；记录全部调用。"""

    def __init__(self, results: list[dict] | None = None) -> None:
        self._results = list(results or [])
        self.calls: list[tuple[object, dict]] = []

    async def run_target(self, target, request: dict) -> dict:
        self.calls.append((target, request))
        if self._results:
            return self._results.pop(0)
        return {
            "status": "succeeded",
            "output": dict(_agent_output()),
            "side_effects": [],
            "retrievals": [],
            "run_id": "run-1",
        }

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeEvidence:
    """内存证据存储 fake。"""

    def __init__(self) -> None:
        self.saved: list[tuple[str, object]] = []

    async def save_snapshot(self, target, snapshot):
        ref = f"ev-{len(self.saved)}"
        self.saved.append((ref, snapshot))
        return ref

    async def load_snapshot(self, snapshot_ref: str):
        for ref, snapshot in self.saved:
            if ref == snapshot_ref:
                return snapshot
        raise EvaluationNotFoundError(f"snapshot not found: {snapshot_ref}")

    async def has_snapshot(self, snapshot_ref: str) -> bool:
        return any(ref == snapshot_ref for ref, _ in self.saved)


class FakeRootScopeFactory:
    """实验项根 scope fake：记录 identity，返回可配置有效性的句柄。"""

    def __init__(self, *, valid: bool = True) -> None:
        self.identities: list[dict] = []
        self._valid = valid

    def __call__(self, identity: dict):
        return self._scope(identity)

    @contextmanager
    def _scope(self, identity: dict):
        self.identities.append(dict(identity))
        if self._valid:
            yield ExperimentRootHandle(
                trace_id=HEX32, span_id=HEX16, is_valid=True
            )
        else:
            yield ExperimentRootHandle(trace_id=None, span_id=None, is_valid=False)


class FakeCleanup:
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[dict] = []
        self._error = error

    async def __call__(self, manifest: dict) -> dict:
        self.requests.append(dict(manifest))
        if self._error is not None:
            raise self._error
        return {"status": "ok"}


def _runner(
    store: SQLiteEvaluationStore,
    target_runner: FakeTargetRunner,
    *,
    evidence: FakeEvidence | None = None,
    scope: FakeRootScopeFactory | None = None,
    fingerprint=None,
    cleanup=None,
    service: EvaluationService | None = None,
) -> EvaluationExperimentRunner:
    return EvaluationExperimentRunner(
        service=service or EvaluationService(store, enabled=True),
        ledger=store,
        target_runner=target_runner,
        evidence=evidence or FakeEvidence(),
        experiment_root_scope=scope or FakeRootScopeFactory(),
        environment_fingerprint=fingerprint,
        cleanup_executor=cleanup,
        clock=lambda: datetime(2026, 11, 11, tzinfo=timezone.utc),
    )


async def _definitions(
    store: SQLiteEvaluationStore, *defs: EvaluatorDefinition
) -> None:
    service = EvaluationService(store, enabled=True)
    for definition in defs:
        await service.register_definition(definition)


# ---------------------------------------------------------------------------
# execute：冻结配置逐 case 调用 / 会话隔离 / replicate / item ID
# ---------------------------------------------------------------------------


async def test_execute_invokes_frozen_config_per_case_replicate(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner()
    runner = _runner(store, target)
    dataset = _manifest(_agent_case("case-1"), _agent_case("case-2"))
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-a",
            target_config={"model": "gpt-x", "temperature": 0},
            evaluator_set=(("agent.task_success", "v1"),),
            replicates=3,
            seed="seed-42",
        )
    )
    # 2 case x 3 replicate = 6 次目标调用，每次携带同一冻结目标配置
    assert len(report.items) == 6
    assert {item.item_id for item in report.items} == {
        "case-1#r0", "case-1#r1", "case-1#r2",
        "case-2#r0", "case-2#r1", "case-2#r2",
    }
    frozen = {"model": "gpt-x", "temperature": 0}
    assert target_frozen_configs(target.calls) == [frozen] * 6


def target_frozen_configs(calls) -> list[dict]:
    return [req["target_config"] for _, req in calls]


async def test_execute_isolates_session_per_case_and_replicate_shares_multi_turn(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner()
    runner = _runner(store, target)
    dataset = _manifest(_agent_case("case-1"), _agent_case("case-2", multi_turn=True))
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-a",
            target_config={"model": "gpt-x"},
            evaluator_set=(("agent.task_success", "v1"),),
            replicates=2,
        )
    )
    # 多轮 case 在同一请求内共享会话：每次 run_target 调用携带全部 turns
    multi_turn_requests = [
        req for _, req in target.calls
        if len(req["turns"]) == 2
    ]
    assert len(multi_turn_requests) == 2  # 2 个 replicate，各一次调用
    for req in multi_turn_requests:
        assert req["turns"][0]["message"] == "turn-1"
        assert req["turns"][1]["message"] == "turn-2"
    # 每 case 独立 namespace；每 replicate 独立 session
    namespaces = {req["session"]["namespace"] for _, req in target.calls}
    assert len(namespaces) == 2  # 两个 case 两个隔离 namespace
    for _, req in target.calls:
        assert req["session"]["namespace"] in req["session"]["session_id"]
    sessions = {req["session"]["session_id"] for _, req in target.calls}
    # 2 case x 2 replicate（多轮 case 同 replicate 共享一个 session）
    assert len(sessions) == 4


async def test_execute_default_single_replicate_explicit_three(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    dataset = _manifest(_agent_case("case-1"))
    default_target = FakeTargetRunner()
    await _runner(store, default_target).execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    assert default_target.call_count == 1  # 默认单次执行

    explicit_target = FakeTargetRunner()
    await _runner(store, explicit_target).execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-b",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
            replicates=3,
        )
    )
    assert explicit_target.call_count == 3  # 显式重复实验


async def test_item_id_composes_case_id_and_replicate(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    scope = FakeRootScopeFactory()
    runner = _runner(store, FakeTargetRunner(), scope=scope)
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-9")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
            replicates=2,
        )
    )
    assert [identity["item_id"] for identity in scope.identities] == [
        "case-9#r0",
        "case-9#r1",
    ]


# ---------------------------------------------------------------------------
# 失败重试边界：仅无业务副作用的传输失败允许重试；attempt 全保留
# ---------------------------------------------------------------------------


async def test_all_attempts_retained_and_transport_retry_succeeds(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "transport_error",
                "error_type": "network",
                "output": {},
                "side_effects": [],
                "retrievals": [],
            },
            {
                "status": "succeeded",
                "output": dict(_agent_output()),
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-2",
            },
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-1")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    assert target.call_count == 2  # 传输失败安全重试了一次
    item = report.items[0]
    assert item.outcome == "scored"
    assert [a.status for a in item.attempts] == ["transport_error", "succeeded"]
    assert item.attempts[0].error_type == "network"
    assert item.attempts[0].attempt_index == 0
    assert item.attempts[1].attempt_index == 1


async def test_transport_retry_blocked_when_side_effects_unobserved(tmp_path):
    """#16：side_effects_unobserved（HTTP 传输失败、副作用未采集）不满足
    无副作用证据前提——不自动重跑整个 case。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "transport_error",
                "error_type": "timeout",
                "output": {},
                "side_effects": [],
                "side_effects_unobserved": True,
                "retrievals": [],
            },
            {
                "status": "succeeded",
                "output": dict(_agent_output()),
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-ok",
            },
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-u")),
            name="exp-u",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    item = report.items[0]
    # 单 attempt：未观测副作用不自动重试（重试成功结果也不得出现）
    assert [a.status for a in item.attempts] == ["transport_error"]
    assert item.outcome == "transport_exhausted"


async def test_transport_retry_blocked_after_side_effects(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "transport_error",
                "error_type": "timeout",
                "output": {},
                "side_effects": ["artifact.created"],  # 已产生业务副作用
                "retrievals": [],
            },
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-1")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    assert target.call_count == 1  # 有副作用的传输失败禁止重试
    item = report.items[0]
    assert item.outcome == "transport_exhausted"
    assert len(item.attempts) == 1
    assert item.attempts[0].side_effects == ("artifact.created",)


async def test_business_error_not_retried(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "business_error",
                "error_type": "agent_run_failed",
                "output": {},
                "side_effects": [],
                "retrievals": [],
            },
            {
                "status": "succeeded",
                "output": dict(_agent_output()),
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-x",
            },
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-1")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    # 业务失败不是可重试传输失败：第二次预置结果不被消费（禁挑好的）
    assert target.call_count == 1
    assert report.items[0].outcome == "business_error"
    assert report.items[0].attempts[0].status == "business_error"


async def test_unknown_target_status_fails_fast(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "weird_status",  # 契约外结局词汇
                "output": {},
                "side_effects": [],
                "retrievals": [],
            },
        ]
    )
    runner = _runner(store, target)
    with pytest.raises(EvaluationValidationError):
        await runner.execute(
            ExecuteExperimentRequest(
                dataset=_manifest(_agent_case("case-1")),
                name="exp-a",
                target_config={"model": "m"},
                evaluator_set=(("agent.task_success", "v1"),),
            )
        )
    # 未知结局不静默归入 transport_exhausted，也不触发重试
    assert target.call_count == 1


async def test_snapshot_oversized_skips_with_evidence_oversized(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": {
                    **_agent_output(),
                    "results": [{"blob": "x" * (2 * 1024 * 1024)}],
                },
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-big",
            },
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-1")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    item = report.items[0]
    assert item.outcome == "skipped"
    assert item.skip_reason == SkipReason.evidence_oversized.value
    assert item.sample_ids == ()  # 不伪造 0 分，无评分样本


async def test_snapshot_unserializable_output_skips_with_evidence_missing(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": {
                    **_agent_output(),
                    "tool_calls": [{"sdk_object": object()}],  # 不可 JSON 序列化
                },
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-bad",
            },
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-1")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    item = report.items[0]
    assert item.outcome == "skipped"
    assert item.skip_reason == SkipReason.evidence_missing.value
    assert item.sample_ids == ()


# ---------------------------------------------------------------------------
# 质量口径：业务 error 计 task_success 失败分母；Judge error 单列
# ---------------------------------------------------------------------------


async def test_business_error_counts_into_task_success_denominator(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "business_error",
                "error_type": "agent_run_failed",
                "output": {},
                "side_effects": [],
                "retrievals": [],
            },
            {
                "status": "succeeded",
                "output": dict(_agent_output()),
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-ok",
            },
        ]
    )
    runner = _runner(store, target)
    dataset = _manifest(_agent_case("case-err"), _agent_case("case-ok"))
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    summaries = summarize_quality(report, dataset)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.metric_name == "agent.task_success"
    # 分母 = 声明适用的全部 case（业务 error 不剔除）；业务 error 计失败
    assert summary.denominator == 2
    assert summary.business_failures == 1
    assert summary.evaluated == 1


async def test_judge_errors_listed_separately(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(
        store,
        _definition("agent.task_success", "experiment_item"),
        _definition(
            "agent.answer_correctness",
            "experiment_item",
            method="llm_judge",
            judge_model="judge-x",
        ),
    )
    runner = _runner(store, FakeTargetRunner())
    dataset = _manifest(_agent_case("case-1"), _agent_case("case-2"))
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(
                ("agent.task_success", "v1"),
                ("agent.answer_correctness", "v1"),
            ),
        )
    )
    item_ids = [item.item_id for item in report.items]
    summaries = summarize_quality(
        report,
        dataset,
        evaluation_outcomes={
            item_ids[0]: {"agent.answer_correctness": "judge_error"},
        },
    )
    task_success = next(s for s in summaries if s.metric_name == "agent.task_success")
    assert task_success.judge_errors == 0
    correctness = next(
        s for s in summaries if s.metric_name == "agent.answer_correctness"
    )
    # Judge error 单列：不混入业务失败，也不从分母剔除
    assert correctness.denominator == 2
    assert correctness.judge_errors == 1
    assert correctness.business_failures == 0


async def test_no_answer_case_metric_accounting(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(
        store,
        _definition("kb.recall_at_5", "retrieval"),
        _definition("kb.no_answer_hit", "retrieval"),
    )
    runner = _runner(store, FakeTargetRunner())
    dataset = _manifest(
        _kb_case("kb-1"),
        _kb_case("kb-empty", no_answer=True),
        target_kind="retrieval",
    )
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-kb",
            target_config={"top_k": 5},
            evaluator_set=(
                ("kb.recall_at_5", "v1"),
                ("kb.no_answer_hit", "v1"),
            ),
        )
    )
    summaries = summarize_quality(report, dataset)
    recall = next(s for s in summaries if s.metric_name == "kb.recall_at_5")
    assert recall.denominator == 1  # no_answer case 预声明不适用，不入分母
    no_answer = next(s for s in summaries if s.metric_name == "kb.no_answer_hit")
    assert no_answer.denominator == 2  # no_answer 指标必须覆盖无答案 query
    assert report.items[1].outcome == "scored"


# ---------------------------------------------------------------------------
# replay：仅重评现有证据；不调用业务目标；Link 原目标；名称标 replay
# ---------------------------------------------------------------------------


async def _seed_source_sample(tmp_path):
    """先用 execute 产生一个带证据的源 sample，供 replay 重评。

    返回 evidence 实例：replay 必须复用同一证据存储（生产环境为同一
    locals/evaluations/ 证据库）。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    evidence = FakeEvidence()
    runner = _runner(store, FakeTargetRunner(), evidence=evidence)
    dataset = _manifest(_agent_case("case-1"))
    execute_report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-src",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    sample_rows = []
    for sample_id in execute_report.items[0].sample_ids:
        sample_rows.append(await store.load_sample(sample_id))
    return store, dataset, execute_report, sample_rows, evidence


async def test_replay_never_invokes_business_target(tmp_path):
    store, dataset, _, sample_rows, evidence = await _seed_source_sample(tmp_path)
    target = FakeTargetRunner()
    runner = _runner(store, target, evidence=evidence)
    report = await runner.replay(
        ReplayExperimentRequest(
            name="exp-src",
            source_sample_ids=tuple(row["sample_id"] for row in sample_rows),
            evaluator_set=(("agent.task_success", "v1"),),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_hash,
        )
    )
    assert target.call_count == 0  # replay 绝不调用业务目标
    assert report.mode == "replay"
    assert all(item.outcome == "replay_rescored" for item in report.items)
    assert len(report.items) == len(sample_rows)
    # 新评分实验项指向既有 snapshot（不产生新业务证据）
    for item, source in zip(report.items, sample_rows):
        assert item.sample_ids
        replayed = await store.load_sample(item.sample_ids[0])
        assert replayed["snapshot_ref"] == source["snapshot_ref"]


async def test_replay_marks_name_and_links_original_target(tmp_path):
    store, dataset, _, sample_rows, evidence = await _seed_source_sample(tmp_path)
    scope = FakeRootScopeFactory()
    runner = _runner(store, FakeTargetRunner(), evidence=evidence, scope=scope)
    await runner.replay(
        ReplayExperimentRequest(
            name="exp-src",
            source_sample_ids=tuple(row["sample_id"] for row in sample_rows),
            evaluator_set=(("agent.task_success", "v1"),),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_hash,
        )
    )
    assert scope.identities
    for identity in scope.identities:
        assert identity["experiment_name"].endswith("replay")
    # Link 原目标真实身份
    original = sample_rows[0]["target"]
    linked = [
        tuple(link) for identity in scope.identities for link in identity.get("links", ())
    ]
    assert (original["trace_id"], original["span_id"]) in linked


async def test_replay_missing_snapshot_skipped(tmp_path):
    store, dataset, _, _, evidence = await _seed_source_sample(tmp_path)
    # 构造一个 snapshot_ref 为空的源 sample（无证据可重评）
    empty = await store.save_sample(
        {
            "producer_event_id": "online-no-evidence",
            "service": "n-agent",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-x",
                "subject_revision": "rev-1",
            },
            "evidence_targets": [],
            "snapshot_ref": "",
            "snapshot_hash": "",
            "schema_version": "1",
            "captured_at": "2026-11-11T00:00:00+00:00",
            "sample_probability": 1.0,
            "sampling_policy_version": "online",
            "job_intent": {
                "definitions": [],
                "mode": "online",
                "experiment_id": None,
                "replicate": 0,
            },
        }
    )
    target = FakeTargetRunner()
    runner = _runner(store, target, evidence=evidence)
    report = await runner.replay(
        ReplayExperimentRequest(
            name="exp-src",
            source_sample_ids=(empty,),
            evaluator_set=(("agent.task_success", "v1"),),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_hash,
        )
    )
    assert target.call_count == 0
    assert report.items[0].outcome == "evidence_missing"
    assert report.items[0].sample_ids == ()


# ---------------------------------------------------------------------------
# 冻结快照：字段完整 + external_unpinned
# ---------------------------------------------------------------------------


async def test_frozen_snapshot_completeness_with_external_unpinned(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(
        store,
        _definition(
            "agent.answer_correctness",
            "experiment_item",
            method="llm_judge",
            judge_model="judge-x",
        ),
    )
    # 指纹注入为可调用对象（延迟求值）；值为非空字符串
    fingerprint = lambda: {  # noqa: E731 - 测试用轻量指纹提供者
        "code_commit": "f" * 40,
        "system_prompt_hash": "e" * 64,
        "tool_inventory_hash": "d" * 64,
        # target_model_request_version / target_model_response_version /
        # skill_inventory_hash / mcp_inventory_hash / kb_corpus_config 缺失
    }
    runner = _runner(store, FakeTargetRunner(), fingerprint=fingerprint)
    dataset = _manifest(_agent_case("case-1"))
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.answer_correctness", "v1"),),
            replicates=3,
            seed="seed-42",
        )
    )
    snapshot = report.frozen_snapshot
    for field in FROZEN_ENVIRONMENT_FIELDS:
        assert field in snapshot, f"冻结快照缺少字段: {field}"
    assert snapshot["code_commit"] == "f" * 40
    assert snapshot["system_prompt_hash"] == "e" * 64
    for field in (
        "target_model_request_version",
        "target_model_response_version",
        "skill_inventory_hash",
        "mcp_inventory_hash",
        "kb_corpus_config",
    ):
        assert snapshot[field] == EXTERNAL_UNPINNED
    assert len(snapshot["evaluator_set_hash"]) == 64
    assert len(snapshot["judge_config_hash"]) == 64
    assert snapshot["dataset_hash"] == dataset.dataset_hash
    assert snapshot["seed"] == "seed-42"
    assert snapshot["replicate_count"] == 3


# ---------------------------------------------------------------------------
# 副作用清理：独立失败上报，不影响已完成评分
# ---------------------------------------------------------------------------


async def test_cleanup_failure_reported_independently(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    cleanup = FakeCleanup(error=RuntimeError("cleanup crashed"))
    evidence = FakeEvidence()
    runner = _runner(store, FakeTargetRunner(), evidence=evidence, cleanup=cleanup)
    case = DatasetCase(
        case_id="case-fx",
        input={"message": "hello"},
        target_kind="agent_run",
        expected_output="ok",
        allowed_side_effects=("artifact.created",),
        cleanup_rule="delete-artifacts",
    )
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(case),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    assert cleanup.requests
    assert report.items[0].cleanup_status == "failed"
    assert report.cleanup_failures == 1
    # 清理失败不影响已完成评分：证据与 sample/job 已提交
    assert evidence.saved
    assert report.items[0].outcome == "scored"
    assert report.items[0].sample_ids


# ---------------------------------------------------------------------------
# 实验项根采样（spec 11.1）：OTel 禁用 -> 分析 unavailable
# ---------------------------------------------------------------------------


async def test_root_scope_identity_and_analysis_unavailable_when_otel_disabled(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    scope = FakeRootScopeFactory(valid=False)  # 模拟 OTel 禁用（无有效根身份）
    runner = _runner(store, FakeTargetRunner(), scope=scope)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-1")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    identity = scope.identities[0]
    assert identity["experiment_id"] == report.experiment_id
    assert identity["experiment_name"] == "exp-a"
    assert identity["dataset_id"] == "ds-1"
    assert identity["item_id"] == "case-1#r0"
    item = report.items[0]
    assert item.analysis_available is False
    assert item.root_trace_id is None
    assert item.root_span_id is None


async def test_root_scope_valid_handle_yields_root_identity(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    scope = FakeRootScopeFactory(valid=True)
    runner = _runner(store, FakeTargetRunner(), scope=scope)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-1")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    item = report.items[0]
    assert item.analysis_available is True
    assert item.root_trace_id == HEX32
    assert item.root_span_id == HEX16


# ---------------------------------------------------------------------------
# 检索子指标附真实 retrieval 目标（附根规则）
# ---------------------------------------------------------------------------


async def test_sub_retrieval_sample_attaches_real_retrieval_target(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("kb.recall_at_5", "retrieval"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": {"results": _kb_results(), "corpus_revision": "corp-1"},
                "side_effects": [],
                "retrievals": [_retrieval_entry()],
                "run_id": "run-kb",
            }
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_kb_case("kb-1"), target_kind="retrieval"),
            name="exp-kb",
            target_config={"top_k": 5},
            evaluator_set=(("kb.recall_at_5", "v1"),),
        )
    )
    item = report.items[0]
    assert item.outcome == "scored"
    sample = await store.load_sample(item.sample_ids[0])
    assert sample["target"]["kind"] == "retrieval"
    assert sample["target"]["retrieval_id"] == "ret-1"
    assert sample["target"]["trace_id"] == HEX32
    assert sample["target"]["span_id"] == HEX16


# ---------------------------------------------------------------------------
# 校验与 Application 层纯净
# ---------------------------------------------------------------------------


async def test_execute_rejects_non_experiment_definition_kinds(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("feedback.helpful", "agent_run"))
    runner = _runner(store, FakeTargetRunner())
    with pytest.raises(EvaluationValidationError):
        await runner.execute(
            ExecuteExperimentRequest(
                dataset=_manifest(_agent_case("case-1")),
                name="exp-a",
                target_config={"model": "m"},
                evaluator_set=(("feedback.helpful", "v1"),),
            )
        )


async def test_execute_rejects_non_positive_replicates(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    runner = _runner(store, FakeTargetRunner())
    with pytest.raises(EvaluationValidationError):
        await runner.execute(
            ExecuteExperimentRequest(
                dataset=_manifest(_agent_case("case-1")),
                name="exp-a",
                target_config={"model": "m"},
                evaluator_set=(("agent.task_success", "v1"),),
                replicates=0,
            )
        )


def test_application_module_stays_pure_no_infrastructure_or_httpx():
    source = (REPO_ROOT / "app" / "application" / "evaluation_experiment.py").read_text()
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any(m == "httpx" or m.startswith("httpx.") for m in modules)
    assert not any(m.startswith("app.infrastructure") for m in modules)


# ---------------------------------------------------------------------------
# AttemptRecord / handle 形状守卫
# ---------------------------------------------------------------------------


def test_attempt_record_and_root_handle_shapes():
    attempt = AttemptRecord(
        attempt_index=0,
        status="transport_error",
        error_type="network",
        side_effects=(),
        started_at="2026-11-11T00:00:00+00:00",
        ended_at="2026-11-11T00:00:01+00:00",
    )
    assert attempt.status == "transport_error"
    handle = ExperimentRootHandle(trace_id=HEX32, span_id=HEX16, is_valid=True)
    assert handle.is_valid


# ---------------------------------------------------------------------------
# 每 attempt 独立会话：重试不在共享会话内重放已执行轮次
# ---------------------------------------------------------------------------


async def test_transport_retry_uses_fresh_session_per_attempt(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "transport_error",
                "error_type": "network",
                "output": {},
                "side_effects": [],
                "retrievals": [],
            },
            {
                "status": "succeeded",
                "output": dict(_agent_output()),
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-2",
            },
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-mt", multi_turn=True)),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    sessions = [req["session"]["session_id"] for _, req in target.calls]
    assert len(sessions) == 2
    assert len(set(sessions)) == 2  # 每 attempt 独立会话
    # 重试在全新会话内完整重放全部轮次，不污染首次 attempt 会话
    for _, req in target.calls:
        assert [t["message"] for t in req["turns"]] == ["turn-1", "turn-2"]
    assert report.items[0].outcome == "scored"
    assert [a.status for a in report.items[0].attempts] == [
        "transport_error",
        "succeeded",
    ]


# ---------------------------------------------------------------------------
# 清理覆盖全部 attempt 的副作用（含重试被副作用阻断的失败 attempt）
# ---------------------------------------------------------------------------


async def test_cleanup_covers_side_effects_from_failed_attempts(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    cleanup = FakeCleanup()
    target = FakeTargetRunner(
        results=[
            {
                "status": "transport_error",
                "error_type": "timeout",
                "output": {},
                "side_effects": ["artifact.created"],  # 失败 attempt 已产生副作用
                "retrievals": [],
            },
        ]
    )
    case = DatasetCase(
        case_id="case-fx",
        input={"message": "hello"},
        target_kind="agent_run",
        expected_output="ok",
        allowed_side_effects=("artifact.created",),
        cleanup_rule="delete-artifacts",
    )
    runner = _runner(store, target, cleanup=cleanup)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(case),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    item = report.items[0]
    assert item.outcome == "transport_exhausted"
    # 失败 attempt 观测到的副作用必须进入清理清单
    assert cleanup.requests
    assert cleanup.requests[0]["side_effects"] == ["artifact.created"]
    assert item.cleanup_status == "ok"


# ---------------------------------------------------------------------------
# 线上样本 replay：业务目标类型映射到评分定义类型
# ---------------------------------------------------------------------------


async def test_replay_online_agent_run_sample_rescores(tmp_path):
    store, dataset, _, sample_rows, evidence = await _seed_source_sample(tmp_path)
    source = sample_rows[0]
    # 线上样本：agent_run 业务目标 + 既有 snapshot 引用（无实验谱系）
    online = await store.save_sample(
        {
            "producer_event_id": "online-agent-run-1",
            "service": "n-agent",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-online-1",
                "subject_revision": "rev-1",
                "trace_id": source["target"]["trace_id"],
                "span_id": source["target"]["span_id"],
            },
            "evidence_targets": [],
            "snapshot_ref": source["snapshot_ref"],
            "snapshot_hash": source["snapshot_hash"],
            "schema_version": source["schema_version"],
            "captured_at": "2026-11-11T00:00:00+00:00",
            "sample_probability": 1.0,
            "sampling_policy_version": "online",
            "job_intent": {
                "definitions": [],
                "mode": "online",
                "experiment_id": None,
                "replicate": 0,
            },
        }
    )
    target = FakeTargetRunner()
    runner = _runner(store, target, evidence=evidence)
    report = await runner.replay(
        ReplayExperimentRequest(
            name="exp-src",
            source_sample_ids=(online,),
            evaluator_set=(("agent.task_success", "v1"),),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_hash,
        )
    )
    assert target.call_count == 0  # replay 绝不调用业务目标
    item = report.items[0]
    # agent_run 源映射到 experiment_item 评分定义：评分 sample 展开出 job
    assert item.outcome == "replay_rescored"
    assert item.sample_ids
    replayed = await store.load_sample(item.sample_ids[0])
    assert replayed["snapshot_ref"] == source["snapshot_ref"]
    # 队列中包含 seed execute 产生的 job：领取直到命中 replay 样本的 job
    job = None
    for _ in range(16):
        claimed = await store.claim_next_job("test-worker")
        if claimed is None:
            break
        if claimed["sample_id"] == replayed["sample_id"]:
            job = claimed
            break
    assert job is not None, "replay 评分 sample 必须展开出评分 job"
    assert job["definition_id"] == "agent.task_success"
    assert job["experiment_id"] == report.experiment_id
    assert item.case_id == "run-online-1"  # 原目标业务身份保留在 case_id


async def test_replay_rejects_session_feedback_source(tmp_path):
    store, dataset, _, sample_rows, evidence = await _seed_source_sample(tmp_path)
    source = sample_rows[0]
    feedback = await store.save_sample(
        {
            "producer_event_id": "online-feedback-1",
            "service": "n-agent",
            "target": {
                "service": "n-agent",
                "kind": "session_feedback",
                "subject_id": "fb-1",
                "subject_revision": "rev-1",
            },
            "evidence_targets": [],
            "snapshot_ref": source["snapshot_ref"],
            "snapshot_hash": source["snapshot_hash"],
            "schema_version": source["schema_version"],
            "captured_at": "2026-11-11T00:00:00+00:00",
            "sample_probability": 1.0,
            "sampling_policy_version": "online",
            "job_intent": {
                "definitions": [],
                "mode": "online",
                "experiment_id": None,
                "replicate": 0,
            },
        }
    )
    runner = _runner(store, FakeTargetRunner(), evidence=evidence)
    # session_feedback 人评重修不是自动重评：契约违规 fail-fast（走 feedback
    # 修订通道），不静默展开空评分 job
    with pytest.raises(EvaluationValidationError):
        await runner.replay(
            ReplayExperimentRequest(
                name="exp-src",
                source_sample_ids=(feedback,),
                evaluator_set=(("agent.task_success", "v1"),),
                dataset_id=dataset.dataset_id,
                dataset_version=dataset.dataset_version,
                dataset_hash=dataset.dataset_hash,
            )
        )


# ---------------------------------------------------------------------------
# 单项异常隔离：基础设施故障/源缺失记单项 error，运行继续
# ---------------------------------------------------------------------------


class FlakySaveStore(SQLiteEvaluationStore):
    """第 N 次之后 save_sample 抛存储故障的 store（模拟基础设施抖动）。"""

    def __init__(self, path: str, *, fail_after: int) -> None:
        super().__init__(path)
        self._save_calls = 0
        self._fail_after = fail_after

    async def save_sample(self, sample):
        self._save_calls += 1
        if self._save_calls > self._fail_after:
            raise RuntimeError("disk exploded")
        return await super().save_sample(sample)


async def test_execute_store_failure_isolated_per_item(tmp_path):
    flaky = FlakySaveStore(str(tmp_path / "evaluations.db"), fail_after=1)
    await _definitions(flaky, _definition("agent.task_success", "experiment_item"))
    runner = _runner(flaky, FakeTargetRunner())
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-ok"), _agent_case("case-bad")),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    # 第 1 项已完成并保留；第 2 项存储故障记单项 error，不中止运行
    assert [item.outcome for item in report.items] == ["scored", "error"]
    assert report.items[0].sample_ids
    assert report.items[1].failure_reason == "RuntimeError"
    assert report.items[1].sample_ids == ()


async def test_error_item_after_business_execution_still_cleans_up(tmp_path):
    flaky = FlakySaveStore(str(tmp_path / "evaluations.db"), fail_after=0)
    await _definitions(flaky, _definition("agent.task_success", "experiment_item"))
    cleanup = FakeCleanup()
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": dict(_agent_output()),
                "side_effects": ["artifact.created"],
                "retrievals": [],
                "run_id": "run-1",
            },
        ]
    )
    case = DatasetCase(
        case_id="case-fx",
        input={"message": "hello"},
        target_kind="agent_run",
        expected_output="ok",
        allowed_side_effects=("artifact.created",),
        cleanup_rule="delete-artifacts",
    )
    runner = _runner(flaky, target, cleanup=cleanup)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(case),
            name="exp-a",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    item = report.items[0]
    # 评分提交存储故障：单项 error（业务执行已完成、attempt 保留）
    assert item.outcome == "error"
    assert item.failure_reason == "RuntimeError"
    assert [a.status for a in item.attempts] == ["succeeded"]
    # error 结局不豁免清理：已观测副作用进入清理清单
    assert cleanup.requests
    assert cleanup.requests[0]["side_effects"] == ["artifact.created"]
    assert item.cleanup_status == "ok"


async def test_replay_missing_source_isolated_as_error_item(tmp_path):
    store, dataset, _, sample_rows, evidence = await _seed_source_sample(tmp_path)
    runner = _runner(store, FakeTargetRunner(), evidence=evidence)
    report = await runner.replay(
        ReplayExperimentRequest(
            name="exp-src",
            source_sample_ids=(sample_rows[0]["sample_id"], "no-such-sample"),
            evaluator_set=(("agent.task_success", "v1"),),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_hash,
        )
    )
    # 源缺失不中止运行：已完成项保留，缺失源记单项 error
    assert [item.outcome for item in report.items] == [
        "replay_rescored",
        "error",
    ]
    assert report.items[1].failure_reason == "EvaluationNotFoundError"


# ---------------------------------------------------------------------------
# 快照检索条目过滤 / 指纹契约
# ---------------------------------------------------------------------------


async def test_snapshot_filters_retrieval_entries_without_id(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("kb.recall_at_5", "retrieval"))
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": {"results": _kb_results(), "corpus_revision": "corp-1"},
                "side_effects": [],
                "retrievals": [
                    _retrieval_entry(),
                    {  # 无 retrieval_id：不可归属，不进快照、不产评分样本
                        "retrieval_id": "",
                        "trace_id": HEX32,
                        "span_id": HEX16,
                        "corpus_revision": "corp-1",
                        "results": _kb_results(),
                    },
                ],
                "run_id": "run-kb",
            }
        ]
    )
    evidence = FakeEvidence()
    runner = _runner(store, target, evidence=evidence)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_kb_case("kb-1"), target_kind="retrieval"),
            name="exp-kb",
            target_config={"top_k": 5},
            evaluator_set=(("kb.recall_at_5", "v1"),),
        )
    )
    snapshot = evidence.saved[0][1]
    retrievals = snapshot.content["retrievals"]
    assert [entry["retrieval_id"] for entry in retrievals] == ["ret-1"]
    # 空 retrieval_id 条目不产生检索评分样本
    assert len(report.items[0].sample_ids) == 1


async def test_fingerprint_non_string_value_rejected(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    fingerprint = lambda: {"code_commit": 12345}  # noqa: E731 - 契约违规形态
    runner = _runner(store, FakeTargetRunner(), fingerprint=fingerprint)
    with pytest.raises(EvaluationValidationError):
        await runner.execute(
            ExecuteExperimentRequest(
                dataset=_manifest(_agent_case("case-1")),
                name="exp-a",
                target_config={"model": "m"},
                evaluator_set=(("agent.task_success", "v1"),),
            )
        )


# ---------------------------------------------------------------------------
# 质量复审 m1：业务 attempt 崩溃不豁免副作用清理（docstring 契约）
# ---------------------------------------------------------------------------


class CrashingTargetRunner:
    """run_target 直接崩溃的执行 fake（非契约异常：适配器意外崩溃）。"""

    async def run_target(self, target, request: dict) -> dict:
        raise RuntimeError("adapter crashed mid-attempt")


async def test_business_attempt_crash_still_runs_cleanup(tmp_path):
    """m1（质量复审）：run_target 非契约异常时业务可能已部分执行——
    模块 docstring 契约"error 结局不豁免副作用清理"必须成立：隔离失败
    记录仍按 case cleanup_rule 执行清理，attempt 记录保留崩溃 attempt
    （business_error、无副作用观测），cleanup_status 如实上报。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    cleanup = FakeCleanup()
    runner = _runner(store, CrashingTargetRunner(), cleanup=cleanup)
    case = DatasetCase(
        case_id="case-crash",
        input={"message": "hello"},
        target_kind="agent_run",
        expected_output="ok",
        allowed_side_effects=("artifact.created",),
        cleanup_rule="delete-artifacts",
    )
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(case),
            name="exp-crash",
            target_config={"model": "m"},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    item = report.items[0]
    assert item.outcome == "error"
    assert item.failure_reason == "RuntimeError"
    # 崩溃 attempt 已记录（业务可能已执行，按 business_error 归类）
    assert len(item.attempts) == 1
    assert item.attempts[0].status == "business_error"
    assert item.attempts[0].error_type == "RuntimeError"
    # 清理执行（RED：当前 cleanup_status=not_required，清理被跳过）
    assert cleanup.requests
    assert item.cleanup_status == "ok"


# ---------------------------------------------------------------------------
# 30 项人工评审修复轮（#6/#24/#7/#17）
# ---------------------------------------------------------------------------


async def test_snapshot_carries_judge_slot_fields(tmp_path):
    """#6：实验快照携带 Judge 槽位派生字段（request/answer/reference/
    chunks/contexts/query）——此前只有实验别名键，Judge 全部
    skipped(evidence_missing)。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    evidence = FakeEvidence()
    target = FakeTargetRunner()
    runner = _runner(store, target, evidence=evidence)
    case = _agent_case("case-1")
    dataset = _manifest(case)
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-slots",
            target_config={},
            evaluator_set=(("agent.task_success", "v1"),),
            replicates=1,
            seed=None,
            baseline_id=None,
        )
    )
    snapshot = evidence.saved[0][1]
    content = snapshot.content
    assert content["request"] == "hello"
    assert content["answer"] == "answer"
    assert content["reference"] == "ok"
    # 无检索条目：chunks/contexts/query 为 None（不是空列表伪造）
    assert content["chunks"] is None
    assert content["contexts"] is None
    assert content["query"] is None


async def test_snapshot_judge_slots_from_retrievals(tmp_path):
    """#6：检索目标快照的 chunks/contexts 从检索条目 chunks/results 展
    平，query 取检索条目。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    evidence = FakeEvidence()
    entry = dict(_retrieval_entry())
    entry["query"] = "what is n-agent"
    entry["chunks"] = ["chunk text one", "chunk text two"]
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": {"results": _kb_results(), "corpus_revision": "corp-1"},
                "side_effects": [],
                "retrievals": [entry],
                "run_id": None,
            }
        ]
    )
    await _definitions(store, _definition("kb.recall_at_5", "experiment_item"))
    runner = _runner(store, target, evidence=evidence)
    dataset = _manifest(_kb_case(), target_kind="retrieval")
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-kb-slots",
            target_config={},
            evaluator_set=(("kb.recall_at_5", "v1"),),
            replicates=1,
            seed=None,
            baseline_id=None,
        )
    )
    content = evidence.saved[0][1].content
    assert content["chunks"] == ["chunk text one", "chunk text two"]
    assert content["contexts"] == ["chunk text one", "chunk text two"]
    assert content["query"] == "what is n-agent"


async def test_snapshot_preserves_qrels_none_vs_empty(tmp_path):
    """#24：qrels 缺失（None）保留 None（worker 按 missing_reference 跳
    过，reference-dependent 指标不运行）；显式空 dict 转空列表。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    evidence = FakeEvidence()
    target = FakeTargetRunner()
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    runner = _runner(store, target, evidence=evidence)
    # 无 qrels 的 agent case：快照 qrels 为 None
    dataset = _manifest(_agent_case("case-nq"))
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=dataset,
            name="exp-qrels-none",
            target_config={},
            evaluator_set=(("agent.task_success", "v1"),),
            replicates=1,
            seed=None,
            baseline_id=None,
        )
    )
    assert evidence.saved[0][1].content["qrels"] is None


async def test_kb_results_stamped_with_corpus_revision(tmp_path):
    """#7：KB runner 逐条 result 盖 corpus_revision 章——worker kb 指标
    构建器按 result 行读取 corpus_revision（qrels 身份键三元组）。"""
    from app.infrastructure.evaluation.target_runner import KBDirectTargetRunner

    async def retrieve(_request: dict) -> dict:
        return {
            "retrieval_id": "ret-x",
            "trace_id": "b" * 32,
            "span_id": "c" * 16,
            "corpus_revision": "corp-9",
            "results": [
                {"document_revision": "d1", "chunk_id": "c1", "rank": 1},
                {"document_revision": "d2", "chunk_id": "c2", "rank": 2},
            ],
        }

    runner = KBDirectTargetRunner(retrieve=retrieve)
    from app.domain.evaluation.models import EvaluationTarget as _T

    result = await runner.run_target(
        _T(service="n-kb", kind="retrieval", subject_id="s", subject_revision="r"),
        {"turns": [{"query": "q"}], "session": {}},
    )
    for row in result["output"]["results"]:
        assert row["corpus_revision"] == "corp-9"
    for entry in result["retrievals"]:
        for row in entry["results"]:
            assert row["corpus_revision"] == "corp-9"


async def test_retrieval_sample_registers_document_targets(tmp_path):
    """#8：retrieval 样本同登记真实消费的文档版本——纯 KB 实验没有根
    样本，文档删除需经该索引撤销 jobs/results/投递。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(
        store,
        _definition("kb.recall_at_5", "retrieval"),
        _definition("kb.ndcg_at_5", "retrieval"),
    )
    entry = dict(_retrieval_entry())
    entry["results"] = [
        {"document_id": "doc-a", "document_revision": "r1", "rank": 1},
        {"document_id": "doc-b", "document_revision": "r2", "rank": 2},
    ]
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": {"results": entry["results"], "corpus_revision": "corp-1"},
                "side_effects": [],
                "retrievals": [entry],
            }
        ]
    )
    runner = _runner(store, target)
    report = await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_kb_case("kb-8")),
            name="exp-kb8",
            target_config={},
            evaluator_set=(("kb.recall_at_5", "v1"),),
        )
    )
    assert report.items[0].outcome == "scored"
    # retrieval 样本的 evidence_targets 含 retrieval + document 条目
    # evidence_targets 反向索引：retrieval + document 条目均已登记
    # （经删除收集同款 subject 反查核验）
    doc_samples = await store.list_sample_ids_by_subject(
        "n-kb", "document", "doc-a", "r1"
    )
    assert doc_samples, "document doc-a 未进入删除反向索引"
    doc_samples_b = await store.list_sample_ids_by_subject(
        "n-kb", "document", "doc-b", "r2"
    )
    assert doc_samples_b, "document doc-b 未进入删除反向索引"


async def test_snapshot_redacts_builtin_credential_shapes(tmp_path):
    """#6：实验快照经双层脱敏——运行时输出中的裸凭据形态不得未脱敏
    落盘（内建 _default_redact 兜底不依赖组合根注入）。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    evidence = FakeEvidence()
    output = dict(_agent_output())
    output["final_answer"] = "token ghp_" + "a" * 36 + " end"
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": output,
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-redact",
            }
        ]
    )
    runner = _runner(store, target, evidence=evidence)
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-redact")),
            name="exp-redact",
            target_config={},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    content = evidence.saved[0][1].content
    assert "ghp_" + "a" * 36 not in content["final_answer"]
    assert "[redacted]" in content["final_answer"]


async def test_snapshot_injected_redactor_applied(tmp_path):
    """#6：组合根注入的 redactor（LogSanitizer）在快照期生效。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    evidence = FakeEvidence()
    output = dict(_agent_output())
    output["final_answer"] = "answer with SECRET-VALUE inside"
    target = FakeTargetRunner(
        results=[
            {
                "status": "succeeded",
                "output": output,
                "side_effects": [],
                "retrievals": [],
                "run_id": "run-redact2",
            }
        ]
    )
    runner = _runner(store, target, evidence=evidence)
    # 手动注入 redactor（模拟组合根 LogSanitizer）
    runner._redactor = lambda text: text.replace("SECRET-VALUE", "[injected-redacted]")
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-redact2")),
            name="exp-redact2",
            target_config={},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    content = evidence.saved[0][1].content
    assert "SECRET-VALUE" not in content["final_answer"]
    assert "[injected-redacted]" in content["final_answer"]


async def test_multi_turn_request_uses_last_user_turn(tmp_path):
    """#22：多轮 case 的 request 取最后一个 user 轮——评价对象是最后一
    轮问答，不是首轮陈述。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    evidence = FakeEvidence()
    target = FakeTargetRunner()
    runner = _runner(store, target, evidence=evidence)
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(_agent_case("case-mt", multi_turn=True)),
            name="exp-mt",
            target_config={},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    content = evidence.saved[0][1].content
    assert content["request"] == "turn-2"


async def test_mapping_expected_output_derives_reference(tmp_path):
    """#22：expected_output 为 Mapping（内置数据集形态）时派生字符串
    reference，correctness 不再因缺参考整体 skipped。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await _definitions(store, _definition("agent.task_success", "experiment_item"))
    evidence = FakeEvidence()
    case = DatasetCase(
        case_id="case-dict",
        input={"message": "hello"},
        target_kind="agent_run",
        expected_output={"task_success": True, "expectation": "reply politely"},
    )
    target = FakeTargetRunner()
    runner = _runner(store, target, evidence=evidence)
    await runner.execute(
        ExecuteExperimentRequest(
            dataset=_manifest(case),
            name="exp-dict",
            target_config={},
            evaluator_set=(("agent.task_success", "v1"),),
        )
    )
    content = evidence.saved[0][1].content
    assert content["reference"] == "reply politely"


async def test_evidence_targets_include_consumed_documents(tmp_path):
    """#17：实验检索真实消费的 N-KB 文档版本进入删除反向索引。"""
    from app.application.evaluation_experiment import (
        EvaluationExperimentRunner as _R,
    )

    result = {
        "run_id": "run-17",
        "retrievals": [
            {
                "retrieval_id": "ret-17",
                "results": [
                    {"document_id": "doc-a", "document_revision": "r1"},
                    {"document_id": "doc-b", "document_revision": "r2"},
                    {"document_id": "doc-a", "document_revision": "r1"},
                ],
            }
        ],
    }
    dataset = _manifest(_agent_case("case-17"))
    targets = _R._evidence_targets(result, dataset)
    kinds = {
        (t["subject_kind"], t["subject_id"], t["subject_revision"])
        for t in targets
    }
    assert ("agent_run", "run-17", dataset.dataset_version) in kinds
    assert ("document", "doc-a", "r1") in kinds
    assert ("document", "doc-b", "r2") in kinds
