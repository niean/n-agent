# app.interfaces.cli.commands.evaluate 用例测试（plan T14，spec 9.2）。
#
# TDD 严格 RED -> GREEN；本文件覆盖 plan T14 S1 的 CLI 要点：
# - evaluate 命令组注册在 app/interfaces/cli/main.py 的 build_parser 与
#   _DISPATCH 分发中（对齐 commands/task.py 的 run(args) -> int 模式）；
# - 子命令 run/replay/show/compare/retry-delivery 全部可解析、可分发；
# - 复用 Application 服务（经模块级 _load_evaluation_runtime 间接层注入，
#   测试 monkeypatch）；
# - 门禁四态 -> CLI 退出码 0/1/3/2；
# - --json 输出结构化结果（稳定字段名）；
# - runner 未接线时明确报告并以 infrastructure_error 退出码返回。
from __future__ import annotations

import json

import pytest

from app.interfaces.cli.commands import evaluate as evaluate_cmd
from app.interfaces.cli.main import _DISPATCH, build_parser, main
from app.application.evaluation_gate import (
    GATE_FAIL,
    GATE_PASS,
    INFRASTRUCTURE_ERROR,
    INCONCLUSIVE,
    GATE_STATE_EXIT_CODES,
    GateConclusion,
)


# ---------------------------------------------------------------------------
# 测试材料
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, report=None):
        self._report = report
        self.calls = []

    async def execute(self, request):
        self.calls.append(("execute", request))
        if self._report is None:
            raise RuntimeError("fake runner report missing")
        return self._report

    async def replay(self, request):
        self.calls.append(("replay", request))
        if self._report is None:
            raise RuntimeError("fake runner report missing")
        return self._report


class FakeStore:
    """账本分数收集的假端口：collect_experiment_scores 直接读 query_results_page。"""

    def __init__(self, rows=None):
        self._rows = rows or []

    async def query_results_page(self, **kwargs):
        return list(self._rows), None

    async def get_job(self, job_id):
        return {"sample_id": f"sample-{job_id}"}


class FakeService:
    def __init__(self, results=None, retry_result=None, error=None,
                 list_error=None):
        self._results = results or []
        self._retry_result = retry_result or {}
        self._error = error
        self._list_error = list_error
        self.calls = []

    async def list_results(self, **kwargs):
        self.calls.append(("list_results", kwargs))
        if self._list_error is not None:
            raise self._list_error
        from app.application.evaluation_service import ResultsPage

        return ResultsPage(items=tuple(self._results), next_cursor=None)

    async def retry_delivery(self, sink, result_id):
        self.calls.append(("retry_delivery", sink, result_id))
        if self._error is not None:
            raise self._error
        return dict(self._retry_result, sink=sink, result_id=result_id)


class FakeGate:
    def __init__(self, conclusion=None):
        self._conclusion = conclusion
        self.requests = []

    async def evaluate(self, request):
        import dataclasses

        self.requests.append(request)
        assert self._conclusion is not None
        # 投递状态单列由真实 gate 从请求透传，fake 保持同一契约
        return dataclasses.replace(
            self._conclusion,
            delivery_status_counts=dict(request.delivery_status_counts),
        )


class FakeRuntime:
    def __init__(self, *, runner=None, store=None, service=None, gate=None):
        self.experiment_runner = runner
        self.store = store
        self.service = service
        self.gate = gate


def _conclusion(state: str, experiment_id: str = "exp-1") -> GateConclusion:
    return GateConclusion(
        state=state,
        reasons=(),
        metrics=(),
        policy_version="default-v1",
        insufficient_data=state == INCONCLUSIVE,
        experiment_id=experiment_id,
        baseline_experiment_id=None,
        delivery_status_counts={},
    )


def _write_manifest(root) -> str:
    """写一份最小合法数据集 manifest，返回 --dataset 路径。"""
    from app.application.evaluation_dataset import compute_dataset_hash

    content = {
        "schema_version": "1",
        "dataset_id": "ds-cli",
        "dataset_version": "v1",
        "target_kind": "agent_run",
        "cases": [
            {"case_id": f"c{i:03d}", "input": {"message": "hi"},
             "target_kind": "agent_run", "expected_output": "ok",
             "allowed_side_effects": []}
            for i in range(2)
        ],
    }
    content["dataset_hash"] = compute_dataset_hash(content)
    dataset_dir = root / "ds-cli"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "manifest.json").write_text(
        json.dumps(content, ensure_ascii=False), encoding="utf-8"
    )
    return str(dataset_dir)


@pytest.fixture
def dataset_path(tmp_path):
    return _write_manifest(tmp_path)


@pytest.fixture
def policy_path(tmp_path):
    """默认门禁策略文件（spec 9.2 初始阈值）。"""
    path = tmp_path / "gate-policy.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "policy_version": "default-v1",
                "required_metrics": {
                    "agent.task_success": 0.90,
                    "kb.recall_at_5": 0.80,
                    "kb.ndcg_at_5": 0.75,
                    "rag.faithfulness": 0.90,
                },
                "coverage_metrics": [
                    "agent.task_success",
                    "kb.recall_at_5",
                    "kb.ndcg_at_5",
                    "rag.faithfulness",
                ],
                "safety_slices": [],
                "key_slices": [],
                "non_inferiority_margin": -0.02,
                "min_cases": 100,
                "min_paired_cases": 100,
                "min_key_slice_cases": 10,
                "bootstrap_replicates": 300,
                "bootstrap_seed": 20261111,
            }
        ),
        encoding="utf-8",
    )
    return str(path)


class _RuntimeInstaller:
    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch

    def install(self, **kwargs) -> FakeRuntime:
        rt = FakeRuntime(**kwargs)
        self._monkeypatch.setattr(
            evaluate_cmd, "_load_evaluation_runtime", lambda: rt
        )
        return rt


@pytest.fixture
def runtime(monkeypatch):
    return _RuntimeInstaller(monkeypatch)


# ---------------------------------------------------------------------------
# 注册与分发
# ---------------------------------------------------------------------------


def test_evaluate_registered_in_parser():
    parser = build_parser()
    args = parser.parse_args(["evaluate", "show", "--experiment-id", "e1"])
    assert args.command == "evaluate"
    assert args.evaluate_command == "show"
    assert args.experiment_id == "e1"


def test_all_subcommands_parse():
    parser = build_parser()
    run_args = parser.parse_args(
        [
            "evaluate", "run",
            "--dataset", "evaluations/datasets/x",
            "--name", "nightly",
            "--evaluator", "agent.task_success@v1",
        ]
    )
    assert run_args.evaluate_command == "run"
    assert run_args.replicates == 1
    replay_args = parser.parse_args(
        [
            "evaluate", "replay",
            "--name", "nightly",
            "--source-sample-ids", "s1,s2",
            "--evaluator", "agent.task_success@v1",
            "--dataset-id", "ds",
            "--dataset-version", "v1",
            "--dataset-hash", "a" * 64,
        ]
    )
    assert replay_args.evaluate_command == "replay"
    compare_args = parser.parse_args(
        [
            "evaluate", "compare",
            "--candidate-id", "cand",
            "--baseline-id", "base",
            "--policy", "gate-policy.json",
        ]
    )
    assert compare_args.evaluate_command == "compare"
    retry_args = parser.parse_args(
        [
            "evaluate", "retry-delivery",
            "--sink", "langfuse",
            "--result-id", "r1",
        ]
    )
    assert retry_args.evaluate_command == "retry-delivery"


def test_evaluate_registered_in_dispatch():
    assert _DISPATCH.get("evaluate") is evaluate_cmd.run
    dispatch = evaluate_cmd.run
    assert callable(dispatch)


def test_run_dispatches_to_subcommand_handlers(monkeypatch):
    seen = {}

    def fake_handler(args):
        seen["cmd"] = args.evaluate_command
        return 0

    monkeypatch.setattr(evaluate_cmd, "_cmd_run", fake_handler)
    args = build_parser().parse_args(
        [
            "evaluate", "run",
            "--dataset", "d",
            "--name", "n",
            "--evaluator", "m@v1",
        ]
    )
    assert evaluate_cmd.run(args) == 0
    assert seen["cmd"] == "run"


def test_unknown_evaluate_subcommand_returns_2():
    args = build_parser().parse_args(
        ["evaluate", "show", "--experiment-id", "e1"]
    )
    args.evaluate_command = "nope"
    assert evaluate_cmd.run(args) == 2


# ---------------------------------------------------------------------------
# run：复用 runner + 账本分数 + 门禁，退出码映射四态
# ---------------------------------------------------------------------------


def _run_argv(dataset: str, policy: str):
    return [
        "evaluate", "run",
        "--dataset", dataset,
        "--name", "nightly",
        "--evaluator", "agent.task_success@v1",
        "--policy", policy,
    ]


def test_run_returns_gate_pass_exit_code(runtime, dataset_path, policy_path, capsys):
    from app.application.evaluation_experiment import ExperimentRunReport

    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    code = main(_run_argv(dataset_path, policy_path))
    assert code == 0


@pytest.mark.parametrize(
    "state,expected_code",
    [
        (GATE_PASS, 0),
        (GATE_FAIL, 1),
        (INFRASTRUCTURE_ERROR, 2),
        (INCONCLUSIVE, 3),
    ],
)
def test_run_exit_codes_follow_gate_state(
    runtime, dataset_path, policy_path, state, expected_code
):
    from app.application.evaluation_experiment import ExperimentRunReport

    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(state)),
    )
    code = main(_run_argv(dataset_path, policy_path))
    assert code == expected_code
    assert GATE_STATE_EXIT_CODES[state] == expected_code


def test_run_without_policy_prints_report_and_exits_zero(
    runtime, dataset_path, capsys
):  # noqa: D401
    from app.application.evaluation_experiment import ExperimentRunReport

    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    argv = [
        "evaluate", "run",
        "--dataset", dataset_path,
        "--name", "nightly",
        "--evaluator", "agent.task_success@v1",
    ]
    code = main(argv)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experiment_id"] == "exp-1"


def test_run_json_output_is_structured_gate_conclusion(
    runtime, dataset_path, policy_path, capsys
):
    from app.application.evaluation_experiment import ExperimentRunReport

    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    code = main(_run_argv(dataset_path, policy_path))
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "gate_pass"
    assert payload["policy_version"] == "default-v1"
    for field in ("reasons", "metrics", "insufficient_data",
                  "delivery_status_counts", "experiment_id",
                  "denominator_basis"):
        assert field in payload


def test_run_with_unwired_runner_reports_and_returns_2(
    runtime, dataset_path, policy_path, capsys
):
    runtime.install(runner=None, store=FakeStore(), service=FakeService(),
                    gate=FakeGate())
    code = main(_run_argv(dataset_path, policy_path))
    assert code == 2
    err = capsys.readouterr().err
    assert "runner" in err


def test_run_unknown_dataset_exits_2_not_gate_fail(runtime, capsys):
    # 操作型失败（manifest 不存在）与 gate_fail 必须可区分：统一退 2
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    code = main(_run_argv("evaluations/datasets/absent-ds", "p.json"))
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_compare_without_policy_exits_2(runtime, capsys):
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    argv = [
        "evaluate", "compare",
        "--candidate-id", "cand",
        "--baseline-id", "base",
    ]
    code = main(argv)
    assert code == 2
    assert "--policy" in capsys.readouterr().err


def test_compare_ledger_failure_exits_2(runtime, policy_path, capsys):
    # compare 账本读取失败（操作型失败，非门禁结论）-> 统一退 2
    class BrokenStore(FakeStore):
        async def query_results_page(self, **kwargs):
            raise RuntimeError("ledger unavailable")

    runtime.install(
        runner=FakeRunner(),
        store=BrokenStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    argv = [
        "evaluate", "compare",
        "--candidate-id", "cand",
        "--baseline-id", "base",
        "--policy", str(policy_path),
    ]
    code = main(argv)
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_run_passes_evaluator_keys_and_replicates_to_runner(
    runtime, dataset_path, capsys
):
    from app.application.evaluation_experiment import ExperimentRunReport

    runner = FakeRunner(
        ExperimentRunReport(
            experiment_id="exp-1",
            mode="execute",
            name="nightly",
            frozen_snapshot={},
            metrics=("agent.task_success",),
            items=(),
        )
    )
    runtime.install(
        runner=runner,
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    argv = [
        "evaluate", "run",
        "--dataset", dataset_path,
        "--name", "nightly",
        "--evaluator", "agent.task_success@v1",
        "--replicates", "3",
        "--baseline-id", "base-1",
    ]
    code = main(argv)
    assert code == 0
    request = runner.calls[0][1]
    assert request.replicates == 3
    assert request.baseline_id == "base-1"
    assert request.evaluator_set[0].definition_id == "agent.task_success"


# ---------------------------------------------------------------------------
# replay / show / compare / retry-delivery
# ---------------------------------------------------------------------------


def test_replay_uses_replay_path_and_gate(runtime, policy_path, capsys):
    from app.application.evaluation_experiment import ExperimentRunReport

    runner = FakeRunner(
        ExperimentRunReport(
            experiment_id="exp-r",
            mode="replay",
            name="nightly-replay",
            frozen_snapshot={},
            metrics=("agent.task_success",),
            items=(),
        )
    )
    runtime.install(
        runner=runner,
        store=FakeStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_FAIL)),
    )
    argv = [
        "evaluate", "replay",
        "--name", "nightly",
        "--source-sample-ids", "s1,s2",
        "--evaluator", "agent.task_success@v1",
        "--dataset-id", "ds",
        "--dataset-version", "v1",
        "--dataset-hash", "a" * 64,
        "--policy", str(policy_path),
    ]
    code = main(argv)
    assert code == 1
    assert runner.calls[0][0] == "replay"
    request = runner.calls[0][1]
    assert request.source_sample_ids == ("s1", "s2")


def test_replay_waits_for_terminal_jobs_before_gate(
    runtime, policy_path, capsys
):
    # 24 项三方审阅 #20：replay 评分作业异步入队，门禁前必须等待终态
    from app.application.evaluation_experiment import ExperimentRunReport

    class CountingStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.count_calls = []

        async def experiment_job_status_counts(self, experiment_id):
            self.count_calls.append(experiment_id)
            return {"succeeded": 2}

    store = CountingStore()
    runner = FakeRunner(
        ExperimentRunReport(
            experiment_id="exp-r",
            mode="replay",
            name="nightly-replay",
            frozen_snapshot={},
            metrics=("agent.task_success",),
            items=(),
        )
    )
    runtime.install(
        runner=runner,
        store=store,
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    argv = [
        "evaluate", "replay",
        "--name", "nightly",
        "--source-sample-ids", "s1,s2",
        "--evaluator", "agent.task_success@v1",
        "--dataset-id", "ds",
        "--dataset-version", "v1",
        "--dataset-hash", "a" * 64,
        "--policy", str(policy_path),
    ]
    code = main(argv)
    assert code == 0
    assert store.count_calls == ["exp-r"]


def test_show_lists_experiment_results_as_json(runtime, capsys):
    service = FakeService(
        results=[
            {
                "result_id": "r1",
                "metric_name": "agent.task_success",
                "status": "value",
                "typed_value": True,
                "provenance": "automatic",
            }
        ]
    )
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=service,
        gate=FakeGate(),
    )
    argv = [
        "evaluate", "show",
        "--experiment-id", "exp-1",
        "--json",
    ]
    code = main(argv)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experiment_id"] == "exp-1"
    assert payload["items"][0]["result_id"] == "r1"
    assert service.calls[0][1]["experiment_id"] == "exp-1"


def test_show_service_failure_exits_1_via_invoke_handler(runtime, capsys):
    # show 的服务故障沿 main._invoke_handler 通用约定：打印 error 退 1
    service = FakeService(list_error=RuntimeError("ledger unavailable"))
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=service,
        gate=FakeGate(),
    )
    code = main(
        ["evaluate", "show", "--experiment-id", "exp-1"]
    )
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_compare_evaluates_gate_with_baseline(runtime, policy_path, capsys):
    gate = FakeGate(_conclusion(INCONCLUSIVE))
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=FakeService(),
        gate=gate,
    )
    argv = [
        "evaluate", "compare",
        "--candidate-id", "cand",
        "--baseline-id", "base",
        "--policy", str(policy_path),
    ]
    code = main(argv)
    assert code == 3
    request = gate.requests[0]
    assert request.candidate.experiment_id == "cand"
    assert request.baseline is not None
    assert request.baseline.experiment_id == "base"


def test_run_lists_pending_deliveries_without_blocking(
    runtime, dataset_path, policy_path, capsys
):
    # Langfuse 不可用（投递全部 pending）：投递状态单列进入 --json 输出，
    # 不改变门禁结论（gate 由 FakeGate 返回 gate_pass）
    from app.application.evaluation_experiment import ExperimentRunReport

    class DeliveryStore(FakeStore):
        def __init__(self):
            super().__init__(
                rows=[
                    {
                        "result_id": "r1",
                        "job_id": "j1",
                        "metric_name": "agent.task_success",
                        "status": "value",
                        "typed_value": True,
                        "passed": True,
                        "provenance": "automatic",
                    }
                ]
            )

        async def list_deliveries(self, result_id):
            return [
                {"sink": "langfuse", "status": "pending"},
                {"sink": "langfuse", "status": "pending"},
            ]

    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=DeliveryStore(),
        service=FakeService(),
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    code = main(_run_argv(dataset_path, policy_path))
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery_status_counts"] == {"langfuse": {"pending": 2}}
    assert payload["state"] == "gate_pass"



def test_retry_delivery_calls_service(runtime, capsys):
    service = FakeService(retry_result={"status": "pending"})
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=service,
        gate=FakeGate(),
    )
    argv = [
        "evaluate", "retry-delivery",
        "--sink", "langfuse",
        "--result-id", "r1",
    ]
    code = main(argv)
    assert code == 0
    assert service.calls[0] == ("retry_delivery", "langfuse", "r1")
    payload = json.loads(capsys.readouterr().out)
    assert payload["result_id"] == "r1"


def test_retry_delivery_missing_returns_1(runtime, capsys):
    from app.domain.evaluation.errors import EvaluationNotFoundError

    service = FakeService(
        error=EvaluationNotFoundError("delivery not found")
    )
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=service,
        gate=FakeGate(),
    )
    argv = [
        "evaluate", "retry-delivery",
        "--sink", "langfuse",
        "--result-id", "r1",
    ]
    code = main(argv)
    assert code == 1


# ---------------------------------------------------------------------------
# #4 等待评分终态 / #23 baseline 参与当次门禁 / #22 show 翻页
# ---------------------------------------------------------------------------


class CountingStore(FakeStore):
    """带 experiment_job_status_counts 的 store fake（#4 等待端口）。"""

    def __init__(self, sequences):
        super().__init__()
        self._sequences = list(sequences)
        self.count_calls: list[str] = []

    async def experiment_job_status_counts(self, experiment_id):
        self.count_calls.append(experiment_id)
        if len(self._sequences) > 1:
            return self._sequences.pop(0)
        return self._sequences[0]


def test_run_waits_for_terminal_jobs_before_gate(
    runtime, dataset_path, policy_path
):
    from app.application.evaluation_experiment import ExperimentRunReport

    store = CountingStore([{"succeeded": 2, "skipped": 1}])
    gate = FakeGate(_conclusion(GATE_PASS))
    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=store,
        service=FakeService(),
        gate=gate,
    )
    code = main(_run_argv(dataset_path, policy_path))
    assert code == 0
    assert store.count_calls == ["exp-1"]
    assert len(gate.requests) == 1


def test_run_timeout_on_pending_jobs_exits_2_not_gate_fail(
    runtime, dataset_path, policy_path
):
    """评分作业未在超时内终态：操作失败退 2（不是 gate_fail 退 1），
    门禁不基于缺失分母下结论。"""
    from app.application.evaluation_experiment import ExperimentRunReport

    store = CountingStore([{"pending": 1}])
    gate = FakeGate(_conclusion(GATE_PASS))
    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=store,
        service=FakeService(),
        gate=gate,
    )
    argv = _run_argv(dataset_path, policy_path) + ["--wait-timeout", "0"]
    code = main(argv)
    assert code == 2
    assert gate.requests == []


def test_run_baseline_id_collects_gate_data(runtime, dataset_path, policy_path):
    """#23：显式 --baseline-id 时基线门禁数据参与当次配对比较。"""
    from app.application.evaluation_experiment import ExperimentRunReport

    gate = FakeGate(_conclusion(GATE_PASS))
    runtime.install(
        runner=FakeRunner(
            ExperimentRunReport(
                experiment_id="exp-1",
                mode="execute",
                name="nightly",
                frozen_snapshot={},
                metrics=("agent.task_success",),
                items=(),
            )
        ),
        store=FakeStore(),
        service=FakeService(),
        gate=gate,
    )
    argv = _run_argv(dataset_path, policy_path) + ["--baseline-id", "base-1"]
    code = main(argv)
    assert code == 0
    request = gate.requests[0]
    assert request.baseline is not None
    assert request.baseline.experiment_id == "base-1"


def test_show_passes_cursor_to_service(runtime, capsys):
    """#22：show --cursor 翻页续读。"""
    service = FakeService()
    runtime.install(
        runner=FakeRunner(),
        store=FakeStore(),
        service=service,
        gate=FakeGate(_conclusion(GATE_PASS)),
    )
    code = main([
        "evaluate", "show", "--experiment-id", "exp-1",
        "--cursor", "cursor-token-1",
    ])
    assert code == 0
    assert service.calls[0][0] == "list_results"
    assert service.calls[0][1]["cursor"] == "cursor-token-1"
