"""Forward-only unified data-routing cutover gates and state machine."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any, Mapping, Protocol

from .errors import DataRoutingError


class CutoverBlockedError(DataRoutingError):
    code = "data_routing_cutover_blocked"


MANDATORY_CUTOVER_GATES = (
    "inventory_zero_bypass", "test_matrix", "capability_preflight", "healthy_instances",
    "effective_policies", "explicit_disables", "credential_key_readiness",
    "background_dry_run", "go_no_go_report",
)


@dataclass(frozen=True)
class CutoverGate:
    key: str
    status: str
    evidence: Mapping[str, Any]
    mandatory: bool = True


@dataclass(frozen=True)
class CutoverState:
    cutover_id: int
    target_version: str
    status: str
    gates: tuple[CutoverGate, ...]

    @property
    def ready(self) -> bool:
        return all(gate.status == "passed" for gate in self.gates if gate.mandatory)


class CutoverRepository(Protocol):
    def create_preflight(self, target_version: str, *, actor_user_id: int, reason: str) -> CutoverState: ...
    def save_gates(self, cutover_id: int, gates: tuple[CutoverGate, ...], *, actor_user_id: int) -> CutoverState: ...
    def transition(self, cutover_id: int, expected_status: str, new_status: str, *, actor_user_id: int, details: Mapping[str, Any]) -> CutoverState: ...


class CutoverGateEvaluator:
    def evaluate(self, evidence: Mapping[str, Any]) -> tuple[CutoverGate, ...]:
        gates = []
        for key in MANDATORY_CUTOVER_GATES:
            item = evidence.get(key)
            passed = item is True or (isinstance(item, Mapping) and item.get("passed") is True)
            details = dict(item) if isinstance(item, Mapping) else {"value": bool(item)}
            gates.append(CutoverGate(key, "passed" if passed else "failed", details))
        return tuple(gates)


class DataRoutingCutoverService:
    def __init__(self, repository: CutoverRepository, evaluator: CutoverGateEvaluator | None = None):
        self.repository = repository
        self.evaluator = evaluator or CutoverGateEvaluator()

    def begin_preflight(self, target_version: str, *, actor_user_id: int, reason: str) -> CutoverState:
        if not target_version or not str(reason or "").strip():
            raise CutoverBlockedError("Target version and reason are required")
        return self.repository.create_preflight(target_version, actor_user_id=actor_user_id, reason=reason)

    def evaluate(self, state: CutoverState, evidence: Mapping[str, Any], *, actor_user_id: int) -> CutoverState:
        if state.status != "preflight":
            raise CutoverBlockedError("Cutover gates can only be evaluated during preflight")
        return self.repository.save_gates(state.cutover_id, self.evaluator.evaluate(evidence), actor_user_id=actor_user_id)

    def enter_maintenance(self, state: CutoverState, *, actor_user_id: int) -> CutoverState:
        if state.status != "preflight" or not state.ready:
            raise CutoverBlockedError("All mandatory gates must pass before maintenance")
        return self.repository.transition(state.cutover_id, "preflight", "maintenance", actor_user_id=actor_user_id, details={"entrypoints_stopped": True})

    def activate(self, state: CutoverState, *, actor_user_id: int, active_attempts: int, active_streams: int, workers_stopped: bool) -> CutoverState:
        if state.status != "maintenance":
            raise CutoverBlockedError("Cutover activation requires maintenance state")
        if active_attempts or active_streams or not workers_stopped:
            raise CutoverBlockedError("Active attempts and streams must be drained and workers stopped")
        return self.repository.transition(
            state.cutover_id, "maintenance", "activated", actor_user_id=actor_user_id,
            details={"active_attempts": 0, "active_streams": 0, "workers_stopped": True, "forward_only": True},
        )

    def abort(self, state: CutoverState, *, actor_user_id: int, reason: str) -> CutoverState:
        if state.status == "activated":
            raise CutoverBlockedError("Activated cutover is forward-only and cannot be aborted")
        if state.status not in {"preflight", "maintenance"}:
            raise CutoverBlockedError("Cutover cannot be aborted from its current state")
        return self.repository.transition(state.cutover_id, state.status, "aborted", actor_user_id=actor_user_id, details={"reason": reason})


class PostgresCutoverRepository:
    def __init__(self, connection_factory=None):
        if connection_factory is None:
            from app.utils.db_postgres import get_pg_connection
            connection_factory = get_pg_connection
        self.connection_factory = connection_factory

    @staticmethod
    def _inventory_digest() -> str:
        path = Path(__file__).resolve().parents[4] / "docs" / "architecture" / "EXTERNAL_DATA_OUTBOUND_INVENTORY.json"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _state(row, gate_rows) -> CutoverState:
        return CutoverState(
            int(row["id"]), str(row["target_application_version"]), str(row["status"]),
            tuple(CutoverGate(str(item["gate_key"]), str(item["status"]), dict(item.get("evidence") or {}), bool(item["mandatory"])) for item in gate_rows),
        )

    def get(self, cutover_id: int) -> CutoverState:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_data_routing_cutovers WHERE id=%s", (cutover_id,)); row = cur.fetchone()
                if not row: raise CutoverBlockedError("Cutover record not found")
                cur.execute("SELECT * FROM qd_data_routing_cutover_gates WHERE cutover_id=%s ORDER BY gate_key", (cutover_id,))
                return self._state(row, cur.fetchall())
            finally: cur.close()

    def drain_status(self) -> Mapping[str, Any]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT COUNT(*) AS count FROM qd_routed_data_requests WHERE completed_at IS NULL")
                active_attempts = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM qd_task_runs WHERE status IN ('running','cancel_requested')")
                active_streams = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM qd_worker_heartbeats WHERE role='scheduler' AND status='running'")
                active_workers = int(cur.fetchone()["count"])
                return {"active_attempts": active_attempts, "active_streams": active_streams, "workers_stopped": active_workers == 0}
            finally:
                cur.close()

    def create_preflight(self, target_version: str, *, actor_user_id: int, reason: str) -> CutoverState:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_data_routing_cutovers(target_application_version,status,requested_by,reason,inventory_digest)
                       VALUES (%s,'preflight',%s,%s,%s) RETURNING *""",
                    (target_version, actor_user_id, reason, self._inventory_digest()),
                ); row = cur.fetchone()
                cur.executemany(
                    "INSERT INTO qd_data_routing_cutover_gates(cutover_id,gate_key,mandatory,status) VALUES (%s,%s,TRUE,'pending')",
                    [(row["id"], key) for key in MANDATORY_CUTOVER_GATES],
                )
                self._audit(cur, actor_user_id, "cutover_preflight", row["id"], reason, {"target_version": target_version})
                db.commit()
                return self.get(int(row["id"]))
            except Exception:
                db.rollback(); raise
            finally: cur.close()

    def save_gates(self, cutover_id: int, gates: tuple[CutoverGate, ...], *, actor_user_id: int) -> CutoverState:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT status FROM qd_data_routing_cutovers WHERE id=%s FOR UPDATE", (cutover_id,)); row = cur.fetchone()
                if not row or row["status"] != "preflight": raise CutoverBlockedError("Cutover is not in preflight")
                for gate in gates:
                    cur.execute(
                        "UPDATE qd_data_routing_cutover_gates SET status=%s,evidence=%s::jsonb,evaluated_at=NOW() WHERE cutover_id=%s AND gate_key=%s",
                        (gate.status, json.dumps(dict(gate.evidence)), cutover_id, gate.key),
                    )
                digest = hashlib.sha256(json.dumps([(g.key, g.status, dict(g.evidence)) for g in gates], sort_keys=True).encode()).hexdigest()
                cur.execute("UPDATE qd_data_routing_cutovers SET gate_result_digest=%s,updated_at=NOW() WHERE id=%s", (digest, cutover_id))
                self._audit(cur, actor_user_id, "cutover_gates_evaluated", cutover_id, "gate evaluation", {"ready": all(g.status == "passed" for g in gates)})
                db.commit()
                return self.get(cutover_id)
            except Exception:
                db.rollback(); raise
            finally: cur.close()

    def transition(self, cutover_id: int, expected_status: str, new_status: str, *, actor_user_id: int, details: Mapping[str, Any]) -> CutoverState:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                activated = new_status == "activated"
                cur.execute(
                    """UPDATE qd_data_routing_cutovers SET status=%s,activated_by=CASE WHEN %s THEN %s ELSE activated_by END,
                       activated_at=CASE WHEN %s THEN NOW() ELSE activated_at END,updated_at=NOW()
                       WHERE id=%s AND status=%s RETURNING id""",
                    (new_status, activated, actor_user_id, activated, cutover_id, expected_status),
                )
                if not cur.fetchone(): raise CutoverBlockedError("Concurrent or invalid cutover transition")
                self._audit(cur, actor_user_id, f"cutover_{new_status}", cutover_id, str(details.get("reason") or new_status), details)
                db.commit()
                return self.get(cutover_id)
            except Exception:
                db.rollback(); raise
            finally: cur.close()

    @staticmethod
    def _audit(cur, actor_user_id: int, action: str, target_id: int, reason: str, after: Mapping[str, Any]) -> None:
        cur.execute(
            """INSERT INTO qd_data_source_audit(actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
               VALUES (%s,%s,'cutover',%s,%s,'{}'::jsonb,%s::jsonb,%s,'succeeded')""",
            (actor_user_id, action, str(target_id), reason, json.dumps(dict(after)), str(uuid.uuid4())),
        )
