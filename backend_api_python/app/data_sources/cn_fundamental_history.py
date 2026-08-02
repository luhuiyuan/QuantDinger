"""Eastmoney annual A-share fundamentals for durable history ingestion.

This adapter returns raw provider rows plus a deliberately small, versioned field
mapping. Unknown/missing fields stay missing; callers must not infer values.
"""
from __future__ import annotations
from datetime import date
from typing import Any
import requests

from app.data_sources.rate_limiter import get_eastmoney_limiter

EASTMONEY_MAIN_DATA_URL = "https://datacenter.eastmoney.com/securities/api/data/get"
EASTMONEY_STATEMENT_DATA_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
MAPPING_VERSION = "eastmoney-main-finance-v1"

_FIELDS = {
    "TOTALOPERATEREVE": "revenue", "PARENTNETPROFIT": "parent_net_income",
    "TOTAL_SHARE": "total_shares",
    "NETCASH_OPERATE_PK": "operating_cash_flow", "OPERATE_PROFIT_PK": "operating_profit",
    "TOTAL_OPERATE_INCOME": "revenue", "OPERATE_INCOME": "revenue",
    "OPERATE_COST": "operating_cost", "PARENT_HOLDER_EQUITY": "parent_equity_end",
    "NETCASH_OPERATE": "operating_cash_flow", "CONSTRUCT_LONG_ASSET": "capex_cash_paid",
    "TOTAL_PROFIT": "profit_before_tax", "FE_INTEREST_EXPENSE": "interest_expense",
    "EBIT": "ebit",
    "INTEREST_COVERAGE_RATIO": "provider_interest_coverage", "ROEJQ": "provider_roe",
    "XSMLL": "provider_gross_margin", "XSJLL": "provider_net_margin",
}

_STATEMENT_REPORTS = (
    "RPT_DMSK_FN_INCOME",
    "RPT_DMSK_FN_CASHFLOW",
    "RPT_DMSK_FN_BALANCE",
)
_STATEMENT_OPERATIONS = {
    "RPT_DMSK_FN_INCOME": "annual_income_statement",
    "RPT_DMSK_FN_CASHFLOW": "annual_cashflow_statement",
    "RPT_DMSK_FN_BALANCE": "annual_balance_statement",
}

def _exchange(code: str) -> str:
    return "SH" if str(code).zfill(6).startswith(("6", "9", "5")) else "SZ"
def _number(value: Any) -> float | None:
    try:
        out=float(value)
        return out if out == out and abs(out) != float("inf") else None
    except (TypeError, ValueError): return None

def fetch_eastmoney_annual_reports(code: str, session=requests) -> list[dict]:
    code=str(code).replace('.SH','').replace('.SZ','').zfill(6)
    rows_by_period={}
    limiter = get_eastmoney_limiter()
    main_params={"type":"RPT_F10_FINANCE_MAINFINADATA","sty":"ALL","filter":f'(SECUCODE="{code}.{_exchange(code)}")(REPORT_TYPE="年报")',"p":"1","ps":"100","sr":"-1","st":"REPORT_DATE","source":"HSF10","client":"PC"}
    limiter.wait()
    response=session.get(EASTMONEY_MAIN_DATA_URL, params=main_params, timeout=20, headers={"User-Agent":"Mozilla/5.0"})
    response.raise_for_status()
    main_rows = (response.json().get("result") or {}).get("data") or []
    for item in main_rows:
        report=str(item.get("REPORT_DATE") or "")[:10]
        if len(report) == 10:
            rows_by_period[report] = dict(item)
    for report_name in _STATEMENT_REPORTS:
        params={"reportName":report_name,"columns":"ALL","filter":f'(SECUCODE="{code}.{_exchange(code)}")',"pageNumber":"1","pageSize":"100","sortTypes":"-1","sortColumns":"REPORT_DATE","source":"HSF10","client":"PC"}
        limiter.wait()
        response=session.get(EASTMONEY_STATEMENT_DATA_URL, params=params, timeout=20, headers={"User-Agent":"Mozilla/5.0"})
        response.raise_for_status()
        statement_rows = (response.json().get("result") or {}).get("data") or []
        for item in statement_rows:
            report=str(item.get("REPORT_DATE") or "")[:10]
            if report in rows_by_period:
                rows_by_period[report].update(item)
    output=[]
    ordered = sorted(rows_by_period.items(), reverse=True)
    for index, (report, raw) in enumerate(ordered):
        notice=str(raw.get("NOTICE_DATE") or "")[:10]
        if len(report)!=10 or len(notice)!=10: continue
        fields={}
        for source, target in _FIELDS.items():
            value = _number(raw.get(source))
            if value is not None:
                fields[target] = value
        if fields.get("gross_profit") is None and fields.get("revenue") is not None and fields.get("operating_cost") is not None:
            fields["gross_profit"] = fields["revenue"] - fields["operating_cost"]
        if fields.get("ebit") is None and fields.get("profit_before_tax") is not None and fields.get("interest_expense") is not None:
            fields["ebit"] = fields["profit_before_tax"] + fields["interest_expense"]
        # Beginning equity is the preceding year-end observation. Since rows
        # are newest first, the next item is the prior annual report.
        if index + 1 < len(ordered):
            fields["parent_equity_begin"] = _number(ordered[index + 1][1].get("PARENT_HOLDER_EQUITY") or ordered[index + 1][1].get("TOTAL_EQUITY_PK"))
        output.append({"period_end":date.fromisoformat(report),"available_at":date.fromisoformat(notice),"raw":raw,"fields":fields,
                       "source_version":str(raw.get("UPDATE_DATE") or ""),"announcement_ref":{"noticeDate":notice,"securityCode":raw.get("SECUCODE")},
                       "request_context":{"endpoint":EASTMONEY_MAIN_DATA_URL,"reportType":"annual","securityCode":raw.get("SECUCODE")},"mapping_version":MAPPING_VERSION})
    return output
