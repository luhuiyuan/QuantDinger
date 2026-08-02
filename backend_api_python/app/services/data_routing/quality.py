"""Trusted runtime quality gates for normalized routed data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .errors import DataRoutingError
from .models import CapabilityDefinition


class QualityProfileError(DataRoutingError):
    code = "quality_profile_invalid"


@dataclass(frozen=True, slots=True)
class QualityWarning:
    code: str
    message: str
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class QualityOutcome:
    passed: bool
    hard_failures: tuple[str, ...]
    warnings: tuple[QualityWarning, ...]
    applied_profiles: tuple[str, ...]


def _as_warning(value: Mapping[str, Any] | QualityWarning) -> QualityWarning:
    if isinstance(value, QualityWarning):
        return value
    return QualityWarning(
        code=str(value.get("code") or "provider_warning")[:120],
        message=str(value.get("message") or "Provider returned a warning")[:500],
        details=dict(value.get("details") or {}),
    )


def _is_stricter(name: str, selected: Any, baseline: Any) -> bool:
    if name.startswith("min_") and isinstance(selected, (int, float)) and isinstance(baseline, (int, float)):
        return selected >= baseline
    if (name.startswith("max_") or name == "max_delay") and isinstance(selected, (int, float)) and isinstance(baseline, (int, float)):
        return selected <= baseline
    if name == "required_fields":
        return set(selected or ()) >= set(baseline or ())
    if name == "warnings_as_errors":
        return set(selected or ()) >= set(baseline or ())
    return selected == baseline


def resolve_quality_thresholds(
    capability: CapabilityDefinition,
    *selected_profiles: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    thresholds: dict[str, Any] = {}
    applied: list[str] = []
    for selection in selected_profiles:
        for profile_name, override in dict(selection or {}).items():
            declared = capability.strict_profiles.get(profile_name)
            if declared is None:
                raise QualityProfileError(
                    "Routing policy selected an undeclared quality profile",
                    details={"capability_key": capability.key, "profile": profile_name},
                )
            if override is False or override is None:
                continue
            if override is True:
                selected = dict(declared)
            elif isinstance(override, Mapping):
                selected = dict(declared)
                for name, value in override.items():
                    if name not in declared or not _is_stricter(name, value, declared[name]):
                        raise QualityProfileError(
                            "Routing policy attempted to relax or extend a quality profile",
                            details={"profile": profile_name, "threshold": name},
                        )
                    selected[name] = value
            else:
                raise QualityProfileError("Quality profile selection must be true, false, or an object")
            for name, value in selected.items():
                current = thresholds.get(name)
                if current is None or _is_stricter(name, value, current):
                    thresholds[name] = value
            if profile_name not in applied:
                applied.append(profile_name)
    return thresholds, tuple(applied)


def _threshold_failures(
    data: Any,
    acquired_at: datetime,
    thresholds: Mapping[str, Any],
    now: datetime,
) -> list[str]:
    failures: list[str] = []
    if "required_fields" in thresholds:
        missing = sorted(set(thresholds["required_fields"]) - set(data if isinstance(data, Mapping) else ()))
        if missing:
            failures.append(f"required_fields_missing:{','.join(missing)}")
    if "min_items" in thresholds:
        try:
            if len(data) < int(thresholds["min_items"]):
                failures.append("min_items")
        except TypeError:
            failures.append("min_items")
    max_age = thresholds.get("max_age_seconds", thresholds.get("max_delay"))
    if max_age is not None:
        age = max(0.0, (now - acquired_at).total_seconds())
        if age > float(max_age):
            failures.append("max_age_seconds")
    return failures


def evaluate_quality(
    capability: CapabilityDefinition,
    data: Any,
    acquired_at: datetime,
    *,
    policy_profile: Mapping[str, Any],
    entry_profile: Mapping[str, Any],
    warnings: Sequence[Mapping[str, Any] | QualityWarning] = (),
    now: datetime | None = None,
) -> QualityOutcome:
    evaluated_at = now or datetime.now(timezone.utc)
    failures: list[str] = []
    if data is None:
        failures.append("universal:data_missing")
    if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
        failures.append("universal:acquired_at_timezone_missing")
    elif (acquired_at - evaluated_at).total_seconds() > 300:
        failures.append("universal:acquired_at_in_future")

    if not failures:
        for gate in capability.hard_quality_gates:
            try:
                failures.extend(f"capability:{item}" for item in gate(data))
            except Exception:
                failures.append("capability:gate_error")

    thresholds, applied = resolve_quality_thresholds(capability, policy_profile, entry_profile)
    failures.extend(f"profile:{item}" for item in _threshold_failures(data, acquired_at, thresholds, evaluated_at))
    normalized_warnings = tuple(_as_warning(item) for item in warnings)
    promoted = set(thresholds.get("warnings_as_errors") or ())
    failures.extend(f"warning_promoted:{item.code}" for item in normalized_warnings if item.code in promoted)
    return QualityOutcome(not failures, tuple(failures), normalized_warnings, applied)
