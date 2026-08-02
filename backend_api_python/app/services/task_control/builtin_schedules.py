"""Initial schedule materialization for code-registered maintenance tasks.

These rows are inserted only when absent.  Once materialized, Cron, timezone,
parameters and enabled state belong to the database and are managed through
Task Management; a deployment restart never overwrites operator revisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BuiltinSchedule:
    task_key: str
    cron_expression: str
    timezone_name: str = "Asia/Shanghai"
    parameters: dict[str, Any] | None = None
    enabled: bool = True


BUILTIN_SCHEDULES = (
    BuiltinSchedule("reflection_cycle", "0 3 * * *"),
    BuiltinSchedule("ai_calibration_cycle", "15 3 * * *"),
    BuiltinSchedule("market_catalog_sync", "30 3 * * *"),
    BuiltinSchedule("cn_market_history_daily", "0 19 * * 1-5"),
    BuiltinSchedule("cn_stock_quote_refresh", "*/5 * * * *"),
    BuiltinSchedule("cn_fundamental_incremental", "30 20 * * *"),
    BuiltinSchedule("runtime_metadata_cleanup", "0 2 * * *"),
    BuiltinSchedule("external_data_request_log_cleanup", "10 2 * * *"),
    BuiltinSchedule("task_event_cleanup", "20 2 * * *", parameters={"retention_days": 90, "batch_size": 10000}),
    BuiltinSchedule("provider_capability_revalidation", "45 4 * * *", parameters={"limit": 10}),
    BuiltinSchedule("provider_health_maintenance", "*/15 * * * *", parameters={"limit": 5}),
    # The scheduler-worker writes a process heartbeat every scan.  This
    # registered task remains available for an auditable manual health probe,
    # but is intentionally not scheduled to avoid duplicate heartbeat rows.
)
