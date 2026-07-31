"""Stable internal Task Run error classification."""

from __future__ import annotations


TEMPORARY_ERROR_CODES = frozenset({
    "TimeoutError", "ReadTimeout", "ConnectTimeout", "ConnectionError", "ConnectionResetError", "ProxyError", "RateLimitError",
    "provider_timeout", "provider_rate_limited", "network_error", "temporary_provider_error",
    "TemporaryProviderError",
})


class TemporaryProviderError(RuntimeError):
    """A checkpoint-safe provider failure eligible for in-Run retry."""


class PermanentTaskError(RuntimeError):
    """A domain failure that requires an administrator-created retry Run."""


def is_temporary_error(error_code: str, configured: tuple[str, ...] = ()) -> bool:
    return str(error_code) in TEMPORARY_ERROR_CODES or str(error_code) in set(configured)


def retry_delay_seconds(attempt_no: int, *, base: float = 2.0, maximum: float = 300.0, jitter: float = 0.25) -> float:
    import random

    raw = min(float(maximum), float(base) * (2 ** max(0, int(attempt_no) - 1)))
    return max(0.0, raw + random.uniform(0.0, raw * max(0.0, float(jitter))))
