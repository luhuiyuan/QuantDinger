"""Disk-space guardrails for durable market-history writes."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum

from .config import CNMarketHistorySettings


class DiskGuardLevel(StrEnum):
    OK = "ok"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True, slots=True)
class DiskGuardStatus:
    path: str
    level: DiskGuardLevel
    total_bytes: int
    used_bytes: int
    free_bytes: int
    soft_free_bytes: int
    hard_free_bytes: int

    @property
    def allows_new_sync(self) -> bool:
        return self.level is DiskGuardLevel.OK

    @property
    def allows_current_write(self) -> bool:
        return self.level is not DiskGuardLevel.HARD


class DiskGuard:
    def __init__(self, settings: CNMarketHistorySettings) -> None:
        self.settings = settings

    def check(self) -> DiskGuardStatus:
        usage = shutil.disk_usage(self.settings.disk_path)
        if usage.free <= self.settings.disk_hard_free_bytes:
            level = DiskGuardLevel.HARD
        elif usage.free <= self.settings.disk_soft_free_bytes:
            level = DiskGuardLevel.SOFT
        else:
            level = DiskGuardLevel.OK
        return DiskGuardStatus(
            path=self.settings.disk_path,
            level=level,
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
            soft_free_bytes=self.settings.disk_soft_free_bytes,
            hard_free_bytes=self.settings.disk_hard_free_bytes,
        )
