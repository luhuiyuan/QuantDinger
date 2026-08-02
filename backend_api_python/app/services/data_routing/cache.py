"""Capability-scoped Routing Cache contracts and storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from .models import CapabilityDefinition
from .quality import QualityWarning


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def routing_cache_key(
    capability: CapabilityDefinition,
    subject: Mapping[str, Any],
    constraints: Mapping[str, Any],
    mode: str,
) -> str:
    semantics: dict[str, Any] = {}
    for field in capability.cache_key_fields:
        semantics[field] = dict(subject) if field == "subject" else constraints.get(field)
    payload = {
        "capability_key": capability.key,
        "capability_version": capability.version,
        "mode": mode,
        "semantics": semantics,
    }
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return f"data-routing:{capability.key}:{digest}"


def content_digest(data: Any) -> str:
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RoutingCacheEntry:
    key: str
    capability_key: str
    data: Any
    provider_public_name: str
    provider_instance_id: int
    adapter_key: str
    acquired_at: datetime
    stored_at: datetime
    quality_warnings: tuple[QualityWarning, ...]
    quality_profiles: tuple[str, ...]
    content_digest: str


class RoutingCacheStore(Protocol):
    def get(self, key: str) -> Mapping[str, Any] | None: ...
    def set(self, key: str, value: Mapping[str, Any], ttl: int) -> None: ...


class RoutingCache:
    """JSON-safe wrapper over the existing Redis/process CacheManager boundary."""

    def __init__(self, store: RoutingCacheStore | None = None):
        if store is None:
            from app.utils.cache import CacheManager

            store = CacheManager()
        self.store = store

    @staticmethod
    def enabled(capability: CapabilityDefinition) -> bool:
        return str(capability.freshness_contract.get("storage_class") or "routing_cache") == "routing_cache"

    def get(self, key: str) -> RoutingCacheEntry | None:
        raw = self.store.get(key)
        if not isinstance(raw, Mapping):
            return None
        try:
            return RoutingCacheEntry(
                key=str(raw["key"]),
                capability_key=str(raw["capability_key"]),
                data=raw["data"],
                provider_public_name=str(raw["provider_public_name"]),
                provider_instance_id=int(raw["provider_instance_id"]),
                adapter_key=str(raw["adapter_key"]),
                acquired_at=datetime.fromisoformat(str(raw["acquired_at"])),
                stored_at=datetime.fromisoformat(str(raw["stored_at"])),
                quality_warnings=tuple(
                    QualityWarning(str(item["code"]), str(item["message"]), dict(item.get("details") or {}))
                    for item in raw.get("quality_warnings") or ()
                ),
                quality_profiles=tuple(str(item) for item in raw.get("quality_profiles") or ()),
                content_digest=str(raw["content_digest"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def put(
        self,
        *,
        key: str,
        capability: CapabilityDefinition,
        data: Any,
        provider_public_name: str,
        provider_instance_id: int,
        adapter_key: str,
        acquired_at: datetime,
        quality_warnings: tuple[QualityWarning, ...],
        quality_profiles: tuple[str, ...],
        now: datetime | None = None,
    ) -> None:
        if not self.enabled(capability):
            return
        stored_at = now or datetime.now(timezone.utc)
        stale_seconds = max(
            int(capability.freshness_contract.get("stale_seconds") or 0),
            int(capability.freshness_contract.get("fresh_seconds") or 0),
        )
        if stale_seconds <= 0:
            return
        value = {
            "key": key,
            "capability_key": capability.key,
            "data": data,
            "provider_public_name": provider_public_name,
            "provider_instance_id": provider_instance_id,
            "adapter_key": adapter_key,
            "acquired_at": acquired_at.isoformat(),
            "stored_at": stored_at.isoformat(),
            "quality_warnings": [
                {"code": item.code, "message": item.message, "details": dict(item.details)}
                for item in quality_warnings
            ],
            "quality_profiles": list(quality_profiles),
            "content_digest": content_digest(data),
        }
        self.store.set(key, value, stale_seconds)
