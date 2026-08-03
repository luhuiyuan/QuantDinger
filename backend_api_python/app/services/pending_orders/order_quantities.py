"""Exchange quantity normalization and durable completion decisions."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from app.services.pending_orders.sent_order_recovery import is_final_fill


def exchange_executable_base_quantity(
    client: Any,
    *,
    exchange_id: str,
    symbol: str,
    market_type: str,
    requested: float,
    exchange_config: Dict[str, Any],
) -> float:
    """Return the base quantity accepted after exchange step-size flooring."""
    quantity = max(0.0, float(requested or 0.0))
    if (
        str(exchange_id or "").strip().lower() != "bitget"
        or str(market_type or "").strip().lower() != "swap"
        or not hasattr(client, "normalize_base_order_size")
    ):
        return quantity
    product_type = str(
        exchange_config.get("product_type")
        or exchange_config.get("productType")
        or "USDT-FUTURES"
    )
    normalized = client.normalize_base_order_size(
        symbol=str(symbol),
        product_type=product_type,
        base_size=quantity,
    )
    return max(0.0, float(normalized or 0.0))


def exchange_quantity_snapshot(
    client: Any,
    *,
    exchange_id: str,
    symbol: str,
    market_type: str,
    requested: float,
    exchange_config: Dict[str, Any],
    source: str = "order_submission",
) -> Tuple[float, Dict[str, Any]]:
    executable = exchange_executable_base_quantity(
        client,
        exchange_id=exchange_id,
        symbol=symbol,
        market_type=market_type,
        requested=requested,
        exchange_config=exchange_config,
    )
    return executable, {
        "requested_base_qty": max(0.0, float(requested or 0.0)),
        "executable_base_qty": executable,
        "source": str(source or "order_submission"),
    }


def reconciled_queue_status(
    client: Any,
    *,
    exchange_id: str,
    symbol: str,
    market_type: str,
    requested: float,
    filled: float,
    avg_price: float,
    exchange_status: str,
    exchange_config: Dict[str, Any],
) -> Tuple[str, float]:
    executable = exchange_executable_base_quantity(
        client,
        exchange_id=exchange_id,
        symbol=symbol,
        market_type=market_type,
        requested=requested,
        exchange_config=exchange_config,
    )
    if is_final_fill(executable, filled, avg_price, exchange_status):
        return "filled", executable
    if str(exchange_status or "").strip().lower() == "cancelled":
        return "cancelled", executable
    return "sent", executable
