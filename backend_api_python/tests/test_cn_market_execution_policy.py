from datetime import date

import pandas as pd
import pytest

from app.services.strategy_v2.data import MultiAssetDataPortal
from app.services.strategy_v2.execution_policy import CNStockExecutionPolicy
from app.services.strategy_v2.runtime import MultiAssetSimulationBroker, OrderIntent, Position


SYMBOL = "CNStock:600519.SH"


def _frame(*, limit_up=False, suspended=False, volume=100000):
    index = pd.DatetimeIndex(["2026-01-05", "2026-01-06"])
    frame = pd.DataFrame(
        {
            "open": [10.0, 11.0 if limit_up else 10.0],
            "high": [10.2, 11.0 if limit_up else 10.2],
            "low": [9.8, 11.0 if limit_up else 9.8],
            "close": [10.0, 11.0 if limit_up else 10.0],
            "volume": [volume, volume],
            "previous_close": [9.9, 10.0],
            "board_classification": ["main_board", "main_board"],
            "status_classification": ["non_st", "non_st"],
            "classification_confirmed": [True, True],
            "is_suspended": [False, suspended],
        },
        index=index,
    )
    return frame


@pytest.mark.parametrize(
    ("side", "trade_date", "expected_stamp", "expected_transfer"),
    [
        ("buy", date(2026, 1, 5), 0.0, 0.01),
        ("sell", date(2026, 1, 5), 0.5, 0.01),
        ("sell", date(2023, 8, 25), 1.0, 0.01),
        ("sell", date(2022, 4, 28), 1.0, 0.02),
    ],
)
def test_cn_fee_schedule_is_asymmetric_and_date_aware(
    side, trade_date, expected_stamp, expected_transfer
):
    policy = CNStockExecutionPolicy(commission_rate=0.0003)

    fees = policy.fee_breakdown(side, 1000.0, trade_date)

    assert fees["brokerCommission"] == pytest.approx(0.3)
    assert fees["minimumCommissionAdjustment"] == pytest.approx(4.7)
    assert fees["commission"] == pytest.approx(5.0)
    assert fees["stampDuty"] == pytest.approx(expected_stamp)
    assert fees["transferFee"] == pytest.approx(expected_transfer)
    assert fees["total"] == pytest.approx(5.0 + expected_stamp + expected_transfer)


def test_cn_policy_enforces_t_plus_one_then_allows_next_session_sale():
    portal = MultiAssetDataPortal({SYMBOL: _frame()})
    broker = MultiAssetSimulationBroker(
        initial_capital=10000,
        commission=0.0003,
        slippage=0,
    )
    first = pd.Timestamp("2026-01-05")
    second = pd.Timestamp("2026-01-06")

    broker.execute([OrderIntent(SYMBOL, "quantity", 200)], portal, first)
    broker.execute([OrderIntent(SYMBOL, "target_quantity", 0)], portal, first)

    assert broker.portfolio.positions[SYMBOL].amount == 200
    assert broker.order_ledger[-1]["statusReason"] == "t_plus_one"

    broker.execute([OrderIntent(SYMBOL, "target_quantity", 0)], portal, second)

    assert SYMBOL not in broker.portfolio.positions
    assert broker.executions[-1]["side"] == "sell"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(250, 200), (99, 0), (-100, 0)],
)
def test_cn_policy_enforces_buy_lots_and_long_only(amount, expected):
    portal = MultiAssetDataPortal({SYMBOL: _frame()})
    broker = MultiAssetSimulationBroker(initial_capital=10000, commission=0, slippage=0)

    broker.execute([OrderIntent(SYMBOL, "quantity", amount)], portal, pd.Timestamp("2026-01-05"))

    position = broker.portfolio.positions.get(SYMBOL, Position(SYMBOL))
    assert position.amount == expected
    if amount < 0:
        assert broker.order_ledger[-1]["statusReason"] == "long_only"


def test_cn_policy_allows_full_odd_lot_liquidation():
    portal = MultiAssetDataPortal({SYMBOL: _frame()})
    broker = MultiAssetSimulationBroker(initial_capital=10000, commission=0, slippage=0)
    broker.portfolio.positions[SYMBOL] = Position(SYMBOL, amount=150, avg_cost=9, last_price=10)
    broker.portfolio.available_cash = 8500
    broker.portfolio.total_value = 10000

    broker.execute(
        [OrderIntent(SYMBOL, "target_quantity", 0)],
        portal,
        pd.Timestamp("2026-01-05"),
    )

    assert SYMBOL not in broker.portfolio.positions
    assert broker.executions[-1]["quantity"] == 150


def test_cn_policy_rejects_non_liquidating_odd_lot_sale():
    portal = MultiAssetDataPortal({SYMBOL: _frame()})
    broker = MultiAssetSimulationBroker(initial_capital=10000, commission=0, slippage=0)
    broker.portfolio.positions[SYMBOL] = Position(SYMBOL, amount=150, avg_cost=9, last_price=10)
    broker.portfolio.available_cash = 8500
    broker.portfolio.total_value = 10000

    broker.execute(
        [OrderIntent(SYMBOL, "quantity", -50)],
        portal,
        pd.Timestamp("2026-01-05"),
    )

    assert broker.portfolio.positions[SYMBOL].amount == 150
    assert broker.order_ledger[-1]["statusReason"] == "minimum_trade_unit"


@pytest.mark.parametrize(
    ("frame", "reason"),
    [
        (_frame(limit_up=True), "limit_up"),
        (_frame(suspended=True), "suspended"),
        (_frame(volume=0), "no_liquidity"),
    ],
)
def test_cn_policy_blocks_untradable_daily_bars(frame, reason):
    portal = MultiAssetDataPortal({SYMBOL: frame})
    broker = MultiAssetSimulationBroker(initial_capital=10000, commission=0, slippage=0)

    broker.execute(
        [OrderIntent(SYMBOL, "quantity", 100)],
        portal,
        pd.Timestamp("2026-01-06"),
    )

    assert broker.executions == []
    assert broker.order_ledger[-1]["statusReason"] == reason


def test_cn_policy_fails_closed_without_confirmed_rule_classification():
    frame = _frame()
    frame.loc[:, "classification_confirmed"] = False
    portal = MultiAssetDataPortal({SYMBOL: frame})
    broker = MultiAssetSimulationBroker(initial_capital=10000, commission=0, slippage=0)

    broker.execute(
        [OrderIntent(SYMBOL, "quantity", 100)],
        portal,
        pd.Timestamp("2026-01-05"),
    )

    assert broker.order_ledger[-1]["statusReason"] == "market_rule_data_incomplete"


def test_cn_policy_rejects_invalid_ohlc_and_board_exchange_mismatch():
    invalid = _frame()
    invalid.loc[pd.Timestamp("2026-01-05"), "high"] = 0
    mismatch = _frame()
    mismatch.loc[:, "board_classification"] = "chinext"
    for frame, reason in (
        (invalid, "invalid_ohlc"),
        (mismatch, "market_rule_data_incomplete"),
    ):
        portal = MultiAssetDataPortal({SYMBOL: frame})
        broker = MultiAssetSimulationBroker(initial_capital=10000, commission=0, slippage=0)
        broker.execute(
            [OrderIntent(SYMBOL, "quantity", 100)],
            portal,
            pd.Timestamp("2026-01-05"),
        )
        assert broker.order_ledger[-1]["statusReason"] == reason


def test_cn_fee_is_included_in_cash_boundary_and_execution_ledger():
    portal = MultiAssetDataPortal({SYMBOL: _frame()})
    broker = MultiAssetSimulationBroker(initial_capital=1005, commission=0.0003, slippage=0)

    broker.execute(
        [OrderIntent(SYMBOL, "quantity", 100)],
        portal,
        pd.Timestamp("2026-01-05"),
    )

    assert broker.executions == []
    assert broker.order_ledger[-1]["statusReason"] == "insufficient_cash"

    funded = MultiAssetSimulationBroker(initial_capital=1010, commission=0.0003, slippage=0)
    funded.execute(
        [OrderIntent(SYMBOL, "quantity", 100)],
        portal,
        pd.Timestamp("2026-01-05"),
    )
    execution = funded.executions[-1]
    assert execution["commission"] == pytest.approx(execution["feeBreakdown"]["total"])
    assert funded.order_ledger[-1]["feeBreakdown"] == execution["feeBreakdown"]


def test_cn_market_assumptions_and_fee_attribution_are_published():
    portal = MultiAssetDataPortal({SYMBOL: _frame()})
    broker = MultiAssetSimulationBroker(initial_capital=1010, commission=0.0003, slippage=0)
    broker.execute(
        [OrderIntent(SYMBOL, "quantity", 100)],
        portal,
        pd.Timestamp("2026-01-05"),
    )

    assumptions = broker.execution_assumptions()["CNStock"]
    assert assumptions["marketRuleVersion"] == "cn-stock-rules-2026.1"
    assert assumptions["settlement"] == "T+1"
    assert assumptions["buyLotSize"] == 100
