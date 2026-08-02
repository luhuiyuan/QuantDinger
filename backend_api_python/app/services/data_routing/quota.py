"""Conservative quota planning, reservation, and degraded safety slices."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

from .errors import DataRoutingError
from .models import AdapterDefinition


class QuotaError(DataRoutingError):
    code = "provider_quota_error"


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    reservation_ids: tuple[str, ...]
    costs: Mapping[str, float]
    reserved_calls: int


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    reservation: QuotaReservation | None = None
    reason: str = ""
    reset_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChunkQuotaPlan:
    allowed: bool
    costs: Mapping[str, float]
    reservation: QuotaReservation | None
    action: str
    reset_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SafetySlice:
    slice_id: str
    instance_id: int
    capability_key: str
    bucket_key: str
    remaining: float
    expires_at: datetime


class QuotaRepository(Protocol):
    def reserve(
        self, *, instance_id: int, capability_key: str, costs: Mapping[str, float],
        bucket_contracts: Mapping[str, Mapping[str, Any]], holder_id: str,
        purpose: str, expires_at: datetime, reserved_calls: int,
    ) -> QuotaDecision: ...

    def complete(self, reservation: QuotaReservation, *, consumed_fraction: float) -> None: ...

    def observe(
        self, *, instance_id: int, capability_key: str, bucket_key: str,
        observed_limit: float | None, consumed: float | None, reset_at: datetime | None,
        source: str, metadata: Mapping[str, Any],
    ) -> None: ...

    def configure_limit(
        self, *, instance_id: int, capability_key: str | None, bucket_key: str,
        unit: str, configured_limit: float, metadata: Mapping[str, Any],
    ) -> None: ...


class SafetySlicePool:
    def __init__(self):
        self._lock = Lock()
        self._slices: dict[tuple[int, str, str], SafetySlice] = {}

    def install(self, slices: tuple[SafetySlice, ...]) -> None:
        with self._lock:
            for item in slices:
                self._slices[(item.instance_id, item.capability_key, item.bucket_key)] = item

    def consume(
        self, instance_id: int, capability_key: str, costs: Mapping[str, float], now: datetime
    ) -> bool:
        with self._lock:
            selected = []
            for bucket, amount in costs.items():
                item = self._slices.get((instance_id, capability_key, bucket))
                if item is None or item.expires_at <= now or item.remaining < amount:
                    return False
                selected.append((bucket, item, amount))
            for bucket, item, amount in selected:
                self._slices[(instance_id, capability_key, bucket)] = SafetySlice(
                    item.slice_id, item.instance_id, item.capability_key, bucket,
                    item.remaining - amount, item.expires_at,
                )
            return True


def quota_bucket_contracts(adapter: AdapterDefinition) -> dict[str, Mapping[str, Any]]:
    raw = adapter.quota_contract.get("buckets")
    if isinstance(raw, Mapping):
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}
    if isinstance(raw, (list, tuple)):
        return {str(key): {} for key in raw}
    return {}


class QuotaManager:
    def __init__(
        self,
        repository: QuotaRepository,
        *,
        safety_slices: SafetySlicePool | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        interactive_wait_seconds: float = 1.0,
    ):
        self.repository = repository
        self.safety_slices = safety_slices or SafetySlicePool()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.interactive_wait_seconds = max(0.0, min(float(interactive_wait_seconds), 5.0))

    @staticmethod
    def estimate(adapter: AdapterDefinition, capability_key: str, operation: str, units: int) -> dict[str, float]:
        declared = quota_bucket_contracts(adapter)
        costs = {
            str(key): float(value)
            for key, value in adapter.runtime.estimate_quota_cost(capability_key, operation, max(1, int(units))).items()
        }
        if not costs or any(key not in declared or value <= 0 for key, value in costs.items()):
            raise QuotaError("Adapter returned an invalid or undeclared quota cost")
        for key, contract in declared.items():
            if contract.get("configured_limit") is None and contract.get("unknown_safety_limit") is None:
                raise QuotaError(
                    "Quota bucket has neither a configured limit nor an unknown-limit safety budget",
                    details={"bucket_key": key},
                )
        return costs

    def acquire(
        self,
        adapter: AdapterDefinition,
        *,
        instance_id: int,
        capability_key: str,
        holder_id: str,
        mode: str,
        deadline: datetime,
        units: int,
        purpose: str = "routed_request",
    ) -> QuotaDecision:
        try:
            costs = self.estimate(adapter, capability_key, "fetch", units)
            contracts = quota_bucket_contracts(adapter)
        except QuotaError:
            return QuotaDecision(False, reason="quota_model_invalid")

        def reserve() -> QuotaDecision:
            return self.repository.reserve(
                instance_id=instance_id, capability_key=capability_key, costs=costs,
                bucket_contracts=contracts, holder_id=holder_id, purpose=purpose,
                expires_at=min(deadline, self.clock() + timedelta(minutes=5)),
                reserved_calls=units,
            )

        try:
            decision = reserve()
            if (
                not decision.allowed and mode == "interactive" and decision.reset_at is not None
                and decision.reset_at > self.clock()
            ):
                wait = (decision.reset_at - self.clock()).total_seconds()
                if wait <= self.interactive_wait_seconds and self.clock() + timedelta(seconds=wait) < deadline:
                    self.sleep(wait)
                    decision = reserve()
            return decision
        except Exception:
            if self.safety_slices.consume(instance_id, capability_key, costs, self.clock()):
                return QuotaDecision(True, QuotaReservation((), costs, units), "degraded_safety_slice")
            return QuotaDecision(False, reason="quota_control_plane_unavailable")

    def complete(self, reservation: QuotaReservation | None, *, actual_calls: int) -> None:
        if reservation is None or not reservation.reservation_ids:
            return
        fraction = min(1.0, max(0.0, actual_calls / max(1, reservation.reserved_calls)))
        try:
            self.repository.complete(reservation, consumed_fraction=fraction)
        except Exception:
            pass

    def observe(
        self,
        adapter: AdapterDefinition,
        *,
        instance_id: int,
        capability_key: str,
        observations: tuple[Mapping[str, Any], ...],
    ) -> None:
        contracts = quota_bucket_contracts(adapter)
        for raw in observations[:16]:
            bucket = str(raw.get("bucket_key") or "")
            if bucket not in contracts:
                continue
            contract = contracts[bucket]
            scope_capability = capability_key if contract.get("scope", "capability") == "capability" else None
            try:
                self.repository.observe(
                    instance_id=instance_id,
                    capability_key=scope_capability,
                    bucket_key=bucket,
                    observed_limit=float(raw["observed_limit"]) if raw.get("observed_limit") is not None else None,
                    consumed=float(raw["consumed"]) if raw.get("consumed") is not None else None,
                    reset_at=raw.get("reset_at") if isinstance(raw.get("reset_at"), datetime) else None,
                    source=str(raw.get("source") or "provider_response")[:40],
                    metadata=dict(raw.get("metadata") or {}),
                )
            except Exception:
                pass

    def plan_chunk(
        self,
        adapter: AdapterDefinition,
        *,
        instance_id: int,
        capability_key: str,
        holder_id: str,
        units: int,
        deadline: datetime,
    ) -> ChunkQuotaPlan:
        decision = self.acquire(
            adapter, instance_id=instance_id, capability_key=capability_key,
            holder_id=holder_id, mode="background", deadline=deadline,
            units=units, purpose="background_chunk",
        )
        action = "start" if decision.allowed else ("wait" if decision.reset_at and decision.reset_at < deadline else "defer")
        try:
            costs = decision.reservation.costs if decision.reservation else self.estimate(adapter, capability_key, "fetch", units)
        except QuotaError:
            costs = {}
        return ChunkQuotaPlan(decision.allowed, costs, decision.reservation, action, decision.reset_at)

    def configure_plan_limit(
        self,
        adapter: AdapterDefinition,
        *,
        instance_id: int,
        capability_key: str,
        bucket_key: str,
        configured_limit: float,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        contracts = quota_bucket_contracts(adapter)
        contract = contracts.get(bucket_key)
        if contract is None or float(configured_limit) < 0:
            raise QuotaError("Configured quota limit is invalid or uses an undeclared bucket")
        scope = capability_key if contract.get("scope", "capability") == "capability" else None
        self.repository.configure_limit(
            instance_id=instance_id,
            capability_key=scope,
            bucket_key=bucket_key,
            unit=str(contract.get("unit") or "request"),
            configured_limit=float(configured_limit),
            metadata=dict(metadata or {}),
        )

    def preallocate_safety_slice(
        self,
        adapter: AdapterDefinition,
        *,
        instance_id: int,
        capability_key: str,
        bucket_key: str,
        amount: float,
        holder_id: str,
        expires_at: datetime,
    ) -> SafetySlice:
        contracts = quota_bucket_contracts(adapter)
        contract = contracts.get(bucket_key)
        if contract is None or amount <= 0 or expires_at <= self.clock():
            raise QuotaError("Safety slice request is invalid")
        decision = self.repository.reserve(
            instance_id=instance_id,
            capability_key=capability_key,
            costs={bucket_key: float(amount)},
            bucket_contracts=contracts,
            holder_id=holder_id,
            purpose="degraded_safety_slice",
            expires_at=expires_at,
            reserved_calls=1,
        )
        if not decision.allowed or not decision.reservation or not decision.reservation.reservation_ids:
            raise QuotaError("Safety slice capacity is unavailable")
        item = SafetySlice(
            decision.reservation.reservation_ids[0], instance_id, capability_key,
            bucket_key, float(amount), expires_at,
        )
        self.safety_slices.install((item,))
        return item


class PostgresQuotaRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    def reserve(self, **values) -> QuotaDecision:
        now = datetime.now(timezone.utc)
        created: list[str] = []
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                states = []
                for bucket_key in sorted(values["costs"]):
                    contract = values["bucket_contracts"].get(bucket_key, {})
                    scope_capability = values["capability_key"] if contract.get("scope", "capability") == "capability" else None
                    cur.execute(
                        """SELECT * FROM qd_provider_quota_states
                           WHERE instance_id=%s AND capability_key IS NOT DISTINCT FROM %s AND bucket_key=%s
                           FOR UPDATE""",
                        (values["instance_id"], scope_capability, bucket_key),
                    )
                    row = cur.fetchone()
                    if row is None:
                        configured = contract.get("configured_limit")
                        fallback = contract.get("unknown_safety_limit")
                        cur.execute(
                            """INSERT INTO qd_provider_quota_states
                               (instance_id,capability_key,bucket_key,unit,configured_limit,observation_source,metadata)
                               VALUES (%s,%s,%s,%s,%s,'adapter_contract',%s::jsonb) RETURNING *""",
                            (
                                values["instance_id"], scope_capability, bucket_key,
                                str(contract.get("unit") or "request"),
                                configured if configured is not None else fallback,
                                json.dumps({"unknown_safety_limit": fallback}, sort_keys=True),
                            ),
                        )
                        row = cur.fetchone()
                    row = dict(row)
                    cur.execute(
                        """SELECT reservation_id,amount FROM qd_provider_quota_reservations
                           WHERE quota_state_id=%s AND status='reserved' AND expires_at<=%s FOR UPDATE""",
                        (row["id"], now),
                    )
                    expired = list(cur.fetchall() or [])
                    if expired:
                        amount = sum(Decimal(str(item["amount"])) for item in expired)
                        cur.execute(
                            """UPDATE qd_provider_quota_reservations SET status='expired',completed_at=%s
                               WHERE reservation_id=ANY(%s)""",
                            (now, [item["reservation_id"] for item in expired]),
                        )
                        cur.execute(
                            """UPDATE qd_provider_quota_states SET reserved=GREATEST(0,reserved-%s),
                               state_version=state_version+1,updated_at=%s WHERE id=%s""",
                            (amount, now, row["id"]),
                        )
                        row["reserved"] = max(Decimal("0"), Decimal(str(row["reserved"])) - amount)
                    if row.get("reset_at") and row["reset_at"] <= now:
                        cur.execute(
                            """UPDATE qd_provider_quota_states SET consumed=0,reserved=0,
                               state_version=state_version+1,updated_at=%s WHERE id=%s""",
                            (now, row["id"]),
                        )
                        row["consumed"] = row["reserved"] = Decimal("0")
                    limits = [Decimal(str(item)) for item in (row.get("configured_limit"), row.get("observed_limit")) if item is not None]
                    limit = min(limits) if limits else None
                    needed = Decimal(str(values["costs"][bucket_key]))
                    if limit is None or Decimal(str(row["consumed"])) + Decimal(str(row["reserved"])) + needed > limit:
                        db.rollback()
                        return QuotaDecision(False, reason="quota_exhausted", reset_at=row.get("reset_at"))
                    states.append((row, needed))
                for row, amount in states:
                    reservation_id = uuid.uuid4().hex
                    cur.execute(
                        """INSERT INTO qd_provider_quota_reservations
                           (reservation_id,quota_state_id,holder_id,amount,purpose,expires_at)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (reservation_id, row["id"], values["holder_id"], amount, values["purpose"], values["expires_at"]),
                    )
                    cur.execute(
                        """UPDATE qd_provider_quota_states SET reserved=reserved+%s,
                           state_version=state_version+1,updated_at=%s WHERE id=%s""",
                        (amount, now, row["id"]),
                    )
                    created.append(reservation_id)
                db.commit()
            finally:
                cur.close()
        calls = max(1, int(values.get("reserved_calls") or 1))
        return QuotaDecision(True, QuotaReservation(tuple(created), dict(values["costs"]), calls))

    def complete(self, reservation: QuotaReservation, *, consumed_fraction: float) -> None:
        fraction = Decimal(str(min(1.0, max(0.0, consumed_fraction))))
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT reservation_id,quota_state_id,amount FROM qd_provider_quota_reservations
                       WHERE reservation_id=ANY(%s) AND status='reserved' FOR UPDATE""",
                    (list(reservation.reservation_ids),),
                )
                for row in cur.fetchall() or []:
                    amount = Decimal(str(row["amount"]))
                    consumed = amount * fraction
                    status = "consumed" if consumed > 0 else "released"
                    cur.execute(
                        """UPDATE qd_provider_quota_reservations SET status=%s,completed_at=NOW()
                           WHERE reservation_id=%s""",
                        (status, row["reservation_id"]),
                    )
                    cur.execute(
                        """UPDATE qd_provider_quota_states SET reserved=GREATEST(0,reserved-%s),
                           consumed=consumed+%s,state_version=state_version+1,updated_at=NOW() WHERE id=%s""",
                        (amount, consumed, row["quota_state_id"]),
                    )
                db.commit()
            finally:
                cur.close()

    def observe(self, **values) -> None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_quota_states SET
                       observed_limit=CASE WHEN %s IS NULL THEN observed_limit
                         WHEN observed_limit IS NULL THEN %s ELSE LEAST(observed_limit,%s) END,
                       consumed=CASE WHEN %s IS NULL THEN consumed ELSE GREATEST(consumed,%s) END,
                       reset_at=CASE WHEN %s IS NULL THEN reset_at ELSE %s END,
                       observation_source=%s,metadata=%s::jsonb,state_version=state_version+1,updated_at=NOW()
                       WHERE instance_id=%s AND capability_key IS NOT DISTINCT FROM %s AND bucket_key=%s""",
                    (
                        values["observed_limit"], values["observed_limit"], values["observed_limit"],
                        values["consumed"], values["consumed"], values["reset_at"], values["reset_at"],
                        values["source"], json.dumps(dict(values["metadata"]), sort_keys=True),
                        values["instance_id"], values["capability_key"], values["bucket_key"],
                    ),
                )
                db.commit()
            finally:
                cur.close()

    def configure_limit(self, **values) -> None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_provider_quota_states
                       (instance_id,capability_key,bucket_key,unit,configured_limit,observation_source,metadata)
                       VALUES (%s,%s,%s,%s,%s,'administrator',%s::jsonb)
                       ON CONFLICT (instance_id,(COALESCE(capability_key,'')),bucket_key) DO UPDATE SET
                         unit=EXCLUDED.unit,configured_limit=EXCLUDED.configured_limit,
                         observation_source='administrator',metadata=EXCLUDED.metadata,
                         state_version=qd_provider_quota_states.state_version+1,updated_at=NOW()""",
                    (
                        values["instance_id"], values["capability_key"], values["bucket_key"],
                        values["unit"], values["configured_limit"],
                        json.dumps(dict(values["metadata"]), sort_keys=True),
                    ),
                )
                db.commit()
            finally:
                cur.close()
