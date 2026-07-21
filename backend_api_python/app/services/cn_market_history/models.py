"""Provider-neutral contracts for durable China A-share history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping


class AdjustmentMode(StrEnum):
    RAW = "raw"
    FORWARD = "forward"
    BACKWARD = "backward"


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class QualitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class CNInstrument:
    code: str
    exchange: str
    canonical: str

    @property
    def tdx_market(self) -> int:
        return 1 if self.exchange == "SH" else 0


@dataclass(frozen=True, slots=True)
class CNInstrumentMetadata:
    instrument: CNInstrument
    name: str
    security_type: str
    listed_on: date | None
    delisted_on: date | None
    source: str
    source_version: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class CNClassification:
    instrument: CNInstrument
    classification: str
    effective_start: date
    effective_end: date | None
    source: str
    source_version: str
    confirmed: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawDailyBar:
    instrument: CNInstrument
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal
    provider: str
    provider_version: str
    content_hash: str
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class CorporateAction:
    instrument: CNInstrument
    event_date: date
    category: int
    event_name: str
    cash_dividend: Decimal | None = None
    rights_price: Decimal | None = None
    bonus_ratio: Decimal | None = None
    rights_ratio: Decimal | None = None
    consolidation_ratio: Decimal | None = None
    provider: str = "easy_tdx"
    provider_version: str = ""
    content_hash: str = ""
    collected_at: datetime | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class AdjustmentFactor:
    instrument: CNInstrument
    trade_date: date
    mode: AdjustmentMode
    factor: Decimal
    algorithm_version: str
    factor_version: str
    anchor_date: date
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class DataProvenance:
    instrument: str
    provider: str
    provider_version: str
    data_version: str
    adjustment_mode: AdjustmentMode
    factor_version: str | None
    first_trade_date: date
    last_trade_date: date
    as_of: datetime


@dataclass(frozen=True, slots=True)
class CoverageGap:
    start_date: date
    end_date: date
    reason: str
    dates: tuple[date, ...] = ()


@dataclass(frozen=True, slots=True)
class CoverageReport:
    instrument: str
    requested_start: date
    requested_end: date
    first_trade_date: date | None
    last_trade_date: date | None
    expected_sessions: int
    actual_sessions: int
    gaps: tuple[CoverageGap, ...] = ()
    blocking_findings: int = 0
    complete: bool = False
    data_version: str = ""


@dataclass(frozen=True, slots=True)
class QualityFinding:
    instrument: str
    finding_type: str
    severity: QualitySeverity
    start_date: date
    end_date: date
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    host: str
    latency_ms: float | None
    healthy: bool
    checked_at: datetime
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    last_success_at: datetime | None = None
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class SyncTarget:
    instrument: CNInstrument
    start_date: date
    end_date: date
    adjustment_mode: AdjustmentMode = AdjustmentMode.RAW
