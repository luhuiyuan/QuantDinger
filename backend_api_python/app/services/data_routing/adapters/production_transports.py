"""Explicit provider transports used by the production Adapter catalog."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from functools import partial
from typing import Any, Iterable, Mapping

from .catalog import bind_adapter_transport


_TIMEFRAME_ALIASES = {
    "1h": "1H", "1hour": "1H", "60m": "1H",
    "4h": "4H", "1d": "1D", "1day": "1D", "d": "1D",
    "1w": "1W", "1wk": "1W", "w": "1W",
}


def _timeframe(value: Any) -> str:
    raw = str(value or "1D").strip()
    return _TIMEFRAME_ALIASES.get(raw.lower(), raw)


def _first_number(payload: Any, *keys: str) -> float | None:
    if isinstance(payload, Mapping):
        for key in keys:
            if key in payload:
                try:
                    return float(str(payload[key]).replace(",", ""))
                except (TypeError, ValueError):
                    pass
        for value in payload.values():
            found = _first_number(value, *keys)
            if found is not None:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in reversed(payload):
            found = _first_number(value, *keys)
            if found is not None:
                return found
    return None


def _merge_bars(bars: list[dict[str, Any]], size: int, *, limit: int) -> list[dict[str, Any]]:
    if size <= 1:
        return bars[-limit:]
    merged = []
    for offset in range(0, len(bars), size):
        chunk = bars[offset:offset + size]
        if len(chunk) != size:
            continue
        merged.append({
            "time": chunk[0]["time"],
            "open": chunk[0]["open"],
            "high": max(item["high"] for item in chunk),
            "low": min(item["low"] for item in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(float(item.get("volume") or 0) for item in chunk),
        })
    return merged[-limit:]


def _date(value):
    from datetime import date

    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _bars_from_frame(frame, *, limit: int):
    if frame is None or getattr(frame, "empty", True):
        return []
    output = []
    for stamp, row in frame.iterrows():
        try:
            output.append({
                "time": int(stamp.timestamp()),
                "open": float(row["Open"]), "high": float(row["High"]),
                "low": float(row["Low"]), "close": float(row["Close"]),
                "volume": float(row.get("Volume") or 0),
            })
        except Exception:
            continue
    return output[-max(1, int(limit)):]


def _us_fundamentals_from_yfinance(symbol: str) -> dict[str, Any]:
    import yfinance as yf

    info = yf.Ticker(symbol.replace(".", "-")).info or {}
    return {
        "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
        "pb_ratio": info.get("priceToBook"),
        "ps_ratio": info.get("priceToSalesTrailing12Months"),
        "market_cap": info.get("marketCap"),
        "dividend_yield": info.get("dividendYield"),
        "beta": info.get("beta"),
        "52w_high": info.get("fiftyTwoWeekHigh"),
        "52w_low": info.get("fiftyTwoWeekLow"),
        "roe": info.get("returnOnEquity"),
        "eps": info.get("trailingEps"),
        "revenue_growth": info.get("revenueGrowth"),
        "profit_margin": info.get("profitMargins"),
        "debt_to_equity": info.get("debtToEquity"),
        "current_ratio": info.get("currentRatio"),
        "free_cash_flow": info.get("freeCashflow"),
        "source": "yfinance",
    }


def _asia_subject(subject: Mapping[str, Any], constraints: Mapping[str, Any]):
    market = str(subject.get("market") or constraints.get("market") or "CNStock")
    is_hk = market in {"HKStock", "HK", "HShare"}
    symbol = str(subject.get("symbol") or "")
    timeframe = str(constraints.get("timeframe") or subject.get("timeframe") or "1D")
    limit = max(1, int(subject.get("limit") or constraints.get("limit") or 300))
    return is_hk, symbol, timeframe, limit, subject.get("before_time")


def _snapshot_timeout(config: Mapping[str, Any], deadline: datetime) -> int:
    configured = max(1, int(config.get("timeout_seconds") or 8))
    remaining = max(1, int((deadline - datetime.now(timezone.utc)).total_seconds()))
    return min(configured, remaining)


def _snapshot_batches(values: Iterable[Any], batch_size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for value in values:
        if value:
            batch.append(value)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _snapshot_symbols(subject: Mapping[str, Any], config: Mapping[str, Any]) -> list[str]:
    requested = [str(symbol).strip() for symbol in (subject.get("symbols") or []) if str(symbol).strip()]
    if requested:
        return requested
    from app.utils.db import get_db_connection

    maximum = max(1, min(int(config.get("snapshot_max_symbols") or 6000), 7000))
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """SELECT symbol FROM qd_market_symbols
                   WHERE market='CNStock' AND is_active=1
                   ORDER BY symbol ASC LIMIT %s""",
                (maximum,),
            )
            return [str(row["symbol"]).strip() for row in (cur.fetchall() or []) if row.get("symbol")]
        finally:
            cur.close()


def _normalize_snapshot_rows(records: Iterable[Mapping[str, Any]], *, source: str, fetched_at: str) -> list[dict[str, Any]]:
    from app.services.market.cn_stock_market import normalize_cn_snapshot_row

    return [
        normalized
        for record in records
        if (normalized := normalize_cn_snapshot_row(dict(record), as_of=fetched_at, source=source))
    ]


def _snapshot_payload(rows: list[dict[str, Any]], *, source: str, fetched_at: str) -> dict[str, Any]:
    if not rows:
        raise ValueError("A-share snapshot returned no normalized Shanghai/Shenzhen rows")
    return {"rows": rows, "asOf": fetched_at, "source": source}


def _tencent_snapshot(subject, config, deadline):
    from app.data_sources.tencent import fetch_quote_map, normalize_cn_code, parse_quote_to_market_row

    symbols = _snapshot_symbols(subject, config)
    if not symbols:
        raise ValueError("A-share snapshot requires an active local symbol universe")
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    batch_size = max(1, min(int(config.get("snapshot_batch_size") or 80), 120))
    for symbols_batch in _snapshot_batches(symbols, batch_size):
        if datetime.now(timezone.utc) >= deadline:
            raise TimeoutError("Tencent A-share snapshot deadline exceeded")
        codes = [normalize_cn_code(symbol) for symbol in symbols_batch]
        quote_map = fetch_quote_map(codes, timeout=_snapshot_timeout(config, deadline))
        raw_rows = [parse_quote_to_market_row(raw) for raw in quote_map.values()]
        rows.extend(_normalize_snapshot_rows(raw_rows, source="tencent-batch", fetched_at=fetched_at))
    return _snapshot_payload(rows, source="tencent-batch", fetched_at=fetched_at)


def _sina_snapshot(subject, config, deadline):
    import re
    import requests

    from app.data_sources.tencent import normalize_cn_code

    symbols = _snapshot_symbols(subject, config)
    if not symbols:
        raise ValueError("A-share snapshot requires an active local symbol universe")
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    batch_size = max(1, min(int(config.get("snapshot_batch_size") or 80), 120))
    for symbols_batch in _snapshot_batches(symbols, batch_size):
        if datetime.now(timezone.utc) >= deadline:
            raise TimeoutError("Sina A-share snapshot deadline exceeded")
        codes = [normalize_cn_code(symbol).lower() for symbol in symbols_batch]
        response = requests.get(
            str(config.get("base_url") or "https://hq.sinajs.cn/list=") + ",".join(codes),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
            timeout=_snapshot_timeout(config, deadline),
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="replace")
        for code, values in re.findall(r'var hq_str_([a-z0-9]+)="([^"]*)"', text, re.IGNORECASE):
            parts = values.split(",")
            if len(parts) < 10 or not parts[0].strip():
                continue
            def number(index):
                try:
                    return float(parts[index])
                except (IndexError, TypeError, ValueError):
                    return None
            rows.extend(_normalize_snapshot_rows(({
                "code": code[2:], "name": parts[0], "open": number(1), "previousClose": number(2),
                "latest": number(3), "high": number(4), "low": number(5),
                "volume": number(8), "amount": number(9),
            },), source="sina", fetched_at=fetched_at))
    return _snapshot_payload(rows, source="sina", fetched_at=fetched_at)


def _sina_code(symbol: str) -> str:
    from app.data_sources.tencent import normalize_cn_code

    return normalize_cn_code(symbol).lower()


def _sina_daily_records(symbol: str, *, adjustment: str, before_time: Any, limit: int) -> list[dict[str, Any]]:
    import akshare as ak  # type: ignore

    adjust = {"raw": "", "forward": "qfq", "backward": "hfq"}.get(str(adjustment or "").lower(), str(adjustment or ""))
    frame = ak.stock_zh_a_daily(symbol=_sina_code(symbol), adjust=adjust)
    records = frame.to_dict("records") if frame is not None else []
    cutoff = datetime.fromtimestamp(int(before_time), tz=timezone.utc).date() if before_time else None
    rows = []
    for record in records:
        try:
            trade_date = date.fromisoformat(str(record.get("date"))[:10])
            if cutoff and trade_date > cutoff:
                continue
            rows.append({
                "trade_date": trade_date, "open": float(record["open"]), "high": float(record["high"]),
                "low": float(record["low"]), "close": float(record["close"]),
                "volume": float(record.get("volume") or 0), "amount": float(record.get("amount") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows[-max(1, int(limit)):]


def _sina_kline(subject, constraints, config):
    import akshare as ak  # type: ignore

    is_hk, symbol, timeframe, limit, before_time = _asia_subject(subject, constraints)
    if is_hk:
        raise ValueError("Sina A-share transport does not support Hong Kong equities")
    normalized = _timeframe(timeframe)
    adjustment = str(constraints.get("adjustment") or "raw")
    if normalized in {"1D", "1W"}:
        rows = _sina_daily_records(symbol, adjustment=adjustment, before_time=before_time, limit=limit * (7 if normalized == "1W" else 1) + 10)
        if normalized == "1W":
            grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
            for row in rows:
                iso = row["trade_date"].isocalendar()
                grouped.setdefault((iso.year, iso.week), []).append(row)
            rows = [{
                "trade_date": items[0]["trade_date"], "open": items[0]["open"],
                "high": max(item["high"] for item in items), "low": min(item["low"] for item in items),
                "close": items[-1]["close"], "volume": sum(item["volume"] for item in items), "amount": sum(item["amount"] for item in items),
            } for _, items in sorted(grouped.items())]
        return [{"time": int(datetime.combine(row["trade_date"], datetime.min.time(), tzinfo=timezone.utc).timestamp()), **{key: row[key] for key in ("open", "high", "low", "close", "volume")}} for row in rows[-limit:]]
    period = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1H": "60", "4H": "60"}.get(normalized)
    if period is None:
        raise ValueError(f"Sina does not support K-line timeframe {timeframe}")
    frame = ak.stock_zh_a_minute(symbol=_sina_code(symbol), period=period, adjust={"forward": "qfq", "backward": "hfq"}.get(adjustment, ""))
    records = frame.to_dict("records") if frame is not None else []
    rows = []
    for record in records:
        try:
            stamp = datetime.fromisoformat(str(record.get("day"))).replace(tzinfo=timezone.utc)
            rows.append({"time": int(stamp.timestamp()), "open": float(record["open"]), "high": float(record["high"]), "low": float(record["low"]), "close": float(record["close"]), "volume": float(record.get("volume") or 0)})
        except (TypeError, ValueError, KeyError):
            continue
    return _merge_bars(rows, 4, limit=limit) if normalized == "4H" else rows[-limit:]


def _sina_fundamentals(subject, constraints):
    import akshare as ak  # type: ignore

    is_hk, symbol, _, _, _ = _asia_subject(subject, constraints)
    if is_hk:
        raise ValueError("Sina fundamentals transport does not support Hong Kong equities")
    code = _sina_code(symbol)
    reports = {name: ak.stock_financial_report_sina(stock=code, symbol=name) for name in ("利润表", "资产负债表", "现金流量表")}
    def latest(name, *fields):
        frame = reports[name]
        if frame is None or getattr(frame, "empty", True): return None
        row = frame.iloc[0]
        for field in fields:
            try:
                value = row.get(field)
                if value is not None: return float(value)
            except (TypeError, ValueError): pass
        return None
    income = {"total_revenue": latest("利润表", "营业总收入", "营业收入"), "operating_income": latest("利润表", "营业利润"), "net_income": latest("利润表", "净利润")}
    balance = {"total_assets": latest("资产负债表", "资产总计"), "total_liabilities": latest("资产负债表", "负债合计"), "current_assets": latest("资产负债表", "流动资产合计"), "current_liabilities": latest("资产负债表", "流动负债合计")}
    cash = {"operating_cash_flow": latest("现金流量表", "经营活动产生的现金流量净额"), "investing_cash_flow": latest("现金流量表", "投资活动产生的现金流量净额"), "financing_cash_flow": latest("现金流量表", "筹资活动产生的现金流量净额")}
    result = {"source": "sina", "financial_statements": {"income_statement": income, "balance_sheet": balance, "cash_flow": cash}}
    if income["total_revenue"] and income["net_income"] is not None: result["profit_margin"] = round(income["net_income"] / income["total_revenue"] * 100, 2)
    if balance["current_assets"] is not None and balance["current_liabilities"]: result["current_ratio"] = round(balance["current_assets"] / balance["current_liabilities"], 4)
    return result


def _sina_catalog(subject, constraints):
    import akshare as ak  # type: ignore
    from app.services.symbol_master_sync import SymbolMasterRow

    market = str(subject.get("market") or constraints.get("market") or "CNStock")
    if market != "CNStock": raise ValueError("Sina catalog only supports CNStock")
    frame = ak.stock_zh_a_spot()
    records = frame.to_dict("records") if frame is not None else []
    rows = [SymbolMasterRow("CNStock", str(item.get("代码") or item.get("symbol") or "").zfill(6), str(item.get("名称") or item.get("name") or ""), "CN", "CNY") for item in records]
    return [row for row in rows if row.symbol.isdigit() and row.name]


def _sina_history(subject):
    from app.services.cn_market_history.instruments import parse_cn_instrument
    from app.services.cn_market_history.models import CNInstrumentMetadata, RawDailyBar
    from app.services.cn_market_history.tdx_provider import DailyBarPage, _content_hash

    instrument = parse_cn_instrument(str(subject.get("instrument") or "").replace(":", ""))
    operation = str(subject.get("operation") or "")
    if operation == "metadata":
        payload = {"instrument": instrument.canonical, "security_type": "ordinary_share"}
        return (CNInstrumentMetadata(instrument, "", "ordinary_share", None, None, "sina", "sina-api", _content_hash(payload)), ())
    if operation != "daily_page":
        raise ValueError(f"Sina history does not implement {operation}")
    start_date, end_date = _date(subject["start_date"]), _date(subject["end_date"])
    if end_date < start_date: raise ValueError("end_date must not precede start_date")
    collected_at = datetime.now(timezone.utc)
    rows = _sina_daily_records(instrument.canonical, adjustment="raw", before_time=None, limit=100000)
    bars = tuple(
        RawDailyBar(
            instrument=instrument, trade_date=row["trade_date"], open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])), close=Decimal(str(row["close"])), volume=Decimal(str(row["volume"])), amount=Decimal(str(row["amount"])),
            provider="sina", provider_version="sina-api", collected_at=collected_at,
            content_hash=_content_hash({"instrument": instrument.canonical, "trade_date": row["trade_date"].isoformat(), **{key: str(row[key]) for key in ("open", "high", "low", "close", "volume", "amount")}}),
        ) for row in rows if start_date <= row["trade_date"] <= end_date
    )
    return DailyBarPage(offset=max(0, int(subject.get("start_offset") or 0)), next_offset=len(rows), raw_count=len(rows), bars=bars, reached_start=True)


def _sina(capability, subject, constraints, config, credentials, deadline):
    if capability == "cn_market_snapshot": return _sina_snapshot(subject, config, deadline)
    if capability == "cn_hk_quote":
        # Sina's hq endpoint is also a single-security quote source for A/HK
        # symbols. Reuse the normalized snapshot contract so callers receive
        # the same fields regardless of provider.
        symbols = list(subject.get("symbols") or [])
        if not symbols and subject.get("symbol"):
            symbols = [str(subject["symbol"])]
        payload = _sina_snapshot({"symbols": symbols}, config, deadline)
        rows = payload.get("rows") or []
        return rows if subject.get("symbols") else (rows[0] if rows else {})
    if capability == "asia_equity_kline": return _sina_kline(subject, constraints, config)
    if capability == "cn_hk_fundamentals": return _sina_fundamentals(subject, constraints)
    if capability == "cn_equity_history": return _sina_history(subject)
    if capability in {"market_catalog", "symbol_master"}:
        rows = _sina_catalog(subject, constraints)
        if capability == "symbol_master" and subject.get("operation") == "full_sync": return rows
        query = str(subject.get("query") or "").upper()
        return [{"market": row.market, "symbol": row.symbol, "name": row.name, "exchange": row.exchange, "currency": row.currency} for row in rows if not query or query in row.symbol or query in row.name][:max(1, int(subject.get("limit") or 20))]
    if capability == "symbol_reference":
        payload = _sina_snapshot({"symbols": [str(subject.get("symbol") or "")]}, config, deadline)
        return str(payload["rows"][0].get("name") or "") if payload.get("rows") else ""
    raise ValueError(f"sina does not implement {capability}")


def _eastmoney_snapshot(subject, config, deadline):
    import requests

    maximum = max(1, min(int(config.get("snapshot_max_symbols") or 6000), 7000))
    response = requests.get(
        str(config.get("base_url") or "https://82.push2.eastmoney.com/api/qt/clist/get"),
        params={"pn": 1, "pz": maximum, "po": 1, "np": 1, "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2, "fid": "f3", "fs": "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80", "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f15,f16,f17,f18"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=_snapshot_timeout(config, deadline),
    )
    response.raise_for_status()
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []
    for item in (response.json().get("data") or {}).get("diff") or []:
        records.append({"code": item.get("f12"), "name": item.get("f14"), "latest": item.get("f2"), "change": item.get("f4"), "changePercent": item.get("f3"), "volume": item.get("f5"), "amount": item.get("f6"), "high": item.get("f15"), "low": item.get("f16"), "open": item.get("f17"), "previousClose": item.get("f18")})
    return _snapshot_payload(_normalize_snapshot_rows(records, source="eastmoney", fetched_at=fetched_at), source="eastmoney", fetched_at=fetched_at)


def _easy_tdx_snapshot(subject, config, deadline):
    from app.services.cn_market_history.instruments import CNInstrumentError, parse_cn_instrument
    from app.services.cn_market_history.tdx_provider import TDXProvider

    symbols = _snapshot_symbols(subject, config)
    if not symbols:
        raise ValueError("A-share snapshot requires an active local symbol universe")
    instruments = []
    for symbol in symbols:
        try:
            instruments.append(parse_cn_instrument(symbol))
        except CNInstrumentError:
            # The shared symbol universe can contain ETFs, BSE and other
            # instruments outside EasyTDX's strict Shanghai/Shenzhen A-share
            # contract. Skip them instead of failing the entire snapshot.
            continue
    if not instruments:
        raise ValueError("A-share snapshot has no EasyTDX-supported symbols")
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    batch_size = max(1, min(int(config.get("snapshot_batch_size") or 80), 80))
    provider = TDXProvider()
    provider.probe_hosts()
    with provider:
        for batch in _snapshot_batches(instruments, batch_size):
            if datetime.now(timezone.utc) >= deadline:
                raise TimeoutError("EasyTDX A-share snapshot deadline exceeded")
            records = provider.fetch_market_quotes(batch)
            mapped = ({"code": item.get("code"), "name": item.get("name"), "latest": item.get("price") or item.get("last"), "previousClose": item.get("last_close") or item.get("previous_close"), "open": item.get("open"), "high": item.get("high"), "low": item.get("low"), "volume": item.get("vol") or item.get("volume"), "amount": item.get("amount") or item.get("turnover") } for item in records)
            rows.extend(_normalize_snapshot_rows(mapped, source="easy_tdx", fetched_at=fetched_at))
    return _snapshot_payload(rows, source="easy_tdx", fetched_at=fetched_at)


def _tencent(capability, subject, constraints, config, credentials, deadline):
    from app.data_sources.tencent import (
        fetch_kline, fetch_quote, normalize_cn_code, normalize_hk_code,
        parse_quote_to_ticker, tencent_kline_rows_to_dicts,
    )
    if capability == "symbol_reference":
        is_hk, symbol, _, _, _ = _asia_subject(subject, constraints)
        code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
        parts = fetch_quote(code, timeout=int(config.get("timeout_seconds") or 8))
        return str(parts[1]).strip() if parts and len(parts) > 1 else ""
    if capability == "cn_market_snapshot":
        return _tencent_snapshot(subject, config, deadline)
    if capability == "cn_hk_quote":
        symbols = list(subject.get("symbols") or [])
        if symbols:
            from app.data_sources.tencent import fetch_quote_map, parse_quote_to_market_row
            from app.services.market.cn_stock_market import normalize_cn_snapshot_row

            codes = [normalize_cn_code(symbol) for symbol in symbols]
            quote_map = fetch_quote_map(codes, timeout=int(config.get("timeout_seconds") or 8))
            fetched_at = datetime.now(timezone.utc).isoformat()
            definitions = list(subject.get("index_definitions") or [])
            if definitions:
                by_code = {str(item[2]).lower(): item for item in definitions}
                output = []
                for code in codes:
                    definition = by_code.get(code.lower())
                    parts = quote_map.get(code.lower())
                    ticker = parse_quote_to_ticker(parts or []) if parts else {}
                    latest = ticker.get("last")
                    valid = latest is not None and float(latest) > 0
                    output.append({
                        "symbol": definition[0] if definition else code,
                        "name": ticker.get("name") or (definition[1] if definition else code),
                        "latest": float(latest) if valid else None,
                        "change": ticker.get("change") if valid else None,
                        "changePercent": ticker.get("changePercent") if valid else None,
                        "asOf": fetched_at if valid else None,
                        "source": "tencent",
                        "freshness": "fresh" if valid else "unavailable",
                        "status": "available" if valid else "unavailable",
                        **({"warning": "quote unavailable"} if not valid else {}),
                    })
                return output
            rows = []
            for code in codes:
                raw = quote_map.get(code.lower())
                if not raw:
                    continue
                market_row = parse_quote_to_market_row(raw)
                normalized = normalize_cn_snapshot_row(
                    market_row,
                    as_of=market_row.get("quoteTime") or fetched_at,
                    source="tencent-batch",
                )
                if normalized:
                    rows.append(normalized)
            return rows
        is_hk, symbol, timeframe, limit, _ = _asia_subject(subject, constraints)
        code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
        parts = fetch_quote(code, timeout=int(config.get("timeout_seconds") or 8))
        return parse_quote_to_ticker(parts) if parts else {}
    if capability == "asia_equity_kline":
        is_hk, symbol, timeframe, limit, _ = _asia_subject(subject, constraints)
        code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
        period = "week" if timeframe in {"1W", "1w"} else "day"
        return tencent_kline_rows_to_dicts(fetch_kline(code, period=period, count=limit, adj=str(constraints.get("adjustment") or "qfq")))
    raise ValueError(f"Tencent does not implement {capability}")


def _twelve_data(capability, subject, constraints, config, credentials, deadline):
    if capability == "commodity_quote" and subject.get("operation") == "overview":
        from app.data_providers.commodities import _fetch_td

        return _fetch_td(list(subject.get("commodities") or []), str(credentials.get("api_key") or ""))
    if capability == "forex_quote" and subject.get("operation") == "overview":
        from app.data_providers.forex import _fetch_td

        return _fetch_td(list(subject.get("pairs") or []), str(credentials.get("api_key") or ""))
    if capability == "cn_hk_fundamentals":
        from app.data_sources.cn_hk_fundamentals import (
            fetch_twelvedata_earnings,
            fetch_twelvedata_fundamental,
            fetch_twelvedata_statements,
        )
        from app.data_sources.tencent import normalize_cn_code, normalize_hk_code

        is_hk, symbol, _, _, _ = _asia_subject(subject, constraints)
        code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
        api_key = str(credentials.get("api_key") or "")
        result = fetch_twelvedata_fundamental(code, is_hk, api_key=api_key)
        statements = fetch_twelvedata_statements(code, is_hk, api_key=api_key)
        financial_statements = statements.pop("financial_statements", None)
        for key, value in statements.items():
            if value is not None and result.get(key) is None:
                result[key] = value
        if financial_statements:
            result["financial_statements"] = financial_statements
        earnings = fetch_twelvedata_earnings(code, is_hk, api_key=api_key)
        if earnings:
            result["earnings"] = earnings
        result["source"] = "twelvedata"
        return result
    if capability in {"forex_market_data", "forex_quote", "futures_market_data", "commodity_quote"}:
        import requests
        from app.data_sources.forex import _td_forex_symbol

        symbol = str(subject.get("symbol") or "")
        operation = str(subject.get("operation") or "ticker")
        api_key = str(credentials.get("api_key") or "")
        if capability.startswith("forex"):
            symbol = _td_forex_symbol(symbol)
        if operation == "ticker":
            response = requests.get("https://api.twelvedata.com/quote", params={"symbol": symbol, "apikey": api_key}, timeout=15)
            response.raise_for_status()
            data = response.json()
            last, previous = float(data.get("close") or 0), float(data.get("previous_close") or 0)
            return {"symbol": subject.get("symbol"), "last": last, "previousClose": previous, "change": last - previous, "changePercent": ((last - previous) / previous * 100) if previous else 0}
        interval = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1H": "1h", "4H": "4h", "1D": "1day", "1W": "1week"}.get(str(constraints.get("timeframe") or "1D"), "1day")
        response = requests.get("https://api.twelvedata.com/time_series", params={"symbol": symbol, "interval": interval, "outputsize": int(subject.get("limit") or 300), "apikey": api_key}, timeout=20)
        response.raise_for_status()
        values = response.json().get("values") or []
        return [{"time": int(datetime.fromisoformat(item["datetime"]).replace(tzinfo=timezone.utc).timestamp()), "open": float(item["open"]), "high": float(item["high"]), "low": float(item["low"]), "close": float(item["close"]), "volume": float(item.get("volume") or 0)} for item in reversed(values)]
    if capability != "asia_equity_kline":
        raise ValueError(f"Twelve Data transport for {capability} is not implemented")
    from app.data_sources.asia_stock_kline import fetch_twelvedata_klines
    from app.data_sources.tencent import normalize_cn_code, normalize_hk_code
    is_hk, symbol, timeframe, limit, before_time = _asia_subject(subject, constraints)
    code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
    return fetch_twelvedata_klines(
        is_hk=is_hk, tencent_code=code, timeframe=timeframe, limit=limit,
        before_time=before_time, api_key=str(credentials.get("api_key") or ""),
    )


def _yfinance(capability, subject, constraints, config, credentials, deadline):
    if capability == "commodity_quote" and subject.get("operation") == "overview":
        from app.data_providers.commodities import _fetch_yf

        return _fetch_yf(list(subject.get("commodities") or []))
    if capability == "forex_quote" and subject.get("operation") == "overview":
        from app.data_providers.forex import _fetch_yf

        return _fetch_yf(list(subject.get("pairs") or []))
    if capability == "crypto_market_snapshot" and subject.get("operation") == "overview":
        from app.data_providers.crypto import fetch_crypto_prices_yfinance

        return fetch_crypto_prices_yfinance()
    if capability == "market_index_quote" and subject.get("operation") == "overview":
        from app.data_providers.indices import _fetch_stock_indices_yfinance

        return _fetch_stock_indices_yfinance(list(subject.get("indices") or []))
    if capability == "global_heatmap":
        from app.data_providers.heatmap import _generate_heatmap_yfinance

        return _generate_heatmap_yfinance()
    if capability == "market_sentiment":
        from app.data_providers.sentiment import (
            fetch_dollar_index, fetch_gvz, fetch_put_call_ratio, fetch_vix, fetch_vxn, fetch_yield_curve,
        )

        return {
            "fear_greed": {"value": 50, "classification": "Neutral", "source": "not_available"},
            "vix": fetch_vix(), "dxy": fetch_dollar_index(), "yield_curve": fetch_yield_curve(),
            "vxn": fetch_vxn(), "gvz": fetch_gvz(), "vix_term": fetch_put_call_ratio(),
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        }
    if capability == "us_fundamentals":
        return _us_fundamentals_from_yfinance(str(subject.get("symbol") or ""))
    if capability in {"us_equity_market_data", "forex_market_data", "forex_quote", "futures_market_data", "commodity_quote", "market_index_quote"}:
        import yfinance as yf
        from datetime import timedelta
        from app.data_sources.forex import _YF_SYMBOL_MAP

        symbol = str(subject.get("symbol") or "")
        if capability.startswith("forex"):
            symbol = _YF_SYMBOL_MAP.get(symbol, f"{symbol}=X")
        elif capability in {"futures_market_data", "commodity_quote"} and not symbol.endswith("=F"):
            symbol = f"{symbol}=F"
        ticker = yf.Ticker(symbol.replace(".", "-"))
        if str(subject.get("operation") or "ticker") == "ticker":
            info = getattr(ticker, "fast_info", {}) or {}
            last = info.get("last_price") or info.get("lastPrice") or 0
            previous = info.get("previous_close") or info.get("previousClose") or 0
            return {"symbol": subject.get("symbol"), "last": float(last or 0), "previousClose": float(previous or 0), "change": float(last or 0) - float(previous or 0)}
        timeframe = _timeframe(constraints.get("timeframe"))
        interval = {"1m": "1m", "3m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h", "4H": "1h", "1D": "1d", "1W": "1wk"}.get(timeframe, "1d")
        merge_size = {"3m": 3, "4H": 4}.get(timeframe, 1)
        requested_limit = int(subject.get("limit") or 300)
        end = datetime.fromtimestamp(int(subject["before_time"])) if subject.get("before_time") else datetime.now()
        start = end - timedelta(days=max(10, int(subject.get("limit") or 300) * (7 if timeframe == "1W" else 2)))
        bars = _bars_from_frame(
            ticker.history(start=start, end=end + timedelta(days=1), interval=interval),
            limit=requested_limit * merge_size,
        )
        return _merge_bars(bars, merge_size, limit=requested_limit)
    if capability != "asia_equity_kline":
        raise ValueError(f"yfinance transport for {capability} is not implemented")
    from app.data_sources.asia_stock_kline import fetch_yfinance_klines
    from app.data_sources.tencent import normalize_cn_code, normalize_hk_code
    is_hk, symbol, timeframe, limit, before_time = _asia_subject(subject, constraints)
    code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
    return fetch_yfinance_klines(
        is_hk=is_hk, tencent_code=code, timeframe=timeframe, limit=limit,
        before_time=before_time,
    )


def _akshare(capability, subject, constraints, config, credentials, deadline):
    if capability == "cn_hk_quote":
        payload = _akshare("cn_market_snapshot", subject, constraints, config, credentials, deadline)
        wanted = {str(item).split(".")[0].upper() for item in (subject.get("symbols") or [])}
        rows = [row for row in (payload.get("rows") or []) if not wanted or str(row.get("code") or "").upper() in wanted]
        return rows if subject.get("symbols") else (rows[0] if rows else {})
    if capability in {"market_catalog", "symbol_master"}:
        if capability == "symbol_master" and subject.get("operation") == "full_sync":
            from app.services.symbol_master_sync import fetch_cn_stock_symbols, fetch_hk_stock_symbols_akshare

            market = str(subject.get("market") or constraints.get("market") or "CNStock")
            return fetch_cn_stock_symbols() if market == "CNStock" else fetch_hk_stock_symbols_akshare()
        from app.services.market.symbol_search import _search_cn_akshare, _search_hk_akshare

        market = str(subject.get("market") or constraints.get("market") or "CNStock")
        query = str(subject.get("query") or "")
        limit = int(subject.get("limit") or 20)
        if market == "CNStock":
            return _search_cn_akshare(query, limit)
        if market == "HKStock":
            return _search_hk_akshare(query, limit)
        raise ValueError(f"AkShare catalog does not support market {market}")
    if capability == "cn_market_snapshot":
        import akshare as ak  # type: ignore

        from app.services.market.cn_stock_market import CNMarketSnapshotUnavailable, normalize_cn_snapshot_row

        fetched_at = datetime.now(timezone.utc).isoformat()
        frame = ak.stock_zh_a_spot_em()
        records = frame.to_dict("records") if frame is not None else []
        source = "eastmoney-akshare"
        rows = [
            normalized
            for row in records
            if (normalized := normalize_cn_snapshot_row(row, as_of=fetched_at, source=source))
        ]
        if not rows:
            raise CNMarketSnapshotUnavailable("A-share snapshot returned no Shanghai/Shenzhen rows")
        return {"rows": rows, "asOf": fetched_at, "source": source}
    if capability == "cn_hk_fundamentals":
        from app.data_sources.cn_hk_fundamentals import (
            fetch_cn_financial_indicators,
            fetch_cn_financial_statements,
            fetch_cn_fundamental_akshare,
            fetch_hk_financial_indicators,
            fetch_hk_financial_statements,
            fetch_hk_fundamental_akshare,
        )
        from app.data_sources.tencent import normalize_cn_code, normalize_hk_code

        is_hk, symbol, _, _, _ = _asia_subject(subject, constraints)
        code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
        result = fetch_hk_fundamental_akshare(code) if is_hk else fetch_cn_fundamental_akshare(code)
        indicators = fetch_hk_financial_indicators(code) if is_hk else fetch_cn_financial_indicators(code)
        for key, value in indicators.items():
            if value is not None and result.get(key) is None:
                result[key] = value
        statements = fetch_hk_financial_statements(code) if is_hk else fetch_cn_financial_statements(code)
        if statements:
            result["financial_statements"] = statements
        result["source"] = "akshare"
        return result
    if capability != "asia_equity_kline":
        raise ValueError(f"AkShare transport for {capability} is not implemented")
    from app.data_sources.asia_stock_kline import (
        fetch_akshare_minute_klines, fetch_akshare_weekly_klines, normalize_chart_timeframe,
    )
    from app.data_sources.tencent import normalize_cn_code, normalize_hk_code
    is_hk, symbol, timeframe, limit, before_time = _asia_subject(subject, constraints)
    code = normalize_hk_code(symbol) if is_hk else normalize_cn_code(symbol)
    normalized = normalize_chart_timeframe(timeframe)
    if normalized == "1W":
        return fetch_akshare_weekly_klines(is_hk=is_hk, tencent_code=code, limit=limit, before_time=before_time)
    return fetch_akshare_minute_klines(
        is_hk=is_hk, tencent_code=code, timeframe=normalized,
        limit=limit, before_time=before_time,
    )


def _easy_tdx(capability, subject, constraints, config, credentials, deadline):
    if capability == "cn_market_snapshot":
        return _easy_tdx_snapshot(subject, config, deadline)
    from app.services.cn_market_history.instruments import parse_cn_instrument
    from app.services.cn_market_history.tdx_provider import TDXProvider

    operation = str(subject.get("operation") or "")
    provider = TDXProvider()
    provider.probe_hosts()
    with provider:
        if capability == "cn_hk_quote":
            symbols = list(subject.get("symbols") or ([subject.get("symbol")] if subject.get("symbol") else []))
            instruments = [parse_cn_instrument(str(symbol)) for symbol in symbols]
            fetched_at = datetime.now(timezone.utc).isoformat()
            raw = provider.fetch_market_quotes(instruments)
            rows = _normalize_snapshot_rows(raw, source="easy_tdx", fetched_at=fetched_at)
            return rows if subject.get("symbols") else (rows[0] if rows else {})
        if capability == "asia_equity_kline":
            market = str(subject.get("market") or constraints.get("market") or "CNStock")
            if market not in {"CNStock", "CN", "A股"}:
                raise ValueError("easy_tdx K-line transport supports A shares only")
            instrument = parse_cn_instrument(str(subject.get("symbol") or subject.get("instrument") or ""))
            limit = max(1, int(subject.get("limit") or constraints.get("limit") or 300))
            end_date = date.today()
            start_date = date(1990, 1, 1)
            pages = provider.iter_daily_pages(instrument, start_date, end_date, start_offset=0)
            bars = []
            for page in pages:
                bars.extend({
                    "time": int(datetime.combine(bar.trade_date, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
                    "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
                    "close": float(bar.close), "volume": float(bar.volume), "amount": float(bar.amount),
                } for bar in page.bars)
                if len(bars) >= limit:
                    break
            return bars[-limit:]
        instrument = parse_cn_instrument(str(subject.get("instrument") or ""))
        if capability == "cn_equity_history":
            if operation == "metadata":
                return provider.fetch_instrument_metadata(instrument)
            if operation == "daily_page":
                pages = provider.iter_daily_pages(
                    instrument,
                    _date(subject["start_date"]),
                    _date(subject["end_date"]),
                    start_offset=int(subject.get("start_offset") or 0),
                )
                return next(pages)
        if capability == "cn_corporate_actions" and operation == "corporate_actions":
            return provider.fetch_corporate_actions(instrument)
    raise ValueError(f"easy_tdx does not implement {capability}/{operation}")


def _official_adjustment(adapter_key, capability, subject, constraints, config, credentials, deadline):
    if capability != "cn_official_adjustment_reference":
        raise ValueError(f"{adapter_key} does not implement {capability}")
    from app.services.cn_market_history.instruments import parse_cn_instrument
    from app.services.cn_market_history.official_adjustments import (
        OfficialAdjustmentReferenceProvider,
        OfficialAdjustmentSourceError,
    )

    instrument = parse_cn_instrument(str(subject.get("instrument") or ""))
    event_dates = [_date(item) for item in (subject.get("event_dates") or [])]
    provider = OfficialAdjustmentReferenceProvider()
    if adapter_key == "cninfo":
        references = provider._fetch_cninfo(instrument, event_dates)
    elif instrument.exchange == "SH":
        references = provider._fetch_sse(instrument)
    else:
        references = provider._fetch_szse(instrument, event_dates, allow_cninfo=False)
    selected = {item: references[item] for item in event_dates if item in references}
    if len(selected) != len(event_dates):
        raise OfficialAdjustmentSourceError(
            f"{adapter_key} returned {len(selected)} of {len(event_dates)} required references"
        )
    return selected


def _cninfo(capability, subject, constraints, config, credentials, deadline):
    if capability == "cn_corporate_announcement":
        from app.data_sources.cninfo_fundamental_history import fetch_cninfo_annual_announcements

        return fetch_cninfo_annual_announcements(
            str(subject.get("instrument") or subject.get("symbol") or ""),
            start_date=_date(subject["start_date"]),
            end_date=_date(subject["end_date"]),
        )
    return _official_adjustment("cninfo", capability, subject, constraints, config, credentials, deadline)


def _cn_exchange_official(capability, subject, constraints, config, credentials, deadline):
    return _official_adjustment("cn_exchange_official", capability, subject, constraints, config, credentials, deadline)


def _eastmoney(capability, subject, constraints, config, credentials, deadline):
    if capability == "cn_hk_quote":
        payload = _eastmoney_snapshot(subject, config, deadline)
        wanted = {str(item).split(".")[0].upper() for item in (subject.get("symbols") or [])}
        rows = [row for row in (payload.get("rows") or []) if not wanted or str(row.get("code") or "").upper() in wanted]
        return rows if subject.get("symbols") else (rows[0] if rows else {})
    if capability == "cn_market_snapshot":
        return _eastmoney_snapshot(subject, config, deadline)
    if capability != "cn_fundamental_history":
        raise ValueError(f"eastmoney does not implement {capability}")
    from app.data_sources.cn_fundamental_history import fetch_eastmoney_annual_reports

    instrument = str(subject.get("instrument") or subject.get("symbol") or "")
    code = instrument.split(":")[-1].split(".")[0]
    return fetch_eastmoney_annual_reports(code)


def _fred(capability, subject, constraints, config, credentials, deadline):
    if capability != "macro_series" or subject.get("operation") != "fred_series":
        raise ValueError("FRED only supports fred_series macro requests")
    import requests

    params = {
        "series_id": subject.get("series_id"), "api_key": credentials.get("api_key"),
        "file_type": "json", "sort_order": "desc", "limit": max(1, min(int(subject.get("limit") or 120), 1000)),
    }
    if subject.get("start"):
        params["observation_start"] = subject["start"]
    if subject.get("end"):
        params["observation_end"] = subject["end"]
    response = requests.get(f"{str(config.get('base_url') or 'https://api.stlouisfed.org/fred').rstrip('/')}/series/observations", params=params, timeout=int(config.get("timeout_seconds") or 15))
    response.raise_for_status()
    return {"provider": "FRED", "series_id": subject.get("series_id"), "observations": response.json().get("observations") or []}


def _bls(capability, subject, constraints, config, credentials, deadline):
    if capability not in {"macro_series", "us_macro_release"}:
        raise ValueError("BLS only supports bls_series macro requests")
    import requests

    if capability == "us_macro_release":
        current_year = datetime.now(timezone.utc).year
        series_ids = ["CES0000000001"]
        start_year, end_year = current_year - 1, current_year
    elif subject.get("operation") == "bls_series":
        series_ids = list(subject.get("series_ids") or [])
        start_year, end_year = subject.get("start_year"), subject.get("end_year")
    else:
        raise ValueError("BLS only supports bls_series macro requests")
    payload = {"seriesid": series_ids, "startyear": str(start_year), "endyear": str(end_year)}
    if credentials.get("api_key"):
        payload["registrationkey"] = credentials["api_key"]
    response = requests.post(f"{str(config.get('base_url') or 'https://api.bls.gov/publicAPI/v2').rstrip('/')}/timeseries/data/", json=payload, timeout=int(config.get("timeout_seconds") or 15))
    response.raise_for_status()
    data = response.json()
    series = data.get("Results", {}).get("series") or []
    if capability == "macro_series":
        return {"provider": "BLS", "series": series, "status": data.get("status"), "messages": data.get("message") or []}
    points = []
    for item in (series[0].get("data") or []) if series else []:
        period = str(item.get("period") or "")
        if not period.startswith("M") or period == "M13":
            continue
        try:
            points.append((int(item["year"]), int(period[1:]), float(item["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    points.sort(reverse=True)
    if len(points) < 2:
        raise ValueError("BLS returned fewer than two monthly nonfarm observations")
    latest, previous = points[:2]
    return {
        "status": "ok", "actual": round(latest[2] - previous[2]), "forecast": None,
        "previous": None, "period": f"{latest[0]}-{latest[1]:02d}", "release_time": "",
        "unit": "thousand jobs, monthly change in total nonfarm payroll employment",
        "evidence": [{"source": "BLS public API", "title": "CES0000000001 total nonfarm payrolls"}],
    }


def _bea(capability, subject, constraints, config, credentials, deadline):
    if capability != "macro_series" or subject.get("operation") != "bea_dataset":
        raise ValueError("BEA only supports bea_dataset macro requests")
    import requests

    params = {"UserID": credentials.get("api_key"), "method": "GetData", "DataSetName": subject.get("dataset"), "ResultFormat": "JSON"}
    params.update(dict(subject.get("params") or {}))
    response = requests.get(str(config.get("base_url") or "https://apps.bea.gov/api/data"), params=params, timeout=int(config.get("timeout_seconds") or 30))
    response.raise_for_status()
    return {"provider": "BEA", "dataset": subject.get("dataset"), "data": response.json()}


def _calendar_payload(source: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"events": events, "status": "ok" if events else "empty", "source": source, "config_key": "", "message": "" if events else f"{source} returned no economic calendar events."}


def _trading_economics(capability, subject, constraints, config, credentials, deadline):
    if capability != "economic_calendar":
        raise ValueError(f"Trading Economics does not implement {capability}")
    from app.data_providers.economic_calendar import _fetch_tradingeconomics_calendar

    return _calendar_payload("tradingeconomics", _fetch_tradingeconomics_calendar(str(credentials.get("api_key") or "")))


def _akshare_calendar(capability, subject, constraints, config, credentials, deadline):
    if capability == "us_macro_release":
        import akshare as ak  # type: ignore

        frame = ak.macro_usa_non_farm()
        if frame is None or frame.empty:
            raise ValueError("AkShare nonfarm release returned no rows")
        clean = frame.copy()
        clean["日期"] = clean["日期"].astype("datetime64[ns]")
        released = clean[clean["今值"].notna()].sort_values("日期")
        if released.empty:
            raise ValueError("AkShare nonfarm release has no published actual value")
        latest = released.iloc[-1]
        return {
            "status": "ok", "actual": latest.get("今值"), "forecast": latest.get("预测值"),
            "previous": latest.get("前值"), "period": str(latest.get("日期").date()),
            "release_time": "", "unit": "thousand jobs",
            "evidence": [{"source": "AkShare", "title": "US nonfarm payrolls"}],
        }
    if capability == "market_sentiment":
        from app.data_providers.sentiment import fetch_dollar_index_akshare, fetch_vix_akshare

        return {
            "fear_greed": {"value": 50, "classification": "Neutral", "source": "not_available"},
            "vix": fetch_vix_akshare(), "dxy": fetch_dollar_index_akshare(),
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        }
    if capability != "economic_calendar":
        return _akshare(capability, subject, constraints, config, credentials, deadline)
    from app.data_providers.economic_calendar import _fetch_akshare_calendar

    return _calendar_payload("akshare_wallstreetcn", _fetch_akshare_calendar())


def _finnhub_calendar_or_market(capability, subject, constraints, config, credentials, deadline):
    if capability != "economic_calendar":
        return _finnhub(capability, subject, constraints, config, credentials, deadline)
    from app.data_providers.economic_calendar import _fetch_finnhub_calendar

    return _calendar_payload("finnhub", _fetch_finnhub_calendar(str(credentials.get("api_key") or "")))


def _finnhub_opportunities_or_market(capability, subject, constraints, config, credentials, deadline):
    if capability != "equity_opportunity":
        return _finnhub_calendar_or_market(capability, subject, constraints, config, credentials, deadline)
    import requests

    symbols = list(subject.get("symbols") or ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"])
    output = []
    for symbol in symbols[:20]:
        response = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": symbol, "token": credentials.get("api_key")}, timeout=int(config.get("timeout_seconds") or 8))
        response.raise_for_status()
        item = response.json() or {}
        change = float(item.get("dp") or 0)
        if abs(change) < 2:
            continue
        output.append({
            "symbol": symbol, "name": symbol, "price": float(item.get("c") or 0), "change_24h": change,
            "signal": "bullish_momentum" if change > 0 else "bearish_momentum",
            "strength": "strong" if abs(change) >= 5 else "medium",
            "reason": f"Daily move {change:+.2f}%", "impact": "bullish" if change > 0 else "bearish",
            "market": "USStock", "timestamp": int(datetime.now(timezone.utc).timestamp()),
        })
    return output


def _adanos(capability, subject, constraints, config, credentials, deadline):
    if capability != "us_equity_sentiment":
        raise ValueError(f"Adanos does not implement {capability}")
    from app.data_providers.adanos_sentiment import _fetch_adanos_transport

    return _fetch_adanos_transport(
        list(subject.get("tickers") or []), source=subject.get("source"), days=int(subject.get("days") or 7),
        api_key=str(credentials.get("api_key") or ""), base_url=str(config.get("base_url") or "") or None,
        timeout=int(config.get("timeout_seconds") or 10),
    )


def _cnn_fear_greed(capability, subject, constraints, config, credentials, deadline):
    if capability != "market_sentiment":
        raise ValueError(f"Fear and Greed does not implement {capability}")
    from app.data_providers.sentiment import fetch_fear_greed_index

    return {"fear_greed": fetch_fear_greed_index(), "timestamp": int(datetime.now(timezone.utc).timestamp())}


def _search_provider(adapter_key, capability, subject, constraints, config, credentials, deadline):
    if capability != "analysis_search":
        raise ValueError(f"{adapter_key} does not implement {capability}")
    from app.services.search import (
        AlphaVantageNewsProvider, BingSearchProvider, GDELTSearchProvider,
        SearXNGSearchProvider, TavilySearchProvider,
    )

    key = str(credentials.get("api_key") or "")
    providers = {
        "tavily": lambda: TavilySearchProvider([key]),
        "bing": lambda: BingSearchProvider(key),
        "gdelt": GDELTSearchProvider,
        "alpha_vantage": lambda: AlphaVantageNewsProvider(key),
        "searxng": SearXNGSearchProvider,
    }
    if adapter_key in providers:
        provider = providers[adapter_key]()
        queries = []
        language = str(subject.get("language") or "all")
        if subject.get("operation") == "financial_news":
            if language in {"all", "cn"}:
                queries.append(("cn", "全球金融市场 最新新闻"))
            if language in {"all", "en"}:
                queries.append(("en", "global financial markets latest news"))
        else:
            queries.append(("", str(subject.get("query") or "")))
        grouped = {"cn": [], "en": []}
        flat = []
        for lang, query in queries:
            response = provider._do_search(query, key or "free", int(subject.get("max_results") or 5), int(subject.get("days") or 7))
            rows = [item.to_dict() | {"url": item.url} for item in response.results]
            if lang:
                grouped[lang].extend(rows)
            flat.extend(rows)
        return grouped if subject.get("operation") == "financial_news" else {"provider": adapter_key, "results": flat}
    if adapter_key in {"brave", "jina"}:
        return {"provider": adapter_key, "results": []}
    raise ValueError(f"Search transport is not implemented for {adapter_key}")


def _ccxt_public_market(capability, subject, constraints, config, credentials, deadline):
    if capability not in {"market_catalog", "symbol_master"}:
        raise ValueError(f"ccxt_public_market does not implement {capability}")
    if capability == "symbol_master" and subject.get("operation") == "full_sync":
        from app.services.symbol_master_sync import fetch_crypto_symbols

        return fetch_crypto_symbols()
    from app.services.market.symbol_search import _search_crypto_exchange

    return _search_crypto_exchange(
        str(subject.get("query") or ""),
        int(subject.get("limit") or 20),
        set(),
        str(subject.get("exchange_id") or constraints.get("exchange_id") or "binance"),
        str(subject.get("market_type") or constraints.get("market_type") or "spot"),
    )


def _finnhub(capability, subject, constraints, config, credentials, deadline):
    api_key = str(credentials.get("api_key") or "")
    if capability in {"us_equity_market_data", "us_equity_quote"}:
        import requests

        symbol = str(subject.get("symbol") or "")
        if str(subject.get("operation") or "ticker") == "ticker":
            response = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": symbol, "token": api_key}, timeout=8)
            response.raise_for_status()
            data = response.json()
            return {"symbol": symbol, "last": data.get("c"), "change": data.get("d"), "changePercent": data.get("dp"), "high": data.get("h"), "low": data.get("l"), "open": data.get("o"), "previousClose": data.get("pc")}
        timeframe = _timeframe(constraints.get("timeframe"))
        resolution = {"1m": "1", "3m": "1", "5m": "5", "15m": "15", "30m": "30", "1H": "60", "4H": "60", "1D": "D", "1W": "W"}.get(timeframe)
        if not resolution:
            raise ValueError(f"Finnhub does not support timeframe {timeframe}")
        limit = max(1, int(subject.get("limit") or 300))
        merge_size = {"3m": 3, "4H": 4}.get(timeframe, 1)
        seconds = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "4H": 14400, "1D": 86400, "1W": 604800}[timeframe]
        end = int(subject.get("before_time") or datetime.now(timezone.utc).timestamp())
        start = end - seconds * limit * 2
        response = requests.get(
            "https://finnhub.io/api/v1/stock/candle",
            params={"symbol": symbol, "resolution": resolution, "from": start, "to": end, "token": api_key},
            timeout=int(config.get("timeout_seconds") or 8),
        )
        response.raise_for_status()
        data = response.json()
        bars = [
            {"time": int(stamp), "open": float(open_), "high": float(high), "low": float(low), "close": float(close), "volume": float(volume or 0)}
            for stamp, open_, high, low, close, volume in zip(
                data.get("t") or [], data.get("o") or [], data.get("h") or [],
                data.get("l") or [], data.get("c") or [], data.get("v") or [],
            )
        ]
        return _merge_bars(bars, merge_size, limit=limit)
    if capability == "symbol_reference":
        from app.services.symbol_name import _resolve_name_from_finnhub

        return _resolve_name_from_finnhub(str(subject.get("symbol") or ""), api_key=api_key) or ""
    if capability == "market_catalog":
        import requests

        response = requests.get(
            "https://finnhub.io/api/v1/search",
            params={"q": subject.get("query"), "token": api_key},
            timeout=int(config.get("timeout_seconds") or 8),
        )
        response.raise_for_status()
        market = str(subject.get("market") or "USStock")
        return [
            {"market": market, "symbol": item.get("symbol"), "name": item.get("description") or item.get("displaySymbol")}
            for item in (response.json().get("result") or [])[: int(subject.get("limit") or 20)]
        ]
    raise ValueError(f"finnhub does not implement {capability}")


def _yfinance_reference(capability, subject, constraints, config, credentials, deadline):
    if capability != "symbol_reference":
        return _yfinance(capability, subject, constraints, config, credentials, deadline)
    from app.services.symbol_name import _resolve_name_from_yfinance

    return _resolve_name_from_yfinance(str(subject.get("symbol") or "")) or ""


def _moex(capability, subject, constraints, config, credentials, deadline):
    if capability == "symbol_master":
        from app.services.symbol_master_sync import fetch_moex_symbols

        return fetch_moex_symbols()
    if capability != "symbol_reference":
        raise ValueError(f"moex does not implement {capability}")
    import requests
    from app.data_sources.moex import ISS_BASE, MOEXDataSource

    symbol = MOEXDataSource._normalize_symbol(str(subject.get("symbol") or ""))
    response = requests.get(
        f"{ISS_BASE}/securities/{symbol}.json",
        params={"iss.meta": "off"},
        timeout=int(config.get("timeout_seconds") or 8),
    )
    response.raise_for_status()
    description = (response.json() or {}).get("description") or {}
    columns, rows = description.get("columns") or [], description.get("data") or []
    if "name" not in columns or "value" not in columns:
        return ""
    name_index, value_index = columns.index("name"), columns.index("value")
    for row in rows:
        if row[name_index] in {"SHORTNAME", "SECNAME"} and row[value_index]:
            return str(row[value_index]).strip()
    return ""


def _nasdaq_trader(capability, subject, constraints, config, credentials, deadline):
    if capability != "symbol_master":
        raise ValueError(f"nasdaq_trader does not implement {capability}")
    from app.services.symbol_master_sync import fetch_us_stock_symbols

    return fetch_us_stock_symbols()


def _ccxt_market_data(capability, subject, constraints, config, credentials, deadline):
    if capability not in {"crypto_public_market_data", "crypto_derivatives_market_data"}:
        return _ccxt_public_market(capability, subject, constraints, config, credentials, deadline)
    import ccxt
    from app.data_sources.crypto import apply_public_ccxt_endpoint_config, resolve_ccxt_for_live_trading

    exchange_id = str(constraints.get("exchange_id") or "binance")
    market_type = str(constraints.get("market_type") or ("swap" if capability == "crypto_derivatives_market_data" else "spot"))
    ccxt_id, options = resolve_ccxt_for_live_trading(exchange_id, market_type)
    options_config = {"enableRateLimit": True, "timeout": int(config.get("timeout_seconds") or 30) * 1000}
    if options:
        options_config["options"] = options
    exchange = getattr(ccxt, ccxt_id)(apply_public_ccxt_endpoint_config(options_config, exchange_id))
    symbol = str(subject.get("symbol") or "").strip().upper()
    if market_type == "swap" and ":" not in symbol and "/" in symbol:
        quote = symbol.split("/", 1)[1]
        if quote:
            symbol = f"{symbol}:{quote}"
    if str(subject.get("operation") or "ticker") == "ticker":
        return exchange.fetch_ticker(symbol)
    timeframe = _timeframe(constraints.get("timeframe"))
    ccxt_timeframe = {"1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}.get(timeframe, timeframe.lower())
    rows = exchange.fetch_ohlcv(symbol, timeframe=ccxt_timeframe, limit=int(subject.get("limit") or 300))
    return [{"time": int(item[0] / 1000), "open": item[1], "high": item[2], "low": item[3], "close": item[4], "volume": item[5]} for item in rows]


def _binance_public(capability, subject, constraints, config, credentials, deadline):
    if capability == "global_market_overview":
        import requests

        symbol = str(subject.get("symbol") or "BTC").replace("/", "").replace("USDT", "") + "USDT"
        base_url = str(config.get("base_url") or "https://fapi.binance.com").rstrip("/")
        funding = requests.get(f"{base_url}/fapi/v1/fundingRate", params={"symbol": symbol, "limit": 1}, timeout=8)
        interest = requests.get(f"{base_url}/futures/data/openInterestHist", params={"symbol": symbol, "period": "1d", "limit": 2}, timeout=8)
        ratio = requests.get(f"{base_url}/futures/data/globalLongShortAccountRatio", params={"symbol": symbol, "period": "1d", "limit": 1}, timeout=8)
        for response in (funding, interest, ratio):
            response.raise_for_status()
        funding_rows, interest_rows, ratio_rows = funding.json() or [], interest.json() or [], ratio.json() or []
        current_oi = float(interest_rows[-1].get("sumOpenInterestValue") or 0) if interest_rows else None
        previous_oi = float(interest_rows[-2].get("sumOpenInterestValue") or 0) if len(interest_rows) > 1 else None
        return {
            "funding_rate": float(funding_rows[-1].get("fundingRate") or 0) if funding_rows else None,
            "open_interest": current_oi,
            "open_interest_change_24h": ((current_oi - previous_oi) / previous_oi * 100) if current_oi is not None and previous_oi else None,
            "long_short_ratio": float(ratio_rows[-1].get("longShortRatio") or 0) if ratio_rows else None,
            "source": "binance_public",
        }
    if capability != "crypto_public_quote":
        raise ValueError(f"Binance public transport does not implement {capability}")
    import requests

    market_type = str(constraints.get("market_type") or "spot").lower()
    path = "/fapi/v1/ticker/price" if market_type == "swap" else "/api/v3/ticker/price"
    base_url = str(config.get("base_url") or "https://api.binance.com").rstrip("/")
    symbol = str(subject.get("symbol") or "").replace("/", "").split(":", 1)[0].upper()
    response = requests.get(f"{base_url}{path}", params={"symbol": symbol}, timeout=int(config.get("timeout_seconds") or 8))
    response.raise_for_status()
    return {"symbol": subject.get("symbol"), "last": float((response.json() or {}).get("price") or 0)}


def _bybit_public(capability, subject, constraints, config, credentials, deadline):
    if capability != "crypto_public_quote":
        raise ValueError(f"Bybit public transport does not implement {capability}")
    import requests

    category = "spot" if str(constraints.get("market_type") or "spot").lower() == "spot" else "linear"
    symbol = str(subject.get("symbol") or "").replace("/", "").split(":", 1)[0].upper()
    base_url = str(config.get("base_url") or "https://api.bybit.com").rstrip("/")
    response = requests.get(f"{base_url}/v5/market/tickers", params={"category": category, "symbol": symbol}, timeout=int(config.get("timeout_seconds") or 8))
    response.raise_for_status()
    rows = (((response.json() or {}).get("result") or {}).get("list") or [])
    if not rows:
        raise ValueError("Bybit public quote returned no rows")
    return {"symbol": subject.get("symbol"), "last": float(rows[0].get("lastPrice") or rows[0].get("markPrice") or 0)}


def _tiingo(capability, subject, constraints, config, credentials, deadline):
    if capability == "commodity_quote" and subject.get("operation") == "overview":
        from app.data_providers.commodities import _fetch_tiingo

        return _fetch_tiingo(list(subject.get("commodities") or []), str(credentials.get("api_key") or ""))
    if capability == "forex_quote" and subject.get("operation") == "overview":
        from app.data_providers.forex import _fetch_tiingo

        return _fetch_tiingo(list(subject.get("pairs") or []), str(credentials.get("api_key") or ""))
    if capability not in {"forex_market_data", "forex_quote", "futures_market_data", "commodity_quote"}:
        raise ValueError(f"tiingo does not implement {capability}")
    import requests
    from datetime import timedelta

    symbol = str(subject.get("symbol") or "").replace("/", "").lower()
    if capability in {"futures_market_data", "commodity_quote"}:
        symbol = {"gc": "xauusd", "si": "xagusd"}.get(symbol.replace("=f", ""), symbol)
    token = str(credentials.get("api_key") or "")
    base_url = str(config.get("base_url") or "https://api.tiingo.com/tiingo").rstrip("/")
    if base_url.endswith("/fx"):
        fx_base = base_url
    else:
        fx_base = f"{base_url}/fx"
    if str(subject.get("operation") or "ticker") == "ticker":
        response = requests.get(f"{fx_base}/top", params={"tickers": symbol, "token": token}, timeout=15)
        response.raise_for_status()
        item = (response.json() or [{}])[0]
        bid, ask = float(item.get("bidPrice") or 0), float(item.get("askPrice") or 0)
        last = float(item.get("midPrice") or 0) or ((bid + ask) / 2 if bid and ask else bid or ask)
        return {"symbol": subject.get("symbol"), "last": last}
    timeframe = _timeframe(constraints.get("timeframe"))
    frequency = {"5m": "5min", "15m": "15min", "30m": "30min", "1H": "1hour", "4H": "4hour", "1D": "1day"}.get(timeframe)
    if not frequency:
        raise ValueError(f"Tiingo does not support timeframe {timeframe}")
    end = datetime.fromtimestamp(int(subject["before_time"])) if subject.get("before_time") else datetime.now()
    start = end - timedelta(days=max(10, int(subject.get("limit") or 300) * 2))
    response = requests.get(
        f"{fx_base}/{symbol}/prices",
        params={"startDate": start.date().isoformat(), "endDate": end.date().isoformat(), "resampleFreq": frequency, "token": token},
        timeout=20,
    )
    response.raise_for_status()
    return [{"time": int(datetime.fromisoformat(str(item.get("date")).replace("Z", "+00:00")).timestamp()), "open": float(item["open"]), "high": float(item["high"]), "low": float(item["low"]), "close": float(item["close"]), "volume": float(item.get("volume") or 0)} for item in response.json()]


def _coingecko(capability, subject, constraints, config, credentials, deadline):
    if capability == "global_market_overview":
        import requests

        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "symbols": str(subject.get("symbol") or "btc").lower(), "price_change_percentage": "24h"},
            timeout=int(config.get("timeout_seconds") or 8),
        )
        response.raise_for_status()
        rows = response.json() or []
        if not rows:
            raise ValueError("CoinGecko market overview returned no rows")
        volume, market_cap = rows[0].get("total_volume"), rows[0].get("market_cap")
        return {
            "volume_24h": float(volume) if volume is not None else None,
            "volume_change_24h": (float(volume) / float(market_cap) * 100) if volume is not None and market_cap else None,
            "source": "coingecko",
        }
    if capability != "crypto_market_snapshot":
        raise ValueError(f"CoinGecko does not implement {capability}")
    from app.data_providers.crypto import fetch_crypto_heatmap_coingecko

    return fetch_crypto_heatmap_coingecko()


def _coincap(capability, subject, constraints, config, credentials, deadline):
    if capability != "crypto_market_snapshot":
        raise ValueError(f"CoinCap does not implement {capability}")
    from app.data_providers.crypto import fetch_crypto_heatmap_coincap

    return fetch_crypto_heatmap_coincap()


def _coinglass(capability, subject, constraints, config, credentials, deadline):
    if capability != "global_market_overview":
        raise ValueError(f"CoinGlass does not implement {capability}")
    import requests

    base_url = str(config.get("base_url") or "https://open-api-v4.coinglass.com").rstrip("/")
    headers = {"CG-API-KEY": str(credentials.get("api_key") or "")}
    operation = str(subject.get("operation") or "crypto_derivatives")
    def fetch(path, params):
        response = requests.get(f"{base_url}{path}", params=params, headers=headers, timeout=int(config.get("timeout_seconds") or 8))
        response.raise_for_status()
        return response.json() or {}

    if operation == "crypto_capital_flow":
        payload = fetch("/api/futures/coin/netflow", {"symbol": subject.get("symbol")})
        inflow = _first_number(payload, "inflow", "inflowUsd", "inflow_usd")
        outflow = _first_number(payload, "outflow", "outflowUsd", "outflow_usd")
        netflow = inflow - outflow if inflow is not None and outflow is not None else _first_number(payload, "netflow", "netFlow", "net_flow")
        return {"exchange_netflow": netflow, "stablecoin_netflow": None, "source": "coinglass"}
    funding = fetch("/api/futures/fundingRate/exchange-list", {"symbol": subject.get("symbol")})
    interest = fetch("/api/futures/open-interest/exchange-list", {"symbol": subject.get("symbol")})
    ratio = fetch("/api/futures/global-long-short-account-ratio/history", {"symbol": subject.get("symbol"), "interval": "1d", "limit": 1})
    return {
        "funding_rate": _first_number(funding, "oi_weighted_funding_rate", "funding_rate", "fundingRate"),
        "open_interest": _first_number(interest, "open_interest_usd", "openInterestUsd", "open_interest", "openInterest"),
        "open_interest_change_24h": _first_number(interest, "open_interest_change_percent_24h", "openInterestCh24h", "openInterestChangePercent24h", "open_interest_change_24h"),
        "long_short_ratio": _first_number(ratio, "long_short_ratio", "longShortRatio", "global_account_long_short_ratio"),
        "source": "coinglass",
    }


def _cryptoquant(capability, subject, constraints, config, credentials, deadline):
    if capability != "global_market_overview" or subject.get("operation") != "crypto_capital_flow":
        raise ValueError(f"CryptoQuant does not implement {capability}/{subject.get('operation')}")
    import requests

    base_url = str(config.get("base_url") or "https://api.cryptoquant.com").rstrip("/")
    response = requests.get(
        f"{base_url}/v1/stablecoin/exchange-flows/netflow",
        params={"exchange": "all_exchange", "symbol": "all", "window": "day", "limit": 1},
        headers={"Authorization": f"Bearer {credentials.get('api_key') or ''}"},
        timeout=int(config.get("timeout_seconds") or 8),
    )
    response.raise_for_status()
    return {
        "exchange_netflow": None,
        "stablecoin_netflow": _first_number(response.json() or {}, "netflow", "netFlow", "exchange_netflow_total", "value"),
        "source": "cryptoquant",
    }


def bind_production_transports() -> None:
    for key, transport in {
        "tencent": _tencent,
        "twelve_data": _twelve_data,
        "yfinance": _yfinance_reference,
        "akshare": _akshare_calendar,
        "easy_tdx": _easy_tdx,
        "cninfo": _cninfo,
        "cn_exchange_official": _cn_exchange_official,
        "eastmoney": _eastmoney,
        "sina": _sina,
        "ccxt_public_market": _ccxt_market_data,
        "binance_public": _binance_public,
        "bybit_public": _bybit_public,
        "finnhub": _finnhub_opportunities_or_market,
        "moex": _moex,
        "nasdaq_trader": _nasdaq_trader,
        "tiingo": _tiingo,
        "fred": _fred,
        "bls": _bls,
        "bea": _bea,
        "trading_economics": _trading_economics,
        "coingecko": _coingecko,
        "coincap": _coincap,
        "coinglass": _coinglass,
        "cryptoquant": _cryptoquant,
        "adanos": _adanos,
        "cnn_fear_greed": _cnn_fear_greed,
        "searxng": partial(_search_provider, "searxng"),
        "brave": partial(_search_provider, "brave"),
        "bing": partial(_search_provider, "bing"),
        "gdelt": partial(_search_provider, "gdelt"),
        "tavily": partial(_search_provider, "tavily"),
        "jina": partial(_search_provider, "jina"),
        "alpha_vantage": partial(_search_provider, "alpha_vantage"),
    }.items():
        bind_adapter_transport(key, transport)
