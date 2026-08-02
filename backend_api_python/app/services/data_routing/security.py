"""Fine-grained data-source permissions and recent Step-up proofs."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .errors import DataRoutingError


DATA_SOURCE_PERMISSIONS = frozenset({
    "data_sources:view",
    "data_sources:instances",
    "data_sources:credentials",
    "data_sources:routing",
    "data_sources:diagnostics",
    "data_sources:cutover",
})


class StepUpRequiredError(DataRoutingError):
    code = "data_source_step_up_required"


@dataclass(frozen=True, slots=True)
class StepUpProof:
    token: str
    expires_at: datetime
    verification_method: str


class StepUpService:
    def __init__(
        self,
        connection_factory: Callable[[], Any] | None = None,
        *,
        ttl_seconds: int = 600,
        clock: Callable[[], datetime] | None = None,
    ):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory
        self.ttl_seconds = max(60, min(int(ttl_seconds), 900))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def issue(self, user_id: int, *, password: str = "", mfa_code: str = "") -> StepUpProof:
        method = ""
        if password:
            from app.services.user_service import get_user_service

            with self.connection_factory() as db:
                cur = db.cursor()
                try:
                    cur.execute("SELECT password_hash FROM qd_users WHERE id=%s AND status='active'", (int(user_id),))
                    row = cur.fetchone()
                finally:
                    cur.close()
            if not row or not get_user_service().verify_password(password, str(row.get("password_hash") or "")):
                raise StepUpRequiredError("Password verification failed")
            method = "password"
        elif mfa_code:
            from app.services.mfa_service import get_mfa_service

            ok, _ = get_mfa_service().verify_user_code(int(user_id), mfa_code)
            if not ok:
                raise StepUpRequiredError("MFA verification failed")
            method = "mfa"
        else:
            raise StepUpRequiredError("Password or MFA verification is required")
        token = secrets.token_urlsafe(32)
        expires_at = self.clock() + timedelta(seconds=self.ttl_seconds)
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("DELETE FROM qd_data_source_step_up_proofs WHERE expires_at<=%s", (self.clock(),))
                cur.execute(
                    """INSERT INTO qd_data_source_step_up_proofs
                       (proof_hash,user_id,verification_method,expires_at) VALUES (%s,%s,%s,%s)""",
                    (self._hash(token), int(user_id), method, expires_at),
                )
                db.commit()
            finally:
                cur.close()
        return StepUpProof(token, expires_at, method)

    def require(self, user_id: int, token: str) -> Mapping[str, Any]:
        if not str(token or "").strip():
            raise StepUpRequiredError("Recent Step-up proof is required")
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT verification_method,expires_at FROM qd_data_source_step_up_proofs
                       WHERE proof_hash=%s AND user_id=%s AND expires_at>%s""",
                    (self._hash(token), int(user_id), self.clock()),
                )
                row = cur.fetchone()
            finally:
                cur.close()
        if not row:
            raise StepUpRequiredError("Step-up proof is invalid or expired")
        return {"verification_method": row["verification_method"], "expires_at": row["expires_at"]}
