"""Bounded Provider credential master-key reencryption."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from app.utils.credential_crypto import ProviderCredentialKeyring

from .errors import DataRoutingError


class ProviderCredentialKeyRotationError(DataRoutingError):
    code = "provider_credential_key_rotation_failed"


class CredentialKeyRotationRepository(Protocol):
    def reencrypt_batch(self, keyring: ProviderCredentialKeyring, *, batch_size: int) -> tuple[int, tuple[int, ...]]: ...
    def finalize_readiness(
        self, keyring: ProviderCredentialKeyring, *, batch_size: int
    ) -> tuple[Mapping[str, int], int]: ...


@dataclass(frozen=True, slots=True)
class CredentialKeyRotationResult:
    migrated: int
    batches: int
    remaining_by_key: Mapping[str, int]
    verified_current: int
    removable_previous_key_ids: tuple[str, ...]
    complete: bool


class ProviderCredentialKeyRotationService:
    def __init__(self, repository: CredentialKeyRotationRepository, keyring: ProviderCredentialKeyring):
        self.repository = repository
        self.keyring = keyring

    def run(self, *, batch_size: int = 25, max_batches: int = 10, reporter=None) -> CredentialKeyRotationResult:
        bounded_size = max(1, min(int(batch_size), 100))
        bounded_batches = max(1, min(int(max_batches), 100))
        migrated = 0
        completed_batches = 0
        for index in range(bounded_batches):
            count, credential_ids = self.repository.reencrypt_batch(self.keyring, batch_size=bounded_size)
            if count != len(credential_ids):
                raise ProviderCredentialKeyRotationError("credential key rotation repository returned inconsistent count")
            migrated += count
            if count:
                completed_batches += 1
                if reporter is not None:
                    reporter.progress(
                        stage="provider_credential_reencrypt",
                        current=migrated,
                        total=None,
                        unit="credentials",
                        message=f"Re-encrypted bounded batch {index + 1}",
                    )
            if count < bounded_size:
                break

        remaining_raw, verified = self.repository.finalize_readiness(self.keyring, batch_size=bounded_size)
        remaining = dict(remaining_raw)
        nonactive_remaining = sum(count for key, count in remaining.items() if key != self.keyring.active_key_id)
        removable: tuple[str, ...] = ()
        complete = nonactive_remaining == 0
        if complete:
            removable = self.keyring.previous_key_ids
        return CredentialKeyRotationResult(migrated, completed_batches, remaining, verified, removable, complete)


class PostgresCredentialKeyRotationRepository:
    _LOCK_NAME = "provider-credential-key-rotation"

    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection
            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    def reencrypt_batch(self, keyring: ProviderCredentialKeyring, *, batch_size: int):
        with self.connection_factory() as db:
            cur = db.cursor()
            sanitized_error = None
            try:
                cur.execute(
                    """SELECT id,encryption_key_id,ciphertext FROM qd_provider_credentials
                       WHERE status IN ('active','pending') AND encryption_key_id<>%s
                       ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s""",
                    (keyring.active_key_id, batch_size),
                )
                rows = list(cur.fetchall() or [])
                migrated = []
                for row in rows:
                    try:
                        plaintext = keyring.decrypt(key_id=row["encryption_key_id"], ciphertext=row["ciphertext"])
                        replacement = keyring.encrypt(plaintext)
                        if keyring.decrypt(key_id=replacement.key_id, ciphertext=replacement.ciphertext) != plaintext:
                            raise ValueError("round-trip verification mismatch")
                    except Exception as exc:
                        raise ProviderCredentialKeyRotationError(
                            "Provider credential batch could not be decrypted and verified",
                            details={"credential_id": int(row["id"]), "key_id": str(row["encryption_key_id"])},
                        ) from exc
                    cur.execute(
                        """UPDATE qd_provider_credentials SET encryption_key_id=%s,ciphertext=%s
                           WHERE id=%s AND status IN ('active','pending') AND encryption_key_id=%s""",
                        (replacement.key_id, replacement.ciphertext, row["id"], row["encryption_key_id"]),
                    )
                    if cur.rowcount != 1:
                        raise ProviderCredentialKeyRotationError(
                            "Provider credential changed during key rotation",
                            details={"credential_id": int(row["id"])},
                        )
                    migrated.append(int(row["id"]))
                db.commit()
                return len(migrated), tuple(migrated)
            except ProviderCredentialKeyRotationError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                sanitized_error = ProviderCredentialKeyRotationError(
                    "Provider credential key rotation persistence failed"
                )
            finally:
                cur.close()
            if sanitized_error is not None:
                raise sanitized_error

    def finalize_readiness(self, keyring: ProviderCredentialKeyring, *, batch_size: int):
        with self.connection_factory() as db:
            cur = db.cursor()
            sanitized_error = None
            try:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self._LOCK_NAME,))
                cur.execute("LOCK TABLE qd_provider_credentials IN SHARE MODE")
                cur.execute(
                    """SELECT encryption_key_id,COUNT(*) AS count FROM qd_provider_credentials
                       WHERE status IN ('active','pending') GROUP BY encryption_key_id ORDER BY encryption_key_id"""
                )
                remaining = {str(row["encryption_key_id"]): int(row["count"]) for row in cur.fetchall() or []}
                if any(key_id != keyring.active_key_id for key_id in remaining):
                    db.commit()
                    return remaining, 0

                after_id = 0
                verified = 0
                while True:
                    cur.execute(
                        """SELECT id,encryption_key_id,ciphertext FROM qd_provider_credentials
                           WHERE status IN ('active','pending') AND encryption_key_id=%s AND id>%s
                           ORDER BY id LIMIT %s""",
                        (keyring.active_key_id, after_id, batch_size),
                    )
                    rows = list(cur.fetchall() or [])
                    for row in rows:
                        try:
                            keyring.decrypt(key_id=row["encryption_key_id"], ciphertext=row["ciphertext"])
                        except Exception:
                            raise ProviderCredentialKeyRotationError(
                                "Provider credential verification failed after key rotation",
                                details={"credential_id": int(row["id"]), "key_id": str(row["encryption_key_id"])},
                            ) from None
                        after_id = int(row["id"])
                        verified += 1
                    if len(rows) < batch_size:
                        db.commit()
                        return remaining, verified
            except ProviderCredentialKeyRotationError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                sanitized_error = ProviderCredentialKeyRotationError(
                    "Provider credential key rotation readiness check failed"
                )
            finally:
                cur.close()
            if sanitized_error is not None:
                raise sanitized_error
