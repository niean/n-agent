import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.application.information_flow_service import ReleaseResult
from app.application.schedule_run_service import ScheduleRunService
from app.application.scheduled_agent_executor import ScheduledAgentResult
from app.domain.gateway_policy import GatewayPolicy
from app.domain.information_flow import (
    InformationReleaseDecision,
    ReleaseTarget,
)
from app.domain.policy import PolicyOutcome
from app.domain.schedule import (
    DeliveryResult,
    DeliveryTarget,
    DeliveryTargetType,
    ScheduledTask,
    ScheduledTaskClaim,
    ScheduledTaskExecutionStatus,
    ScheduleExpression,
    ScheduleTimezone,
)


NOW = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)


def _task(*, next_run_at=None, delivery_target=None):
    return ScheduledTask(
        id="task-1",
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("* * * * *"),
        timezone=ScheduleTimezone("UTC"),
        session_id="session-1",
        delivery_target=delivery_target or DeliveryTarget.dashboard(),
        next_run_at=next_run_at or NOW,
    )


def _claim(*, claimed_next_run_at=None, task=None, skipped=False):
    """Build a claim.  claimed_next_run_at defaults to NOW (on-time).

    For skipped_missed tests, pass claimed_next_run_at in the past
    (beyond missed_grace_seconds=300).
    """
    task = task or _task()
    ct = claimed_next_run_at or task.next_run_at
    return ScheduledTaskClaim(
        task,
        "claim-1",
        "owner-1",
        NOW + timedelta(seconds=900),
        ct,
        ct,
        "due",
        skipped,
    )


class FakeRegistry:
    def __init__(self, claim=None, stale=False):
        self.claim = claim
        self.started = []
        self.completed = []
        self.delivered = []
        self.stale = stale
        self.dashboard_unread = False

    async def claim_task_for_run_now(self, task_id, now, lease_seconds):
        return self.claim

    async def claim_due_tasks(self, now, limit, lease_seconds):
        return [self.claim] if self.claim else []

    async def record_execution_started(self, execution):
        self.started.append(execution)
        return execution

    async def record_execution_completed(self, execution):
        self.completed.append(execution)
        return not self.stale

    async def record_delivery_result(self, execution):
        self.delivered.append(execution)
        return not self.stale

    async def mark_dashboard_unread(self, task_id, claim_id, lease_owner):
        self.dashboard_unread = True
        return True


class FakeExecutor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def run(self, task):
        self.calls.append(task)
        return self.result


class FakeDelivery:
    def __init__(self):
        self.contents = []

    async def deliver(self, target, content):
        self.contents.append(content)
        return DeliveryResult("success")


@pytest.mark.asyncio
async def test_run_due_claims_recovers_missing_origin_sessions_before_claiming():
    registry = FakeRegistry(_claim())
    recovered = []

    async def recover():
        recovered.append(True)

    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        FakeDelivery(),
        recover_missing_origin_sessions=recover,
    )

    await service.run_due_claims(now=NOW)

    assert recovered == [True]


@pytest.mark.asyncio
async def test_run_now_claims_and_runs_shared_path():
    registry = FakeRegistry(_claim())
    delivery = FakeDelivery()
    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
    )

    result = await service.run_now("task-1", now=NOW)

    await _wait_for(lambda: bool(registry.completed))
    assert result["status"] == "triggered"
    assert result["claim_id"] == "claim-1"
    assert registry.started
    assert registry.completed[0].status is ScheduledTaskExecutionStatus.SUCCEEDED
    assert delivery.contents == ["done"]
    assert registry.dashboard_unread is True


@pytest.mark.asyncio
async def test_skipped_missed_does_not_execute_or_deliver():
    """Two-segment model: SchedulePolicy detects past grace post-claim."""
    past = NOW - timedelta(seconds=301)
    registry = FakeRegistry(_claim(claimed_next_run_at=past))
    executor = FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done"))
    delivery = FakeDelivery()
    service = ScheduleRunService(registry, executor, delivery, missed_grace_seconds=300)

    await service.run_due_claims(now=NOW)

    assert executor.calls == []
    assert delivery.contents == []
    assert registry.completed[0].status is ScheduledTaskExecutionStatus.SKIPPED_MISSED


@pytest.mark.asyncio
async def test_blocked_execution_delivers_safety_summary():
    registry = FakeRegistry(_claim())
    delivery = FakeDelivery()
    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.BLOCKED, error="blocked prompt")),
        delivery,
    )

    await service.run_due_claims(now=NOW)

    assert registry.completed[0].status is ScheduledTaskExecutionStatus.BLOCKED
    assert "blocked prompt" in delivery.contents[0]


@pytest.mark.asyncio
async def test_stale_completion_does_not_deliver():
    registry = FakeRegistry(_claim(), stale=True)
    delivery = FakeDelivery()
    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
    )

    await service.run_due_claims(now=NOW)

    assert delivery.contents == []


@pytest.mark.asyncio
async def test_silent_delivery_ends_before_outbound_call():
    """silent delivery_mode -> no outbound client call."""
    task = _task(delivery_target=DeliveryTarget.silent())
    registry = FakeRegistry(_claim(task=task))
    delivery = FakeDelivery()
    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
    )

    await service.run_due_claims(now=NOW)

    assert delivery.contents == []
    assert registry.completed[0].status is ScheduledTaskExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_deny_records_blocked_and_completes_claim():
    """Post-claim deny -> record BLOCKED + complete claim normally."""
    from app.domain.schedule import ScheduledTaskStatus

    task = ScheduledTask(
        **{**_task().__dict__, "enabled": False, "status": ScheduledTaskStatus.PAUSED}
    )
    registry = FakeRegistry(_claim(task=task))
    executor = FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done"))
    delivery = FakeDelivery()
    service = ScheduleRunService(registry, executor, delivery)

    await service.run_due_claims(now=NOW)

    assert executor.calls == []
    assert delivery.contents == []
    assert registry.completed[0].status is ScheduledTaskExecutionStatus.BLOCKED


# ---------------------------------------------------------------------------
# S6: ORIGIN delivery pipeline tests (InformationFlow -> GatewayPolicy -> Outbound)
# ---------------------------------------------------------------------------


class FakeInformationFlow:
    """Spy for InformationFlowService.release()."""

    def __init__(self, *, allowed: bool = True, sanitized_content: str | None = None):
        self._allowed = allowed
        self._sanitized = sanitized_content
        self.release_calls: list[dict] = []

    def release(self, content, target, *, classification=None, origin="", labels=frozenset()):
        self.release_calls.append({"content": content, "target": target, "origin": origin})
        if self._allowed:
            return ReleaseResult(
                allowed=True,
                content=self._sanitized if self._sanitized is not None else content,
                error=None,
                decision=InformationReleaseDecision(
                    verdict=PolicyOutcome.ALLOW,
                    transform=None,
                    allowed_fields=frozenset(),
                    retention="raw",
                    audit_level="summary",
                    reason="test_allow",
                ),
            )
        return ReleaseResult(
            allowed=False,
            content=None,
            error="denied",
            decision=InformationReleaseDecision(
                verdict=PolicyOutcome.DENY,
                transform=None,
                allowed_fields=frozenset(),
                retention="none",
                audit_level="summary",
                reason="test_deny",
            ),
        )


def _origin_task(*, origin=None, target_ctx=None):
    """Build a task with ORIGIN delivery target and matching origin context."""
    ctx = origin or {"platform": "feishu", "receive_id": "oc_1", "receive_id_type": "chat_id", "thread_id": ""}
    return ScheduledTask(
        id="task-1",
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("* * * * *"),
        timezone=ScheduleTimezone("UTC"),
        session_id="session-1",
        delivery_target=DeliveryTarget.origin(target_ctx or ctx),
        next_run_at=NOW,
        origin=dict(ctx),
    )


@pytest.mark.asyncio
async def test_origin_delivery_full_pipeline():
    """ORIGIN delivery: InformationFlow release -> GatewayPolicy allow -> OutboundDelivery called."""
    info_flow = FakeInformationFlow(sanitized_content="sanitized:done")
    delivery = FakeDelivery()
    service = ScheduleRunService(
        FakeRegistry(_claim(task=_origin_task())),
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
        information_flow_service=info_flow,
        gateway_policy=GatewayPolicy(),
    )

    await service.run_due_claims(now=NOW)

    # InformationFlow.release called with GATEWAY target
    assert len(info_flow.release_calls) == 1
    assert info_flow.release_calls[0]["target"] is ReleaseTarget.GATEWAY
    assert info_flow.release_calls[0]["content"] == "done"

    # OutboundDelivery called with sanitized content (not original)
    assert delivery.contents == ["sanitized:done"]

    # Delivery recorded as success
    assert registry_delivered_status(service) == "success"


@pytest.mark.asyncio
async def test_origin_delivery_gateway_denied():
    """ORIGIN delivery: GatewayPolicy denies on origin mismatch -> OutboundDelivery NOT called."""
    origin_ctx = {"platform": "feishu", "receive_id": "oc_1", "receive_id_type": "chat_id", "thread_id": ""}
    target_ctx = {"platform": "feishu", "receive_id": "oc_different", "receive_id_type": "chat_id", "thread_id": ""}
    info_flow = FakeInformationFlow()
    delivery = FakeDelivery()
    service = ScheduleRunService(
        FakeRegistry(_claim(task=_origin_task(origin=origin_ctx, target_ctx=target_ctx))),
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
        information_flow_service=info_flow,
        gateway_policy=GatewayPolicy(),
    )

    await service.run_due_claims(now=NOW)

    # InformationFlow was called (release happens before gateway check)
    assert len(info_flow.release_calls) == 1

    # OutboundDelivery NOT called (gateway denied)
    assert delivery.contents == []

    # Delivery recorded as failed with gateway_denied
    status = registry_delivered_status(service)
    assert status == "failed"
    assert "gateway_denied" in (registry_delivered_error(service) or "")


@pytest.mark.asyncio
async def test_origin_delivery_information_flow_denied():
    """ORIGIN delivery: InformationFlow denies -> OutboundDelivery NOT called."""
    info_flow = FakeInformationFlow(allowed=False)
    delivery = FakeDelivery()
    service = ScheduleRunService(
        FakeRegistry(_claim(task=_origin_task())),
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
        information_flow_service=info_flow,
        gateway_policy=GatewayPolicy(),
    )

    await service.run_due_claims(now=NOW)

    # InformationFlow was called
    assert len(info_flow.release_calls) == 1

    # OutboundDelivery NOT called (information flow denied)
    assert delivery.contents == []

    # Delivery recorded as failed with information_flow_denied
    status = registry_delivered_status(service)
    assert status == "failed"
    assert "information_flow_denied" in (registry_delivered_error(service) or "")


def registry_delivered_status(service: ScheduleRunService) -> str | None:
    """Extract delivery_status from the last delivered execution."""
    delivered = service.registry.delivered  # type: ignore[attr-defined]
    if not delivered:
        return None
    return delivered[-1].delivery_status


def registry_delivered_error(service: ScheduleRunService) -> str | None:
    """Extract delivery_error from the last delivered execution."""
    delivered = service.registry.delivered  # type: ignore[attr-defined]
    if not delivered:
        return None
    return delivered[-1].delivery_error


async def _wait_for(predicate, attempts: int = 20) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("background scheduled task did not finish")
