"""T20: main.py evaluation 子域组合根装配测试。

覆盖 plan T20 S1：
- B 类总闸 evaluation_enabled=False：全部 evaluation 字段 None、业务
  服务无捕获注入副作用、producer 路由不注册（404）、create_app/E2E 兼容；
- B 类总闸 True：store/config/capture/worker/delivery/deletion/gauge
  全部装配（类型断言）、捕获三 hook 注入、session post-delete handler
  注册、producer 路由注册（认证 401 + max_bytes 锁定 settings）；
- lifespan 启动/停止（TestClient 上下文）；装配部分配置 fail-fast；
- C 类并发基线经装配进入 worker；B 类上限约束拒绝越界配置。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.application.evaluation_capture import EvaluationCaptureService
from app.application.evaluation_config import EvaluationConfigService
from app.application.evaluation_deletion import EvaluationDeletionService
from app.application.evaluation_service import EvaluationService
from app.application.evaluation_worker import EvaluationWorker
from app.config import Settings
from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from app.main import (
    build_application_services,
    build_evaluation_services,
    create_app,
)


def _settings(
    tmp_path: Path,
    *,
    evaluation_enabled: bool = False,
    **overrides,
) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    kwargs = dict(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=False,
        feishu_enabled=False,
        artifacts_enabled=False,
        evaluation_enabled=evaluation_enabled,
        evaluation_ledger_path=str(tmp_path / "evaluations.db"),
        evaluation_snapshots_root=str(tmp_path / "evaluation_snapshots"),
        evaluation_producer_token="tok-n-kb",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.mark.asyncio
async def test_scenario_artifact_record_reads_bounded_actual_content():
    from types import SimpleNamespace
    from app.main import _scenario_artifact_record

    art = SimpleNamespace(id="a1", name="report.md", kind="markdown", labels=["origin_ref=workspace:report.md"])
    class Reader:
        async def get_content(self, artifact_id):
            assert artifact_id == "a1"
            return "# 实际报告".encode(), art
    row = await _scenario_artifact_record(Reader(), art)
    assert row["content"] == "# 实际报告"
    assert row["origin_ref"] == "workspace:report.md"


@pytest.mark.asyncio
async def test_scenario_memory_intent_precedes_write_and_failure_is_recoverable():
    from app.main import _scenario_prepare_memory
    events = []
    class Ledger:
        async def register_intent(self, intent): events.append("intent")
        async def confirm_owned(self, *args, **kwargs): events.append("owned")
        async def mark_unknown(self, *args, **kwargs): events.append("unknown")
    class Binder:
        def prepare(self, *args, **kwargs):
            events.append("write")
            assert events[0] == "intent"
            raise RuntimeError("prepare disconnected")
    with pytest.raises(RuntimeError):
        await _scenario_prepare_memory(Binder(), Ledger(), "session-1", "mem0-e2e-test", "合成事实",
                                       {"execution_id": "exec1", "case_id": "memory-case"})
    assert events == ["intent", "write", "unknown"]


@pytest.mark.asyncio
async def test_scenario_worker_refs_use_agent_registry_not_task_run_integer():
    from app.main import _scenario_task_agent_runs
    class Registry:
        def list_for_session(self, session_id):
            assert session_id == "worker-session"
            return [{"id": "agent-worker-1", "session_id": session_id, "task_run_id": 7},
                    {"id": "unrelated", "session_id": session_id, "task_run_id": 8}]
    refs = await _scenario_task_agent_runs(Registry(), "worker-session", {7})
    assert refs == {7: [{"run_id": "agent-worker-1", "session_id": "worker-session"}]}


# ---------------------------------------------------------------------------
# 禁用：无副作用
# ---------------------------------------------------------------------------


def test_disabled_leaves_evaluation_fields_none(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.evaluation_config_service is None
    assert services.evaluation_store is None
    assert services.evaluation_ledger_service is None
    assert services.evaluation_worker is None
    assert services.evaluation_delivery_worker is None
    assert services.evaluation_capture is None
    assert services.evaluation_deletion_service is None
    assert services.evaluation_gauge_holder is None


def test_disabled_no_capture_injection_side_effects(tmp_path: Path):
    """禁用时业务服务的 evaluation 字段缺省（Judge 调用 0 的前提）."""
    services = build_application_services(_settings(tmp_path))
    assert services.tool_service.evaluation_capture is None
    assert services.knowledge_service.evaluation_capture is None
    # session post-delete 列表可能含其它子系统（schedule）处理器；
    # 评估删除处理器未注册
    assert all(
        not isinstance(
            getattr(handler, "__self__", None), EvaluationDeletionService
        )
        for handler in services.session_service._on_session_deleted_handlers
    )


def test_disabled_producer_route_not_registered(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    resp = client.post(
        "/evaluation/producer/messages", json={"type": "unknown"}
    )
    assert resp.status_code == 404


def test_disabled_app_boots_e2e_compatible(tmp_path: Path):
    """禁用路径 create_app 正常启动（E2E 兼容）."""
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/health").status_code in (200, 503)


# ---------------------------------------------------------------------------
# 启用：全量装配
# ---------------------------------------------------------------------------


def test_enabled_assembles_evaluation_subsystem(tmp_path: Path):
    services = build_application_services(_settings(tmp_path, evaluation_enabled=True))
    assert isinstance(services.evaluation_store, SQLiteEvaluationStore)
    assert services.evaluation_store.path == Path(tmp_path / "evaluations.db")
    assert isinstance(services.evaluation_ledger_service, EvaluationService)
    assert services.evaluation_ledger_service._enabled is True
    assert isinstance(services.evaluation_worker, EvaluationWorker)
    assert isinstance(services.evaluation_capture, EvaluationCaptureService)
    assert isinstance(services.evaluation_deletion_service, EvaluationDeletionService)
    assert isinstance(services.evaluation_config_service, EvaluationConfigService)
    assert services.evaluation_gauge_holder is not None
    # Langfuse 未配置（B 类四字段空）-> 不装配投递 worker，worker 无 sink
    assert services.evaluation_delivery_worker is None
    assert services.evaluation_worker._sinks == ()


def test_enabled_wires_capture_hooks(tmp_path: Path):
    services = build_application_services(_settings(tmp_path, evaluation_enabled=True))
    capture = services.evaluation_capture
    assert services.tool_service.evaluation_capture is capture
    assert services.knowledge_service.evaluation_capture is capture
    # post-delete 列表含其它子系统处理器时按归属断言
    assert services.evaluation_deletion_service.handle_session_deleted in (
        services.session_service._on_session_deleted_handlers
    )


def test_enabled_policy_provider_reads_c_config(tmp_path: Path):
    """C 类 sampling/enabled 经 policy_provider 逐次读取（热重载接缝）."""
    enabled_dir = tmp_path / "e1"
    enabled_dir.mkdir()
    services = build_application_services(
        _settings(
            enabled_dir,
            evaluation_enabled=True,
            evaluation_capture_enabled=True,
            evaluation_sampling_rate=0.5,
            evaluation_failure_sampling_rate=0.1,
        )
    )
    policy = services.evaluation_capture._current_policy()
    assert policy is not None
    assert policy.rate == 0.5
    assert policy.failure_rate == 0.1
    # C enabled=False（DB 覆盖前基线）-> None（捕获总闸关闭）
    disabled_dir = tmp_path / "d2"
    disabled_dir.mkdir()
    disabled = build_application_services(
        _settings(disabled_dir, evaluation_enabled=True)
    )
    assert disabled.evaluation_capture._current_policy() is None


def test_enabled_worker_concurrency_from_settings(tmp_path: Path):
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_worker_concurrency=3,
            evaluation_worker_max_concurrency=8,
        )
    )
    assert services.evaluation_worker._llm_concurrency == 3


def test_enabled_worker_concurrency_above_legacy_cap(tmp_path: Path):
    """Mi2：数值上限由 domain 单源（C <= B_max）；B_max=16 时 C=6 合法
    装配，worker 不再有独立 4 上限缝隙。"""
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_worker_concurrency=6,
            evaluation_worker_max_concurrency=16,
        )
    )
    assert services.evaluation_worker._llm_concurrency == 6


def test_enabled_langfuse_external_deleter_registered(tmp_path: Path):
    """M2：langfuse 就绪时 sink 注册为 langfuse 外部删除端（同一实例
    供投递 worker 与删除编排复用），并满足 ExternalDeleter 端口形状。"""
    from app.application.evaluation_deletion import ExternalDeleter
    from app.infrastructure.evaluation.langfuse_sink import LangfuseScoreSink

    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_langfuse_base_url="https://langfuse.example.com",
            evaluation_langfuse_public_key="pk",
            evaluation_langfuse_secret_key="sk",
            evaluation_langfuse_project="proj",
        )
    )
    sink = services.evaluation_deletion_service._external_deleters.get(
        "langfuse"
    )
    assert isinstance(sink, LangfuseScoreSink)
    assert isinstance(sink, ExternalDeleter)
    # 同一实例复用：投递 worker 的 langfuse sink 即删除端
    assert services.evaluation_delivery_worker._sinks["langfuse"] is sink


def test_enabled_without_langfuse_no_external_deleter(tmp_path: Path):
    """M2：langfuse 关闭时不注册删除端（删除任务如实保留 pending 等
    删除端注册，不宣称外部已删除）。"""
    services = build_application_services(
        _settings(tmp_path, evaluation_enabled=True)
    )
    assert services.evaluation_deletion_service._external_deleters == {}


def test_enabled_capacity_limits_from_settings(tmp_path: Path):
    """Mi1：待执行作业/证据总容量经 B 类 env 传入（默认 10000/256MiB）。"""
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_max_pending_jobs=123,
            evaluation_max_evidence_bytes=4096,
        )
    )
    assert services.evaluation_ledger_service._max_pending_jobs == 123
    assert services.evaluation_ledger_service._max_evidence_bytes == 4096


async def test_enabled_submit_sample_executes_real_capacity_probe(
    tmp_path: Path,
):
    """M2（C1 回归锁定）：经装配链路执行 submit_sample——真实容量探针
    （asyncio.to_thread 同步探针）满足 Awaitable 契约。注入同步探针时
    本用例以 TypeError 失败（捕获/producer 提交链路全断）。"""
    from app.application.evaluation_service import SubmitSampleRequest
    from app.domain.evaluation.models import EvaluationTarget

    services = build_application_services(
        _settings(tmp_path, evaluation_enabled=True)
    )
    request = SubmitSampleRequest(
        producer_event_id="evt-1",
        service="n-kb",
        target=EvaluationTarget(
            service="n-kb",
            kind="retrieval",
            subject_id="ret-1",
            subject_revision="rev-1",
            retrieval_id="ret-1",
            trace_id=None,
        ),
    )
    result = await services.evaluation_ledger_service.submit_sample(request)
    assert result.sample_id
    assert result.job_ids == ()


async def test_enabled_submit_sample_capacity_503_path(tmp_path: Path):
    """M2：容量 503 路径经装配链路实测——证据目录已有内容且上限为 1
    字节时探针返回超限，EvaluationCapacityError（路由映射 503）。"""
    import pytest as _pytest

    from app.application.evaluation_service import (
        EvaluationCapacityError,
        SubmitSampleRequest,
    )
    from app.domain.evaluation.models import EvaluationTarget

    snapshots_root = tmp_path / "evaluation_snapshots"
    snapshots_root.mkdir()
    (snapshots_root / "snap-1.json").write_text("{}", encoding="utf-8")
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_max_evidence_bytes=1,
        )
    )
    request = SubmitSampleRequest(
        producer_event_id="evt-1",
        service="n-kb",
        target=EvaluationTarget(
            service="n-kb",
            kind="retrieval",
            subject_id="ret-1",
            subject_revision="rev-1",
            retrieval_id="ret-1",
            trace_id=None,
        ),
    )
    with _pytest.raises(EvaluationCapacityError, match="evidence capacity"):
        await services.evaluation_ledger_service.submit_sample(request)


def test_enabled_review_service_and_management_routes(tmp_path: Path):
    """T21：复核/导出服务装配 + 管理面路由注册。

    - EvaluationReviewService 装配（datasets_root 相对路径锚定
      workspace_root，与 artifacts_root 同款解析）；
    - create_app 注册 /chat/evaluations/*（config GET 200）；
    - B 类总闸关闭：review service None、管理面路由不注册（404）。
    """
    services = build_application_services(
        _settings(tmp_path, evaluation_enabled=True)
    )
    from app.application.evaluation_review import EvaluationReviewService

    assert isinstance(services.evaluation_review_service, EvaluationReviewService)
    assert services.evaluation_review_service._datasets_root == Path(
        tmp_path / "locals" / "evaluation-datasets"
    )
    assert services.evaluation_review_service._store is services.evaluation_store

    app = create_app(_settings(tmp_path, evaluation_enabled=True))
    client = TestClient(app)
    resp = client.get("/chat/evaluations/config")
    assert resp.status_code == 200
    assert "evaluation_enabled" in resp.json()["config"]

    disabled_app = create_app(_settings(tmp_path))
    disabled_client = TestClient(disabled_app)
    assert build_application_services(
        _settings(tmp_path)
    ).evaluation_review_service is None
    assert (
        disabled_client.get("/chat/evaluations/config").status_code == 404
    )
    # Task 9 审查修复：总闸关闭下新路径同样 404（评估器/标注/分析，
    # 对齐上方 /config 既有断言模式）
    for path in (
        "/chat/evaluations/evaluators",
        "/chat/evaluations/evaluators/def/v1",
        "/chat/evaluations/annotations",
        "/chat/evaluations/analytics",
    ):
        assert disabled_client.get(path).status_code == 404


def test_enabled_feedback_helpful_definition_registered_at_startup(tmp_path: Path):
    """C1（质量修复轮）：feedback.helpful 定义启动期注册接线。

    - lifespan 启动期（TestClient 上下文）幂等注册 feedback.helpful@v1；
    - 注册后 submit_feedback('feedback.helpful','v1') 不再 404
      （EvaluationNotFoundError 即路由 404 的来源）；
    - 重复启动（第二次 lifespan）同版本同内容幂等重放，不 409。
    """
    from app.application.evaluation_service import (
        EvaluationTarget,
        SubmitFeedbackRequest,
    )

    settings = _settings(tmp_path, evaluation_enabled=True)
    app = create_app(settings)
    with TestClient(app):
        services = build_application_services(settings)
        request = SubmitFeedbackRequest(
            target=EvaluationTarget(
                service="chat",
                kind="agent_run",
                subject_id="run-c1",
                subject_revision="rev-1",
            ),
            definition_id="feedback.helpful",
            definition_version="v1",
            value=True,
        )
        result = asyncio.run(
            services.evaluation_ledger_service.submit_feedback(
                "dashboard", request
            )
        )
        assert result.revision == 1

    # 幂等重放：第二次启动不因同版本定义已存在而失败
    with TestClient(create_app(settings)):
        pass


def test_enabled_producer_route_registered(tmp_path: Path):
    app = create_app(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_producer_max_bytes=64,
        )
    )
    client = TestClient(app)
    # B 类凭据绑定：匿名 401（路由已注册但认证失败）
    resp = client.post(
        "/evaluation/producer/messages",
        json={"type": "evaluation.sample.v1"},
    )
    assert resp.status_code == 401
    # max_message_bytes 锁定 settings 值（T17 N5 装配待办）
    resp = client.post(
        "/evaluation/producer/messages",
        content=b"x" * 100,
        headers={
            "Authorization": "Bearer tok-n-kb",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 413


def test_lifespan_starts_and_stops_evaluation_workers(tmp_path: Path):
    """TestClient 上下文触发 lifespan：start/stop 不抛错即通过（worker
    内建有界停止；对齐 task wiring 的 lifespan 测试口径）."""
    app = create_app(_settings(tmp_path, evaluation_enabled=True))
    with TestClient(app) as client:
        assert client.get("/health").status_code in (200, 503)


def test_worker_start_stop_idempotent(tmp_path: Path):
    """生命周期（Phase 5 扫描修复：补断言）：enabled 装配下 worker 与
    capture 必须存在（静默跳过会掩盖装配回归），双 start/stop 幂等且
    worker 循环任务有界回收。"""
    services = build_application_services(
        _settings(tmp_path, evaluation_enabled=True)
    )
    assert services.evaluation_worker is not None
    assert services.evaluation_capture is not None

    async def _run():
        await services.evaluation_worker.start()
        await services.evaluation_worker.start()
        assert services.evaluation_worker._loop_task is not None
        loop_task = services.evaluation_worker._loop_task
        await services.evaluation_worker.stop()
        await services.evaluation_worker.stop()
        # 有界回收证据：循环任务已终结、句柄清空
        assert services.evaluation_worker._loop_task is None
        assert loop_task.done()
        await services.evaluation_capture.start()
        await services.evaluation_capture.start()
        await services.evaluation_capture.stop()
        await services.evaluation_capture.stop()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# fail-fast：部分配置 / 越界配置
# ---------------------------------------------------------------------------


def test_partial_judge_config_fails_fast(tmp_path: Path):
    with pytest.raises(ValueError, match="evaluation_judge_provider"):
        build_application_services(
            _settings(
                tmp_path,
                evaluation_enabled=True,
                evaluation_judge_provider_base_url="https://judge.example.com",
            )
        )


def test_partial_langfuse_config_fails_fast(tmp_path: Path):
    with pytest.raises(ValueError, match="evaluation_langfuse"):
        build_application_services(
            _settings(
                tmp_path,
                evaluation_enabled=True,
                evaluation_langfuse_base_url="https://langfuse.example.com",
            )
        )


def test_full_langfuse_config_assembles_delivery_worker(tmp_path: Path):
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_langfuse_base_url="https://langfuse.example.com",
            evaluation_langfuse_public_key="pk",
            evaluation_langfuse_secret_key="sk",
            evaluation_langfuse_project="proj",
        )
    )
    assert services.evaluation_delivery_worker is not None
    assert services.evaluation_worker._sinks == (
        {"sink": "langfuse", "projection_version": "v1"},
    )


def test_disabled_skips_subsystem_even_with_partial_judge_config(tmp_path: Path):
    """禁用时部分配置不触发 fail-fast（子域整体未装配）。"""
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=False,
            evaluation_judge_provider_base_url="https://judge.example.com",
        )
    )
    assert services.evaluation_worker is None


def test_enabled_delivery_worker_linkability_wired(tmp_path: Path):
    """T22 收口（T20 遗留接缝）：投递 worker 注入真实可关联状态——
    sampled 恒真（账本 sample 即选中样本），otel_enabled 取 B 类
    settings；缺省 fail-safe 全 unlinked 会让 Score 永不出站。"""
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            otel_enabled=False,
            evaluation_langfuse_base_url="http://n-langfuse-web:3000",
            evaluation_langfuse_public_key="pk-e2e",
            evaluation_langfuse_secret_key="sk-e2e",
            evaluation_langfuse_project="n-agent-local-project",
        )
    )
    provider = services.evaluation_delivery_worker._linkability_provider
    context = provider()
    assert context == {"otel_enabled": False, "sampled": True}
    assert services.evaluation_langfuse_sink is not None


def test_cli_runtime_wires_experiment_runner(tmp_path: Path):
    """T22：build_evaluation_services 接线 experiment_runner（T20 延期项
    收口）。

    - KB 启用时 retrieval 目标经 CompositeTargetRunner→KBDirectTargetRunner
      直调；agent_run 未接线（应用全图组合，逐项 target_unwired 上报）；
    - runner 复用同一账本 store 与证据目录（B 类 settings）。
    """
    from app.application.evaluation_experiment import EvaluationExperimentRunner

    services = build_evaluation_services(
        _settings(
            tmp_path,
            kb_enabled=True,
            kb_base_url="http://n-kb:8100",
        )
    )
    assert isinstance(services.experiment_runner, EvaluationExperimentRunner)
    composite = services.experiment_runner._target_runner
    assert "retrieval" in composite._runners
    assert "agent_run" not in composite._runners
    assert services.experiment_runner._ledger is services.store


def test_cli_runtime_unwired_target_reports_transport_error(tmp_path: Path):
    """agent_run 目标未接线：CompositeTargetRunner 返回 transport_error
    （error_type=target_unwired），不伪造成功，也不 fail-fast 整个实验。"""
    import asyncio

    from app.domain.evaluation.models import EvaluationTarget
    from app.infrastructure.evaluation.target_runner import (
        CompositeTargetRunner,
    )

    runner = CompositeTargetRunner(runners={})
    target = EvaluationTarget(
        service="n-agent",
        kind="agent_run",
        subject_id="s",
        subject_revision="rev-1",
    )
    result = asyncio.run(runner.run_target(target, {"turns": []}))
    assert result["status"] == "transport_error"
    assert result["error_type"] == "target_unwired"


# ---------------------------------------------------------------------------
# #3 agent_run 目标 HTTP 接线 / #13 OTel sink 投递装配
# ---------------------------------------------------------------------------


def test_cli_runtime_wires_agent_http_runner(tmp_path: Path):
    """#3：evaluation_agent_base_url 非空时 agent_run 目标经
    AgentHTTPTargetRunner 调运行中的 N-Agent 服务。"""
    from app.application.evaluation_experiment import EvaluationExperimentRunner
    from app.infrastructure.evaluation.target_runner import (
        AgentHTTPTargetRunner,
    )

    services = build_evaluation_services(
        _settings(
            tmp_path,
            evaluation_agent_base_url="http://n-agent:8000",
            provider_model="test-model",
        )
    )
    assert isinstance(services.experiment_runner, EvaluationExperimentRunner)
    composite = services.experiment_runner._target_runner
    agent_runner = composite._runners["agent_run"]
    assert isinstance(agent_runner, AgentHTTPTargetRunner)
    assert agent_runner._base_url == "http://n-agent:8000"
    assert agent_runner._model == "test-model"


def test_delivery_worker_assembles_otel_sink(tmp_path: Path):
    """#13：otel_enabled 时投递 worker 注册 otel sink（与 langfuse 并存）；
    仅 otel 启用（langfuse 未配置）也成立。"""
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            otel_enabled=True,
        )
    )
    assert services.evaluation_delivery_worker is not None
    assert {"sink": "otel", "projection_version": "v1"} in (
        services.evaluation_worker._sinks
    )
    otel_sink = services.evaluation_delivery_worker._sinks["otel"]
    from app.infrastructure.evaluation.otel_sink import OtelScoreSink

    assert isinstance(otel_sink, OtelScoreSink)


def test_langfuse_and_otel_sinks_coexist(tmp_path: Path):
    """langfuse 与 otel 双出口并存：两个 sink spec 都注册。"""
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            otel_enabled=True,
            evaluation_langfuse_base_url="https://langfuse.example.com",
            evaluation_langfuse_public_key="pk",
            evaluation_langfuse_secret_key="sk",
            evaluation_langfuse_project="proj",
        )
    )
    assert services.evaluation_delivery_worker is not None
    assert services.evaluation_worker._sinks == (
        {"sink": "langfuse", "projection_version": "v1"},
        {"sink": "otel", "projection_version": "v1"},
    )


async def test_evidence_sweep_loop_runs_and_isolates_failures():
    """#18：sweep 循环周期执行、单轮失败隔离为 warning 不中断循环、
    取消即停止（lifespan 语义）。"""
    import contextlib

    from app.main import _evaluation_evidence_sweep_loop

    class FakeSweepStore:
        def __init__(self):
            self.calls = 0

        async def sweep(self):
            self.calls += 1
            if self.calls >= 2:
                raise RuntimeError("boom")
            return {"deleted_files": 3}

    store = FakeSweepStore()
    task = asyncio.create_task(
        _evaluation_evidence_sweep_loop(store, interval_seconds=0.01)
    )
    await asyncio.sleep(0.08)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert store.calls >= 2  # 第 2 轮失败后循环仍继续跑


def test_judge_without_call_ceiling_fails_fast(tmp_path: Path):
    """#12：Judge 已配置但日调用上限未设（C 类 0）拒绝启动——禁止无上限
    的自动 Judge 调用；设置正整数后装配成功。"""
    judge_settings = dict(
        evaluation_judge_provider_base_url="https://judge.example.com",
        evaluation_judge_provider_api_key="sk",
        evaluation_judge_provider_model="judge-model",
    )
    with pytest.raises(ValueError, match="evaluation_daily_call_ceiling"):
        build_application_services(_settings(tmp_path, evaluation_enabled=True, **judge_settings))
    # C 类配置显式设置上限后装配成功
    services = build_application_services(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_daily_call_ceiling=100,
            **judge_settings,
        )
    )
    assert services.evaluation_worker is not None


@pytest.mark.parametrize('enabled', [False, True])
def test_evaluation_availability_and_shell_routes(tmp_path: Path, enabled: bool):
    client = TestClient(create_app(_settings(tmp_path, evaluation_enabled=enabled)))
    response = client.get('/chat/evaluations/status')
    assert response.status_code == 200
    assert response.json() == {'enabled': enabled}
    assert response.headers['cache-control'] == 'no-store'
    for path in ('/evaluations', '/evaluations/datasets', '/evaluations/evaluators', '/evaluations/experiments', '/evaluations/results', '/evaluations/annotations', '/evaluations/review'):
        shell = client.get(path)
        assert shell.status_code == 200
        assert 'id="tab-evaluations"' in shell.text
    if not enabled:
        assert client.get('/chat/evaluations/overview').status_code == 404
        assert not (tmp_path / 'evaluations.db').exists()


# ---------------------------------------------------------------------------
# T8: 组合根装配、lifespan 时序与 usage 一次性回填（spec 4.3）
# ---------------------------------------------------------------------------


def test_enabled_assembles_workbench_services(tmp_path: Path):
    """T8：评估工作台三服务装配进 HTTP 组合根（ApplicationServices 可选
    新字段，默认 None）。

    - admin：judge_ready 回调读 judge provider B 类配置状态；
    - annotation：注入 evidence_reader / agent_run_lookup / tombstone_reader；
    - analytics：store 只读装配；
    - EvaluationServices 是 CLI 专用容器，不承载这些 HTTP 服务；
    - B 类总闸关闭：三字段 None。"""
    from app.application.evaluation_admin import EvaluationAdminService
    from app.application.evaluation_analytics import (
        EvaluationAnalyticsService,
    )
    from app.application.evaluation_annotation import AnnotationService

    services = build_application_services(
        _settings(tmp_path, evaluation_enabled=True)
    )
    assert isinstance(services.evaluation_admin_service, EvaluationAdminService)
    assert isinstance(
        services.evaluation_annotation_service, AnnotationService
    )
    assert isinstance(
        services.evaluation_analytics_service, EvaluationAnalyticsService
    )
    # Judge 未配置（B 类三字段空）-> judge_ready False；配置齐全 -> True
    assert services.evaluation_admin_service._judge_ready() is False
    judge_dir = tmp_path / "judge"
    judge_dir.mkdir()
    ready = build_application_services(
        _settings(
            judge_dir,
            evaluation_enabled=True,
            evaluation_judge_provider_base_url="https://judge.example.com",
            evaluation_judge_provider_api_key="sk",
            evaluation_judge_provider_model="judge-model",
            evaluation_daily_call_ceiling=100,
        )
    )
    assert ready.evaluation_admin_service._judge_ready() is True
    # annotation 读侧接缝注入（证据 / 真实 Run / tombstone 门禁）
    assert services.evaluation_annotation_service._evidence_reader is not None
    assert (
        services.evaluation_annotation_service._agent_run_lookup is not None
    )
    assert (
        services.evaluation_annotation_service._tombstone_reader is not None
    )
    # CLI 专用容器不承载 HTTP 组合根服务
    cli = build_evaluation_services(
        _settings(tmp_path, evaluation_enabled=True)
    )
    assert not hasattr(cli, "evaluation_admin_service")
    assert not hasattr(cli, "evaluation_annotation_service")
    assert not hasattr(cli, "evaluation_analytics_service")
    # B 类总闸关闭：三字段 None
    disabled_dir = tmp_path / "disabled"
    disabled_dir.mkdir()
    disabled = build_application_services(_settings(disabled_dir))
    assert disabled.evaluation_admin_service is None
    assert disabled.evaluation_annotation_service is None
    assert disabled.evaluation_analytics_service is None


def test_lifespan_evaluation_start_order(tmp_path: Path, monkeypatch):
    """T8：lifespan 启动顺序——delivery.start -> evaluation_worker.start ->
    deletion.start -> 注册预设（feedback.helpful + presets）-> usage 回填 ->
    capture.start -> gauge 刷新 -> evidence sweep。

    修正现状"注册预设与回填在 capture.start 之后"：注册与回填须先于
    capture.start 完成（spec 4.3 回填时序），捕获队列启动后产生的样本
    才能按已启用定义展开线上作业。"""
    import app.main as main_module
    from app.application.evaluation_admin import EvaluationAdminService
    from app.application.evaluation_capture import EvaluationCaptureService
    from app.application.evaluation_deletion import EvaluationDeletionService
    from app.application.evaluation_worker import EvaluationWorker
    from app.infrastructure.evaluation.delivery_worker import (
        EvaluationDeliveryWorker,
    )

    events: list[str] = []

    def _recorder(name):
        async def _record(self, *args, **kwargs):
            events.append(name)

        return _record

    monkeypatch.setattr(
        EvaluationDeliveryWorker, "start", _recorder("delivery.start")
    )
    monkeypatch.setattr(
        EvaluationWorker, "start", _recorder("evaluation_worker.start")
    )
    monkeypatch.setattr(
        EvaluationDeletionService, "start", _recorder("deletion.start")
    )
    monkeypatch.setattr(
        EvaluationService,
        "register_feedback_helpful_definition",
        _recorder("register.feedback_helpful"),
    )
    monkeypatch.setattr(
        EvaluationService,
        "register_preset_definitions",
        _recorder("register.presets"),
    )
    monkeypatch.setattr(
        EvaluationAdminService,
        "backfill_definition_usage",
        _recorder("usage.backfill"),
    )
    monkeypatch.setattr(
        EvaluationCaptureService, "start", _recorder("capture.start")
    )

    async def _fake_gauge_loop(*args, **kwargs):
        events.append("gauge.refresh")

    async def _fake_sweep_loop(*args, **kwargs):
        events.append("evidence.sweep")

    monkeypatch.setattr(
        main_module, "_evaluation_gauge_refresh_loop", _fake_gauge_loop
    )
    monkeypatch.setattr(
        main_module, "_evaluation_evidence_sweep_loop", _fake_sweep_loop
    )

    app = create_app(
        _settings(
            tmp_path,
            evaluation_enabled=True,
            evaluation_langfuse_base_url="https://langfuse.example.com",
            evaluation_langfuse_public_key="pk",
            evaluation_langfuse_secret_key="sk",
            evaluation_langfuse_project="proj",
        )
    )
    with TestClient(app):
        pass
    assert events == [
        "delivery.start",
        "evaluation_worker.start",
        "deletion.start",
        "register.feedback_helpful",
        "register.presets",
        "usage.backfill",
        "capture.start",
        "gauge.refresh",
        "evidence.sweep",
    ]


def test_register_failure_skips_backfill_until_retry(tmp_path: Path, monkeypatch):
    """T8（注册失败隔离回填）：启动期注册抛异常（瞬态故障）时本次回填
    被跳过——账本不提交 version=1001 标记（否则下次启动注册重试成功后
    回填被标记挡住，预设自动定义永久停用）；下次启动注册重试成功后回填
    正常执行，自动定义启用。回填自身失败不阻塞启动的既有语义不变。"""
    import sqlite3

    from app.application.evaluation_service import EvaluationService

    settings = _settings(tmp_path, evaluation_enabled=True)

    async def _raise(self, *args, **kwargs):
        raise RuntimeError("transient register failure")

    monkeypatch.setattr(
        EvaluationService, "register_preset_definitions", _raise
    )
    # 第一次启动：注册失败 -> 回填跳过（账本无 1001 标记）
    with TestClient(create_app(settings)):
        pass
    conn = sqlite3.connect(tmp_path / "evaluations.db")
    try:
        marker = conn.execute(
            "SELECT 1 FROM evaluation_schema_migrations "
            "WHERE version = 1001"
        ).fetchone()
    finally:
        conn.close()
    assert marker is None
    # 回填未执行：human 定义 usage 行存在且为 disabled（精确断言，
    # 避免 rows 为空时 all() 真空通过）
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    rows = asyncio.run(store.list_definitions_with_usage())
    by_key = {(r["definition_id"], r["version"]): r for r in rows}
    assert ("feedback.helpful", "v1") in by_key, "注册应产生 human 定义"
    assert (
        by_key[("feedback.helpful", "v1")]["usage"]["online_enabled"] is False
    )

    # 第二次启动：注册重试成功 -> 回填执行，预设自动定义启用
    monkeypatch.undo()
    with TestClient(create_app(settings)):
        pass
    rows = asyncio.run(store.list_definitions_with_usage())
    by_key = {(r["definition_id"], r["version"]): r for r in rows}
    assert ("kb.recall_at_5", "v1") in by_key, "重试注册应产生预设定义"
    assert by_key[("kb.recall_at_5", "v1")]["usage"]["online_enabled"] is True
    assert by_key[("feedback.helpful", "v1")]["usage"]["online_enabled"] is False


def test_startup_backfill_enables_automatic_definitions(tmp_path: Path):
    """T8（spec 4.3 回填时序，空库首次启动）：预设注册产生的 disabled
    自动 usage 在本次一次性回填中启用；human（feedback.helpful）永不
    启用；回填标记记录进 migrations 账本。"""
    import sqlite3

    settings = _settings(tmp_path, evaluation_enabled=True)
    with TestClient(create_app(settings)):
        pass
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    rows = asyncio.run(store.list_definitions_with_usage())
    assert rows, "启动注册应产生定义"
    for row in rows:
        if row["method"] == "human":
            assert row["definition_id"] == "feedback.helpful"
            assert row["usage"]["online_enabled"] is False
        else:
            assert row["usage"] is not None
            assert row["usage"]["online_enabled"] is True
    conn = sqlite3.connect(tmp_path / "evaluations.db")
    try:
        marker = conn.execute(
            "SELECT name FROM evaluation_schema_migrations "
            "WHERE version = 1001"
        ).fetchone()
    finally:
        conn.close()
    assert marker is not None


def test_startup_backfill_upgrades_legacy_ledger(tmp_path: Path):
    """T8（旧库升级）：定义已存在但无 usage 行时，回填仅对自动定义插入
    enabled=1 行；human 定义不插入 usage（usage=None，禁启用）。"""
    from app.application.evaluation_service import (
        EvaluationService,
        definition_to_mapping,
    )

    db_path = str(tmp_path / "evaluations.db")
    legacy = SQLiteEvaluationStore(db_path)
    # 模拟 T5 之前旧库：定义存在、无 usage 行（与生产同源 definition_json）
    for definition in EvaluationService.preset_definitions(None):
        if definition.method == "human":
            continue
        asyncio.run(
            legacy.save_definition(definition_to_mapping(definition))
        )
    helpful = EvaluationService.helpful_definition_factory(
        "feedback.helpful", "v1"
    )
    asyncio.run(legacy.save_definition(definition_to_mapping(helpful)))

    with TestClient(create_app(_settings(tmp_path, evaluation_enabled=True))):
        pass
    store = SQLiteEvaluationStore(db_path)
    rows = asyncio.run(store.list_definitions_with_usage())
    by_key = {(r["definition_id"], r["version"]): r for r in rows}
    assert by_key[("kb.recall_at_5", "v1")]["usage"]["online_enabled"] is True
    assert by_key[("feedback.helpful", "v1")]["usage"] is None


def test_restart_backfill_preserves_disabled_usage(tmp_path: Path):
    """T8（重启跳过 + 用户状态保留）：回填标记后重启不再回填——用户
    停用（enabled=0）的自动定义不复活；标记之后新注册定义保持
    disabled（之后新注册版本始终 disabled）。"""
    from app.application.evaluation_service import EvaluationService
    from app.domain.evaluation.models import EvaluatorDefinition

    settings = _settings(tmp_path, evaluation_enabled=True)
    with TestClient(create_app(settings)):
        pass
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    # 用户显式停用一个自动定义（经 admin service 同款 updated_by）
    asyncio.run(
        store.upsert_definition_usage(
            "agent.task_success",
            "v1",
            online_enabled=False,
            expected_revision=1,
            updated_by="n-agent",
        )
    )
    # 标记之后新注册的自动定义（disabled 起步）
    custom = EvaluatorDefinition(
        definition_id="custom.metric",
        version="v1",
        metric_name="custom.metric",
        target_kind="retrieval",
        method="deterministic",
        required_fields=(),
        evidence_policy="snapshot",
        output_type="number",
        range_or_categories=(0.0, 1.0),
        direction="higher_is_better",
        material_hash={},
        judge_model=None,
        judge_params={},
        timeout_seconds=30,
        budget=None,
        ci_admissible=False,
    )
    asyncio.run(EvaluationService(store).register_definition(custom))

    with TestClient(create_app(settings)):
        pass
    rows = asyncio.run(store.list_definitions_with_usage())
    by_key = {(r["definition_id"], r["version"]): r for r in rows}
    # 用户停用不复活（重启回填不触碰 enabled=0 用户状态）
    assert by_key[("agent.task_success", "v1")]["usage"]["online_enabled"] is False
    assert by_key[("agent.task_success", "v1")]["usage"]["updated_by"] == "n-agent"
    # 标记之后注册的定义保持 disabled
    assert by_key[("custom.metric", "v1")]["usage"]["online_enabled"] is False
    # 其余自动定义仍启用
    assert by_key[("kb.recall_at_5", "v1")]["usage"]["online_enabled"] is True


def test_backfill_once_rolls_back_on_failure(tmp_path: Path, monkeypatch):
    """T8（失败回滚）：检查标记、回填与记录标记同事务——标记写入失败
    时回填整体回滚（usage 行保持原状、账本无标记）；下次启动重试成功。
    日志不能充当持久标记，账本行才是幂等依据。"""
    import sqlite3

    import app.infrastructure.evaluation.store as evaluation_store_module
    from app.application.evaluation_service import EvaluationService

    db_path = str(tmp_path / "evaluations.db")
    store = SQLiteEvaluationStore(db_path)
    service = EvaluationService(store)
    # 预设注册产生 disabled 的自动 usage（updated_by='system'）
    asyncio.run(service.register_preset_definitions(None))
    asyncio.run(service.register_feedback_helpful_definition())

    # 标记写入失败（name TEXT NOT NULL 拒绝 None）-> 整体回滚
    monkeypatch.setattr(evaluation_store_module, "_USAGE_BACKFILL_NAME", None)
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(
            store.backfill_definition_usage_once(
                updated_by="startup-migration"
            )
        )
    rows = asyncio.run(store.list_definitions_with_usage())
    for row in rows:
        if row["method"] == "human":
            continue
        assert row["usage"]["online_enabled"] is False
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM evaluation_schema_migrations "
                "WHERE version = 1001"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
    # 下次启动（标记写入恢复）重试成功：自动定义启用、human 不动
    monkeypatch.undo()
    inserted = asyncio.run(
        store.backfill_definition_usage_once(updated_by="startup-migration")
    )
    assert inserted > 0
    rows = asyncio.run(store.list_definitions_with_usage())
    for row in rows:
        if row["method"] == "human":
            assert row["usage"]["online_enabled"] is False
        else:
            assert row["usage"]["online_enabled"] is True
