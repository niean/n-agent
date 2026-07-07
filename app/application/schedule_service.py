from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.application.session_service import SessionService
from app.domain.schedule import (
    DeliveryTarget,
    DeliveryTargetType,
    PromptSafetyScanner,
    ScheduledTask,
    ScheduledTaskRegistry,
    ScheduledTaskStatus,
    ScheduleCalculator,
    ScheduleExpression,
    ScheduleTimezone,
)
from app.domain.session import SessionSource


class ScheduleServiceError(Exception):
    pass


class ScheduledTaskNotFoundError(ScheduleServiceError):
    pass


class ScheduleValidationError(ScheduleServiceError):
    pass


class ScheduleDeliveryContextError(ScheduleServiceError):
    pass


class ScheduledTaskNotRunnableError(ScheduleServiceError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ScheduledTaskCreateInput:
    name: str
    prompt: str
    cron_expression: str
    timezone: str = "Asia/Shanghai"
    delivery_target: str = "dashboard"
    origin: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None


@dataclass(frozen=True)
class ScheduledTaskUpdateInput:
    name: str | None = None
    prompt: str | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    delivery_target: str | None = None
    origin: dict[str, Any] | None = None
    session_id: str | None = None


RunNowCallable = Callable[[str], Awaitable[Any]]


class ScheduleService:
    def __init__(
        self,
        registry: ScheduledTaskRegistry,
        calculator: ScheduleCalculator,
        scanner: PromptSafetyScanner,
        session_service: SessionService,
        run_now_handler: RunNowCallable | None = None,
    ):
        self.registry = registry
        self.calculator = calculator
        self.scanner = scanner
        self.session_service = session_service
        self.run_now_handler = run_now_handler

    async def create(self, request: ScheduledTaskCreateInput) -> ScheduledTask:
        expression = ScheduleExpression(request.cron_expression)
        timezone_value = ScheduleTimezone(request.timezone)
        self._validate(expression, timezone_value, request.prompt)
        target = self._delivery_target(request.delivery_target, request.origin)
        session_id = request.session_id
        if session_id is None or target.target_type is DeliveryTargetType.ORIGIN:
            session_id = f"schedule-{uuid4()}"
            await self.session_service.create_session(session_id, source=SessionSource.SCHEDULE.value)
        now = datetime.now(timezone.utc)
        task = ScheduledTask(
            id=f"sched-{uuid4().hex}",
            name=request.name.strip() or "Scheduled Task",
            prompt=request.prompt,
            schedule=expression,
            timezone=timezone_value,
            session_id=session_id,
            origin=dict(request.origin),
            delivery_target=target,
            next_run_at=self.calculator.next_after(expression, now, timezone_value),
            created_at=now,
            updated_at=now,
        )
        return await self.registry.create(task)

    async def list(self) -> list[ScheduledTask]:
        return await self.registry.list()

    async def get(self, task_id: str) -> ScheduledTask:
        task = await self.registry.get(task_id)
        if task is None:
            raise ScheduledTaskNotFoundError(task_id)
        return task

    async def update(self, task_id: str, request: ScheduledTaskUpdateInput) -> ScheduledTask:
        task = await self.get(task_id)
        expression = ScheduleExpression(request.cron_expression or task.schedule.value)
        timezone_value = ScheduleTimezone(request.timezone or task.timezone.value)
        prompt = request.prompt if request.prompt is not None else task.prompt
        self._validate(expression, timezone_value, prompt)
        origin = request.origin if request.origin is not None else task.origin
        delivery_target = self._delivery_target(
            request.delivery_target or task.delivery_target.target_type.value,
            origin,
        )
        updated = ScheduledTask(
            **{
                **task.__dict__,
                "name": request.name if request.name is not None else task.name,
                "prompt": prompt,
                "schedule": expression,
                "timezone": timezone_value,
                "origin": dict(origin),
                "delivery_target": delivery_target,
                "session_id": request.session_id if request.session_id is not None else task.session_id,
                "next_run_at": self.calculator.next_after(expression, datetime.now(timezone.utc), timezone_value),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return await self.registry.update(updated)

    async def pause(self, task_id: str) -> ScheduledTask:
        await self.get(task_id)
        return await self.registry.update_status(task_id, ScheduledTaskStatus.PAUSED, False)

    async def resume(self, task_id: str) -> ScheduledTask:
        task = await self.get(task_id)
        refreshed = ScheduledTask(
            **{
                **task.__dict__,
                "next_run_at": self.calculator.next_after(task.schedule, datetime.now(timezone.utc), task.timezone),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await self.registry.update(refreshed)
        return await self.registry.update_status(task_id, ScheduledTaskStatus.ACTIVE, True)

    async def delete(self, task_id: str) -> bool:
        await self.get(task_id)
        return await self.registry.delete(task_id)

    async def run_now(self, task_id: str) -> Any:
        await self.recover_missing_origin_sessions()
        task = await self.get(task_id)
        if task.status is ScheduledTaskStatus.PAUSED or not task.enabled:
            raise ScheduledTaskNotRunnableError("scheduled_task_paused")
        if task.status is ScheduledTaskStatus.SESSION_MISSING:
            raise ScheduledTaskNotRunnableError("scheduled_task_session_missing")
        if self.run_now_handler is None:
            raise ScheduleServiceError("run_now is not configured")
        result = await self.run_now_handler(task_id)
        if isinstance(result, dict) and result.get("status") == "not_claimed":
            raise ScheduledTaskNotRunnableError("scheduled_task_claim_conflict")
        return result

    async def list_executions(self, task_id: str, limit: int = 10):
        await self.get(task_id)
        if limit < 1 or limit > 50:
            raise ScheduleValidationError("execution history limit must be between 1 and 50")
        return await self.registry.list_executions(task_id, limit)

    async def handle_session_deleted(self, session_id: str) -> int:
        return await self.registry.mark_session_missing(session_id)

    async def recover_missing_origin_sessions(self) -> int:
        recovered = 0
        for task in await self.registry.list_recoverable_origin_tasks():
            session_id = f"schedule-{uuid4()}"
            await self.session_service.create_session(session_id, source=SessionSource.SCHEDULE.value)
            await self.registry.update(
                ScheduledTask(
                    **{
                        **task.__dict__,
                        "session_id": session_id,
                        "enabled": True,
                        "status": ScheduledTaskStatus.ACTIVE,
                    }
                )
            )
            recovered += 1
        return recovered

    def _validate(self, expression: ScheduleExpression, timezone_value: ScheduleTimezone, prompt: str) -> None:
        try:
            self.calculator.validate(expression, timezone_value)
        except Exception as exc:
            raise ScheduleValidationError(str(exc)) from exc
        safety = self.scanner.scan(prompt)
        if not safety.allowed:
            raise ScheduleValidationError(safety.reason)

    def _delivery_target(self, target: str, origin: dict[str, Any]) -> DeliveryTarget:
        target_type = DeliveryTargetType(target)
        if target_type is DeliveryTargetType.ORIGIN:
            if origin.get("target") != "home" and (not origin.get("receive_id") or not origin.get("receive_id_type")):
                raise ScheduleDeliveryContextError("origin delivery requires receive_id and receive_id_type")
            return DeliveryTarget.origin(origin)
        if target_type is DeliveryTargetType.SILENT:
            return DeliveryTarget.silent()
        return DeliveryTarget.dashboard()
