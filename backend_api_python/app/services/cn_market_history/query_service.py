"""Local raw and adjusted A-share daily-history queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from .models import AdjustmentMode
from .repository import CNMarketHistoryRepository


class AdjustmentCoverageError(RuntimeError):
    code = "cn_history.adjustment_coverage_incomplete"


@dataclass(frozen=True)
class HistoryQueryResult:
    frame: pd.DataFrame
    provenance: dict


class CNMarketHistoryQueryService:
    def __init__(self, repository: CNMarketHistoryRepository | None = None) -> None:
        self.repository = repository or CNMarketHistoryRepository()

    def load(
        self,
        instrument: str,
        start_date: date,
        end_date: date,
        *,
        mode: AdjustmentMode = AdjustmentMode.RAW,
        provider: str = "easy_tdx",
    ) -> HistoryQueryResult:
        context_start = start_date - timedelta(days=14)
        bars = self.repository.fetch_daily_bars(
            instrument, context_start, end_date, provider=provider
        )
        factors: dict[date, Decimal] = {}
        factor_version = ""
        if mode is not AdjustmentMode.RAW:
            factor_version, factor_rows = self.repository.fetch_active_factors(
                instrument, mode, context_start, end_date
            )
            factors = {row["trade_date"]: Decimal(row["factor"]) for row in factor_rows}
            missing = [bar.trade_date for bar in bars if bar.trade_date not in factors]
            if not factor_version or missing:
                raise AdjustmentCoverageError(
                    f"Missing {mode.value} factors for {len(missing) or len(bars)} sessions"
                )

        records = []
        for bar in bars:
            factor = factors.get(bar.trade_date, Decimal("1"))
            records.append(
                {
                    "date": bar.trade_date,
                    "open": float(bar.open * factor),
                    "high": float(bar.high * factor),
                    "low": float(bar.low * factor),
                    "close": float(bar.close * factor),
                    "volume": float(bar.volume),
                    "amount": float(bar.amount),
                }
            )
        frame = pd.DataFrame.from_records(records)
        if not frame.empty:
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("date")))
            frame.index.name = "date"
            frame["previous_close"] = frame["close"].shift(1)
            frame = frame[frame.index.date >= start_date].copy()
            self._attach_rule_context(frame, instrument, start_date, end_date)
        data_version = self.repository.get_data_version(
            instrument, start_date, end_date, provider=provider
        )
        provider_version = bars[-1].provider_version if bars else ""
        return HistoryQueryResult(
            frame=frame,
            provenance={
                "instrument": instrument,
                "provider": provider,
                "providerVersion": provider_version,
                "dataVersion": data_version,
                "adjustmentMode": mode.value,
                "factorVersion": factor_version or None,
                "firstTradeDate": frame.index[0].date().isoformat() if not frame.empty else None,
                "lastTradeDate": frame.index[-1].date().isoformat() if not frame.empty else None,
            },
        )

    def _attach_rule_context(
        self,
        frame: pd.DataFrame,
        instrument: str,
        start_date: date,
        end_date: date,
    ) -> None:
        frame["board_classification"] = ""
        frame["status_classification"] = ""
        frame["classification_confirmed"] = False
        frame["is_suspended"] = False
        fetch_context = getattr(self.repository, "fetch_rule_context", None)
        if not callable(fetch_context):
            return
        context = fetch_context(instrument, start_date, end_date)
        classifications = list(context.get("classifications") or [])
        statuses = list(context.get("statuses") or [])
        status_by_date: dict[date, list[dict]] = {}
        for row in statuses:
            if row.get("confirmed"):
                status_by_date.setdefault(row["trade_date"], []).append(row)

        board_names = {"main_board", "star_board", "chinext"}
        state_names = {"st", "non_st", "delisting"}
        for timestamp in frame.index:
            trade_date = pd.Timestamp(timestamp).date()
            active = [
                row
                for row in classifications
                if row.get("confirmed")
                and row["effective_start"] <= trade_date
                and (row.get("effective_end") is None or row["effective_end"] >= trade_date)
            ]
            boards = {
                row["classification"]
                for row in active
                if row["classification"] in board_names
            }
            states = {
                row["classification"]
                for row in active
                if row["classification"] in state_names
            }
            daily_statuses = status_by_date.get(trade_date, [])
            if any(row.get("status") == "suspended" for row in daily_statuses):
                frame.at[timestamp, "is_suspended"] = True
            if len(boards) == 1:
                frame.at[timestamp, "board_classification"] = next(iter(boards))
            if len(states) == 1:
                frame.at[timestamp, "status_classification"] = next(iter(states))
            frame.at[timestamp, "classification_confirmed"] = (
                len(boards) == 1 and len(states) == 1
            )
