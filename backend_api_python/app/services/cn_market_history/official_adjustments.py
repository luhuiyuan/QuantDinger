"""Official exchange reference prices for corporate-action verification."""

from __future__ import annotations

import hashlib
import json
import time
from base64 import b64encode
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable, Mapping, Sequence

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .adjustments import corporate_action_ratio
from .config import CNMarketHistorySettings, load_cn_market_history_settings
from .models import CNInstrument, CorporateAction, RawDailyBar


SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_REPORT_URL = "https://www.szse.cn/api/report/ShowReport/data"
CNINFO_DIVIDEND_URL = "https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1139"
PRICE_TICK = Decimal("0.01")
PARAMETER_TOLERANCE = Decimal("0.000001")


class OfficialAdjustmentError(RuntimeError):
    code = "cn_history.official_adjustment_invalid"
    retryable = False


class OfficialAdjustmentSourceError(OfficialAdjustmentError):
    code = "cn_history.official_adjustment_unavailable"
    retryable = True


class OfficialAdjustmentMismatch(OfficialAdjustmentError):
    code = "cn_history.official_adjustment_mismatch"


@dataclass(frozen=True, slots=True)
class OfficialAdjustmentReference:
    instrument: str
    event_date: date
    reference_price: Decimal | None
    source: str
    source_url: str
    response_hash: str
    retrieved_at: datetime
    evidence: Mapping[str, object]


Requester = Callable[[str, dict, dict, float], tuple[object, bytes, str]]


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OfficialAdjustmentSourceError(f"Invalid official {field}: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise OfficialAdjustmentSourceError(f"Invalid official {field}: {value!r}")
    return number


def _parse_compact_date(value: object) -> date:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise OfficialAdjustmentSourceError(f"Invalid official event date: {value!r}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _default_requester(
    url: str,
    params: dict,
    headers: dict,
    timeout: float,
) -> tuple[object, bytes, str]:
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json(), response.content, response.url


class OfficialAdjustmentReferenceProvider:
    def __init__(
        self,
        settings: CNMarketHistorySettings | None = None,
        *,
        requester: Requester = _default_requester,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or load_cn_market_history_settings()
        self._requester = requester
        self._sleep = sleep

    def fetch_references(
        self,
        instrument: CNInstrument,
        actions: Sequence[CorporateAction],
    ) -> dict[date, OfficialAdjustmentReference]:
        event_dates = sorted(
            {
                action.event_date
                for action in actions
                if action.category in {1, 11, 12}
            }
        )
        if not event_dates:
            return {}
        if instrument.exchange == "SH":
            references = self._fetch_sse(instrument)
        else:
            references = self._fetch_szse(instrument, event_dates)
        missing = [item.isoformat() for item in event_dates if item not in references]
        if missing:
            raise OfficialAdjustmentSourceError(
                f"Official adjustment reference missing for {instrument.canonical}: {missing}"
            )
        return {item: references[item] for item in event_dates}

    def _request(self, url: str, params: dict, headers: dict):
        last_error: Exception | None = None
        for attempt in range(self.settings.provider_retry_attempts + 1):
            try:
                return self._requester(
                    url,
                    params,
                    headers,
                    self.settings.provider_timeout_seconds,
                )
            except (requests.RequestException, ValueError, OSError) as exc:
                last_error = exc
                if attempt >= self.settings.provider_retry_attempts:
                    break
                self._sleep(min(2.0, 0.25 * (2**attempt)))
        raise OfficialAdjustmentSourceError(
            f"Official adjustment request failed: {last_error or 'unknown error'}"
        ) from last_error

    def _fetch_sse(
        self,
        instrument: CNInstrument,
    ) -> dict[date, OfficialAdjustmentReference]:
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "200",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_LRFP_FH_L",
            "IS_STAR": "1" if instrument.code.startswith("688") else "0",
            "CONDITION_ZBA": "1",
            "CONDITION_ZBB": "",
            "COMPANY_CODE": instrument.code,
        }
        payload, body, source_url = self._request(
            SSE_QUERY_URL,
            params,
            {
                "Referer": (
                    "https://www.sse.com.cn/assortment/stock/list/info/company/"
                    f"index.shtml?COMPANY_CODE={instrument.code}"
                ),
                "User-Agent": "QuantDinger/official-adjustment-verifier",
            },
        )
        if not isinstance(payload, dict):
            raise OfficialAdjustmentSourceError("Unexpected SSE adjustment response")
        rows = payload.get("result") or []
        response_hash = hashlib.sha256(body).hexdigest()
        retrieved_at = datetime.now(timezone.utc)
        output = {}
        for row in rows:
            if str(row.get("A_STOCK_CODE") or "") != instrument.code:
                continue
            event_date = _parse_compact_date(row.get("A_DIV_DATE"))
            output[event_date] = OfficialAdjustmentReference(
                instrument=instrument.canonical,
                event_date=event_date,
                reference_price=_decimal(row.get("PRE_CLOSE_PRICE"), field="reference price"),
                source="sse_dividend",
                source_url=source_url,
                response_hash=response_hash,
                retrieved_at=retrieved_at,
                evidence={
                    "registration_date": row.get("A_REG_DATE"),
                    "cash_dividend": row.get("A_BEFR_TAX_DIV"),
                    "stock_volume": row.get("A_STOCK_VOL"),
                },
            )
        return output

    def _fetch_szse(
        self,
        instrument: CNInstrument,
        event_dates: Sequence[date],
    ) -> dict[date, OfficialAdjustmentReference]:
        output = {}
        for index, event_date in enumerate(event_dates):
            params = {
                "SHOWTYPE": "JSON",
                "CATALOGID": "1815_stock",
                "TABKEY": "tab1",
                "txtDMorJC": instrument.code,
                "txtBeginDate": event_date.isoformat(),
                "PAGENO": "1",
            }
            payload, body, source_url = self._request(
                SZSE_REPORT_URL,
                params,
                {
                    "Referer": "https://www.szse.cn/",
                    "User-Agent": "QuantDinger/official-adjustment-verifier",
                },
            )
            rows = []
            if isinstance(payload, list):
                for section in payload:
                    if isinstance(section, dict):
                        rows.extend(section.get("data") or [])
            row = next(
                (
                    item
                    for item in rows
                    if str(item.get("zqdm") or "") == instrument.code
                    and str(item.get("jyrq") or "") == event_date.isoformat()
                ),
                None,
            )
            if row is not None:
                output[event_date] = OfficialAdjustmentReference(
                    instrument=instrument.canonical,
                    event_date=event_date,
                    reference_price=_decimal(row.get("qss"), field="reference price"),
                    source="szse_daily_quote",
                    source_url=source_url,
                    response_hash=hashlib.sha256(body).hexdigest(),
                    retrieved_at=datetime.now(timezone.utc),
                    evidence={"security_name": row.get("zqjc")},
                )
            if index + 1 < len(event_dates) and self.settings.request_interval_seconds:
                self._sleep(self.settings.request_interval_seconds)
        missing = [item for item in event_dates if item not in output]
        if missing:
            output.update(self._fetch_cninfo(instrument, missing))
        return output

    def _fetch_cninfo(
        self,
        instrument: CNInstrument,
        event_dates: Sequence[date],
    ) -> dict[date, OfficialAdjustmentReference]:
        payload, body, source_url = self._request(
            CNINFO_DIVIDEND_URL,
            {"scode": instrument.code},
            {
                "Accept": "*/*",
                "Accept-Enckey": _cninfo_access_key(),
                "Origin": "https://webapi.cninfo.com.cn",
                "Referer": "https://webapi.cninfo.com.cn/",
                "User-Agent": "QuantDinger/official-adjustment-verifier",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise OfficialAdjustmentSourceError("Unexpected CNINFO dividend response")
        wanted = set(event_dates)
        response_hash = hashlib.sha256(body).hexdigest()
        retrieved_at = datetime.now(timezone.utc)
        output = {}
        for row in payload["records"]:
            if not row.get("F020D"):
                continue
            event_date = _parse_compact_date(row.get("F020D"))
            if event_date not in wanted:
                continue
            cash_per_ten = _optional_decimal(row.get("F012N"))
            output[event_date] = OfficialAdjustmentReference(
                instrument=instrument.canonical,
                event_date=event_date,
                reference_price=None,
                source="cninfo_dividend_parameters",
                source_url=source_url,
                response_hash=response_hash,
                retrieved_at=retrieved_at,
                evidence={
                    "announcement_date": row.get("F006D"),
                    "record_date": row.get("F018D"),
                    "cash_dividend_per_share": str(cash_per_ten / Decimal("10")),
                    "bonus_ratio_per_ten": row.get("F010N"),
                    "transfer_ratio_per_ten": row.get("F011N"),
                    "plan": row.get("F007V"),
                    "report_period": row.get("F001V"),
                },
            )
        return output


def _optional_decimal(value: object) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OfficialAdjustmentSourceError(f"Invalid official adjustment parameter: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise OfficialAdjustmentSourceError(f"Invalid official adjustment parameter: {value!r}")
    return number


def _cninfo_access_key() -> str:
    key = b"1234567887654321"
    padder = padding.PKCS7(128).padder()
    plaintext = padder.update(str(int(time.time())).encode("ascii")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return b64encode(encryptor.update(plaintext) + encryptor.finalize()).decode("ascii")


def relevant_corporate_actions(
    bars: Sequence[RawDailyBar],
    actions: Sequence[CorporateAction],
) -> tuple[CorporateAction, ...]:
    if len(bars) < 2:
        return ()
    ordered = sorted(bars, key=lambda item: item.trade_date)
    return tuple(
        action
        for action in actions
        if ordered[0].trade_date < action.event_date <= ordered[-1].trade_date
        and action.category in {1, 11, 12}
    )


def verify_corporate_action_references(
    bars: Sequence[RawDailyBar],
    actions: Sequence[CorporateAction],
    references: Mapping[date, OfficialAdjustmentReference],
) -> tuple[CorporateAction, ...]:
    ordered_bars = sorted(bars, key=lambda item: item.trade_date)
    verified = []
    for action in actions:
        reference = references.get(action.event_date)
        if reference is None:
            raise OfficialAdjustmentSourceError(
                f"Official adjustment reference missing for {action.instrument.canonical} "
                f"on {action.event_date}"
            )
        prior_closes = [
            bar.close for bar in ordered_bars if bar.trade_date < action.event_date
        ]
        if not prior_closes:
            raise OfficialAdjustmentMismatch(
                f"No previous close for official adjustment on {action.event_date}"
            )
        previous_close = prior_closes[-1]
        formula_ratio = corporate_action_ratio(previous_close, action)
        formula_reference = previous_close * formula_ratio
        official_reference = reference.reference_price
        if official_reference is None:
            official_cash = _official_cash_dividend(reference)
            tdx_cash = action.cash_dividend or Decimal("0")
            if not _is_cash_only(action) or abs(tdx_cash - official_cash) > PARAMETER_TOLERANCE:
                raise OfficialAdjustmentMismatch(
                    f"Official cash parameter {official_cash} does not match TDX action "
                    f"{tdx_cash} for {action.instrument.canonical} on {action.event_date}"
                )
            official_reference = previous_close - official_cash
            final_ratio = official_reference / previous_close
            status = "verified_official_parameters"
        elif formula_reference.quantize(PRICE_TICK, rounding=ROUND_HALF_UP) == official_reference:
            final_ratio = formula_ratio
            status = "verified_formula"
        elif _is_cash_only(action):
            final_ratio = official_reference / previous_close
            status = "official_reference_override"
        else:
            raise OfficialAdjustmentMismatch(
                f"Official reference {official_reference} does not match complex action "
                f"formula {formula_reference} for {action.instrument.canonical} "
                f"on {action.event_date}"
            )
        if final_ratio <= 0:
            raise OfficialAdjustmentMismatch("Official adjustment ratio must be positive")
        audit = {
            "status": status,
            "reference_price": str(official_reference),
            "previous_close": str(previous_close),
            "formula_reference": str(formula_reference),
            "adjustment_ratio": str(final_ratio),
            "source": reference.source,
            "source_url": reference.source_url,
            "response_hash": reference.response_hash,
            "retrieved_at": reference.retrieved_at.isoformat(),
            "evidence": dict(reference.evidence),
        }
        raw_payload = dict(action.raw_payload)
        raw_payload["official_adjustment"] = audit
        content_payload = {
            "tdx_content_hash": action.content_hash,
            "official_adjustment": {
                "status": audit["status"],
                "reference_price": audit["reference_price"],
                "previous_close": audit["previous_close"],
                "formula_reference": audit["formula_reference"],
                "adjustment_ratio": audit["adjustment_ratio"],
                "source": audit["source"],
                "evidence": audit["evidence"],
            },
        }
        content_hash = hashlib.sha256(
            json.dumps(
                content_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        verified.append(
            replace(action, raw_payload=raw_payload, content_hash=content_hash)
        )
    return tuple(verified)


def _official_cash_dividend(reference: OfficialAdjustmentReference) -> Decimal:
    value = reference.evidence.get("cash_dividend_per_share")
    cash = _optional_decimal(value)
    if cash <= 0:
        raise OfficialAdjustmentSourceError(
            f"Official cash dividend missing for {reference.instrument} on {reference.event_date}"
        )
    return cash


def _is_cash_only(action: CorporateAction) -> bool:
    zero = Decimal("0")
    return (
        action.category == 1
        and (action.cash_dividend or zero) > zero
        and (action.bonus_ratio or zero) == zero
        and (action.rights_ratio or zero) == zero
        and (action.consolidation_ratio or zero) == zero
    )
