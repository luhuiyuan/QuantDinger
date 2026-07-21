"""Strict canonical identifiers for supported Shanghai and Shenzhen A-shares."""

from __future__ import annotations

import re

from .models import CNInstrument


class CNInstrumentError(ValueError):
    code = "cn_history.instrument_invalid"


_CANONICAL_RE = re.compile(
    r"^(?:(?:CNSTOCK):)?(?P<code>\d{6})(?:\.(?P<suffix>SH|SZ))?$",
    re.IGNORECASE,
)
_PREFIX_RE = re.compile(r"^(?P<exchange>SH|SZ)(?P<code>\d{6})$", re.IGNORECASE)

_SH_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688")
_SZ_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")


def _exchange_for_code(code: str) -> str:
    if code.startswith(_SH_A_SHARE_PREFIXES):
        return "SH"
    if code.startswith(_SZ_A_SHARE_PREFIXES):
        return "SZ"
    raise CNInstrumentError(f"Unsupported China A-share code: {code}")


def parse_cn_instrument(value: object) -> CNInstrument:
    raw = str(value or "").strip().upper().replace(" ", "")
    if not raw:
        raise CNInstrumentError("China A-share instrument is required")

    prefix_match = _PREFIX_RE.fullmatch(raw)
    if prefix_match:
        code = prefix_match.group("code")
        explicit_exchange = prefix_match.group("exchange")
    else:
        match = _CANONICAL_RE.fullmatch(raw)
        if not match:
            raise CNInstrumentError(f"Invalid China A-share instrument: {raw}")
        code = match.group("code")
        explicit_exchange = match.group("suffix")

    inferred_exchange = _exchange_for_code(code)
    if explicit_exchange and explicit_exchange != inferred_exchange:
        raise CNInstrumentError(
            f"Instrument {code} belongs to {inferred_exchange}, not {explicit_exchange}"
        )
    return CNInstrument(
        code=code,
        exchange=inferred_exchange,
        canonical=f"CNStock:{code}.{inferred_exchange}",
    )
