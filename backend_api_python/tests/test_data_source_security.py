from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask, g

from app.services.data_routing.management_repository import (
    DataSourceManagementError,
    PostgresDataSourceManagementRepository,
)
from app.services.data_routing.health import HealthState, PostgresHealthRepository, RecoveryProbe
from app.services.data_routing.security import StepUpRequiredError, StepUpService
from app.utils.auth import permission_required


class Clock:
    def __init__(self): self.value = datetime(2026, 8, 1, tzinfo=timezone.utc)
    def __call__(self): return self.value


class StepCursor:
    def __init__(self, owner): self.owner = owner
    def execute(self, query, params=()):
        if "SELECT password_hash" in query:
            self.row = {"password_hash": "hash"}
        elif "INSERT INTO qd_data_source_step_up_proofs" in query:
            self.owner.proofs[params[0]] = {"user_id": params[1], "verification_method": params[2], "expires_at": params[3]}
            self.row = None
        elif "SELECT verification_method,expires_at" in query:
            proof = self.owner.proofs.get(params[0])
            self.row = proof if proof and proof["user_id"] == params[1] and proof["expires_at"] > params[2] else None
        else:
            self.row = None
    def fetchone(self): return self.row
    def close(self): pass


class StepDB:
    def __init__(self, owner): self.owner = owner
    def cursor(self): return StepCursor(self.owner)
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


class StepFactory:
    def __init__(self): self.proofs = {}
    def __call__(self): return StepDB(self)


def test_step_up_proof_is_hashed_bound_to_user_and_expires(monkeypatch):
    class Users:
        @staticmethod
        def verify_password(password, password_hash): return password == "correct" and password_hash == "hash"

    monkeypatch.setattr("app.services.user_service.get_user_service", lambda: Users())
    clock = Clock()
    factory = StepFactory()
    service = StepUpService(factory, ttl_seconds=120, clock=clock)

    proof = service.issue(7, password="correct")

    assert proof.token not in factory.proofs
    assert service.require(7, proof.token)["verification_method"] == "password"
    with pytest.raises(StepUpRequiredError):
        service.require(8, proof.token)
    clock.value += timedelta(seconds=121)
    with pytest.raises(StepUpRequiredError):
        service.require(7, proof.token)


def test_permission_decorator_rejects_role_without_data_source_permission(monkeypatch):
    class Users:
        @staticmethod
        def get_user_permissions(role):
            return ["data_sources:view"] if role == "admin" else []

    monkeypatch.setattr("app.services.user_service.get_user_service", lambda: Users())
    app = Flask(__name__)

    @permission_required("data_sources:view")
    def protected(): return "ok"

    with app.test_request_context("/"):
        g.user_role = "user"
        response, status = protected()
        assert status == 403 and response.get_json()["data"] is None
        g.user_role = "admin"
        assert protected() == "ok"


def test_reason_is_required_before_management_database_change():
    called = {"value": False}

    def factory():
        called["value"] = True
        raise AssertionError("database should not be opened")

    repository = PostgresDataSourceManagementRepository(factory)
    with pytest.raises(DataSourceManagementError, match="reason"):
        repository.quarantine(
            state_id=1, expected_version=1, until=None, actor_user_id=7, reason="",
        )
    assert called["value"] is False


def test_management_queries_do_not_select_credential_ciphertext():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "data_routing" / "management_repository.py"
    ).read_text()
    assert "SELECT ciphertext" not in source
    assert "secret_comparison_tag" not in source


def test_audit_insert_failure_rolls_back_management_change():
    class Cursor:
        rowcount = 1
        def execute(self, query, params=()):
            if query.strip().startswith("SELECT"):
                self.row = {"id": 1, "state_version": 1, "circuit_state": "closed"}
            elif "RETURNING" in query:
                self.row = {"id": 1, "state_version": 2, "circuit_state": "open"}
            elif "INSERT INTO qd_data_source_audit" in query:
                raise RuntimeError("audit unavailable")
        def fetchone(self): return self.row
        def close(self): pass

    class DB:
        def __init__(self): self.commits = 0; self.rollbacks = 0
        def cursor(self): return Cursor()
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1
        def __enter__(self): return self
        def __exit__(self, *args): return False

    db = DB()
    repository = PostgresDataSourceManagementRepository(lambda: db)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        repository.quarantine(
            state_id=1, expected_version=1, until=None,
            actor_user_id=7, reason="incident containment",
        )
    assert db.rollbacks == 1 and db.commits == 0


def test_recovery_probe_audit_failure_rolls_back_state_and_probe():
    class Cursor:
        rowcount = 1
        def execute(self, query, params=()):
            if "INSERT INTO qd_data_source_audit" in query:
                raise RuntimeError("audit unavailable")
        def close(self): pass

    class DB:
        def __init__(self): self.commits = 0; self.rollbacks = 0
        def cursor(self): return Cursor()
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1
        def __enter__(self): return self
        def __exit__(self, *args): return False

    db = DB()
    repository = PostgresHealthRepository(lambda: db)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    before = HealthState(1, 10, "quote", circuit_state="open", state_version=2)
    after = HealthState(1, 10, "quote", circuit_state="probe_pending", state_version=3)
    probe = RecoveryProbe("probe-1", 1, "administrator", "queued", now, 1)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        repository.compare_and_set_and_create_probe(
            before, after, probe, requested_by=7, reason="provider remediation complete"
        )

    assert db.rollbacks == 1 and db.commits == 0
