"""Environment-backed settings for China A-share history collection."""

from __future__ import annotations

import os
from dataclasses import dataclass


GIB = 1024**3


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True, slots=True)
class CNMarketHistorySettings:
    enabled: bool
    sync_enabled: bool
    page_size: int
    write_batch_size: int
    request_interval_seconds: float
    provider_timeout_seconds: float
    provider_retry_attempts: int
    max_targets_per_run: int
    incremental_lookback_days: int
    daily_symbols: tuple[str, ...]
    disk_path: str
    disk_soft_free_bytes: int
    disk_hard_free_bytes: int

    def __post_init__(self) -> None:
        if self.disk_soft_free_bytes <= self.disk_hard_free_bytes:
            raise ValueError("CN history disk soft threshold must exceed hard threshold")


def load_cn_market_history_settings() -> CNMarketHistorySettings:
    return CNMarketHistorySettings(
        enabled=_bool_env("CN_HISTORY_ENABLED", False),
        sync_enabled=_bool_env("CN_HISTORY_SYNC_ENABLED", False),
        page_size=_int_env("CN_HISTORY_PAGE_SIZE", 800, minimum=1, maximum=800),
        write_batch_size=_int_env("CN_HISTORY_WRITE_BATCH_SIZE", 200, minimum=1, maximum=2000),
        request_interval_seconds=_float_env(
            "CN_HISTORY_REQUEST_INTERVAL_SEC", 0.25, minimum=0.0, maximum=10.0
        ),
        provider_timeout_seconds=_float_env(
            "CN_HISTORY_PROVIDER_TIMEOUT_SEC", 15.0, minimum=1.0, maximum=120.0
        ),
        provider_retry_attempts=_int_env(
            "CN_HISTORY_PROVIDER_RETRY_ATTEMPTS", 3, minimum=0, maximum=10
        ),
        max_targets_per_run=_int_env(
            "CN_HISTORY_MAX_TARGETS_PER_RUN", 50, minimum=1, maximum=500
        ),
        incremental_lookback_days=_int_env(
            "CN_HISTORY_INCREMENTAL_LOOKBACK_DAYS", 14, minimum=3, maximum=90
        ),
        daily_symbols=tuple(
            item.strip()
            for item in os.getenv("CN_HISTORY_DAILY_SYMBOLS", "").split(",")
            if item.strip()
        ),
        disk_path=(os.getenv("CN_HISTORY_DISK_PATH", "/") or "/").strip(),
        disk_soft_free_bytes=_int_env(
            "CN_HISTORY_DISK_SOFT_FREE_BYTES", 5 * GIB, minimum=512 * 1024**2, maximum=1024**5
        ),
        disk_hard_free_bytes=_int_env(
            "CN_HISTORY_DISK_HARD_FREE_BYTES", 2 * GIB, minimum=256 * 1024**2, maximum=1024**5
        ),
    )
