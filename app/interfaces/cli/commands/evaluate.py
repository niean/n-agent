"""T14: evaluate CLI 命令（离线实验与门禁，spec 9.2）。

对齐 commands/task.py 的模式：模块级 ``_load_evaluation_runtime`` 间接层
（测试 monkeypatch）、``run(args) -> int`` 分发、``--json`` 结构化输出。

子命令：
- run       执行 execute 实验（复用 EvaluationExperimentRunner）并按
            gate_policy 输出门禁结论（--policy 缺省时仅打印实验报告）；
- replay    仅对既有 sample 证据重新评分（不调用业务目标）；
- show      分页查看实验结果（复用 EvaluationService.list_results）；
- compare   候选 vs 预指定 baseline 的配对非劣比较（复用 EvaluationGate）；
- retry-delivery 投递补发（复用 EvaluationService.retry_delivery）。

退出码契约：gate_pass/gate_fail/inconclusive/infrastructure_error ->
0/1/3/2（GATE_STATE_EXIT_CODES）；--policy 缺省的 run/replay 恒 0；
操作型失败（runner 未接线、数据集/策略加载失败、配置不一致、账本不可用
等"本轮评估根本未完成"的情形）在 run/replay/compare 中统一退出 2 并输出
明确错误——与 gate_fail（1，评估结论）严格区分；retry-delivery 维持
_invoke_handler 通用约定（结果不存在等异常退 1）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.application.evaluation_gate import (
    GATE_STATE_EXIT_CODES,
    RESULTS_PAGE_LIMIT,
    GateEvaluationRequest,
    collect_experiment_scores,
    collect_gate_data,
    gate_data_from_report,
    load_gate_policy,
)
from app.domain.evaluation.models import EvaluationValidationError
from app.interfaces.cli.render import make_console, render_data, resolve_format


def _load_evaluation_runtime() -> Any:
    """评估子域 CLI 运行时（app/main.py 服务装配；测试 monkeypatch 点）。

    返回 build_evaluation_services() 产物：store/service/gate/
    experiment_runner（runner 未接线为 None，见 app/main.py 装配说明）。"""
    from app.main import build_evaluation_services

    return build_evaluation_services()


def _load_dataset_manifest(path: str):
    from app.application.evaluation_dataset import load_dataset_manifest

    return load_dataset_manifest(path)


def run(args) -> int:
    dispatch = {
        "run": _cmd_run,
        "replay": _cmd_replay,
        "show": _cmd_show,
        "compare": _cmd_compare,
        "retry-delivery": _cmd_retry_delivery,
        "convert-e2e": _cmd_convert_e2e,
    }
    handler = dispatch.get(args.evaluate_command)
    if handler is None:
        console = make_console()
        console.print(
            f"[red]unknown evaluate command: {args.evaluate_command}[/red]"
        )
        return 2
    return handler(args)


def _parse_evaluator_set(evaluator_arg: str):
    """``id@version,id@version`` -> DefinitionKey 元组（fail-fast）。"""
    from app.application.evaluation_service import DefinitionKey
    from app.domain.evaluation.models import EvaluationValidationError

    keys = []
    for part in evaluator_arg.split(","):
        part = part.strip()
        if not part:
            continue
        if "@" not in part:
            raise EvaluationValidationError(
                f"--evaluator 条目必须是 definition_id@version: {part!r}"
            )
        definition_id, _, version = part.partition("@")
        keys.append(
            DefinitionKey(
                definition_id=definition_id, definition_version=version
            )
        )
    if not keys:
        raise EvaluationValidationError("--evaluator 至少需要一个定义")
    return tuple(keys)


def _load_policy(policy_arg: str | None):
    if not policy_arg:
        return None
    return load_gate_policy(policy_arg)


def _print_conclusion(conclusion, args) -> None:
    render_data(
        conclusion.to_mapping(),
        fmt=resolve_format(args),
        console=make_console(),
    )


def _exit_code(conclusion) -> int:
    return GATE_STATE_EXIT_CODES[conclusion.state]


def _operational_failure(exc: Exception) -> int:
    """操作型失败统一退出码 2（infrastructure_error 语义）。

    门禁结论（gate_fail/inconclusive）与操作失败（数据集/策略/配置/
    账本不可用等）必须可区分：前者是评估结论 1/3，后者是本轮评估根本
    未完成，一律 2，避免与 gate_fail 混淆。仅捕获 Exception——
    KeyboardInterrupt/SystemExit 正常向上传播。"""
    print(f"error: {exc}", file=sys.stderr)
    return 2


def _cmd_run(args) -> int:
    try:
        return _run_experiment(args)
    except Exception as exc:  # noqa: BLE001 - 操作失败统一 2（见 docstring）
        return _operational_failure(exc)


def _run_experiment(args) -> int:
    runtime = _load_evaluation_runtime()
    runner = getattr(runtime, "experiment_runner", None)
    if runner is None:
        raise EvaluationValidationError(
            "evaluation experiment runner 未接线（T20 组合根装配），"
            "无法执行 evaluate run"
        )
    policy = _load_policy(getattr(args, "policy", None))
    dataset = _load_dataset_manifest(args.dataset)
    from app.application.evaluation_experiment import ExecuteExperimentRequest

    report = asyncio.run(
        runner.execute(
            ExecuteExperimentRequest(
                dataset=dataset,
                name=args.name,
                target_config=_parse_target_config(args),
                evaluator_set=_parse_evaluator_set(args.evaluator),
                replicates=args.replicates,
                seed=getattr(args, "seed", None),
                baseline_id=getattr(args, "baseline_id", None),
            )
        )
    )
    # #4：等待评分作业全部终态再算门禁——execute 只入队异步评分作业，
    # 立即收集分数会把排队中作业静默当作缺失分母（门禁结论失真）。
    # 无评分作业（空计数）直接通过。超时按操作失败退 2（本轮评估未完
    # 成，不是 gate_fail）。
    raw_wait_timeout = getattr(args, "wait_timeout", None)
    wait_timeout = 900.0 if raw_wait_timeout is None else float(raw_wait_timeout)
    asyncio.run(
        _wait_for_terminal_jobs(
            runtime.store,
            report.experiment_id,
            timeout=wait_timeout,
        )
    )
    return _finish_with_policy(
        args, runtime, report=report, dataset=dataset, policy=policy
    )


_GATE_TERMINAL_JOB_STATUSES = frozenset({
    "succeeded", "skipped", "error", "cancelled",
})

_GATE_JOB_POLL_INTERVAL_SECONDS = 2.0


async def _wait_for_terminal_jobs(
    store: Any, experiment_id: str, *, timeout: float
) -> None:
    """等待实验的评分作业全部终态（#4）。store 缺少计数形状（测试
    fake）时跳过等待。超时抛 EvaluationValidationError（操作失败退 2）。"""
    counts_fn = getattr(store, "experiment_job_status_counts", None)
    if counts_fn is None:
        return
    import time as _time

    deadline = _time.monotonic() + max(0.0, timeout)
    while True:
        counts = await counts_fn(experiment_id)
        non_terminal = {
            status: count
            for status, count in counts.items()
            if status not in _GATE_TERMINAL_JOB_STATUSES
        }
        if not non_terminal:
            return
        if _time.monotonic() >= deadline:
            raise EvaluationValidationError(
                "评分作业未在超时内全部终态，门禁取消："
                f"experiment={experiment_id} pending={non_terminal} "
                f"timeout={timeout}s"
            )
        await asyncio.sleep(_GATE_JOB_POLL_INTERVAL_SECONDS)


def _cmd_replay(args) -> int:
    try:
        return _replay_experiment(args)
    except Exception as exc:  # noqa: BLE001 - 操作失败统一 2（见 docstring）
        return _operational_failure(exc)


def _replay_experiment(args) -> int:
    runtime = _load_evaluation_runtime()
    runner = getattr(runtime, "experiment_runner", None)
    if runner is None:
        raise EvaluationValidationError(
            "evaluation experiment runner 未接线（T20 组合根装配），"
            "无法执行 evaluate replay"
        )
    policy = _load_policy(getattr(args, "policy", None))
    from app.application.evaluation_experiment import ReplayExperimentRequest

    report = asyncio.run(
        runner.replay(
            ReplayExperimentRequest(
                name=args.name,
                source_sample_ids=tuple(
                    part.strip()
                    for part in args.source_sample_ids.split(",")
                    if part.strip()
                ),
                evaluator_set=_parse_evaluator_set(args.evaluator),
                dataset_id=args.dataset_id,
                dataset_version=args.dataset_version,
                dataset_hash=args.dataset_hash,
                target_config=_parse_target_config(args),
            )
        )
    )
    # replay 评分作业同样经 submit_sample 异步入队：与 run 路径同一
    # 等待终态（24 项三方审阅 #20——立即收集分数会把排队中作业静默
    # 当作缺失分母，覆盖率与门禁结论受后台时序影响）
    raw_wait_timeout = getattr(args, "wait_timeout", None)
    wait_timeout = 900.0 if raw_wait_timeout is None else float(raw_wait_timeout)
    asyncio.run(
        _wait_for_terminal_jobs(
            runtime.store,
            report.experiment_id,
            timeout=wait_timeout,
        )
    )
    # replay 无 manifest 预声明可用：门禁分母以出现过的 case 近似
    return _finish_with_policy(
        args, runtime, report=report, dataset=None, policy=policy
    )


def _finish_with_policy(args, runtime, *, report, dataset, policy) -> int:
    if policy is None:
        render_data(
            {
                "experiment_id": report.experiment_id,
                "mode": report.mode,
                "name": report.name,
                "metrics": list(report.metrics),
                "items": len(report.items),
                "cleanup_failures": report.cleanup_failures,
            },
            fmt=resolve_format(args),
            console=make_console(),
        )
        return 0
    scores = asyncio.run(
        collect_experiment_scores(runtime.store, report.experiment_id)
    )
    # #23：显式 --baseline-id 时基线门禁数据参与当次配对比较（与 compare
    # 同一 collect_gate_data 形态）；加载失败按操作失败退 2（基线缺失
    # 不允许伪装成单边门禁）
    baseline = None
    baseline_id = getattr(args, "baseline_id", None)
    if policy is not None and baseline_id:
        baseline = asyncio.run(collect_gate_data(runtime.store, baseline_id))
    gate_request = GateEvaluationRequest(
        candidate=gate_data_from_report(report, dataset, scores),
        policy=policy,
        delivery_status_counts=asyncio.run(
            _collect_delivery_status_counts(runtime.store, report.experiment_id)
        ),
        baseline=baseline,
    )
    conclusion = asyncio.run(runtime.gate.evaluate(gate_request))
    _print_conclusion(conclusion, args)
    return _exit_code(conclusion)


async def _collect_delivery_status_counts(
    ledger, experiment_id: str
) -> dict[str, dict[str, int]]:
    """投递状态单列汇总（sink -> status -> 计数）。

    Langfuse 不可用（delivery 全部 pending 等）不阻断门禁结论，仅在
    结论输出中单独展示。ledger 缺少投递查询形状时返回空汇总。"""
    counts: dict[str, dict[str, int]] = {}
    list_deliveries = getattr(ledger, "list_deliveries", None)
    if list_deliveries is None:
        return counts
    cursor = None
    while True:
        rows, next_cursor = await ledger.query_results_page(
            experiment_id=experiment_id, cursor=cursor,
            limit=RESULTS_PAGE_LIMIT,
        )
        for row in rows:
            result_id = row.get("result_id")
            if not result_id:
                continue
            for delivery in await list_deliveries(result_id):
                sink = str(delivery.get("sink") or "unknown")
                status = str(delivery.get("status") or "unknown")
                per_sink = counts.setdefault(sink, {})
                per_sink[status] = per_sink.get(status, 0) + 1
        if next_cursor is None:
            break
        cursor = next_cursor
    return counts


def _parse_target_config(args) -> dict[str, Any]:
    raw = getattr(args, "target_config", None)
    if not raw:
        return {}
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvaluationValidationError(
            f"--target-config 不是合法 JSON: {exc}"
        ) from exc
    if not isinstance(config, dict):
        raise EvaluationValidationError("--target-config 必须是 JSON 对象")
    return config


def _cmd_show(args) -> int:
    runtime = _load_evaluation_runtime()
    service = runtime.service
    page = asyncio.run(
        service.list_results(
            experiment_id=args.experiment_id,
            limit=args.limit,
            # #22：翻页入口——上一页 next_cursor 经 --cursor 续读
            cursor=getattr(args, "cursor", None),
        )
    )
    payload = {
        "experiment_id": args.experiment_id,
        "items": [dict(row) for row in page.items],
        "next_cursor": page.next_cursor,
    }
    render_data(
        payload, fmt=resolve_format(args), console=make_console()
    )
    return 0


def _cmd_compare(args) -> int:
    try:
        return _compare_experiments(args)
    except Exception as exc:  # noqa: BLE001 - 操作失败统一 2（见 docstring）
        return _operational_failure(exc)


def _compare_experiments(args) -> int:
    runtime = _load_evaluation_runtime()
    policy = _load_policy(getattr(args, "policy", None))
    if policy is None:
        raise EvaluationValidationError("evaluate compare 需要 --policy")
    candidate = asyncio.run(
        collect_gate_data(runtime.store, args.candidate_id)
    )
    baseline = asyncio.run(
        collect_gate_data(runtime.store, args.baseline_id)
    )
    conclusion = asyncio.run(
        runtime.gate.evaluate(
            GateEvaluationRequest(
                candidate=candidate,
                baseline=baseline,
                policy=policy,
            )
        )
    )
    _print_conclusion(conclusion, args)
    return _exit_code(conclusion)


def _cmd_retry_delivery(args) -> int:
    runtime = _load_evaluation_runtime()
    try:
        result = asyncio.run(
            runtime.service.retry_delivery(args.sink, args.result_id)
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    render_data(
        dict(result), fmt=resolve_format(args), console=make_console()
    )
    return 0


def _cmd_convert_e2e(args) -> int:
    """离线 E2E 会话样例转换（plan T2）。

    只加载转换依赖（纯数据变换），不初始化业务服务、不触网、不写
    evaluations.db。产物：
    - {out}/manifest.json      待审核 manifest 载荷（含 dataset_hash）
    - {out}/coverage.json      覆盖报告（含 unsupported 清单）
    - {out}/dataset/{id}/{ver}/ 权威布局数据集（manifest + 素材），
      经 T12 权威加载器回读校验
    """
    import json
    from pathlib import Path

    from app.application.evaluation_e2e_conversion import (
        convert_e2e_conversations,
    )

    try:
        result = convert_e2e_conversations(
            args.source_dir,
            dataset_id=args.dataset_id,
            dataset_version=args.dataset_version,
        )
    except Exception as exc:  # noqa: BLE001 - 操作失败统一 2
        return _operational_failure(exc)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = dict(result.manifest)
    manifest["dataset_hash"] = __import__(
        "app.application.evaluation_dataset", fromlist=["compute_dataset_hash"]
    ).compute_dataset_hash(manifest)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "coverage.json").write_text(
        json.dumps(result.coverage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    dataset_dir = out / "dataset" / args.dataset_id / args.dataset_version
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in result.materials:
        target = dataset_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (dataset_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from app.application.evaluation_dataset import load_dataset_manifest

    try:
        load_dataset_manifest(dataset_dir / "manifest.json")
    except Exception as exc:  # noqa: BLE001
        return _operational_failure(exc)
    summary = dict(result.coverage["summary"])
    summary["output"] = str(out)
    print(json.dumps(summary, ensure_ascii=False))
    return 0
