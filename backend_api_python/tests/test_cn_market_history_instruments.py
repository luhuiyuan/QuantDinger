from __future__ import annotations

import pytest

from app.services.cn_market_history.instruments import CNInstrumentError, parse_cn_instrument


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "CNStock:600519.SH"),
        ("sh600519", "CNStock:600519.SH"),
        ("600519.SH", "CNStock:600519.SH"),
        ("CNStock:600519.SH", "CNStock:600519.SH"),
        ("000001", "CNStock:000001.SZ"),
        ("sz300750", "CNStock:300750.SZ"),
        ("301001.SZ", "CNStock:301001.SZ"),
        ("688001.SH", "CNStock:688001.SH"),
    ],
)
def test_parse_cn_instrument_normalizes_supported_a_shares(raw: str, expected: str) -> None:
    instrument = parse_cn_instrument(raw)

    assert instrument.canonical == expected
    assert instrument.tdx_market == (1 if expected.endswith(".SH") else 0)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ABC",
        "510300.SH",  # ETF
        "159915.SZ",  # ETF
        "110000.SH",  # bond
        "900901.SH",  # Shanghai B-share
        "200002.SZ",  # Shenzhen B-share
        "600519.SZ",  # explicit exchange mismatch
        "000001.SH",  # explicit exchange mismatch
        "CNStock:12345.SH",
    ],
)
def test_parse_cn_instrument_rejects_unsupported_or_ambiguous_inputs(raw: str) -> None:
    with pytest.raises(CNInstrumentError) as exc_info:
        parse_cn_instrument(raw)

    assert exc_info.value.code == "cn_history.instrument_invalid"
