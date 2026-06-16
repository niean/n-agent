from datetime import datetime, timezone

import pytest

from app.domain.schedule import ScheduleExpression, ScheduleTimezone
from app.infrastructure.schedule.croniter_calculator import CroniterScheduleCalculator, ScheduleCalculationError


def test_croniter_calculator_computes_next_run_in_timezone():
    calculator = CroniterScheduleCalculator()
    base = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)

    next_run = calculator.next_after(ScheduleExpression("0 9 * * *"), base, ScheduleTimezone("Asia/Shanghai"))

    assert next_run == datetime(2026, 6, 16, 1, 0, tzinfo=timezone.utc)


def test_croniter_calculator_accepts_valid_cron_and_timezone():
    CroniterScheduleCalculator().validate(ScheduleExpression("*/5 * * * *"), ScheduleTimezone("Asia/Shanghai"))


def test_croniter_calculator_rejects_invalid_cron():
    with pytest.raises(ScheduleCalculationError):
        CroniterScheduleCalculator().validate(ScheduleExpression("bad cron"), ScheduleTimezone("Asia/Shanghai"))


def test_croniter_calculator_rejects_invalid_timezone():
    with pytest.raises(ScheduleCalculationError):
        CroniterScheduleCalculator().validate(ScheduleExpression("*/5 * * * *"), ScheduleTimezone("Not/AZone"))


def test_croniter_calculator_treats_naive_base_as_utc():
    calculator = CroniterScheduleCalculator()
    base = datetime(2026, 6, 16, 0, 0)

    next_run = calculator.next_after(ScheduleExpression("0 9 * * *"), base, ScheduleTimezone("UTC"))

    assert next_run.tzinfo is timezone.utc
    assert next_run == datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
