"""Dashboard 发起实验：持久 admission、受管执行与评分恢复，不驱动 shell。"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import fields
from datetime import datetime
import hashlib
import logging
import time
from typing import Any

from app.application.evaluation_experiment import ExecuteExperimentRequest
from app.application.evaluation_analytics import build_scenario_comparison_groups
from app.application.evaluation_scenario import select_case_ids
from app.application.evaluation_service import EvaluationService
from app.domain.evaluation.errors import EvaluationConflictError, EvaluationLeaseError
from app.domain.evaluation.execution import (
    ExecutionNotReadyError,
    MAX_ITEMS,
    MAX_REPLICATES,
    MAX_QUEUED,
    SCENARIO_RELEASE_WINDOW_SECONDS,
    SCENARIO_SCORING_WINDOW_SECONDS,
    SCENARIO_TASK_TIMEOUT_SECONDS,
    scenario_run_budget_seconds,
)
from app.domain.evaluation.models import DatasetCase, DatasetManifest, EvaluationValidationError, canonical_json_bytes
from app.domain.evaluation.scenario import ScenarioCase, ScenarioValidationError

logger = logging.getLogger(__name__)
_FIELDS = frozenset({"request_key", "name", "dataset_id", "dataset_version", "dataset_hash", "evaluator_set", "replicates", "case_ids"})
_PUBLIC = ("execution_id", "name", "dataset_id", "dataset_version", "dataset_hash", "phase", "experiment_id",
           "created_at", "updated_at", "started_at", "finished_at", "error_code")
_PENDING = frozenset({"queued", "running", "retry_wait", "pending"})
_SUPPORTED = {
    "deterministic": frozenset(p["definition_id"] for p in EvaluationService._PRESET_DETERMINISTIC),
    "llm_judge": frozenset(p["definition_id"] for p in EvaluationService._PRESET_JUDGE),
}


def _fingerprint(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _dataset(raw):
    """只消费 ReviewService 已用权威 loader 验证的数据，不另读路径。

    schema 2 场景负载不再过滤：scenario 负载经 ScenarioCase.from_mapping
    解析（构造期校验封闭词表），schema 1 case 无 scenario 保持 None。"""
    allowed = {field.name for field in fields(DatasetCase)}
    cases = []
    for case in raw["cases"]:
        if case.get("qrels") is not None:
            raise EvaluationValidationError("agent execution does not support retrieval labels")
        values = {key: value for key, value in case.items() if key in allowed}
        scenario_raw = case.get("scenario")
        if scenario_raw is not None:
            try:
                values["scenario"] = ScenarioCase.from_mapping(scenario_raw)
            except ScenarioValidationError as exc:
                raise EvaluationValidationError(
                    f"场景负载校验失败（case {case.get('case_id')}）: {exc}"
                ) from exc
        cases.append(DatasetCase(**values))
    return DatasetManifest(schema_version=raw["schema_version"], dataset_id=raw["dataset_id"],
                           dataset_version=raw["dataset_version"], dataset_hash=raw["dataset_hash"],
                           target_kind=raw["target_kind"], cases=tuple(cases))


class EvaluationExecutionService:
    def __init__(self, *, store, ledger, review, runner, readiness, evidence=None,
                 clock=time.time, poll_interval=1.0, scoring_timeout=900.0, execution_timeout=900.0, heartbeat_interval=10.0,
                 preflight=None, resource_ledger=None, admitted_profiles=("agent",),
                 evidence_retention_seconds=7 * 86400):
        self.store, self.ledger, self.review, self.runner = store, ledger, review, runner
        self.readiness, self.evidence, self.clock = readiness, evidence, clock
        self.poll_interval, self.scoring_timeout, self.execution_timeout = poll_interval, scoring_timeout, execution_timeout
        self.heartbeat_interval = heartbeat_interval
        # plan T5：只读预检（submit 前整单复核）、场景资源账本（completed
        # 前置释放/保留）、已接入执行适配的场景通道白名单
        self.preflight = preflight
        self.resource_ledger = resource_ledger
        self.admitted_profiles = frozenset(admitted_profiles)
        self.evidence_retention_seconds = float(evidence_retention_seconds)
        self._loop_task = None
        self._stopping = False

    async def capabilities(self):
        state = await self.readiness()
        definitions = await self.ledger.list_definitions(target_kind="experiment_item")
        entries = []
        for row in definitions:
            if row["metric_name"] not in _SUPPORTED.get(row["method"], ()):
                continue
            full = await self.ledger.get_definition(row["definition_id"], row["version"])
            entries.append({"definition_id": row["definition_id"], "definition_version": row["version"],
                            "metric_name": row["metric_name"], "method": row["method"],
                            "judge_model": full["definition"].get("judge_model"),
                            "executable": bool(state.get("ready") and (row["method"] != "llm_judge" or state.get("judge_ready")))})
        return {key: state.get(key) for key in ("ready", "judge_ready", "worker_ready", "target_model", "judge_model", "self_judge", "supports_vision")} | {
            "definitions": entries, "max_replicates": MAX_REPLICATES, "max_items": MAX_ITEMS, "max_queued": MAX_QUEUED,
        }

    @staticmethod
    def _normalize(payload):
        if not isinstance(payload, dict) or set(payload)-_FIELDS:
            raise EvaluationValidationError("unsupported execution fields")
        value = dict(payload)
        value.setdefault("replicates", 1)
        for name in ("request_key", "name", "dataset_id", "dataset_version", "dataset_hash"):
            if not isinstance(value.get(name), str) or not value[name].strip() or len(value[name]) > 200:
                raise EvaluationValidationError("invalid "+name)
        value["name"] = value["name"].strip()
        if len(value["dataset_hash"]) != 64 or any(c not in "0123456789abcdef" for c in value["dataset_hash"]):
            raise EvaluationValidationError("invalid dataset_hash")
        if type(value["replicates"]) is not int or not 1 <= value["replicates"] <= MAX_REPLICATES:
            raise EvaluationValidationError("replicates must be 1..10")
        definitions = value.get("evaluator_set")
        if not isinstance(definitions, list) or not definitions or len(definitions) > 20:
            raise EvaluationValidationError("evaluator_set required")
        keys = []
        for item in definitions:
            if not isinstance(item, dict) or set(item) != {"definition_id", "definition_version"} or not all(isinstance(v, str) and v for v in item.values()):
                raise EvaluationValidationError("invalid evaluator_set")
            keys.append((item["definition_id"], item["definition_version"]))
        if len(keys) != len(set(keys)):
            raise EvaluationValidationError("duplicate evaluator")
        value["evaluator_set"] = [{"definition_id": a, "definition_version": b} for a,b in sorted(keys)]
        # plan T5：显式 case_ids 子集（可选）固定分母；空列表视同未提供
        case_ids = value.get("case_ids")
        if case_ids is not None:
            if not isinstance(case_ids, list) or not case_ids \
                or not all(isinstance(cid, str) and cid.strip() and len(cid) <= 200 for cid in case_ids):
                raise EvaluationValidationError("invalid case_ids")
            value["case_ids"] = [cid.strip() for cid in case_ids]
        return value

    async def submit(self, payload):
        request = self._normalize(payload)
        fingerprint = _fingerprint(request)
        prior = await self.store.find_request(request["request_key"])
        if prior:
            if prior["request_fingerprint"] != fingerprint:
                raise EvaluationConflictError("request_key payload conflict")
            return self._public(prior)
        ready = await self.readiness()
        if not ready.get("ready") or not ready.get("worker_ready"):
            raise ExecutionNotReadyError("execution_not_ready")
        raw = await self.review.get_dataset(request["dataset_id"], request["dataset_version"])
        dataset = _dataset(raw)
        if dataset.target_kind != "agent_run" or any(c.target_kind != "agent_run" for c in dataset.cases):
            raise EvaluationValidationError("only agent_run datasets are supported")
        if dataset.dataset_hash != request["dataset_hash"]:
            raise EvaluationValidationError("dataset_hash mismatch")
        # plan T5：显式子集固定分母（未知 case / 重复 / 空子集整单拒绝）
        try:
            selected_ids = select_case_ids(
                raw, request.get("case_ids")
            )
        except ValueError as exc:
            raise EvaluationValidationError(str(exc)) from exc
        # plan T5：profile 准入——schema 2 场景通道未接入执行适配时整单拒绝
        # （固定分母纪律：仅核验选中子集的 profile）
        selected_set = set(selected_ids)
        for case in dataset.cases:
            if str(case.case_id) not in selected_set:
                continue
            if case.scenario is not None and case.scenario.execution.profile not in self.admitted_profiles:
                raise EvaluationValidationError(
                    "场景通道未接入执行适配: "
                    + case.scenario.execution.profile
                )
        hashes = {}
        metric_names = set()
        for key in request["evaluator_set"]:
            row = await self.ledger.get_definition(key["definition_id"], key["definition_version"])
            if row["target_kind"] != "experiment_item" or row["metric_name"] not in _SUPPORTED.get(row["method"], ()):
                raise EvaluationValidationError("incompatible evaluator")
            if row["method"] == "llm_judge" and not ready.get("judge_ready"):
                raise ExecutionNotReadyError("judge_not_ready")
            hashes[key["definition_id"]+"@"+key["definition_version"]] = row["definition_hash"]
            metric_names.add(str(row["metric_name"]))
        # 必需指标遗漏：分母纪律——必需指标未被评分器集合覆盖不得运行
        for case in dataset.cases:
            if str(case.case_id) not in set(selected_ids):
                continue
            if case.scenario is None:
                continue
            missing = {
                metric
                for metric in case.scenario.required_metrics.coverages.values()
                if metric not in metric_names
            }
            if missing:
                raise EvaluationValidationError(
                    f"必需指标未覆盖（case {case.case_id}）: "
                    + ",".join(sorted(missing))
                )
        if len(selected_ids)*request["replicates"] > MAX_ITEMS:
            raise EvaluationValidationError("execution item limit exceeded")
        if any("image_url" in str(c.input) for c in dataset.cases) and not ready.get("supports_vision", True):
            raise ExecutionNotReadyError("target_vision_not_ready")
        # plan T5：submit 前只读预检复核（blocked 整单拒绝，逐题原因回传）
        if self.preflight is not None:
            report = await self.preflight.preflight(
                dataset_id=request["dataset_id"],
                dataset_version=request["dataset_version"],
                dataset_hash=request["dataset_hash"],
                evaluator_set=request["evaluator_set"],
                replicates=request["replicates"],
                case_ids=request.get("case_ids"),
            )
            if not report["runnable"]:
                raise EvaluationValidationError(
                    "preflight blocked: " + "; ".join(report["blocked"])
                )
        # 持久绝对预算（spec 6.3）：submit 时刻冻结，重启/重试不刷新时钟
        now = self.clock()
        items = len(selected_ids) * request["replicates"]
        budget = {
            "submitted_at": now,
            "run_deadline": now + scenario_run_budget_seconds(items),
            "scoring_window": SCENARIO_SCORING_WINDOW_SECONDS,
            "release_window": SCENARIO_RELEASE_WINDOW_SECONDS,
        }
        frozen = {"request": request, "manifest": raw, "definition_hashes": hashes,
                  "config_fingerprint": ready["config_fingerprint"], "target_model": ready.get("target_model"),
                  "selected_case_ids": selected_ids, "budget": budget}
        row = await self.store.submit({**request, "request_fingerprint": fingerprint, "payload": frozen}, now=self.clock())
        return self._public(row)

    @staticmethod
    def _public(row):
        report = copy.deepcopy(row.get("report") or {})
        if ((row.get("payload") or {}).get("manifest") or {}).get("schema_version") == "2":
            for item in report.get("items") or ():
                item["answer"] = None  # Never fall back to an old plaintext cache.
                item.pop("answer_truncated", None)
        return {key: row.get(key) for key in _PUBLIC} | {"report": report}

    async def list(self, *, limit=50, cursor=None):
        page = await self.store.list_page(limit=limit, cursor=cursor)
        return {"items": [self._public(row) for row in page["items"]], "next_cursor": page["next_cursor"]}

    async def _counts(self, experiment_id):
        jobs = await self.ledger.experiment_job_status_counts(experiment_id)
        counts = {"value": 0, "error": 0, "skipped": 0, "pending": sum(v for k,v in jobs.items() if k in _PENDING)}
        cursor = None
        for _ in range(25):
            rows, cursor = await self.ledger.query_results_page(experiment_id=experiment_id, cursor=cursor, limit=200)
            for row in rows:
                status = row.get("status")
                if status in counts and status != "pending":
                    counts[status] += 1
            if cursor is None:
                break
        counts["truncated"] = cursor is not None
        return counts

    async def get(self, execution_id):
        row = await self.store.get(execution_id)
        result = self._public(row)
        if ((row.get("payload") or {}).get("manifest") or {}).get("schema_version") == "2":
            for item in (result.get("report") or {}).get("items") or ():
                item["evidence_unavailable"] = True
                if self.clock() >= item.get("evidence_expires_at", 0) or self.evidence is None:
                    continue
                for sample_id in item.get("evidence_sample_ids") or ():
                    try:
                        if await self.ledger.is_sample_evidence_deleted(sample_id):
                            continue
                        sample = await self.ledger.load_sample(sample_id)
                        if not sample.get("snapshot_ref"):
                            continue
                        captured = datetime.fromisoformat(sample["captured_at"])
                        if captured.tzinfo is None:
                            continue
                        retention = min(item.get("evidence_retention_seconds", self.evidence_retention_seconds),
                                        self.evidence_retention_seconds)
                        expires = min(item.get("evidence_expires_at", 0), captured.timestamp() + retention)
                        if self.clock() >= expires:
                            continue
                        snapshot = await self.evidence.load_snapshot(sample["snapshot_ref"])
                        if await self.ledger.is_sample_evidence_deleted(sample_id) or self.clock() >= expires:
                            continue
                        answer = snapshot.content.get("answer")
                        if isinstance(answer, str):
                            item.update(answer=answer[:16000], answer_truncated=len(answer) > 16000,
                                        evidence_unavailable=False)
                            break
                    except Exception:
                        logger.warning("evaluation report evidence unavailable: sample_id=%s", sample_id)
        result["quality_counts"] = await self._counts(row["experiment_id"]) if row["experiment_id"] else {"value": 0,"error": 0,"skipped": 0,"pending": 0}
        # plan T8：比较分组（提交时冻结的 manifest 计算；scope/profile/
        # fixture/binding 不同不混成同一可比总体，派生题单列）
        manifest = (row.get("payload") or {}).get("manifest") or {}
        selected = (row.get("payload") or {}).get("selected_case_ids")
        cases = manifest.get("cases") or []
        if selected is not None:
            cases = [case for case in cases if case.get("case_id") in set(selected)]
        result["comparison_groups"] = build_scenario_comparison_groups(
            cases
        )
        return result

    async def start(self):
        if self._loop_task is None:
            self._stopping = False
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    async def _loop(self):
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("evaluation execution dispatch failed")
            await asyncio.sleep(self.poll_interval)

    async def run_once(self):
        claim = asyncio.create_task(self.store.claim(now=self.clock()))
        try:
            row = await asyncio.shield(claim)
        except asyncio.CancelledError:
            row = await claim
            if row is not None:
                await self._cancel_owned(row)
            raise
        if row is None:
            return
        work = asyncio.create_task(self._drive(row))

        async def heartbeat():
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                try:
                    await self.store.heartbeat(row["execution_id"], row["owner_token"], now=self.clock())
                except Exception:
                    work.cancel()
                    raise

        pulse = asyncio.create_task(heartbeat())
        try:
            await work
        except asyncio.CancelledError:
            await self._cancel_owned(row)
            if self._stopping:
                raise
        except EvaluationLeaseError:
            pass
        except Exception as exc:
            code = str(exc) if isinstance(exc, ExecutionNotReadyError) else "execution_timeout" if isinstance(exc, TimeoutError) else "execution_failed"
            try:
                await self.store.update(row["execution_id"], row["owner_token"], now=self.clock(), phase="failed", error_code=code)
            except EvaluationLeaseError:
                pass
            logger.warning("evaluation execution failed: execution_id=%s type=%s", row["execution_id"], type(exc).__name__)
        finally:
            pulse.cancel()
            work.cancel()
            await asyncio.gather(pulse, work, return_exceptions=True)

    async def _cancel_owned(self, row):
        current = await self.store.get(row["execution_id"])
        try:
            if current["phase"] == "scoring":
                await self.store.release_scoring(row["execution_id"], row["owner_token"], now=self.clock())
            elif current["phase"] == "running":
                await self.store.update(row["execution_id"], row["owner_token"], now=self.clock(), phase="interrupted", error_code="execution_interrupted")
        except EvaluationLeaseError:
            pass

    async def _check_configuration(self, frozen):
        ready = await self.readiness()
        if ready.get("config_fingerprint") != frozen["config_fingerprint"]:
            raise ExecutionNotReadyError("configuration_changed")
        if not ready.get("ready"):
            raise ExecutionNotReadyError("execution_not_ready")

    async def _drive(self, row):
        frozen = row["payload"]
        await self._check_configuration(frozen)
        request = frozen["request"]
        # 持久绝对预算：run deadline 在 submit 时刻冻结，重启/重试不刷新
        budget = frozen.get("budget") or {}
        run_deadline = budget.get("run_deadline")
        run_timeout = (
            self.execution_timeout
            if run_deadline is None
            else max(0.0, run_deadline - self.clock())
        )
        if row["phase"] == "running":
            raw = await self.review.get_dataset(request["dataset_id"], request["dataset_version"])
            if raw != frozen["manifest"]:
                raise EvaluationValidationError("dataset changed")
            for key in request["evaluator_set"]:
                definition = await self.ledger.get_definition(key["definition_id"], key["definition_version"])
                if definition["definition_hash"] != frozen["definition_hashes"][key["definition_id"]+"@"+key["definition_version"]]:
                    raise EvaluationValidationError("definition changed")

            async def started(experiment):
                await self._check_configuration(frozen)
                await self.store.update(row["execution_id"], row["owner_token"], now=self.clock(), experiment_id=experiment.experiment_id)

            report = await asyncio.wait_for(self.runner.execute(ExecuteExperimentRequest(
                dataset=_dataset(raw), name=request["name"], replicates=request["replicates"],
                target_config={"target_model": frozen["target_model"], "execution_fingerprint": frozen["config_fingerprint"]},
                evaluator_set=tuple((k["definition_id"],k["definition_version"]) for k in request["evaluator_set"]),
                execution_id=row["execution_id"],
                case_ids=(
                    tuple(frozen["selected_case_ids"])
                    if frozen.get("selected_case_ids")
                    else None
                ),
            ), on_started=started), timeout=run_timeout)
            public_report = {"items": [], "cleanup_failures": report.cleanup_failures}
            for item in report.items:
                entry = {"item_id": item.item_id, "case_id": item.case_id, "replicate": item.replicate,
                         "skip_reason": item.skip_reason, "outcome": item.outcome, "failure_reason": item.failure_reason,
                         "cleanup_status": item.cleanup_status, "answer": None}
                if raw.get("schema_version") == "2":
                    entry["evidence_sample_ids"] = list(item.sample_ids)
                    entry["evidence_expires_at"] = self.clock() + self.evidence_retention_seconds
                    entry["evidence_retention_seconds"] = self.evidence_retention_seconds
                elif self.evidence:
                    for sample_id in item.sample_ids:
                        sample = await self.ledger.load_sample(sample_id)
                        try:
                            snapshot = await self.evidence.load_snapshot(sample["snapshot_ref"])
                            answer = snapshot.content.get("answer")
                            if isinstance(answer, str):
                                entry["answer"] = answer[:16000]
                                entry["answer_truncated"] = len(answer) > 16000
                                break
                        except Exception:
                            entry["evidence_unavailable"] = True
                public_report["items"].append(entry)
            row = await self.store.update(row["execution_id"], row["owner_token"], now=self.clock(), phase="scoring", report=public_report)
        while True:
            await self._check_configuration(frozen)
            counts = await self._counts(row["experiment_id"])
            if not counts["pending"]:
                # plan T5：completed 前置条件——评分终态 + 活跃资源释放
                # 成功且保留任务已登记；释放失败进入 failed 并保留可
                # 恢复账目（spec 6.2/6.3）
                release = await self._settle_resources(row)
                report = dict(row["report"] or {})
                report["release"] = release
                if release.get("state") == "release_failed":
                    await self.store.update(
                        row["execution_id"], row["owner_token"], now=self.clock(),
                        phase="failed", error_code="resource_release_failed",
                        report=report,
                    )
                    return
                await self.store.update(row["execution_id"], row["owner_token"], now=self.clock(), phase="completed", report=report)
                return
            scoring_window = budget.get("scoring_window", self.scoring_timeout)
            if self.clock()-(row["scoring_started_at"] or self.clock()) > scoring_window:
                raise TimeoutError()
            await asyncio.sleep(self.poll_interval)

    async def _settle_resources(self, row):
        """释放活跃资源并登记保留（completed 前置条件）。

        未接线资源账本或无场景资源时不适用（schema 1 原路径不变）。"""
        ledger = self.resource_ledger
        if ledger is None:
            return {"state": "not_applicable"}
        retained = await ledger.register_retention(row["execution_id"])
        outcome = await ledger.release_execution(row["execution_id"])
        released = outcome.get("released", 0)
        # 合并两阶段计数（T10 S4 实测）：register_retention 先迁移存储类
        # 资源 -> release_execution 对已 retained 计 0；禁止 **outcome
        # 展开覆盖 retained 计数误报
        total_retained = retained + outcome.get("retained", 0)
        if outcome.get("cleanup_failed"):
            return {
                "state": "release_failed", "retained": total_retained,
                "released": released,
                "cleanup_failed": outcome.get("cleanup_failed", 0),
            }
        state = (
            "retention_registered" if total_retained
            else "released" if released
            else "not_applicable"
        )
        return {
            "state": state, "retained": total_retained,
            "released": released,
            "cleanup_failed": outcome.get("cleanup_failed", 0),
        }
