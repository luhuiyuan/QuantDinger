"""Deterministic A-share forward and backward adjustment-factor calculation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from .models import AdjustmentFactor, AdjustmentMode, CorporateAction, RawDailyBar


ALGORITHM_VERSION = "cn-adjustment-v2"


class AdjustmentCalculationError(RuntimeError):
    code = "cn_history.adjustment_invalid"


def corporate_action_ratio(previous_close: Decimal, action: CorporateAction) -> Decimal:
    if previous_close <= 0:
        raise AdjustmentCalculationError("Previous close must be positive")
    official = action.raw_payload.get("official_adjustment")
    if isinstance(official, dict) and official.get("adjustment_ratio") is not None:
        try:
            ratio = Decimal(str(official["adjustment_ratio"]))
        except Exception as exc:
            raise AdjustmentCalculationError("Official adjustment ratio is invalid") from exc
        if not ratio.is_finite() or ratio <= 0:
            raise AdjustmentCalculationError("Official adjustment ratio is invalid")
        return ratio
    zero = Decimal("0")
    if action.category in {11, 12}:
        ratio = action.consolidation_ratio
        if ratio is None or ratio <= zero:
            raise AdjustmentCalculationError("Consolidation ratio is missing")
        return Decimal("1") / ratio
    if action.category != 1:
        return Decimal("1")
    cash = action.cash_dividend or zero
    bonus = action.bonus_ratio or zero
    rights = action.rights_ratio or zero
    rights_price = action.rights_price or zero
    denominator = Decimal("1") + bonus + rights
    theoretical = (previous_close - cash + rights * rights_price) / denominator
    ratio = theoretical / previous_close
    if ratio <= 0:
        raise AdjustmentCalculationError("Corporate action produced a non-positive factor")
    return ratio


def calculate_adjustment_factors(
    bars: Sequence[RawDailyBar],
    actions: Sequence[CorporateAction],
    mode: AdjustmentMode,
) -> tuple[AdjustmentFactor, ...]:
    if mode is AdjustmentMode.RAW:
        raise ValueError("Raw mode does not use adjustment factors")
    if not bars:
        return ()
    ordered = sorted(bars, key=lambda item: item.trade_date)
    instrument = ordered[0].instrument
    if any(bar.instrument != instrument for bar in ordered):
        raise ValueError("Adjustment input must contain one instrument")
    closes = [(bar.trade_date, bar.close) for bar in ordered]
    event_ratios: list[tuple] = []
    for action in sorted(actions, key=lambda item: (item.event_date, item.category)):
        prior = [close for trade_date, close in closes if trade_date < action.event_date]
        if not prior:
            raise AdjustmentCalculationError(
                f"No previous close exists for corporate action on {action.event_date}"
            )
        ratio = corporate_action_ratio(prior[-1], action)
        if ratio != Decimal("1"):
            event_ratios.append((action.event_date, ratio, action.content_hash))

    digest = hashlib.sha256()
    digest.update(ALGORITHM_VERSION.encode("ascii"))
    digest.update(mode.value.encode("ascii"))
    for bar in ordered:
        digest.update(f"{bar.trade_date}|{bar.content_hash}\n".encode("utf-8"))
    for event_date, ratio, content_hash in event_ratios:
        digest.update(f"{event_date}|{ratio}|{content_hash}\n".encode("utf-8"))
    factor_version = digest.hexdigest()
    generated_at = datetime.now(timezone.utc)
    anchor_date = ordered[-1].trade_date if mode is AdjustmentMode.FORWARD else ordered[0].trade_date
    output = []
    for bar in ordered:
        factor = Decimal("1")
        for event_date, ratio, _ in event_ratios:
            if mode is AdjustmentMode.FORWARD and bar.trade_date < event_date:
                factor *= ratio
            elif mode is AdjustmentMode.BACKWARD and bar.trade_date >= event_date:
                factor /= ratio
        output.append(
            AdjustmentFactor(
                instrument=instrument,
                trade_date=bar.trade_date,
                mode=mode,
                factor=factor,
                algorithm_version=ALGORITHM_VERSION,
                factor_version=factor_version,
                anchor_date=anchor_date,
                generated_at=generated_at,
            )
        )
    return tuple(output)
