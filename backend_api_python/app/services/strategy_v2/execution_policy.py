"""Market-specific execution constraints for the Strategy API V2 broker."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

import pandas as pd


class MarketExecutionPolicy:
    version = "default-v1"

    def begin_session(self, timestamp: Any, positions: Mapping[str, Any]) -> None:
        del timestamp, positions

    def validate_target(self, current_amount: float, target_amount: float) -> str:
        del current_amount, target_amount
        return ""

    def block_reason(
        self,
        *,
        symbol: str,
        side: str,
        bar: Mapping[str, Any] | None,
        open_price: float | None,
        timestamp: Any,
    ) -> str:
        del symbol, timestamp
        if bar is None or open_price is None or not math.isfinite(open_price) or open_price <= 0:
            return "no_price"
        if _truthy(bar.get("suspended")) or _truthy(bar.get("is_suspended")):
            return "suspended"
        if side == "buy" and (_truthy(bar.get("limit_up")) or _truthy(bar.get("is_limit_up"))):
            return "limit_up"
        if side == "sell" and (_truthy(bar.get("limit_down")) or _truthy(bar.get("is_limit_down"))):
            return "limit_down"
        return ""

    def lot_size(self, symbol: str, bar: Mapping[str, Any] | None) -> float:
        explicit = float((bar or {}).get("lot_size") or 0.0)
        if explicit > 0:
            return explicit
        return 1e-8 if str(symbol).startswith("Crypto:") else 1.0

    def normalize_delta(
        self,
        delta: float,
        *,
        current_amount: float,
        target_amount: float,
        lot_size: float,
    ) -> float:
        del current_amount, target_amount
        return _round_to_lot(delta, lot_size)

    def cap_sell_quantity(self, symbol: str, delta: float) -> tuple[float, str]:
        del symbol
        return delta, ""

    def max_affordable_buy(
        self,
        requested_quantity: float,
        *,
        price: float,
        cash: float,
        lot_size: float,
        trade_date: date,
    ) -> float:
        del trade_date
        cap = cash / max(price, 1e-12)
        return _round_to_lot(min(requested_quantity, cap), lot_size)

    def fee_breakdown(self, side: str, notional: float, trade_date: date) -> dict[str, float]:
        del side, trade_date
        return {
            "brokerCommission": 0.0,
            "minimumCommissionAdjustment": 0.0,
            "commission": 0.0,
            "stampDuty": 0.0,
            "transferFee": 0.0,
            "total": 0.0,
        }

    def record_fill(self, symbol: str, side: str, quantity: float) -> None:
        del symbol, side, quantity

    def assumptions(self) -> dict[str, Any]:
        return {"marketRuleVersion": self.version}


class DefaultExecutionPolicy(MarketExecutionPolicy):
    def __init__(self, commission_rate: float) -> None:
        self.commission_rate = max(0.0, float(commission_rate or 0.0))

    def max_affordable_buy(
        self,
        requested_quantity: float,
        *,
        price: float,
        cash: float,
        lot_size: float,
        trade_date: date,
    ) -> float:
        del trade_date
        cap = cash / max(price * (1.0 + self.commission_rate), 1e-12)
        return _round_to_lot(min(requested_quantity, cap), lot_size)

    def fee_breakdown(self, side: str, notional: float, trade_date: date) -> dict[str, float]:
        del side, trade_date
        commission = max(0.0, notional * self.commission_rate)
        return {
            "brokerCommission": commission,
            "minimumCommissionAdjustment": 0.0,
            "commission": commission,
            "stampDuty": 0.0,
            "transferFee": 0.0,
            "total": commission,
        }


class CNStockExecutionPolicy(MarketExecutionPolicy):
    version = "cn-stock-rules-2026.1"
    buy_lot_size = 100.0
    minimum_commission = 5.0

    def __init__(self, commission_rate: float = 0.0003) -> None:
        self.commission_rate = max(0.0, float(commission_rate or 0.0))
        self._session: date | None = None
        self._sellable: dict[str, float] = {}

    def begin_session(self, timestamp: Any, positions: Mapping[str, Any]) -> None:
        session = pd.Timestamp(timestamp).date()
        if self._session == session:
            return
        self._session = session
        self._sellable = {
            key: max(0.0, float(position.amount))
            for key, position in positions.items()
            if str(key).startswith("CNStock:") and float(position.amount) > 0
        }

    def validate_target(self, current_amount: float, target_amount: float) -> str:
        del current_amount
        return "long_only" if target_amount < -1e-12 else ""

    def block_reason(
        self,
        *,
        symbol: str,
        side: str,
        bar: Mapping[str, Any] | None,
        open_price: float | None,
        timestamp: Any,
    ) -> str:
        basic = super().block_reason(
            symbol=symbol,
            side=side,
            bar=bar,
            open_price=open_price,
            timestamp=timestamp,
        )
        if basic:
            return basic
        assert bar is not None
        prices = [bar.get(name) for name in ("open", "high", "low", "close")]
        if any(not _positive_finite(value) for value in prices):
            return "invalid_ohlc"
        if float(bar.get("volume") or 0.0) <= 0:
            return "no_liquidity"
        rate = self._price_limit_rate(symbol, bar, pd.Timestamp(timestamp).date())
        if rate is None:
            return "market_rule_data_incomplete"
        previous_close = float(bar.get("previous_close") or 0.0)
        if previous_close <= 0:
            return "market_rule_data_incomplete"
        upper = _tick_price(previous_close * (1.0 + rate))
        lower = _tick_price(previous_close * (1.0 - rate))
        tolerance = 0.0050001
        if side == "buy" and float(bar["low"]) >= upper - tolerance:
            return "limit_up"
        if side == "sell" and float(bar["high"]) <= lower + tolerance:
            return "limit_down"
        return ""

    def lot_size(self, symbol: str, bar: Mapping[str, Any] | None) -> float:
        del symbol, bar
        return self.buy_lot_size

    def normalize_delta(
        self,
        delta: float,
        *,
        current_amount: float,
        target_amount: float,
        lot_size: float,
    ) -> float:
        if delta < 0 and target_amount <= 1e-12 and current_amount > 0:
            return -current_amount
        return _round_to_lot(delta, lot_size)

    def cap_sell_quantity(self, symbol: str, delta: float) -> tuple[float, str]:
        if delta >= 0:
            return delta, ""
        requested = abs(delta)
        available = max(0.0, self._sellable.get(symbol, 0.0))
        if requested <= available + 1e-12:
            return delta, ""
        return -available, "t_plus_one"

    def max_affordable_buy(
        self,
        requested_quantity: float,
        *,
        price: float,
        cash: float,
        lot_size: float,
        trade_date: date,
    ) -> float:
        quantity = _round_to_lot(min(requested_quantity, cash / max(price, 1e-12)), lot_size)
        while quantity > 0:
            fees = self.fee_breakdown("buy", quantity * price, trade_date)["total"]
            if quantity * price + fees <= cash + 1e-9:
                return quantity
            quantity = max(0.0, quantity - lot_size)
        return 0.0

    def fee_breakdown(self, side: str, notional: float, trade_date: date) -> dict[str, float]:
        broker_commission = max(0.0, notional * self.commission_rate)
        minimum_adjustment = max(0.0, self.minimum_commission - broker_commission)
        commission = broker_commission + minimum_adjustment
        stamp_rate = 0.0
        if side == "sell":
            stamp_rate = 0.0005 if trade_date >= date(2023, 8, 28) else 0.001
        transfer_rate = 0.00001 if trade_date >= date(2022, 4, 29) else 0.00002
        stamp_duty = notional * stamp_rate
        transfer_fee = notional * transfer_rate
        return {
            "brokerCommission": broker_commission,
            "minimumCommissionAdjustment": minimum_adjustment,
            "commission": commission,
            "stampDuty": stamp_duty,
            "transferFee": transfer_fee,
            "total": commission + stamp_duty + transfer_fee,
        }

    def record_fill(self, symbol: str, side: str, quantity: float) -> None:
        if side == "sell":
            self._sellable[symbol] = max(
                0.0, self._sellable.get(symbol, 0.0) - abs(quantity)
            )

    def assumptions(self) -> dict[str, Any]:
        return {
            "marketRuleVersion": self.version,
            "settlement": "T+1",
            "longOnly": True,
            "buyLotSize": int(self.buy_lot_size),
            "oddLotLiquidation": True,
            "priceLimits": "date_and_confirmed_classification_aware",
            "commissionRate": self.commission_rate,
            "minimumCommission": self.minimum_commission,
            "stampDuty": "sell_side_date_aware",
            "transferFee": "both_sides_date_aware",
        }

    @staticmethod
    def _price_limit_rate(
        symbol: str,
        bar: Mapping[str, Any],
        trade_date: date,
    ) -> float | None:
        if not _truthy(bar.get("classification_confirmed")):
            return None
        board = str(bar.get("board_classification") or "").strip().lower()
        status = str(bar.get("status_classification") or "").strip().lower()
        if status in {"st", "delisting"}:
            return 0.05
        if status != "non_st":
            return None
        if board == "main_board":
            return 0.10
        if board == "star_board":
            return 0.20 if str(symbol).endswith(".SH") else None
        if board == "chinext":
            if not str(symbol).endswith(".SZ"):
                return None
            return 0.20 if trade_date >= date(2020, 8, 24) else 0.10
        return None


def _round_to_lot(value: float, lot_size: float) -> float:
    if lot_size <= 0:
        return value
    units = math.floor(abs(value) / lot_size + 1e-8)
    return math.copysign(units * lot_size, value) if units else 0.0


def _tick_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _positive_finite(value: Any) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
