"""Market-data loading for Strategy API V2 backtests."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from app.data_sources import DataSourceFactory
from app.services.backtest_cache import KlineCache
from app.services.cn_market_history import (
    AdjustmentMode,
    CNMarketHistoryQueryService,
    CNMarketHistoryQualityService,
    load_cn_market_history_settings,
    parse_cn_instrument,
)
from app.services.cn_market_history.query_service import AdjustmentCoverageError
from app.services.cn_market_history.calendar import previous_a_share_sessions
from app.services.market_schedule import latest_completed_session
from app.utils.logger import get_logger

logger = get_logger(__name__)
_cache = KlineCache()

TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}

PROVIDER_TIMEFRAMES = {
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
    "1w": "1W",
}


class CNHistoryCoverageError(ValueError):
    code = "strategyV2.cnHistoryCoverageIncomplete"

    def __init__(self, instruments: list[dict[str, Any]]):
        self.details = {
            "code": self.code,
            "market": "CNStock",
            "instruments": instruments,
        }
        super().__init__(self.code)

    @classmethod
    def for_instrument(
        cls,
        *,
        instrument: str,
        requested_start: date,
        requested_end: date,
        warmup_start: date | None,
        warmup_end: date | None,
        available_through: date,
        adjustment_mode: AdjustmentMode,
        issues: list[dict[str, Any]],
    ) -> "CNHistoryCoverageError":
        sync_start = min(
            item for item in (warmup_start, requested_start) if item is not None
        )
        return cls(
            [
                {
                    "instrument": instrument,
                    "requestedRange": _date_range(requested_start, requested_end),
                    "warmupRange": _date_range(warmup_start, warmup_end),
                    "availableThrough": available_through.isoformat(),
                    "adjustmentMode": adjustment_mode.value,
                    "issues": issues,
                    "suggestedAction": {
                        "type": "admin_targeted_sync",
                        "instrument": instrument,
                        "startDate": sync_start.isoformat(),
                        "endDate": requested_end.isoformat(),
                    },
                }
            ]
        )

    @classmethod
    def combine(cls, errors: list["CNHistoryCoverageError"]) -> "CNHistoryCoverageError":
        instruments = []
        for error in errors:
            instruments.extend(error.details.get("instruments") or [])
        return cls(instruments)


def _normalize_utc_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime, interpreting naive inputs as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_strategy_frame(
    market: str,
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
    *,
    market_type: Optional[str] = None,
    exchange_id: Optional[str] = None,
) -> pd.DataFrame:
    normalized_timeframe = str(timeframe or "1d").strip().lower()
    if (
        str(market or "").strip() == "CNStock"
        and normalized_timeframe == "1d"
        and load_cn_market_history_settings().enabled
    ):
        return load_cn_strategy_frame(
            market,
            symbol,
            normalized_timeframe,
            start_date,
            end_date,
            requested_start=start_date,
            warmup_bars=0,
        )
    start_utc = _normalize_utc_datetime(start_date)
    end_utc = _normalize_utc_datetime(end_date)
    total_seconds = max(1.0, (end_utc - start_utc).total_seconds())
    timeframe_seconds = TIMEFRAME_SECONDS.get(normalized_timeframe, 86400)
    provider_timeframe = PROVIDER_TIMEFRAMES.get(normalized_timeframe, normalized_timeframe)
    limit = int(math.ceil(total_seconds / timeframe_seconds * 1.15) + 200)
    after_time = int((start_utc - timedelta(seconds=timeframe_seconds)).timestamp())
    before_time = int((end_utc + timedelta(seconds=timeframe_seconds)).timestamp())
    cache_key = ":".join((
        str(market),
        str(symbol),
        str(timeframe),
        str(market_type or ""),
        str(exchange_id or ""),
        start_utc.isoformat(),
        end_utc.isoformat(),
    ))
    cached = _cache.get(cache_key)
    if cached is not None and not cached.empty:
        return cached.copy()
    try:
        rows = DataSourceFactory.get_kline(
            market=market,
            symbol=symbol,
            timeframe=provider_timeframe,
            limit=limit,
            before_time=before_time,
            after_time=after_time,
            exchange_id=exchange_id,
            market_type=market_type,
        )
    except Exception as exc:
        logger.warning(
            "Strategy market-data fetch failed for %s:%s %s via %s/%s: %s",
            market,
            symbol,
            timeframe,
            exchange_id or "default",
            market_type or "default",
            exc,
        )
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    time_column = next((name for name in ("time", "timestamp", "datetime", "date") if name in frame.columns), "")
    if not time_column:
        return pd.DataFrame()
    raw_time = frame.pop(time_column)
    numeric = pd.to_numeric(raw_time, errors="coerce")
    if numeric.notna().any():
        unit = "ms" if float(numeric.dropna().abs().median()) > 10_000_000_000 else "s"
        converted = pd.to_datetime(numeric, unit=unit, errors="coerce", utc=True)
        frame.index = pd.DatetimeIndex(converted).tz_convert(None)
    else:
        converted = pd.to_datetime(raw_time, errors="coerce", utc=True)
        frame.index = pd.DatetimeIndex(converted).tz_convert(None)
    frame = frame[~frame.index.isna()].sort_index()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    if any(column not in frame.columns for column in ("open", "high", "low", "close")):
        return pd.DataFrame()
    for column in ("open", "high", "low", "close", "volume"):
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    requested_start = pd.Timestamp(start_utc).tz_localize(None)
    requested_end = pd.Timestamp(end_utc).tz_localize(None)
    frame = frame[(frame.index >= requested_start) & (frame.index <= requested_end)].dropna(
        subset=["open", "high", "low", "close"]
    )
    closed_bar_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=timeframe_seconds)
    if requested_end >= closed_bar_cutoff:
        frame = frame[frame.index <= pd.Timestamp(closed_bar_cutoff)]
    if not frame.empty:
        _cache.put(cache_key, frame, timeframe)
    return frame.copy()


def load_cn_strategy_frame(
    market: str,
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
    *,
    requested_start: datetime,
    warmup_bars: int,
    market_type: Optional[str] = None,
    exchange_id: Optional[str] = None,
    adjustment_mode: AdjustmentMode = AdjustmentMode.RAW,
    provider: str = "easy_tdx",
    quality_service: CNMarketHistoryQualityService | None = None,
    query_service: CNMarketHistoryQueryService | None = None,
    completed_through: date | None = None,
) -> pd.DataFrame:
    del market_type, exchange_id
    if str(market or "").strip() != "CNStock" or str(timeframe).lower() != "1d":
        raise ValueError("strategyV2.cnHistoryDailyOnly")

    instrument = parse_cn_instrument(f"CNStock:{symbol}").canonical
    fetch_start = start_date.date()
    requested_start_date = requested_start.date()
    requested_end = end_date.date()
    available_through = completed_through or latest_completed_session("CNStock").date()
    effective_end = min(requested_end, available_through)
    warmup_sessions = previous_a_share_sessions(
        requested_start_date, warmup_bars
    )
    warmup_start = warmup_sessions[0] if warmup_sessions else None
    warmup_end = warmup_sessions[-1] if warmup_sessions else None

    if effective_end < requested_start_date:
        raise CNHistoryCoverageError.for_instrument(
            instrument=instrument,
            requested_start=requested_start_date,
            requested_end=requested_end,
            warmup_start=warmup_start,
            warmup_end=warmup_end,
            available_through=available_through,
            adjustment_mode=adjustment_mode,
            issues=[
                {
                    "scope": "requested",
                    "type": "unfinished_session",
                    "startDate": requested_start_date.isoformat(),
                    "endDate": requested_end.isoformat(),
                }
            ],
        )

    quality = quality_service or CNMarketHistoryQualityService()
    issues: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    if warmup_start is not None and warmup_end is not None and warmup_start <= warmup_end:
        warmup = quality.assess(
            instrument, warmup_start, warmup_end, provider=provider, persist=False
        )
        coverage["warmup"] = _coverage_metadata(warmup.report)
        issues.extend(_assessment_issues("warmup", warmup))
        if warmup.report.actual_sessions < warmup_bars:
            issues.append(
                {
                    "scope": "warmup",
                    "type": "warmup_bars_incomplete",
                    "requiredBars": int(warmup_bars),
                    "availableBars": int(warmup.report.actual_sessions),
                    "startDate": warmup_start.isoformat(),
                    "endDate": warmup_end.isoformat(),
                }
            )

    requested = quality.assess(
        instrument,
        requested_start_date,
        effective_end,
        provider=provider,
        persist=False,
    )
    coverage["requested"] = _coverage_metadata(requested.report)
    issues.extend(_assessment_issues("requested", requested))
    if issues:
        error = CNHistoryCoverageError.for_instrument(
            instrument=instrument,
            requested_start=requested_start_date,
            requested_end=requested_end,
            warmup_start=warmup_start,
            warmup_end=warmup_end,
            available_through=available_through,
            adjustment_mode=adjustment_mode,
            issues=issues,
        )
        error.details["instruments"][0]["coverage"] = coverage
        raise error

    query = query_service or CNMarketHistoryQueryService()
    query_start = min(fetch_start, warmup_start) if warmup_start else fetch_start
    try:
        result = query.load(
            instrument,
            query_start,
            effective_end,
            mode=adjustment_mode,
            provider=provider,
        )
    except AdjustmentCoverageError as exc:
        raise CNHistoryCoverageError.for_instrument(
            instrument=instrument,
            requested_start=requested_start_date,
            requested_end=requested_end,
            warmup_start=warmup_start,
            warmup_end=warmup_end,
            available_through=available_through,
            adjustment_mode=adjustment_mode,
            issues=[
                {
                    "scope": "adjustment",
                    "type": "adjustment_factor_incomplete",
                    "startDate": query_start.isoformat(),
                    "endDate": effective_end.isoformat(),
                }
            ],
        ) from exc

    frame = result.frame.copy()
    if frame.empty:
        raise CNHistoryCoverageError.for_instrument(
            instrument=instrument,
            requested_start=requested_start_date,
            requested_end=requested_end,
            warmup_start=warmup_start,
            warmup_end=warmup_end,
            available_through=available_through,
            adjustment_mode=adjustment_mode,
            issues=[
                {
                    "scope": "requested",
                    "type": "local_history_empty",
                    "startDate": fetch_start.isoformat(),
                    "endDate": effective_end.isoformat(),
                }
            ],
        )
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None).normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    requested_frame = frame[
        (frame.index.date >= requested_start_date) & (frame.index.date <= effective_end)
    ]
    missing_rule_dates = [
        pd.Timestamp(timestamp).date()
        for timestamp, row in requested_frame.iterrows()
        if not bool(row.get("classification_confirmed"))
        or not _positive_number(row.get("previous_close"))
    ]
    if missing_rule_dates:
        raise CNHistoryCoverageError.for_instrument(
            instrument=instrument,
            requested_start=requested_start_date,
            requested_end=requested_end,
            warmup_start=warmup_start,
            warmup_end=warmup_end,
            available_through=available_through,
            adjustment_mode=adjustment_mode,
            issues=[
                {
                    "scope": "market_rules",
                    "type": "market_rule_data_incomplete",
                    "startDate": min(missing_rule_dates).isoformat(),
                    "endDate": max(missing_rule_dates).isoformat(),
                    "dates": [item.isoformat() for item in missing_rule_dates],
                }
            ],
        )
    frame.attrs["cn_history_provenance"] = {
        **result.provenance,
        "coverage": coverage,
        "requestedStart": requested_start_date.isoformat(),
        "requestedEnd": requested_end.isoformat(),
        "availableThrough": available_through.isoformat(),
        "truncatedUnfinishedSession": requested_end > effective_end,
    }
    return frame


def _assessment_issues(scope: str, assessment: Any) -> list[dict[str, Any]]:
    issues = []
    for gap in assessment.report.gaps:
        issues.append(
            {
                "scope": scope,
                "type": gap.reason,
                "startDate": gap.start_date.isoformat(),
                "endDate": gap.end_date.isoformat(),
                "dates": [item.isoformat() for item in gap.dates],
            }
        )
    for finding in assessment.findings:
        if str(finding.severity.value) != "blocking":
            continue
        issues.append(
            {
                "scope": scope,
                "type": finding.finding_type,
                "startDate": finding.start_date.isoformat(),
                "endDate": finding.end_date.isoformat(),
                "evidence": dict(finding.evidence),
            }
        )
    if not assessment.report.complete and not issues:
        issues.append(
            {
                "scope": scope,
                "type": "coverage_incomplete",
                "startDate": assessment.report.requested_start.isoformat(),
                "endDate": assessment.report.requested_end.isoformat(),
            }
        )
    return issues


def _coverage_metadata(report: Any) -> dict[str, Any]:
    return {
        "requestedStart": report.requested_start.isoformat(),
        "requestedEnd": report.requested_end.isoformat(),
        "firstTradeDate": report.first_trade_date.isoformat()
        if report.first_trade_date
        else None,
        "lastTradeDate": report.last_trade_date.isoformat()
        if report.last_trade_date
        else None,
        "expectedSessions": int(report.expected_sessions),
        "actualSessions": int(report.actual_sessions),
        "blockingFindings": int(report.blocking_findings),
        "complete": bool(report.complete),
        "dataVersion": report.data_version,
    }


def _date_range(start: date | None, end: date | None) -> dict[str, str] | None:
    if start is None or end is None:
        return None
    return {"startDate": start.isoformat(), "endDate": end.isoformat()}


def _positive_number(value: Any) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0
