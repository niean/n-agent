from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


class ScheduledTaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    SESSION_MISSING = "session_missing"
    DELETED = "deleted"


class ScheduledTaskExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED_MISSED = "skipped_missed"


class ScheduledExecutionPolicyMode(str, Enum):
    UNATTENDED = "unattended"


class DeliveryTargetType(str, Enum):
    ORIGIN = "origin"
    DASHBOARD = "dashboard"
    SILENT = "silent"


@dataclass(frozen=True)
class ScheduleExpression:
    value: str


@dataclass(frozen=True)
class ScheduleTimezone:
    value: str = "Asia/Shanghai"


@dataclass(frozen=True)
class DeliveryTarget:
    target_type: DeliveryTargetType
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def origin(cls, context: dict[str, Any]) -> DeliveryTarget:
        return cls(DeliveryTargetType.ORIGIN, dict(context))

    @classmethod
    def dashboard(cls) -> DeliveryTarget:
        return cls(DeliveryTargetType.DASHBOARD)

    @classmethod
    def silent(cls) -> DeliveryTarget:
        return cls(DeliveryTargetType.SILENT)


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class PromptSafetyResult:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class ScheduledExecutionPolicy:
    mode: ScheduledExecutionPolicyMode = ScheduledExecutionPolicyMode.UNATTENDED
    tool_exposure_policy: str = "safe_only"
    allow_confirm_tools: bool = False


@dataclass(frozen=True)
class ScheduledTask:
    id: str
    name: str
    prompt: str
    schedule: ScheduleExpression
    timezone: ScheduleTimezone
    session_id: str
    delivery_target: DeliveryTarget
    next_run_at: datetime
    enabled: bool = True
    status: ScheduledTaskStatus = ScheduledTaskStatus.ACTIVE
    origin: dict[str, Any] = field(default_factory=dict)
    execution_policy: ScheduledExecutionPolicy = field(default_factory=ScheduledExecutionPolicy)
    lease_until: datetime | None = None
    lease_owner: str | None = None
    claim_id: str | None = None
    last_run_at: datetime | None = None
    last_status: ScheduledTaskExecutionStatus | None = None
    last_error: str | None = None
    last_delivery_error: str | None = None
    unread_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ScheduledTaskClaim:
    task: ScheduledTask
    claim_id: str
    lease_owner: str
    lease_until: datetime
    claimed_next_run_at: datetime
    next_run_at: datetime
    reason: str
    skipped_missed: bool = False


@dataclass(frozen=True)
class ScheduledTaskExecution:
    id: str
    task_id: str
    session_id: str
    claim_id: str
    lease_owner: str
    status: ScheduledTaskExecutionStatus
    claimed_next_run_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output: str | None = None
    error: str | None = None
    delivery_status: str | None = None
    delivery_error: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ScheduledTaskLease:
    lease_until: datetime
    lease_owner: str = field(default_factory=lambda: str(uuid4()))
    claim_id: str = field(default_factory=lambda: str(uuid4()))


class ScheduleCalculator(Protocol):
    def validate(self, expression: ScheduleExpression, timezone: ScheduleTimezone) -> None:
        ...

    def next_after(self, expression: ScheduleExpression, base_time: datetime, timezone: ScheduleTimezone) -> datetime:
        ...


class PromptSafetyScanner(Protocol):
    def scan(self, prompt: str) -> PromptSafetyResult:
        ...


class OutboundDelivery(Protocol):
    async def deliver(self, target: DeliveryTarget, content: str) -> DeliveryResult:
        ...


class ScheduledTaskRegistry(Protocol):
    async def create(self, task: ScheduledTask) -> ScheduledTask:
        ...

    async def list(self) -> list[ScheduledTask]:
        ...

    async def get(self, task_id: str) -> ScheduledTask | None:
        ...

    async def update(self, task: ScheduledTask) -> ScheduledTask:
        ...

    async def update_status(self, task_id: str, status: ScheduledTaskStatus, enabled: bool) -> ScheduledTask:
        ...

    async def delete(self, task_id: str) -> bool:
        ...

    async def claim_due_tasks(
        self,
        now: datetime,
        limit: int,
        lease_seconds: int,
    ) -> list[ScheduledTaskClaim]:
        ...

    async def claim_task_for_run_now(
        self,
        task_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ScheduledTaskClaim | None:
        ...

    async def record_execution_started(self, execution: ScheduledTaskExecution) -> ScheduledTaskExecution:
        ...

    async def record_execution_completed(self, execution: ScheduledTaskExecution) -> bool:
        ...

    async def record_delivery_result(self, execution: ScheduledTaskExecution) -> bool:
        ...

    async def list_executions(self, task_id: str, limit: int) -> list[ScheduledTaskExecution]:
        ...

    async def mark_dashboard_unread(self, task_id: str, claim_id: str, lease_owner: str) -> bool:
        ...

    async def clear_dashboard_unread(self, task_id: str) -> bool:
        ...

    async def mark_session_missing(self, session_id: str) -> int:
        ...

    async def list_recoverable_origin_tasks(self) -> list[ScheduledTask]:
        ...
