"""PostgreSQL repository for China A-share bars, actions, and factors."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Sequence

from app.utils.db import get_db_connection

from .instruments import parse_cn_instrument
from .models import (
    AdjustmentFactor,
    AdjustmentMode,
    CNClassification,
    CNInstrumentMetadata,
    CorporateAction,
    RawDailyBar,
)


@dataclass(frozen=True, slots=True)
class UpsertSummary:
    inserted: int = 0
    unchanged: int = 0
    revised: int = 0

    @property
    def written(self) -> int:
        return self.inserted + self.revised


def _json_safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _json(value: object) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _action_key(action: CorporateAction) -> str:
    identity = (
        action.instrument.canonical,
        action.event_date.isoformat(),
        action.category,
        action.event_name,
    )
    return hashlib.sha256("|".join(map(str, identity)).encode("utf-8")).hexdigest()


class CNMarketHistoryRepository:
    def __init__(self, connection_factory: Callable = get_db_connection) -> None:
        self._connection_factory = connection_factory

    def upsert_instrument_metadata(
        self,
        metadata: CNInstrumentMetadata,
        classifications: Sequence[CNClassification] = (),
    ) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_cn_instruments
                    (instrument, code, exchange, name, security_type, listed_on, delisted_on,
                     source, source_version, content_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument) DO UPDATE SET
                        name = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE qd_cn_instruments.name END,
                        security_type = EXCLUDED.security_type,
                        listed_on = COALESCE(EXCLUDED.listed_on, qd_cn_instruments.listed_on),
                        delisted_on = COALESCE(EXCLUDED.delisted_on, qd_cn_instruments.delisted_on),
                        source = EXCLUDED.source,
                        source_version = EXCLUDED.source_version,
                        content_hash = EXCLUDED.content_hash,
                        updated_at = NOW()
                    """,
                    (
                        metadata.instrument.canonical,
                        metadata.instrument.code,
                        metadata.instrument.exchange,
                        metadata.name,
                        metadata.security_type,
                        metadata.listed_on,
                        metadata.delisted_on,
                        metadata.source,
                        metadata.source_version,
                        metadata.content_hash,
                    ),
                )
                for classification in classifications:
                    cur.execute(
                        """
                        INSERT INTO qd_cn_instrument_classifications
                        (instrument, classification, effective_start, effective_end,
                         source, source_version, confirmed, evidence)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (instrument, classification, effective_start, source)
                        DO UPDATE SET effective_end = EXCLUDED.effective_end,
                                      source_version = EXCLUDED.source_version,
                                      confirmed = EXCLUDED.confirmed,
                                      evidence = EXCLUDED.evidence
                        """,
                        (
                            classification.instrument.canonical,
                            classification.classification,
                            classification.effective_start,
                            classification.effective_end,
                            classification.source,
                            classification.source_version,
                            classification.confirmed,
                            _json(classification.evidence),
                        ),
                    )
                db.commit()
            finally:
                cur.close()

    def get_instrument_metadata(self, instrument: str) -> dict | None:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_cn_instruments WHERE instrument = %s", (canonical,))
                metadata = cur.fetchone()
                if not metadata:
                    return None
                cur.execute(
                    """
                    SELECT * FROM qd_cn_instrument_classifications
                    WHERE instrument = %s ORDER BY effective_start ASC, id ASC
                    """,
                    (canonical,),
                )
                result = dict(metadata)
                result["classifications"] = cur.fetchall()
                return result
            finally:
                cur.close()

    def fetch_confirmed_non_trading_dates(
        self,
        instrument: str,
        start_date: date,
        end_date: date,
    ) -> set[date]:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT DISTINCT trade_date FROM qd_cn_trading_status
                    WHERE instrument = %s AND confirmed = TRUE
                      AND status IN ('suspended', 'not_listed', 'delisted')
                      AND trade_date BETWEEN %s AND %s
                    """,
                    (canonical, start_date, end_date),
                )
                return {row["trade_date"] for row in cur.fetchall()}
            finally:
                cur.close()

    def fetch_rule_context(
        self,
        instrument: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, list[dict]]:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT classification, effective_start, effective_end, source,
                           source_version, confirmed
                    FROM qd_cn_instrument_classifications
                    WHERE instrument = %s
                      AND effective_start <= %s
                      AND (effective_end IS NULL OR effective_end >= %s)
                    ORDER BY effective_start ASC, id ASC
                    """,
                    (canonical, end_date, start_date),
                )
                classifications = cur.fetchall() or []
                cur.execute(
                    """
                    SELECT trade_date, status, source, source_version, confirmed
                    FROM qd_cn_trading_status
                    WHERE instrument = %s AND trade_date BETWEEN %s AND %s
                    ORDER BY trade_date ASC
                    """,
                    (canonical, start_date, end_date),
                )
                return {
                    "classifications": classifications,
                    "statuses": cur.fetchall() or [],
                }
            finally:
                cur.close()

    def upsert_daily_bars(self, bars: Sequence[RawDailyBar]) -> UpsertSummary:
        if not bars:
            return UpsertSummary()
        instrument = bars[0].instrument.canonical
        provider = bars[0].provider
        if any(bar.instrument.canonical != instrument or bar.provider != provider for bar in bars):
            raise ValueError("Daily-bar upserts must contain one instrument and provider")

        dates = [bar.trade_date for bar in bars]
        inserted = unchanged = revised = 0
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT id, trade_date, open, high, low, close, volume, amount,
                           provider_version, content_hash, data_version, collected_at
                    FROM qd_cn_daily_bars
                    WHERE instrument = %s AND provider = %s AND trade_date = ANY(%s)
                    """,
                    (instrument, provider, dates),
                )
                existing = {row["trade_date"]: row for row in cur.fetchall()}

                for bar in bars:
                    previous = existing.get(bar.trade_date)
                    if previous is None:
                        inserted += 1
                    elif previous["content_hash"] == bar.content_hash:
                        unchanged += 1
                    else:
                        revised += 1
                        previous_payload = {
                            key: previous[key]
                            for key in (
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume",
                                "amount",
                                "provider_version",
                                "collected_at",
                            )
                        }
                        cur.execute(
                            """
                            INSERT INTO qd_cn_daily_bar_revisions
                            (daily_bar_id, instrument, trade_date, provider, previous_version,
                             previous_content_hash, previous_payload, replacement_content_hash)
                            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                            """,
                            (
                                previous["id"],
                                instrument,
                                bar.trade_date,
                                provider,
                                previous["data_version"],
                                previous["content_hash"],
                                _json(previous_payload),
                                bar.content_hash,
                            ),
                        )

                    cur.execute(
                        """
                        INSERT INTO qd_cn_daily_bars
                        (instrument, code, exchange, trade_date, open, high, low, close,
                         volume, amount, provider, provider_version, content_hash, collected_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (instrument, trade_date, provider) DO UPDATE SET
                            open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            volume = EXCLUDED.volume,
                            amount = EXCLUDED.amount,
                            provider_version = EXCLUDED.provider_version,
                            data_version = CASE
                                WHEN qd_cn_daily_bars.content_hash <> EXCLUDED.content_hash
                                THEN qd_cn_daily_bars.data_version + 1
                                ELSE qd_cn_daily_bars.data_version
                            END,
                            content_hash = EXCLUDED.content_hash,
                            collected_at = EXCLUDED.collected_at,
                            updated_at = NOW()
                        """,
                        (
                            instrument,
                            bar.instrument.code,
                            bar.instrument.exchange,
                            bar.trade_date,
                            bar.open,
                            bar.high,
                            bar.low,
                            bar.close,
                            bar.volume,
                            bar.amount,
                            provider,
                            bar.provider_version,
                            bar.content_hash,
                            bar.collected_at,
                        ),
                    )
                db.commit()
            finally:
                cur.close()
        return UpsertSummary(inserted=inserted, unchanged=unchanged, revised=revised)

    def fetch_daily_bars(
        self,
        instrument: str,
        start_date: date,
        end_date: date,
        *,
        provider: str = "easy_tdx",
    ) -> list[RawDailyBar]:
        parsed = parse_cn_instrument(instrument)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT trade_date, open, high, low, close, volume, amount,
                           provider, provider_version, content_hash, collected_at
                    FROM qd_cn_daily_bars
                    WHERE instrument = %s AND provider = %s
                      AND trade_date BETWEEN %s AND %s
                    ORDER BY trade_date ASC
                    """,
                    (parsed.canonical, provider, start_date, end_date),
                )
                rows = cur.fetchall()
            finally:
                cur.close()
        return [
            RawDailyBar(
                instrument=parsed,
                trade_date=row["trade_date"],
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
                amount=Decimal(row["amount"]),
                provider=row["provider"],
                provider_version=row["provider_version"],
                content_hash=row["content_hash"],
                collected_at=row["collected_at"],
            )
            for row in rows
        ]

    def get_data_version(
        self,
        instrument: str,
        start_date: date,
        end_date: date,
        *,
        provider: str = "easy_tdx",
    ) -> str:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT trade_date, content_hash, data_version
                    FROM qd_cn_daily_bars
                    WHERE instrument = %s AND provider = %s
                      AND trade_date BETWEEN %s AND %s
                    ORDER BY trade_date ASC
                    """,
                    (canonical, provider, start_date, end_date),
                )
                rows = cur.fetchall()
            finally:
                cur.close()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(
                f"{row['trade_date']}|{row['content_hash']}|{row['data_version']}\n".encode("utf-8")
            )
        return digest.hexdigest() if rows else ""

    def upsert_corporate_actions(self, actions: Sequence[CorporateAction]) -> UpsertSummary:
        if not actions:
            return UpsertSummary()
        inserted = unchanged = revised = 0
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                for action in actions:
                    event_key = _action_key(action)
                    cur.execute(
                        """
                        SELECT content_hash FROM qd_cn_corporate_actions
                        WHERE instrument = %s AND provider = %s AND event_key = %s
                        """,
                        (action.instrument.canonical, action.provider, event_key),
                    )
                    previous = cur.fetchone()
                    if previous is None:
                        inserted += 1
                    elif previous["content_hash"] == action.content_hash:
                        unchanged += 1
                    else:
                        revised += 1

                    cur.execute(
                        """
                        INSERT INTO qd_cn_corporate_actions
                        (instrument, code, exchange, event_date, category, event_name,
                         cash_dividend, rights_price, bonus_ratio, rights_ratio,
                         consolidation_ratio, provider, provider_version, event_key,
                         content_hash, raw_payload, collected_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s::jsonb, %s)
                        ON CONFLICT (instrument, provider, event_key) DO UPDATE SET
                            event_date = EXCLUDED.event_date,
                            category = EXCLUDED.category,
                            event_name = EXCLUDED.event_name,
                            cash_dividend = EXCLUDED.cash_dividend,
                            rights_price = EXCLUDED.rights_price,
                            bonus_ratio = EXCLUDED.bonus_ratio,
                            rights_ratio = EXCLUDED.rights_ratio,
                            consolidation_ratio = EXCLUDED.consolidation_ratio,
                            provider_version = EXCLUDED.provider_version,
                            data_version = CASE
                                WHEN qd_cn_corporate_actions.content_hash <> EXCLUDED.content_hash
                                THEN qd_cn_corporate_actions.data_version + 1
                                ELSE qd_cn_corporate_actions.data_version
                            END,
                            content_hash = EXCLUDED.content_hash,
                            raw_payload = EXCLUDED.raw_payload,
                            collected_at = EXCLUDED.collected_at,
                            updated_at = NOW()
                        """,
                        (
                            action.instrument.canonical,
                            action.instrument.code,
                            action.instrument.exchange,
                            action.event_date,
                            action.category,
                            action.event_name,
                            action.cash_dividend,
                            action.rights_price,
                            action.bonus_ratio,
                            action.rights_ratio,
                            action.consolidation_ratio,
                            action.provider,
                            action.provider_version,
                            event_key,
                            action.content_hash,
                            _json(action.raw_payload),
                            action.collected_at or datetime.now(timezone.utc),
                        ),
                    )
                db.commit()
            finally:
                cur.close()
        return UpsertSummary(inserted=inserted, unchanged=unchanged, revised=revised)

    def fetch_corporate_actions(
        self,
        instrument: str,
        start_date: date,
        end_date: date,
        *,
        provider: str = "easy_tdx",
    ) -> list[dict]:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT * FROM qd_cn_corporate_actions
                    WHERE instrument = %s AND provider = %s
                      AND event_date BETWEEN %s AND %s
                    ORDER BY event_date ASC, category ASC, id ASC
                    """,
                    (canonical, provider, start_date, end_date),
                )
                return cur.fetchall()
            finally:
                cur.close()

    def store_adjustment_factors(
        self,
        factors: Sequence[AdjustmentFactor],
        *,
        action_data_version: str,
    ) -> None:
        if not factors:
            return
        first = factors[0]
        if first.mode is AdjustmentMode.RAW:
            raise ValueError("Raw prices do not have adjustment factors")
        if any(
            factor.instrument != first.instrument
            or factor.mode != first.mode
            or factor.factor_version != first.factor_version
            for factor in factors
        ):
            raise ValueError("Factor writes must contain one instrument, mode, and version")

        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_cn_adjustment_factor_versions
                    (factor_version, instrument, mode, algorithm_version,
                     action_data_version, anchor_date, generated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (factor_version) DO NOTHING
                    """,
                    (
                        first.factor_version,
                        first.instrument.canonical,
                        first.mode.value,
                        first.algorithm_version,
                        action_data_version,
                        first.anchor_date,
                        first.generated_at,
                    ),
                )
                for factor in factors:
                    cur.execute(
                        """
                        INSERT INTO qd_cn_adjustment_factors
                        (instrument, trade_date, mode, factor, factor_version,
                         algorithm_version, anchor_date, generated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (instrument, trade_date, mode, factor_version)
                        DO UPDATE SET factor = EXCLUDED.factor,
                                      generated_at = EXCLUDED.generated_at
                        """,
                        (
                            factor.instrument.canonical,
                            factor.trade_date,
                            factor.mode.value,
                            factor.factor,
                            factor.factor_version,
                            factor.algorithm_version,
                            factor.anchor_date,
                            factor.generated_at,
                        ),
                    )
                db.commit()
            finally:
                cur.close()

    def invalidate_factor_versions(self, instrument: str, reason: str) -> int:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_cn_adjustment_factor_versions
                    SET status = 'invalidated', invalidated_reason = %s, invalidated_at = NOW()
                    WHERE instrument = %s AND status = 'active'
                    """,
                    (reason, canonical),
                )
                count = cur.rowcount
                db.commit()
                return count
            finally:
                cur.close()

    def fetch_active_factors(
        self,
        instrument: str,
        mode: AdjustmentMode,
        start_date: date,
        end_date: date,
    ) -> tuple[str, list[dict]]:
        if mode is AdjustmentMode.RAW:
            return "", []
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT factor_version
                    FROM qd_cn_adjustment_factor_versions
                    WHERE instrument = %s AND mode = %s AND status = 'active'
                    ORDER BY generated_at DESC LIMIT 1
                    """,
                    (canonical, mode.value),
                )
                version_row = cur.fetchone()
                if not version_row:
                    return "", []
                factor_version = version_row["factor_version"]
                cur.execute(
                    """
                    SELECT trade_date, factor, factor_version, algorithm_version,
                           anchor_date, generated_at
                    FROM qd_cn_adjustment_factors
                    WHERE instrument = %s AND mode = %s AND factor_version = %s
                      AND trade_date BETWEEN %s AND %s
                    ORDER BY trade_date ASC
                    """,
                    (canonical, mode.value, factor_version, start_date, end_date),
                )
                return factor_version, cur.fetchall()
            finally:
                cur.close()
