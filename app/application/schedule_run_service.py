from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.application.information_flow_service import InformationFlowService
from app.application.policy_snapshot import InformationFlowPolicyConfig
from app.domain.gateway import GatewaySessionKey
from app.domain.gateway_policy import GatewayDeliveryDecision, GatewayOutboundRequest, GatewayPolicy
from app.domain.information_flow import ReleaseTarget, SecretCatalog
from app.domain.policy import PolicyOutcome
from app.domain.schedule import (
    DeliveryResult,
    DeliveryTargetType,
    OutboundDelivery,
    ScheduledTask,
    ScheduledTaskClaim,
    ScheduledTaskExecution,
    ScheduledTaskExecutionStatus,
    ScheduledTaskRegistry,
)
from app.domain.schedule_policy import (
    DeliveryMode,
    MissedAction,
    SchedulePolicy,
)


logger = logging.getLogger(__name__)


class ScheduleRunService:
    def __init__(
        self,
        registry: ScheduledTaskRegistry,
        executor,
        outbound_delivery: OutboundDelivery,
        max_due_per_tick: int = 5,
        lease_seconds: int = 900,
        missed_grace_seconds: int = 300,
        recover_missing_origin_sessions=None,
        schedule_policy: SchedulePolicy | None = None,
        information_flow_service: InformationFlowService | None = None,
        gateway_policy: GatewayPolicy | None = None,
    ):
        self.registry = registry
        self.executor = executor
        self.outbound_delivery = outbound_delivery
        self.max_due_per_tick = max_due_per_tick
        self.lease_seconds = lease_seconds
        self.recover_missing_origin_sessions = recover_missing_origin_sessions
        self._schedule_policy = schedule_policy or SchedulePolicy(missed_grace_seconds=missed_grace_seconds)
        self._information_flow = information_flow_service or InformationFlowService(
            InformationFlowPolicyConfig(),
            SecretCatalog(),
        )
        self._gateway_policy = gateway_policy or GatewayPolicy()

    async def run_now(self, task_id: str, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        await self._recover_missing_origin_sessions()
        claim = await self.registry.claim_task_for_run_now(task_id, now, self.lease_seconds)
        if claim is None:
            return {"task_id": task_id, "status": "not_claimed"}
        asyncio.create_task(self._run_claim_safe(claim, now))
        return {"task_id": task_id, "status": "triggered", "claim_id": claim.claim_id}

    async def _run_claim_safe(self, claim: ScheduledTaskClaim, now: datetime | None = None) -> None:
        try:
            await self.run_claim(claim, now=now)
        except Exception:
            logger.exception("scheduled task run failed: task_id=%s", claim.task.id)

    async def run_due_claims(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        await self._recover_missing_origin_sessions()
        claims = await self.registry.claim_due_tasks(now, self.max_due_per_tick, self.lease_seconds)
        return [await self.run_claim(claim, now=now) for claim in claims]

    async def _recover_missing_origin_sessions(self) -> None:
        if self.recover_missing_origin_sessions is not None:
            await self.recover_missing_origin_sessions()

    async def run_claim(self, claim: ScheduledTaskClaim, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        decision = self._schedule_policy.evaluate(claim, now)

        started_at = now
        execution = ScheduledTaskExecution(
            id=f"sched-exec-{uuid4().hex}",
            task_id=claim.task.id,
            session_id=claim.task.session_id,
            claim_id=claim.claim_id,
            lease_owner=claim.lease_owner,
            claimed_next_run_at=claim.claimed_next_run_at,
            started_at=started_at,
            status=ScheduledTaskExecutionStatus.RUNNING,
        )
        await self.registry.record_execution_started(execution)

        # --- Post-claim deny: record blocked and complete claim normally ---
        if decision.verdict is PolicyOutcome.DENY:
            status = ScheduledTaskExecutionStatus.BLOCKED
            completed = self._completed(execution, status, started_at, error=decision.reason)
            await self.registry.record_execution_completed(completed)
            return {"task_id": claim.task.id, "status": status.value, "reason": decision.reason}

        # --- Missed grace: skipped_missed (record, don't run) ---
        if decision.missed_action is MissedAction.SKIPPED_MISSED:
            completed = self._completed(execution, ScheduledTaskExecutionStatus.SKIPPED_MISSED, started_at)
            await self.registry.record_execution_completed(completed)
            return {"task_id": claim.task.id, "status": ScheduledTaskExecutionStatus.SKIPPED_MISSED.value}

        # --- Run the agent ---
        result = await self.executor.run(claim.task)
        completed = self._completed(execution, result.status, started_at, result.output, result.error)
        if not await self.registry.record_execution_completed(completed):
            return {"task_id": claim.task.id, "status": "stale"}

        # --- Silent: ends BEFORE any outbound client call ---
        if decision.delivery_mode is DeliveryMode.SILENT:
            return {"task_id": claim.task.id, "status": result.status.value}

        # --- Delivery: InformationFlow release -> GatewayPolicy -> OutboundDelivery ---
        content = result.output or result.error or "scheduled task completed"
        if result.status is ScheduledTaskExecutionStatus.BLOCKED:
            content = f"Scheduled task blocked: {result.error or 'blocked'}"

        delivery_result = await self._deliver(claim.task, content)
        delivered = ScheduledTaskExecution(
            **{
                **completed.__dict__,
                "delivery_status": delivery_result.status,
                "delivery_error": delivery_result.error,
            }
        )
        await self.registry.record_delivery_result(delivered)
        if claim.task.delivery_target.target_type is DeliveryTargetType.DASHBOARD:
            await self.registry.mark_dashboard_unread(claim.task.id, claim.claim_id, claim.lease_owner)
        return {"task_id": claim.task.id, "status": result.status.value, "delivery_status": delivery_result.status}

    async def _deliver(self, task: ScheduledTask, content: str) -> DeliveryResult:
        """Delivery pipeline: InformationFlow release -> GatewayPolicy -> OutboundDelivery.

        SILENT: handled before this method is called (ends before any outbound call).
        DASHBOARD: no external client call, no Gateway check needed.
        ORIGIN: full pipeline -- release, gateway ownership, then deliver.
        """
        target = task.delivery_target

        # Dashboard: no external client call, no Gateway check needed.
        if target.target_type is DeliveryTargetType.DASHBOARD:
            return await self.outbound_delivery.deliver(target, content)

        # ORIGIN: external client call -- go through the full pipeline.
        # 1. InformationFlow release (GATEWAY target) -- sanitize content.
        release = self._information_flow.release(
            content,
            ReleaseTarget.GATEWAY,
            origin="scheduled_delivery",
        )
        if not release.allowed or release.content is None:
            return DeliveryResult("failed", "information_flow_denied")
        safe_content = release.content

        # 2. GatewayPolicy.evaluate_outbound -- origin ownership check.
        gateway_decision = self._evaluate_gateway_outbound(task)
        if gateway_decision.verdict is PolicyOutcome.DENY:
            return DeliveryResult("failed", f"gateway_denied: {gateway_decision.reason}")

        # 3. OutboundDelivery -- actual delivery with sanitized content.
        return await self.outbound_delivery.deliver(target, safe_content)

    def _evaluate_gateway_outbound(self, task: ScheduledTask) -> GatewayDeliveryDecision:
        """Construct GatewayOutboundRequest from task origin and delivery target."""
        origin = task.origin or {}
        target_ctx = task.delivery_target.context or {}

        origin_platform = str(origin.get("platform") or "")
        origin_receive_id = str(origin.get("receive_id") or "")
        origin_thread = str(origin.get("thread_id") or "")
        target_platform = str(target_ctx.get("platform") or origin_platform)
        target_receive_id = str(target_ctx.get("receive_id") or origin_receive_id)
        target_thread = str(target_ctx.get("thread_id") or origin_thread)

        origin_key = GatewaySessionKey(
            source=origin_platform,
            platform_session_id=origin_receive_id,
            thread_id=origin_thread,
        )
        target_key = GatewaySessionKey(
            source=target_platform,
            platform_session_id=target_receive_id,
            thread_id=target_thread,
        )
        # Actor: use receive_id as actor proxy for schedule deliveries.
        origin_actor = origin.get("actor_id") or origin_receive_id or None
        target_actor = target_ctx.get("actor_id") or origin_actor

        request = GatewayOutboundRequest(
            target_session_key=target_key,
            origin_session_key=origin_key,
            target_actor_id=target_actor,
            origin_actor_id=origin_actor,
        )
        return self._gateway_policy.evaluate_outbound(request)

    def _completed(
        self,
        execution: ScheduledTaskExecution,
        status: ScheduledTaskExecutionStatus,
        completed_at: datetime,
        output: str | None = None,
        error: str | None = None,
    ) -> ScheduledTaskExecution:
        return ScheduledTaskExecution(
            **{
                **execution.__dict__,
                "status": status,
                "completed_at": completed_at,
                "output": output,
                "error": error,
            }
        )
