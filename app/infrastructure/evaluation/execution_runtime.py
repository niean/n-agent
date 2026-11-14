"""组合根执行适配：实际调用前复核冻结的连接身份，不暴露密钥。"""
from __future__ import annotations

import hashlib

from app.application.evaluation_execution import EvaluationExecutionService
from app.application.evaluation_experiment import EvaluationExperimentRunner
from app.domain.evaluation.execution import ExecutionNotReadyError
from app.domain.evaluation.models import canonical_json_bytes
from app.infrastructure.evaluation.execution_store import SQLiteExecutionStore
from app.infrastructure.evaluation.scenario_adapters import ScenarioDashboardSSERunner
from app.infrastructure.evaluation.target_runner import ChatCompletionTargetRunner, build_frozen_environment, experiment_item_root_scope
from app.infrastructure.observability.logging import LogSanitizer


async def execution_configuration(settings, holder, registry):
    config = holder.current_config
    secret = await registry.get_secret(config.id) if config else ""
    material = {
        "target": {"id": config.id, "base_url": config.base_url, "model": config.model,
                   "headers": config.extra_headers, "updated_at": str(config.updated_at)} if config else None,
        "target_secret": hashlib.sha256((secret or "").encode()).hexdigest(),
        "judge_base": settings.evaluation_judge_provider_base_url,
        "judge_model": settings.evaluation_judge_provider_model,
        "judge_secret": hashlib.sha256(settings.evaluation_judge_provider_api_key.encode()).hexdigest(),
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


class GuardedChatTarget:
    def __init__(self, *, chat, readiness):
        self.chat, self.readiness = chat, readiness

    async def run_target(self, target, request):
        expected = request["target_config"]["execution_fingerprint"]

        async def check():
            state = await self.readiness()
            if not state.get("ready") or state["config_fingerprint"] != expected:
                raise ExecutionNotReadyError("configuration_changed")
            return state

        state = await check()
        real_chat = self.chat

        class CheckedChat:
            async def complete(self, payload):
                await check()
                result = await real_chat.complete(payload)
                await check()
                return result

        runner = ChatCompletionTargetRunner(chat=CheckedChat(), model=state["target_model"])
        result = await runner.run_target(target, request)
        await check()
        return result


class ProfileRoutingTargetRunner:
    """按执行 profile 选择正式适配器（spec 101：两个 runner 按 profile
    选择并存）。

    - dashboard/task_api -> ScenarioDashboardSSERunner（SSE stream=true
      消费既有 /chat/completions，正式端口取真实工具证据）；runner 按
      target_model 缓存，模型切换时重建；
    - cli -> ScenarioCliRunner（T11：容器内 subprocess 调真实 n-agent
      CLI 入口，冻结配置纪律同源）；
    - 其余（非场景 agent 路径、无 profile）-> GuardedChatTarget（进程内
      非流式契约不变）。

    dashboard 分支沿用冻结配置纪律：执行前/后复核 configuration
    fingerprint，配置漂移拒绝执行（ExecutionNotReadyError）。
    """

    def __init__(self, *, guarded, dashboard_factory, readiness, cli_factory=None, acp_factory=None) -> None:
        self._guarded = guarded
        self._dashboard_factory = dashboard_factory
        self._readiness = readiness
        self._cli_factory = cli_factory
        self._acp_factory = acp_factory
        self._dashboard = None
        self._dashboard_model: str | None = None
        self._cli = None
        self._cli_model: str | None = None
        self._acp = None
        self._acp_model: str | None = None

    async def run_target(self, target, request):
        profile = request.get("profile")
        # T11：cli 独立通道（subprocess 真实入口），不与 dashboard 混用
        if profile == "cli" and self._cli_factory is not None:
            expected = request["target_config"]["execution_fingerprint"]

            async def check_cli():
                state = await self._readiness()
                if not state.get("ready") or state["config_fingerprint"] != expected:
                    raise ExecutionNotReadyError("configuration_changed")
                return state

            state = await check_cli()
            model = state.get("target_model")
            if self._cli is None or self._cli_model != model:
                self._cli = self._cli_factory()
                self._cli_model = model
            result = await self._cli.run_target(target, request)
            await check_cli()
            return result
        # T12：acp 独立通道（spawn 真实 n-agent acp 子进程），冻结配置
        # 纪律同 cli；普通 agent 适配不能替代 ACP
        if profile == "acp" and self._acp_factory is not None:
            expected = request["target_config"]["execution_fingerprint"]

            async def check_acp():
                state = await self._readiness()
                if not state.get("ready") or state["config_fingerprint"] != expected:
                    raise ExecutionNotReadyError("configuration_changed")
                return state

            state = await check_acp()
            model = state.get("target_model")
            if self._acp is None or self._acp_model != model:
                self._acp = self._acp_factory()
                self._acp_model = model
            result = await self._acp.run_target(target, request)
            await check_acp()
            return result
        # T10：task_api 与 dashboard 同走场景适配器（task_create 步骤不经
        # chat 通道；task_api 题无 chat 轮次时适配器零 SSE 调用）
        if profile not in ("dashboard", "task_api"):
            return await self._guarded.run_target(target, request)
        expected = request["target_config"]["execution_fingerprint"]

        async def check():
            state = await self._readiness()
            if not state.get("ready") or state["config_fingerprint"] != expected:
                raise ExecutionNotReadyError("configuration_changed")
            return state

        state = await check()
        model = state.get("target_model")
        if self._dashboard is None or self._dashboard_model != model:
            self._dashboard = self._dashboard_factory(model)
            self._dashboard_model = model
        result = await self._dashboard.run_target(target, request)
        await check()
        return result


def build_execution_service(*, settings, holder, registry, chat, service, ledger, review, evidence, worker, config_service, session_prepare=None, scenario_memory_facts=None, browser_state_reader=None, browser_observer=None, resource_ledger=None, task_creator=None, task_reader=None, task_runs_reader=None, artifact_lister=None, workspace_file_reader=None, task_intent_before=None, task_intent_confirmed=None, task_intent_unknown=None, workspace_resource_observer=None, cli_runner=None, acp_runner=None):
    async def readiness():
        current = holder.current_config
        worker_ready = worker.is_running
        enabled = config_service.current_sync().evaluation_enabled
        judge_ready = bool(settings.evaluation_judge_provider_base_url and settings.evaluation_judge_provider_api_key and settings.evaluation_judge_provider_model)
        return {
            "ready": bool(current and worker_ready and enabled), "worker_ready": worker_ready,
            "judge_ready": judge_ready, "target_model": current.model if current else None,
            "judge_model": settings.evaluation_judge_provider_model or None,
            "self_judge": bool(current and current.model == settings.evaluation_judge_provider_model),
            "supports_vision": bool(current and current.supports_vision),
            "config_fingerprint": await execution_configuration(settings, holder, registry),
        }

    runner = EvaluationExperimentRunner(
        service=service, ledger=ledger, evidence=evidence,
        target_runner=ProfileRoutingTargetRunner(
            guarded=GuardedChatTarget(chat=chat, readiness=readiness),
            dashboard_factory=lambda model: ScenarioDashboardSSERunner(
                base_url=settings.dashboard_base_url, model=model,
                session_prepare=session_prepare,
                browser_state_reader=browser_state_reader,
                browser_observer=browser_observer,
                # T10：task/制品/文件端口（组合根注入；None 时 task_create
                # 步骤按 task_port_unwired 业务失败，不伪造证据）
                task_creator=task_creator,
                task_reader=task_reader,
                task_runs_reader=task_runs_reader,
                artifact_lister=artifact_lister,
                workspace_file_reader=workspace_file_reader,
                task_intent_before=task_intent_before,
                task_intent_confirmed=task_intent_confirmed,
                task_intent_unknown=task_intent_unknown,
                workspace_resource_observer=workspace_resource_observer,
            ),
            readiness=readiness,
            # T11：cli 通道独立工厂（组合根注入 ScenarioCliRunner；None
            # 时 cli profile 不准入）
            cli_factory=(lambda: cli_runner) if cli_runner is not None else None,
            # T12：acp 通道独立工厂（组合根注入 ScenarioAcpRunner；None
            # 时 acp profile 不准入）
            acp_factory=(lambda: acp_runner) if acp_runner is not None else None,
        ),
        experiment_root_scope=experiment_item_root_scope,
        environment_fingerprint=build_frozen_environment,
        redactor=LogSanitizer().sanitize,
    )
    # plan T5：只读预检（submit 前整单复核；valid != runnable）。
    # 证据保留期未配置（<=0）时预检 blocked——不临时无限保留（spec 6.2）
    from app.application.evaluation_preflight import EvaluationPreflightService

    # T6：dashboard 通道在 base_url 可用时视为已接线（SSE 适配器就绪）；
    # 未配置时不准入，preflight 整单 blocked。
    # T10：task_api 通道在 create 端口接线时准入（TaskService 由组合根
    # 注入；task_enabled=False 时不注入，相应 profile blocked）
    admitted = ("agent", "dashboard") if settings.dashboard_base_url else ("agent",)
    if task_creator is not None:
        admitted = admitted + ("task_api",)
    # T11：cli 通道在 CLI runner 接线时准入（组合根注入；未接线时
    # 相应 profile blocked）
    if cli_runner is not None:
        admitted = admitted + ("cli",)
    # T12：acp 通道在 ACP runner 接线时准入（未接线时 profile blocked）
    if acp_runner is not None:
        admitted = admitted + ("acp",)
    # T8 S4：场景绑定材料核验——服务端权威事实（合成测试事实，非个人
    # 记忆）的 sha256 与 case binding 声明同源比对；未提供事实表时不
    # 接线（无绑定题的集合不受影响，绑定题按缺失 blocked）
    material_hashes_reader = None
    if scenario_memory_facts:
        import hashlib as _hashlib

        _facts = dict(scenario_memory_facts)

        async def material_hashes_reader(dataset_id, dataset_version):
            return {
                alias: _hashlib.sha256(fact.encode("utf-8")).hexdigest()
                for alias, fact in _facts.items()
            }

    preflight = EvaluationPreflightService(
        review=review,
        readiness=readiness,
        ledger=ledger,
        evidence_retention_seconds=float(
            settings.evaluation_evidence_retention_days * 86400
        ),
        admitted_profiles=admitted,
        material_hashes_reader=material_hashes_reader,
    )
    return EvaluationExecutionService(
        store=SQLiteExecutionStore(str(ledger.path)), ledger=ledger, review=review,
        runner=runner, readiness=readiness, evidence=evidence,
        preflight=preflight, admitted_profiles=admitted,
        # T9 S4 缺口修复：场景资源账本接到执行单 settle——没有该接线，
        # owned 资源（memory_binding/browser）无人在终局释放
        resource_ledger=resource_ledger,
        evidence_retention_seconds=settings.evaluation_evidence_retention_days * 86400,
    )
