"""Validated configuration for external data request observability."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class ExternalDataRequestLogSettings:
    enabled: bool
    cleanup_enabled: bool
    successful_retention_days: int
    error_retention_days: int
    cleanup_batch_size: int


def load_external_data_request_log_settings() -> ExternalDataRequestLogSettings:
    return ExternalDataRequestLogSettings(
        enabled=_bool("EXTERNAL_DATA_REQUEST_LOG_ENABLED", True),
        cleanup_enabled=_bool("EXTERNAL_DATA_REQUEST_LOG_CLEANUP_ENABLED", True),
        successful_retention_days=_bounded_int("EXTERNAL_DATA_REQUEST_LOG_SUCCESS_RETENTION_DAYS", 30, 7, 365),
        error_retention_days=_bounded_int("EXTERNAL_DATA_REQUEST_LOG_ERROR_RETENTION_DAYS", 90, 14, 730),
        cleanup_batch_size=_bounded_int("EXTERNAL_DATA_REQUEST_LOG_CLEANUP_BATCH_SIZE", 500, 10, 5000),
    )
