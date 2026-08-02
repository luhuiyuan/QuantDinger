"""Request-local safe provenance for incremental business API responses."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any


_latest_routed_result: ContextVar[Any | None] = ContextVar(
    "latest_routed_data_result",
    default=None,
)


def clear_routed_result() -> None:
    _latest_routed_result.set(None)


def record_routed_result(result: Any) -> None:
    _latest_routed_result.set(result)


def get_routed_result() -> Any | None:
    return _latest_routed_result.get()


def safe_provenance_headers() -> dict[str, str]:
    result = get_routed_result()
    if result is None:
        return {}

    request_id = getattr(result, "routed_request_id", None)
    provider = getattr(result, "provider_public_name", None)
    acquired_at = getattr(result, "acquired_at", None)
    freshness = getattr(result, "freshness", None)
    warnings = getattr(result, "quality_warnings", ()) or ()
    if not request_id or not provider or not acquired_at or not freshness:
        return {}

    if isinstance(acquired_at, datetime):
        if acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=timezone.utc)
        acquired_value = acquired_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        acquired_value = str(acquired_at)

    headers = {
        "X-Routed-Data-Request-ID": str(request_id),
        "X-Data-Provider": str(provider),
        "X-Data-Acquired-At": acquired_value,
        "X-Data-Freshness": str(freshness),
    }
    if warnings:
        headers["X-Data-Quality-Warning-Count"] = str(len(warnings))
    return headers
