"""CNInfo announcement references and conservative structured verification.

CNInfo disclosures are not uniform enough to infer missing line items.  This
adapter therefore returns an explicit ``insufficient`` result unless a caller
supplies a verified structured payload (or a fixture with one in tests).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import requests


CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
MAPPING_VERSION = "cninfo-structured-verification-v1"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def map_cninfo_verification(payload: Mapping[str, Any]) -> dict:
    raw_fields = payload.get("structuredFields")
    if not isinstance(raw_fields, Mapping):
        return {
            "status": "insufficient",
            "fields": {},
            "mapping_version": MAPPING_VERSION,
            "reason": "structured_fields_unavailable",
        }
    fields = {str(code): value for code, raw in raw_fields.items() if (value := _number(raw)) is not None}
    return {
        "status": "available" if fields else "insufficient",
        "fields": fields,
        "mapping_version": MAPPING_VERSION,
        "reason": "" if fields else "structured_fields_unavailable",
    }


def fetch_cninfo_annual_announcements(
    code: str,
    *,
    start_date: date,
    end_date: date,
    session=requests,
) -> list[dict]:
    code = str(code).split(".")[0].zfill(6)
    response = session.post(
        CNINFO_QUERY_URL,
        data={
            "stock": code,
            "category": "category_ndbg_szsh;",
            "pageNum": 1,
            "pageSize": 30,
            "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
            "column": "sse" if code.startswith("6") else "szse",
        },
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"},
    )
    response.raise_for_status()
    announcements = response.json().get("announcements") or []
    output = []
    for item in announcements:
        adjunct = str(item.get("adjunctUrl") or "")
        verification = map_cninfo_verification(item)
        output.append({
            **verification,
            "announcement_ref": {
                "announcementId": item.get("announcementId"),
                "title": item.get("announcementTitle"),
                "url": f"https://static.cninfo.com.cn/{adjunct}" if adjunct else "",
                "announcementTime": item.get("announcementTime"),
            },
            "raw": item,
        })
    return output
