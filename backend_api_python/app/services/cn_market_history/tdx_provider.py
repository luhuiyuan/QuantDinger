"""easy_tdx adapter for raw A-share daily bars and corporate actions."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Callable, Iterator, Sequence

from .config import CNMarketHistorySettings, load_cn_market_history_settings
from .models import (
    CNClassification,
    CNInstrument,
    CNInstrumentMetadata,
    CorporateAction,
    ProviderProbe,
    RawDailyBar,
)


PROVIDER_NAME = "easy_tdx"


class TDXProviderError(RuntimeError):
    code = "cn_history.provider_error"
    retryable = True


class TDXProviderUnavailable(TDXProviderError):
    code = "cn_history.provider_unavailable"


class TDXProviderDataError(TDXProviderError):
    code = "cn_history.provider_data_invalid"
    retryable = False


@dataclass(frozen=True, slots=True)
class DailyBarPage:
    offset: int
    next_offset: int
    raw_count: int
    bars: tuple[RawDailyBar, ...]
    reached_start: bool


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TDXProviderDataError(f"Invalid {field} value: {value!r}") from exc
    if not number.is_finite():
        raise TDXProviderDataError(f"Non-finite {field} value")
    return number


def _optional_decimal(value: object, *, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TDXProviderDataError(f"Invalid {field} value: {value!r}") from exc
    if number.is_nan():
        return None
    if not number.is_finite():
        raise TDXProviderDataError(f"Non-finite {field} value")
    return number


def _record_date(record: dict, *, context: str) -> date:
    year = record.get("year")
    month = record.get("month")
    day = record.get("day")
    if year is not None and month is not None and day is not None:
        try:
            return date(int(year), int(month), int(day))
        except (TypeError, ValueError) as exc:
            raise TDXProviderDataError(f"{context} has no valid date") from exc

    raw_date = record.get("date")
    if isinstance(raw_date, datetime):
        return raw_date.date()
    if isinstance(raw_date, date):
        return raw_date
    if hasattr(raw_date, "date"):
        try:
            parsed = raw_date.date()
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if isinstance(parsed, date):
            return parsed
    try:
        return date.fromisoformat(str(raw_date)[:10])
    except (TypeError, ValueError) as exc:
        raise TDXProviderDataError(f"{context} has no valid date") from exc


def _content_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_easy_tdx() -> SimpleNamespace:
    try:
        import easy_tdx
        from easy_tdx import KlineCategory, Market, TdxClient
        from easy_tdx.config import get_known_hosts
        from easy_tdx.exceptions import TdxCommandError, TdxConnectionError, TdxDecodeError
    except ImportError as exc:
        raise TDXProviderUnavailable(
            "easy_tdx is not installed; build and install the pinned local wheel"
        ) from exc
    return SimpleNamespace(
        client=TdxClient,
        market=Market,
        day=KlineCategory.DAY,
        known_hosts=get_known_hosts,
        retryable=(TdxConnectionError, TdxDecodeError, TdxCommandError, OSError),
        version=easy_tdx.__version__,
    )


class TDXProvider:
    def __init__(
        self,
        settings: CNMarketHistorySettings | None = None,
        *,
        api_loader: Callable[[], SimpleNamespace] = _load_easy_tdx,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or load_cn_market_history_settings()
        self._api_loader = api_loader
        self._sleep = sleep
        self._api: SimpleNamespace | None = None
        self._client = None
        self.selected_host = ""

    @property
    def provider_version(self) -> str:
        return str(self._get_api().version)

    def _get_api(self) -> SimpleNamespace:
        if self._api is None:
            self._api = self._api_loader()
        return self._api

    def probe_hosts(self) -> list[ProviderProbe]:
        api = self._get_api()
        checked_at = datetime.now(timezone.utc)
        hosts = list(api.known_hosts())
        ranked = api.client.ping_all(
            hosts=hosts,
            timeout=min(5.0, self.settings.provider_timeout_seconds),
        )
        latency = {host: seconds for host, seconds in ranked}
        self.selected_host = ranked[0][0] if ranked else ""
        return [
            ProviderProbe(
                host=host,
                latency_ms=(latency[host] * 1000.0 if host in latency else None),
                healthy=host in latency,
                checked_at=checked_at,
                consecutive_failures=0 if host in latency else 1,
                last_success_at=checked_at if host in latency else None,
                error_code="" if host in latency else "unreachable",
            )
            for host in hosts
        ]

    def __enter__(self) -> "TDXProvider":
        api = self._get_api()
        if not self.selected_host:
            probes = self.probe_hosts()
            if not any(probe.healthy for probe in probes):
                raise TDXProviderUnavailable("No reachable TDX hosts")
        self._client = api.client(
            self.selected_host,
            timeout=self.settings.provider_timeout_seconds,
            auto_reconnect=True,
            heartbeat_interval=15.0,
        )
        self._client.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        if self._client is not None:
            self._client.close()
            self._client = None

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("TDXProvider must be used as a context manager")
        return self._client

    def _call(self, operation: Callable):
        api = self._get_api()
        last_error: Exception | None = None
        for attempt in range(self.settings.provider_retry_attempts + 1):
            try:
                return operation()
            except api.retryable as exc:
                last_error = exc
                if attempt >= self.settings.provider_retry_attempts:
                    break
                self._sleep(min(2.0, 0.25 * (2**attempt)))
        raise TDXProviderUnavailable(str(last_error or "TDX operation failed")) from last_error

    def iter_daily_pages(
        self,
        instrument: CNInstrument,
        start_date: date,
        end_date: date,
        *,
        start_offset: int = 0,
    ) -> Iterator[DailyBarPage]:
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        client = self._require_client()
        api = self._get_api()
        market = api.market.SH if instrument.exchange == "SH" else api.market.SZ
        offset = max(0, int(start_offset))

        while True:
            frame = self._call(
                lambda: client.get_security_bars(
                    market,
                    instrument.code,
                    api.day,
                    offset,
                    self.settings.page_size,
                )
            )
            records = frame.to_dict("records") if frame is not None else []
            if not records:
                yield DailyBarPage(
                    offset=offset,
                    next_offset=offset,
                    raw_count=0,
                    bars=(),
                    reached_start=True,
                )
                return

            collected_at = datetime.now(timezone.utc)
            normalized = [
                self._normalize_bar(instrument, record, collected_at=collected_at)
                for record in records
            ]
            normalized.sort(key=lambda item: item.trade_date)
            oldest = normalized[0].trade_date
            selected = tuple(
                bar for bar in normalized if start_date <= bar.trade_date <= end_date
            )
            next_offset = offset + len(records)
            reached_start = oldest <= start_date or len(records) < self.settings.page_size
            yield DailyBarPage(
                offset=offset,
                next_offset=next_offset,
                raw_count=len(records),
                bars=selected,
                reached_start=reached_start,
            )
            if reached_start:
                return
            offset = next_offset
            if self.settings.request_interval_seconds:
                self._sleep(self.settings.request_interval_seconds)

    def fetch_corporate_actions(self, instrument: CNInstrument) -> list[CorporateAction]:
        client = self._require_client()
        api = self._get_api()
        market = api.market.SH if instrument.exchange == "SH" else api.market.SZ
        frame = self._call(lambda: client.get_xdxr_info(market, instrument.code))
        records = frame.to_dict("records") if frame is not None else []
        collected_at = datetime.now(timezone.utc)
        actions = [
            self._normalize_action(instrument, record, collected_at=collected_at)
            for record in records
        ]
        return sorted(actions, key=lambda item: (item.event_date, item.category, item.event_name))

    def fetch_instrument_metadata(
        self,
        instrument: CNInstrument,
    ) -> tuple[CNInstrumentMetadata, tuple[CNClassification, ...]]:
        client = self._require_client()
        api = self._get_api()
        market = api.market.SH if instrument.exchange == "SH" else api.market.SZ
        frame = self._call(lambda: client.get_finance_info(market, instrument.code))
        records = frame.to_dict("records") if frame is not None else []
        record = records[0] if records else {}
        raw_ipo_date = record.get("ipo_date")
        listed_on = None
        if raw_ipo_date:
            text = str(int(raw_ipo_date))
            if len(text) == 8:
                try:
                    listed_on = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
                except ValueError:
                    listed_on = None
        payload = {
            "instrument": instrument.canonical,
            "listed_on": listed_on.isoformat() if listed_on else None,
            "security_type": "ordinary_share",
        }
        metadata = CNInstrumentMetadata(
            instrument=instrument,
            name="",
            security_type="ordinary_share",
            listed_on=listed_on,
            delisted_on=None,
            source=PROVIDER_NAME,
            source_version=self.provider_version,
            content_hash=_content_hash(payload),
        )
        if instrument.code.startswith("688"):
            classification = "star_board"
        elif instrument.code.startswith(("300", "301")):
            classification = "chinext"
        else:
            classification = "main_board"
        classifications = ()
        if listed_on:
            classifications = (
                CNClassification(
                    instrument=instrument,
                    classification=classification,
                    effective_start=listed_on,
                    effective_end=None,
                    source="exchange_code",
                    source_version="1",
                    confirmed=True,
                    evidence={"code_prefix": instrument.code[:3]},
                ),
            )
        return metadata, classifications

    def _normalize_bar(
        self,
        instrument: CNInstrument,
        record: dict,
        *,
        collected_at: datetime,
    ) -> RawDailyBar:
        trade_date = _record_date(record, context="TDX daily bar")
        payload = {
            "instrument": instrument.canonical,
            "trade_date": trade_date.isoformat(),
            "open": str(_decimal(record.get("open"), field="open")),
            "high": str(_decimal(record.get("high"), field="high")),
            "low": str(_decimal(record.get("low"), field="low")),
            "close": str(_decimal(record.get("close"), field="close")),
            "volume": str(_decimal(record.get("vol", 0), field="volume")),
            "amount": str(_decimal(record.get("amount", 0), field="amount")),
        }
        return RawDailyBar(
            instrument=instrument,
            trade_date=trade_date,
            open=Decimal(payload["open"]),
            high=Decimal(payload["high"]),
            low=Decimal(payload["low"]),
            close=Decimal(payload["close"]),
            volume=Decimal(payload["volume"]),
            amount=Decimal(payload["amount"]),
            provider=PROVIDER_NAME,
            provider_version=self.provider_version,
            content_hash=_content_hash(payload),
            collected_at=collected_at,
        )

    def _normalize_action(
        self,
        instrument: CNInstrument,
        record: dict,
        *,
        collected_at: datetime,
    ) -> CorporateAction:
        try:
            category = int(record["category"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TDXProviderDataError("TDX corporate action has no valid identity") from exc
        event_date = _record_date(record, context="TDX corporate action")
        payload = {
            key: record.get(key)
            for key in (
                "date",
                "year",
                "month",
                "day",
                "category",
                "name",
                "fenhong",
                "peigujia",
                "songzhuangu",
                "peigu",
                "suogu",
                "panqian_liutong",
                "panhou_liutong",
                "qian_zongguben",
                "hou_zongguben",
            )
        }

        return CorporateAction(
            instrument=instrument,
            event_date=event_date,
            category=category,
            event_name=str(record.get("name") or ""),
            cash_dividend=_optional_decimal(record.get("fenhong"), field="fenhong"),
            rights_price=_optional_decimal(record.get("peigujia"), field="peigujia"),
            bonus_ratio=_optional_decimal(
                record.get("songzhuangu"), field="songzhuangu"
            ),
            rights_ratio=_optional_decimal(record.get("peigu"), field="peigu"),
            consolidation_ratio=_optional_decimal(record.get("suogu"), field="suogu"),
            provider=PROVIDER_NAME,
            provider_version=self.provider_version,
            content_hash=_content_hash(payload),
            collected_at=collected_at,
            raw_payload=payload,
        )
