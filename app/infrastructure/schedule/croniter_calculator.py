from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from app.domain.schedule import ScheduleExpression, ScheduleTimezone


class ScheduleCalculationError(ValueError):
    pass


class CroniterScheduleCalculator:
    def validate(self, expression: ScheduleExpression, timezone_value: ScheduleTimezone) -> None:
        self._timezone(timezone_value)
        if not croniter.is_valid(expression.value):
            raise ScheduleCalculationError(f"invalid cron expression: {expression.value}")

    def next_after(self, expression: ScheduleExpression, base_time: datetime, timezone_value: ScheduleTimezone) -> datetime:
        self.validate(expression, timezone_value)
        tz = self._timezone(timezone_value)
        base_utc = base_time if base_time.tzinfo else base_time.replace(tzinfo=timezone.utc)
        base_local = base_utc.astimezone(tz)
        next_local = croniter(expression.value, base_local).get_next(datetime)
        return next_local.astimezone(timezone.utc)

    def _timezone(self, timezone_value: ScheduleTimezone) -> ZoneInfo:
        try:
            return ZoneInfo(timezone_value.value)
        except Exception as exc:
            raise ScheduleCalculationError(f"invalid timezone: {timezone_value.value}") from exc
