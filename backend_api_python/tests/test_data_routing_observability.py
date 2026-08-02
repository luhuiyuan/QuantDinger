from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from app.services.data_routing.observability import PostgresRouterObservabilitySink
from app.services.data_routing.router import ProviderAttemptRecord, RetrySummary


class Cursor:
    def __init__(self, owner): self.owner = owner; self.rowcount = 1
    def execute(self, query, params=()):
        if self.owner.fail:
            raise RuntimeError("database unavailable")
        self.owner.queries.append((" ".join(query.split()), params))
    def fetchall(self): return []
    def close(self): pass


class DB:
    def __init__(self, owner): self.owner = owner
    def cursor(self): return Cursor(self.owner)
    def commit(self): self.owner.commits += 1
    def __enter__(self): return self
    def __exit__(self, *args): return False


class Factory:
    def __init__(self): self.queries = []; self.commits = 0; self.fail = False
    def __call__(self): return DB(self)


def test_router_sink_persists_request_attempt_retry_quality_and_completion_contracts():
    factory = Factory()
    sink = PostgresRouterObservabilitySink(factory)
    now = datetime.now(timezone.utc)
    sink.request_started(
        routed_request_id="request-1", capability_key="quote", policy_revision_id=10,
        calling_feature="quotes.api", mode="interactive", subject={"symbol": "A"},
        constraints={"venue": "NYSE"}, deadline=now,
    )
    sink.attempt_recorded(ProviderAttemptRecord(
        "request-1", 1, 7, "provider", "failed", "provider_error", "timeout",
        retry_summary=RetrySummary(2, 1, ("timeout", "timeout"), True),
        quality_failures=("price_missing",),
    ))
    sink.request_completed(
        routed_request_id="request-1", outcome="failed", provider_instance_id=None,
        attempt_count=1, result=None, explanation=("attempt_1:provider_error",),
    )

    sql = " ".join(query for query, _ in factory.queries)
    assert "INSERT INTO qd_routed_data_requests" in sql
    assert "INSERT INTO qd_external_data_request_logs" in sql
    assert "retry_summary" in sql and "quality_outcome" in sql
    assert "UPDATE qd_routed_data_requests" in sql


def test_observability_database_failure_is_fail_open_and_cleanup_is_bounded():
    factory = Factory()
    sink = PostgresRouterObservabilitySink(factory)
    factory.fail = True
    sink.request_started(
        routed_request_id="request-2", capability_key="quote", policy_revision_id=10,
        calling_feature="quotes.api", mode="interactive", subject={}, constraints={},
        deadline=datetime.now(timezone.utc),
    )
    sink.attempt_recorded(ProviderAttemptRecord("request-2", 1, 1, "provider", "accepted"))
    sink.request_completed(
        routed_request_id="request-2", outcome="failed", provider_instance_id=None,
        attempt_count=1, result=None,
    )

    factory.fail = False
    assert sink.cleanup_expired(batch_size=17) == {"attempts": 0, "summaries": 0}
    assert all(17 in params for query, params in factory.queries if "WITH candidates" in query)
