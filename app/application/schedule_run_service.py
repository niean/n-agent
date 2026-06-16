from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.domain.schedule import (
    DeliveryTargetType,
    OutboundDelivery,
    ScheduledTaskClaim,
    ScheduledTaskExecution,
    ScheduledTaskExecutionStatus,
    ScheduledTaskRegistry,
)


class ScheduleRunService:
    def __init__(
        self,
        registry: ScheduledTaskRegistry,
        executor,
        outbound_delivery: OutboundDelivery,
        max_due_per_tick: int = 5,
        lease_seconds: int = 900,
    ):
        self.registry = registry
        self.executor = executor
        self.outbound_delivery = outbound_delivery
        self.max_due_per_tick = max_due_per_tick
        self.lease_seconds = lease_seconds

    async def run_now(self, task_id: str) -> dict:
        claim = await self.registry.claim_task_for_run_now(task_id, datetime.now(timezone.utc), self.lease_seconds)
        if claim is None:
            return {"task_id": task_id, "status": "not_claimed"}
        return await self.run_claim(claim)

    async def run_due_claims(self, now: datetime | None = None) -> list[dict]:
        claims = await self.registry.claim_due_tasks(now or datetime.now(timezone.utc), self.max_due_per_tick, self.lease_seconds)
        return [await self.run_claim(claim) for claim in claims]

    async def run_claim(self, claim: ScheduledTaskClaim) -> dict:
        started_at = datetime.now(timezone.utc)
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
        if claim.skipped_missed:
            completed = self._completed(execution, ScheduledTaskExecutionStatus.SKIPPED_MISSED, started_at)
            await self.registry.record_execution_completed(completed)
            return {"task_id": claim.task.id, "status": ScheduledTaskExecutionStatus.SKIPPED_MISSED.value}

        result = await self.executor.run(claim.task)
        completed = self._completed(execution, result.status, started_at, result.output, result.error)
        if not await self.registry.record_execution_completed(completed):
            return {"task_id": claim.task.id, "status": "stale"}

        if claim.task.delivery_target.target_type is DeliveryTargetType.SILENT:
            return {"task_id": claim.task.id, "status": result.status.value}

        content = result.output or result.error or "scheduled task completed"
        if result.status is ScheduledTaskExecutionStatus.BLOCKED:
            content = f"Scheduled task blocked: {result.error or 'blocked'}"
        delivery = await self.outbound_delivery.deliver(claim.task.delivery_target, content)
        delivered = ScheduledTaskExecution(
            **{
                **completed.__dict__,
                "delivery_status": delivery.status,
                "delivery_error": delivery.error,
            }
        )
        await self.registry.record_delivery_result(delivered)
        if claim.task.delivery_target.target_type is DeliveryTargetType.DASHBOARD:
            await self.registry.mark_dashboard_unread(claim.task.id, claim.claim_id, claim.lease_owner)
        return {"task_id": claim.task.id, "status": result.status.value, "delivery_status": delivery.status}

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
