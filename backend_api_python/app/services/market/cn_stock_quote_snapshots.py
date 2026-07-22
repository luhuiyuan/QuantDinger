"""Persistent latest-quote read model and scheduled refresh for China A-shares."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from app.services.market.cn_stock_market import (
    CNMarketSnapshotService,
    CNMarketSnapshotUnavailable,
    build_market_breadth,
    fetch_cn_quote_rows,
    fetch_core_indices,
    load_cn_symbol_catalog,
)
from app.services.market_schedule import _calendar
from app.utils.cache import CacheManager
from app.utils.db import get_db_connection


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
OVERVIEW_CACHE_KEY = "cn_market:persisted_overview:v1"
SORT_COLUMNS = {
    "symbol": "s.symbol",
    "name": "s.name",
    "change_percent": "q.change_percent",
    "volume": "q.volume",
    "amount": "q.amount",
    "quote_time": "q.quote_time",
}


def _safe_error(value: object, limit: int = 1000) -> str:
    return str(value or "").replace("\x00", " ")[:limit]


def _parse_quote_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RefreshDecision:
    run: bool
    reason: str
    final: bool = False


def quote_refresh_decision(now: datetime | None = None) -> RefreshDecision:
    local = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI_TZ)
    session_date = local.date()
    try:
        if not _calendar("CNStock").is_session(session_date.isoformat()):
            return RefreshDecision(False, "non_trading_day")
    except Exception:
        if session_date.weekday() >= 5:
            return RefreshDecision(False, "non_trading_day")
    current = local.time().replace(tzinfo=None)
    if time(9, 25) <= current <= time(11, 35) or time(12, 55) <= current <= time(15, 0):
        return RefreshDecision(True, "trading_session")
    if time(15, 1) <= current <= time(15, 20):
        return RefreshDecision(True, "final_refresh", final=True)
    return RefreshDecision(False, "outside_trading_session")


class CNStockQuoteSnapshotRepository:
    def create_run(self, trigger_kind: str, *, status: str = "running", reason: str = "") -> str:
        run_id = uuid.uuid4().hex
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_cn_stock_quote_refresh_runs
                    (run_id, trigger_kind, status, skip_reason, finished_at)
                VALUES (?, ?, ?, ?, CASE WHEN ? = 'skipped' THEN NOW() ELSE NULL END)
                """,
                (run_id, trigger_kind, status, reason, status),
            )
            db.commit()
            cur.close()
        return run_id

    def finish_run(self, run_id: str, *, status: str, source: str = "", planned: int = 0,
                   succeeded: int = 0, failed: int = 0, missing: int = 0,
                   reason: str = "", error: str = "") -> None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                UPDATE qd_cn_stock_quote_refresh_runs SET
                    status = ?, source = ?, planned_symbols = ?, succeeded_symbols = ?,
                    failed_symbols = ?, missing_symbols = ?, skip_reason = ?, last_error = ?,
                    finished_at = NOW(), updated_at = NOW()
                WHERE run_id = ?
                """,
                (status, source, planned, succeeded, failed, missing, reason,
                 _safe_error(error), run_id),
            )
            db.commit()
            cur.close()

    def final_refresh_completed(self, session_date: date) -> bool:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT 1 FROM qd_cn_stock_quote_refresh_runs
                WHERE skip_reason = 'final_refresh' AND status IN ('succeeded', 'partial')
                  AND (started_at AT TIME ZONE 'Asia/Shanghai')::date = ?
                LIMIT 1
                """,
                (session_date,),
            )
            found = cur.fetchone() is not None
            cur.close()
        return found

    def upsert_quotes(self, rows: Iterable[dict], run_id: str) -> int:
        values = []
        for row in rows or []:
            latest = row.get("latest")
            instrument = str(row.get("instrument") or "")
            if not instrument or latest is None or float(latest) <= 0:
                continue
            try:
                quote_time = _parse_quote_time(row.get("asOf"))
            except (TypeError, ValueError):
                continue
            values.append((
                instrument, str(row.get("symbol") or ""), str(row.get("code") or ""),
                str(row.get("exchange") or ""), latest, row.get("previousClose"),
                row.get("change"), row.get("changePercent"), row.get("open"),
                row.get("high"), row.get("low"), row.get("volume"), row.get("amount"),
                quote_time, str(row.get("source") or "unknown"), run_id,
            ))
        if not values:
            return 0
        sql = """
            INSERT INTO qd_cn_stock_quote_snapshots (
                instrument, symbol, code, exchange, latest, previous_close,
                change_value, change_percent, open_price, high_price, low_price,
                volume, amount, quote_time, source, refresh_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
            ON CONFLICT (instrument) DO UPDATE SET
                symbol = EXCLUDED.symbol, code = EXCLUDED.code, exchange = EXCLUDED.exchange,
                latest = EXCLUDED.latest, previous_close = EXCLUDED.previous_close,
                change_value = EXCLUDED.change_value, change_percent = EXCLUDED.change_percent,
                open_price = EXCLUDED.open_price, high_price = EXCLUDED.high_price,
                low_price = EXCLUDED.low_price, volume = EXCLUDED.volume, amount = EXCLUDED.amount,
                quote_time = EXCLUDED.quote_time, source = EXCLUDED.source,
                refresh_run_id = EXCLUDED.refresh_run_id, updated_at = NOW()
            WHERE EXCLUDED.quote_time >= qd_cn_stock_quote_snapshots.quote_time
        """
        with get_db_connection() as db:
            cur = db.cursor()
            for value in values:
                cur.execute(sql, value)
            db.commit()
            cur.close()
        return len(values)

    def latest_run(self, *, include_skipped: bool = True) -> dict | None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT run_id, trigger_kind, status, source, planned_symbols,
                       succeeded_symbols, failed_symbols, missing_symbols,
                       skip_reason, started_at, finished_at
                FROM qd_cn_stock_quote_refresh_runs
                {'' if include_skipped else "WHERE status <> 'skipped'"}
                ORDER BY started_at DESC LIMIT 1
                """
            )
            row = cur.fetchone()
            cur.close()
        if not row:
            return None
        output = dict(row)
        for key in ("started_at", "finished_at"):
            if output.get(key):
                output[key] = output[key].isoformat()
        return output

    def get_quote(self, instrument: str) -> dict | None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT instrument, symbol, code, exchange, latest, previous_close,
                       change_value, change_percent, open_price, high_price, low_price,
                       volume, amount, quote_time, source
                FROM qd_cn_stock_quote_snapshots WHERE instrument = ?
                """,
                (instrument,),
            )
            row = cur.fetchone()
            cur.close()
        return _snapshot_db_row(row) if row else None

    def load_market_snapshot(self) -> dict | None:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT instrument, symbol, code, exchange, latest, previous_close,
                       change_value, change_percent, open_price, high_price, low_price,
                       volume, amount, quote_time, source
                FROM qd_cn_stock_quote_snapshots ORDER BY code
                """
            )
            db_rows = cur.fetchall() or []
            cur.close()
        if not db_rows:
            return None
        rows = [_snapshot_db_row(row) for row in db_rows]
        newest = max(row["asOf"] for row in rows if row.get("asOf"))
        local_now = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
        freshness = "closed" if local_now.time() > time(15, 0) else "fresh"
        return {
            "rows": rows, "asOf": newest, "source": "postgres-latest-snapshot",
            "freshness": freshness, "status": "available", "warning": None,
        }


class CNStockQuoteRefreshService:
    lock_key = "cn_market:quote_refresh:lock:v1"

    def __init__(self, repository: CNStockQuoteSnapshotRepository | None = None,
                 snapshot_service: CNMarketSnapshotService | None = None,
                 cache: CacheManager | None = None) -> None:
        self.repository = repository or CNStockQuoteSnapshotRepository()
        self.snapshot_service = snapshot_service or CNMarketSnapshotService()
        self.cache = cache or CacheManager()
        self.batch_size = min(100, max(1, int(os.getenv("CN_QUOTE_REFRESH_BATCH_SIZE", "100"))))
        self.lock_ttl = max(300, int(os.getenv("CN_QUOTE_REFRESH_LOCK_TTL_SEC", "3600")))

    def run(self, *, trigger_kind: str = "scheduled", now: datetime | None = None,
            force: bool = False) -> dict:
        decision = RefreshDecision(True, "forced") if force else quote_refresh_decision(now)
        if not decision.run:
            return {"runId": None, "status": "skipped", "reason": decision.reason}
        local_date = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI_TZ).date()
        if decision.final and self.repository.final_refresh_completed(local_date):
            run_id = self.repository.create_run(trigger_kind, status="skipped", reason="final_already_completed")
            return {"runId": run_id, "status": "skipped", "reason": "final_already_completed"}

        owner = uuid.uuid4().hex
        if not self.cache.acquire_lock(self.lock_key, owner, self.lock_ttl):
            run_id = self.repository.create_run(trigger_kind, status="skipped", reason="refresh_locked")
            return {"runId": run_id, "status": "skipped", "reason": "refresh_locked"}

        run_id = self.repository.create_run(trigger_kind)
        catalog = load_cn_symbol_catalog()
        planned = len(catalog)
        succeeded = failed = missing = 0
        source = ""
        errors: list[str] = []
        try:
            try:
                snapshot = self.snapshot_service.get_snapshot(force=True)
                rows = snapshot.get("rows") or []
                min_coverage = float(os.getenv("CN_QUOTE_FULL_SNAPSHOT_MIN_COVERAGE", "0.85"))
                if snapshot.get("freshness") != "fresh" or len(rows) < max(1, int(planned * min_coverage)):
                    raise CNMarketSnapshotUnavailable("full snapshot coverage insufficient")
                for start in range(0, len(rows), self.batch_size):
                    succeeded += self.repository.upsert_quotes(rows[start:start + self.batch_size], run_id)
                missing = max(0, planned - succeeded)
                source = "full_snapshot"
            except Exception as exc:
                errors.append(_safe_error(exc))
                source = "tencent_batch"
                for start in range(0, planned, self.batch_size):
                    batch = catalog[start:start + self.batch_size]
                    try:
                        rows = fetch_cn_quote_rows([item["symbol"] for item in batch])
                        written = self.repository.upsert_quotes(rows, run_id)
                        succeeded += written
                        missing += max(0, len(batch) - written)
                    except Exception as batch_error:
                        failed += len(batch)
                        errors.append(_safe_error(batch_error, 240))
            status = "succeeded" if succeeded == planned and not failed and not missing else "partial"
            if not succeeded:
                status = "failed"
            self.repository.finish_run(
                run_id, status=status, source=source, planned=planned, succeeded=succeeded,
                failed=failed, missing=missing, reason=decision.reason,
                error="; ".join(errors),
            )
            try:
                rebuild_cn_market_overview_cache(cache=self.cache)
            except Exception:
                # Quote persistence remains successful even if the derived
                # Redis overview cache cannot be rebuilt; the read path can
                # reconstruct it from PostgreSQL on the next request.
                pass
            self.cache.delete("cn_market:catalog_query_version")
            return {"runId": run_id, "status": status, "source": source, "planned": planned,
                    "succeeded": succeeded, "failed": failed, "missing": missing}
        except Exception as exc:
            self.repository.finish_run(run_id, status="failed", source=source, planned=planned,
                                       succeeded=succeeded, failed=failed, missing=missing,
                                       reason=decision.reason, error=str(exc))
            raise
        finally:
            self.cache.release_lock(self.lock_key, owner)


def _snapshot_db_row(row: dict) -> dict:
    quote_time = row.get("quote_time")
    return {
        "instrument": row.get("instrument"), "symbol": row.get("symbol"),
        "code": row.get("code"), "exchange": row.get("exchange"),
        "latest": row.get("latest"), "previousClose": row.get("previous_close"),
        "change": row.get("change_value"), "changePercent": row.get("change_percent"),
        "open": row.get("open_price"), "high": row.get("high_price"), "low": row.get("low_price"),
        "volume": row.get("volume"), "amount": row.get("amount"),
        "asOf": quote_time.isoformat() if quote_time else None,
        "source": row.get("source") or "postgres-latest-snapshot",
    }


def load_persisted_cn_market_snapshot() -> dict | None:
    return CNStockQuoteSnapshotRepository().load_market_snapshot()


def rebuild_cn_market_overview_cache(*, cache: CacheManager | None = None) -> dict | None:
    cache = cache or CacheManager()
    snapshot = load_persisted_cn_market_snapshot()
    if not snapshot:
        return None
    breadth = build_market_breadth(snapshot.get("rows") or [])
    breadth["status"] = snapshot.get("status")
    breadth["warning"] = snapshot.get("warning")
    payload = {
        "indices": fetch_core_indices(),
        "breadth": breadth,
        "snapshot": {
            "asOf": snapshot.get("asOf"), "source": snapshot.get("source"),
            "freshness": snapshot.get("freshness"), "status": snapshot.get("status"),
            "warning": snapshot.get("warning"),
        },
    }
    cache.set(OVERVIEW_CACHE_KEY, payload, ttl=max(3600, int(os.getenv("CN_OVERVIEW_CACHE_TTL_SEC", "172800"))))
    return payload


def get_cn_market_overview_cache(*, rebuild_on_miss: bool = True) -> dict | None:
    cache = CacheManager()
    cached = cache.get(OVERVIEW_CACHE_KEY)
    if cached is not None:
        return cached
    return rebuild_cn_market_overview_cache(cache=cache) if rebuild_on_miss else None


def query_cn_stock_snapshot_page(*, user_id: int, keyword: str = "", exchange: str = "",
                                 change_state: str = "", page: int = 1, page_size: int = 20,
                                 sort_by: str = "symbol", sort_order: str = "asc") -> dict:
    sort_column = SORT_COLUMNS.get(sort_by)
    if sort_column is None or sort_order not in {"asc", "desc"}:
        raise ValueError("cn_market.invalid_sort")
    pattern = f"%{keyword}%"
    change_sql = ""
    if change_state == "up":
        change_sql = "AND q.change_percent > 0"
    elif change_state == "down":
        change_sql = "AND q.change_percent < 0"
    elif change_state == "flat":
        change_sql = "AND ABS(q.change_percent) <= 0.000001"
    where = f"""
        s.market = 'CNStock' AND s.is_active = 1
        AND s.symbol ~ '^(600|601|603|605|688|000|001|002|003|300|301)[0-9]{{3}}$'
        AND (? = '' OR s.symbol ILIKE ? OR s.name ILIKE ?)
        AND (? = '' OR (CASE WHEN LEFT(s.symbol, 1) = '6' THEN 'SH' ELSE 'SZ' END) = ?)
        {change_sql}
    """
    order = f"{sort_column} {sort_order.upper()} NULLS LAST, s.symbol ASC"
    params = (keyword, pattern, pattern, exchange, exchange)
    offset = (page - 1) * page_size
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) AS total FROM qd_market_symbols s LEFT JOIN qd_cn_stock_quote_snapshots q ON q.code = s.symbol WHERE {where}", params)
        total = int((cur.fetchone() or {}).get("total") or 0)
        cur.execute(
            f"""
            SELECT s.symbol AS code, s.name,
                   CASE WHEN LEFT(s.symbol, 1) = '6' THEN 'SH' ELSE 'SZ' END AS exchange,
                   q.instrument, q.symbol, q.latest, q.previous_close, q.change_value,
                   q.change_percent, q.open_price, q.high_price, q.low_price,
                   q.volume, q.amount, q.quote_time, q.source,
                   EXISTS(SELECT 1 FROM qd_watchlist w WHERE w.user_id = ? AND w.market = 'CNStock'
                          AND w.symbol IN (s.symbol, q.symbol)) AS watchlisted
            FROM qd_market_symbols s
            LEFT JOIN qd_cn_stock_quote_snapshots q ON q.code = s.symbol
            WHERE {where}
            ORDER BY {order} LIMIT ? OFFSET ?
            """,
            (user_id, *params, page_size, offset),
        )
        rows = cur.fetchall() or []
        cur.execute(
            """
            SELECT COUNT(*) AS covered, MIN(quote_time) AS oldest_quote_time,
                   MAX(quote_time) AS newest_quote_time
            FROM qd_cn_stock_quote_snapshots
            """
        )
        coverage = cur.fetchone() or {}
        cur.close()
    now = datetime.now(timezone.utc)
    fresh_minutes = max(5, int(os.getenv("CN_QUOTE_FRESH_MINUTES", "10")))
    items = []
    for row in rows:
        quote_time = row.get("quote_time")
        freshness = "unavailable"
        if quote_time:
            local_now = now.astimezone(SHANGHAI_TZ)
            local_quote = quote_time.astimezone(SHANGHAI_TZ)
            if local_quote.date() == local_now.date() and local_now.time() > time(15, 0):
                freshness = "closed"
            elif (now - quote_time).total_seconds() <= fresh_minutes * 60:
                freshness = "fresh"
            else:
                freshness = "stale"
        exchange_value = row.get("exchange")
        code = str(row.get("code") or "")
        symbol = row.get("symbol") or (f"{code}.{exchange_value}" if code else "")
        items.append({
            "instrument": row.get("instrument") or (f"CNStock:{symbol}" if symbol else None),
            "code": code, "symbol": symbol, "exchange": exchange_value, "name": row.get("name") or code,
            "latest": row.get("latest"), "previousClose": row.get("previous_close"),
            "change": row.get("change_value"), "changePercent": row.get("change_percent"),
            "open": row.get("open_price"), "high": row.get("high_price"), "low": row.get("low_price"),
            "volume": row.get("volume"), "amount": row.get("amount"),
            "asOf": quote_time.isoformat() if quote_time else None, "source": row.get("source"),
            "freshness": freshness, "quoteStatus": "available" if row.get("latest") is not None else "unavailable",
            "watchlisted": bool(row.get("watchlisted")),
        })
    repository = CNStockQuoteSnapshotRepository()
    latest_run = repository.latest_run(include_skipped=False)
    latest_trigger = repository.latest_run(include_skipped=True)
    return {
        "items": items,
        "pagination": {"page": page, "pageSize": page_size, "total": total},
        "coverage": {
            "catalogCount": total if not keyword and not exchange and not change_state else None,
            "coveredCount": int(coverage.get("covered") or 0),
            "oldestQuoteTime": coverage.get("oldest_quote_time").isoformat() if coverage.get("oldest_quote_time") else None,
            "newestQuoteTime": coverage.get("newest_quote_time").isoformat() if coverage.get("newest_quote_time") else None,
        },
        "refreshRun": latest_run,
        "latestTrigger": latest_trigger,
        "partial": False,
    }
