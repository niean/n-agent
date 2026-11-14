import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.application.chat_service import ChatCompletionResult
from app.application.evaluation_execution import EvaluationExecutionService
from app.application.evaluation_experiment import EvaluationExperimentRunner, ExperimentRootHandle
from app.application.evaluation_review import EvaluationReviewService
from app.application.evaluation_service import EvaluationService
from app.application.evaluation_worker import EvaluationWorker
from app.domain.evaluation.execution import ExecutionNotReadyError
from app.infrastructure.evaluation.execution_runtime import GuardedChatTarget
from app.infrastructure.evaluation.execution_store import SQLiteExecutionStore
from app.infrastructure.evaluation.store import SQLiteEvaluationStore


class Evidence:
    def __init__(self): self.saved = {}
    async def save_snapshot(self, target, snapshot):
        ref = str(len(self.saved)); self.saved[ref] = snapshot; return ref
    async def load_snapshot(self, ref): return self.saved[ref]
    async def has_snapshot(self, ref): return ref in self.saved


@contextmanager
def root_scope(identity):
    yield ExperimentRootHandle(trace_id=None, span_id=None, is_valid=False)


async def test_real_import_runner_and_scoring_worker_are_connected(tmp_path):
    ledger = SQLiteEvaluationStore(str(tmp_path / "eval.db"))
    service = EvaluationService(ledger, enabled=True)
    await service.register_preset_definitions(judge_model="same-model")
    review = EvaluationReviewService(store=ledger, datasets_root=tmp_path / "datasets")
    imported = await review.import_dataset({"dataset_id": "real-import", "dataset_version": "1", "target_kind": "agent_run", "approved_by": "test-fixture",
        "cases": [{"case_id": "one", "target_kind": "agent_run", "input": {"message": "hello"}, "expected_output": "hello", "fixture_content": {"text": "hello"}}]})
    evidence, calls, judge_calls = Evidence(), [], []

    async def readiness():
        return {"ready": True, "worker_ready": True, "judge_ready": True, "config_fingerprint": "fixed", "target_model": "same-model"}

    class Chat:
        async def complete(self, request):
            calls.append(request)
            return ChatCompletionResult(session_id=request.session_id, model="same-model", message={"role":"assistant", "content":"hello"}, run_id="run-one")

    class Judge:
        async def evaluate(self, definition, snapshot):
            assert snapshot.content["reference"] == "hello"
            assert snapshot.content["target_model"] == "same-model"
            judge_calls.append(snapshot)
            return {"status":"value", "value":1, "reason":"test", "provenance":{"self_judge":True}}

    runner = EvaluationExperimentRunner(service=service, ledger=ledger, evidence=evidence,
        target_runner=GuardedChatTarget(chat=Chat(), readiness=readiness), experiment_root_scope=root_scope)
    execution = EvaluationExecutionService(store=SQLiteExecutionStore(str(ledger.path)), ledger=ledger,
        review=review, runner=runner, readiness=readiness, evidence=evidence, poll_interval=0.01, scoring_timeout=2)
    accepted = await execution.submit({"request_key":"one-request", "name":"real path", "dataset_id":"real-import", "dataset_version":"1",
        "dataset_hash":imported["dataset_hash"], "replicates":1, "evaluator_set":[{"definition_id":"agent.answer_correctness", "definition_version":"experiment-v1"}]})
    work = asyncio.create_task(execution.run_once())
    worker = EvaluationWorker(ledger, evaluator=Judge(), evidence_store=evidence)
    try:
        for _ in range(100):
            if await worker.run_once(): break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(work, 3)
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    detail = await execution.get(accepted["execution_id"])
    assert detail["phase"] == "completed"
    assert detail["quality_counts"]["value"] == 1
    assert detail["report"]["items"][0]["answer"] == "hello"
    assert len(calls) == len(judge_calls) == 1
    assert len(await ledger.list_experiments()) == 1


async def test_guarded_target_rechecks_between_turns():
    state = {"ready":True,"config_fingerprint":"original", "target_model":"model"}
    calls = []

    async def ready(): return dict(state)

    class Chat:
        async def complete(self, request):
            calls.append(request)
            state["config_fingerprint"] = "changed"
            return ChatCompletionResult(session_id="session", model="model", message={"content":"answer"})

    runner = GuardedChatTarget(chat=Chat(), readiness=ready)
    with pytest.raises(ExecutionNotReadyError):
        await runner.run_target(None, {"target_config":{"execution_fingerprint":"original"}, "turns":[{"message":"one"},{"message":"two"}]})
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# T6：profile 路由（dashboard -> SSE 适配器；其余 -> 受保护进程内 runner）
# ---------------------------------------------------------------------------


class _StubRunner:
    def __init__(self, tag, calls):
        self._tag, self._calls = tag, calls

    async def run_target(self, target, request):
        self._calls.append(self._tag)
        return {"status": "succeeded", "output": {}, "side_effects": [], "run_id": "r"}


async def test_profile_routing_dashboard_to_sse_agent_to_guarded():
    from app.infrastructure.evaluation.execution_runtime import ProfileRoutingTargetRunner

    calls = []
    models = []
    async def readiness():
        return {"ready": True, "config_fingerprint": "fp", "target_model": "m1"}

    runner = ProfileRoutingTargetRunner(
        guarded=_StubRunner("guarded", calls),
        dashboard_factory=lambda model: (models.append(model), _StubRunner("sse", calls))[1],
        readiness=readiness,
    )
    ok = {"status": "succeeded", "output": {}, "side_effects": [], "run_id": "r"}
    await runner.run_target(None, {"profile": "dashboard", "target_config": {"execution_fingerprint": "fp"}})
    await runner.run_target(None, {"target_config": {}})
    await runner.run_target(None, {"profile": "agent", "target_config": {}})
    assert calls == ["sse", "guarded", "guarded"]
    assert models == ["m1"]


async def test_profile_routing_rejects_changed_configuration():
    from app.infrastructure.evaluation.execution_runtime import ProfileRoutingTargetRunner

    state = {"ready": True, "config_fingerprint": "fp", "target_model": "m1"}
    calls = []

    async def readiness():
        return dict(state)

    runner = ProfileRoutingTargetRunner(
        guarded=_StubRunner("guarded", calls),
        dashboard_factory=lambda model: _StubRunner("sse", calls),
        readiness=readiness,
    )
    with pytest.raises(ExecutionNotReadyError):
        await runner.run_target(None, {
            "profile": "dashboard",
            "target_config": {"execution_fingerprint": "stale"},
        })
    assert not calls


async def test_execution_service_wires_dashboard_profile(tmp_path):
    from types import SimpleNamespace

    from app.infrastructure.evaluation.execution_runtime import build_execution_service
    from app.infrastructure.evaluation.scenario_adapters import ScenarioDashboardSSERunner

    ledger = SQLiteEvaluationStore(str(tmp_path / "eval.db"))
    settings = SimpleNamespace(
        evaluation_judge_provider_base_url="http://judge.test",
        evaluation_judge_provider_api_key="key",
        evaluation_judge_provider_model="judge-model",
        evaluation_evidence_retention_days=7,
        dashboard_base_url="http://127.0.0.1:8201",
    )
    holder = SimpleNamespace(current_config=None)

    async def get_secret(config_id):
        return "secret"

    class ConfigService:
        def current_sync(self):
            return SimpleNamespace(evaluation_enabled=True)

    class Worker:
        is_running = True

    service = build_execution_service(
        settings=settings, holder=holder, registry=SimpleNamespace(get_secret=get_secret),
        chat=None, service=EvaluationService(ledger, enabled=True), ledger=ledger,
        review=EvaluationReviewService(store=ledger, datasets_root=tmp_path / "datasets"),
        evidence=Evidence(), worker=Worker(), config_service=ConfigService(),
        scenario_memory_facts={"file_1": "文件记忆1"},
    )
    # preflight 准入 profile 覆盖已接线的 dashboard 通道
    assert "dashboard" in service.preflight._admitted_profiles
    # submit 侧准入与 preflight 同源（否则 preflight 通过但 submit 拒绝）
    assert service.admitted_profiles == frozenset({"agent", "dashboard"})
    # 目标 runner 为 profile 路由器：dashboard 分支是正式 SSE 适配器
    dispatcher = service.runner._target_runner
    from app.infrastructure.evaluation.execution_runtime import ProfileRoutingTargetRunner
    assert isinstance(dispatcher, ProfileRoutingTargetRunner)
    dashboard_runner = dispatcher._dashboard_factory("target-model")
    assert isinstance(dashboard_runner, ScenarioDashboardSSERunner)
    assert dashboard_runner._base_url == "http://127.0.0.1:8201"
    assert dashboard_runner._model == "target-model"
    # T8 S4：场景绑定材料核验接线——服务端权威事实 sha256 与 case
    # binding 声明同源（缺失该接线时绑定题被整体 blocked）
    reader = service.preflight._material_hashes_reader
    assert reader is not None
    hashes = await reader("agent-e2e-scenarios", "2")
    import hashlib as _hashlib
    assert hashes["file_1"] == _hashlib.sha256("文件记忆1".encode("utf-8")).hexdigest()


async def test_execution_service_wires_browser_evidence_hooks(tmp_path):
    """T9：浏览器证据读取与账目观察钩子贯通到 dashboard SSE 适配器。"""
    from types import SimpleNamespace

    from app.application.evaluation_scenario_resources import (
        ScenarioResourceLedger,
    )
    from app.infrastructure.evaluation.execution_runtime import (
        ProfileRoutingTargetRunner,
        build_execution_service,
    )
    from app.infrastructure.evaluation.scenario_adapters import (
        ScenarioDashboardSSERunner,
    )
    from app.infrastructure.evaluation.scenario_store import (
        SQLiteScenarioResourceStore,
    )

    ledger = SQLiteEvaluationStore(str(tmp_path / "eval.db"))
    settings = SimpleNamespace(
        evaluation_judge_provider_base_url="http://judge.test",
        evaluation_judge_provider_api_key="key",
        evaluation_judge_provider_model="judge-model",
        evaluation_evidence_retention_days=7,
        dashboard_base_url="http://127.0.0.1:8201",
    )
    holder = SimpleNamespace(current_config=None)

    async def get_secret(config_id):
        return "secret"

    class ConfigService:
        def current_sync(self):
            return SimpleNamespace(evaluation_enabled=True)

    class Worker:
        is_running = True

    async def browser_state_reader(session_id):
        return []

    async def browser_observer(session_id, records, context):
        return None

    scenario_resource_store = SQLiteScenarioResourceStore(
        str(tmp_path / "resources.db")
    )
    scenario_resource_ledger = ScenarioResourceLedger(
        scenario_resource_store,
        stopper=type("_S", (), {"stop": staticmethod(lambda *a, **k: "stopped")})(),
        file_deleter=type("_F", (), {"delete_file": staticmethod(lambda *a, **k: "deleted")})(),
        evidence_deleter=type("_E", (), {"delete_evidence": staticmethod(lambda *a, **k: None)})(),
        evidence_retention_seconds=0.0,
    )

    async def _task_creator(payload, session_id, context):
        return {"task_id": "t_1", "status": "queued"}

    async def _task_reader(task_id):
        return {"task_id": task_id, "status": "succeeded"}

    async def _task_runs_reader(task_id):
        return []

    async def _artifact_lister(source_context_ref):
        return []

    async def _workspace_file_reader(rel_path):
        return None

    async def _task_intent_before(payload, context):
        return "k"

    async def _task_intent_confirmed(business_key, task_id):
        return None

    async def _task_intent_unknown(business_key, reason):
        return None
    service = build_execution_service(
        settings=settings, holder=holder, registry=SimpleNamespace(get_secret=get_secret),
        chat=None, service=EvaluationService(ledger, enabled=True), ledger=ledger,
        review=EvaluationReviewService(store=ledger, datasets_root=tmp_path / "datasets"),
        evidence=Evidence(), worker=Worker(), config_service=ConfigService(),
        browser_state_reader=browser_state_reader,
        browser_observer=browser_observer,
        resource_ledger=scenario_resource_ledger,
        task_creator=_task_creator,
        task_reader=_task_reader,
        task_runs_reader=_task_runs_reader,
        artifact_lister=_artifact_lister,
        workspace_file_reader=_workspace_file_reader,
        task_intent_before=_task_intent_before,
        task_intent_confirmed=_task_intent_confirmed,
        task_intent_unknown=_task_intent_unknown,
    )
    dashboard_runner = service.runner._target_runner._dashboard_factory("target-model")
    assert isinstance(dashboard_runner, ScenarioDashboardSSERunner)
    assert dashboard_runner._browser_state_reader is browser_state_reader
    assert dashboard_runner._browser_observer is browser_observer
    # T10：task/制品/文件端口贯通（task_api 随 create 端口准入）
    assert dashboard_runner._task_creator is _task_creator
    assert dashboard_runner._task_reader is _task_reader
    assert dashboard_runner._task_runs_reader is _task_runs_reader
    assert dashboard_runner._artifact_lister is _artifact_lister
    assert dashboard_runner._workspace_file_reader is _workspace_file_reader
    assert dashboard_runner._task_intent_before is _task_intent_before
    assert dashboard_runner._task_intent_confirmed is _task_intent_confirmed
    assert dashboard_runner._task_intent_unknown is _task_intent_unknown
    assert "task_api" in service.admitted_profiles
    # T9 S4 缺口回归：场景资源账本必须接到执行单 settle——否则 owned
    # 资源无人释放（实测 browser 记录停在 owned、release 报告恒
    # not_applicable）
    assert service.resource_ledger is scenario_resource_ledger


async def test_judge_dispatch_rejects_changed_connection_before_call():
    from app.main import _EvaluationJudgeDispatch, _EvaluationJudgeNotConfiguredError
    from app.domain.evaluation.models import EvidenceSnapshot
    calls = []

    class Judge:
        async def evaluate(self, definition, snapshot):
            calls.append(snapshot)

    async def fingerprint(): return "new-configuration"

    dispatch = _EvaluationJudgeDispatch({"agent.answer_correctness": Judge()}, configuration_fingerprint=fingerprint)
    with pytest.raises(_EvaluationJudgeNotConfiguredError):
        await dispatch.evaluate(SimpleNamespace(metric_name="agent.answer_correctness"),
                                EvidenceSnapshot(content={"execution_fingerprint":"old-configuration"}, redaction_version="test"))
    assert not calls
