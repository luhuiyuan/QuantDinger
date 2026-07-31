from datetime import datetime, timezone

import pytest

from app.services.task_control.cron import CronExpression


def test_cron_supports_five_fields_and_timezone():
    cron = CronExpression.parse("30 20 * * *")
    result = cron.next_after(datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc), "Asia/Shanghai")
    assert result.isoformat() == "2026-07-31T20:30:00+08:00"


def test_cron_supports_lists_ranges_and_steps():
    cron = CronExpression.parse("*/15 9-10 * * MON-FRI")
    assert cron.next_after(datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc), "Asia/Shanghai").minute == 0


def test_cron_day_of_month_and_day_of_week_use_standard_or_semantics():
    cron = CronExpression.parse("0 0 1 * MON")
    monday = cron.next_after(datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc), "Asia/Shanghai")
    assert monday.weekday() == 0


@pytest.mark.parametrize("expression", ["* * * *", "60 * * * *", "*/0 * * * *", "1-0 * * * *"])
def test_cron_rejects_invalid_expression(expression):
    with pytest.raises(ValueError):
        CronExpression.parse(expression)
