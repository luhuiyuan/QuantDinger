"""Versioned industry mapping used by fundamental quality rules."""

from __future__ import annotations

from typing import Mapping

MAPPING_VERSION = "eastmoney-to-csrc-v1"

# Conservative first-party mapping. Unknown labels remain unmapped and never
# gain a financial-sector exemption by inference.
EASTMONEY_TO_CSRC: dict[str, tuple[str, str]] = {
    "银行": ("J66", "货币金融服务"),
    "保险": ("J68", "保险业"),
    "保险及其他": ("J68", "保险业"),
}


def map_eastmoney_industry(label: str | None) -> dict:
    normalized = str(label or "").strip()
    mapped = EASTMONEY_TO_CSRC.get(normalized)
    if not mapped:
        return {"status": "unmapped", "sourceLabel": normalized, "mappingVersion": MAPPING_VERSION}
    code, name = mapped
    return {"status": "mapped", "taxonomy": "csrc", "industryCode": code, "industryName": name, "sourceLabel": normalized, "mappingVersion": MAPPING_VERSION}


def is_interest_coverage_exempt(classification: Mapping | None) -> bool:
    if not classification or str(classification.get("taxonomy") or "").lower() != "csrc":
        return False
    code = str(classification.get("industry_code") or classification.get("industryCode") or "")
    return code.startswith(("J66", "J68"))
