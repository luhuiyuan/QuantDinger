from __future__ import annotations

import pytest

from app.services.data_routing.key_rotation import (
    ProviderCredentialKeyRotationError,
    ProviderCredentialKeyRotationService,
)
from app.utils.credential_crypto import ProviderCredentialKeyring


class Repo:
    def __init__(self, rows):
        self.rows = rows
        self.verify_calls = 0
        self.before_finalize = None

    def reencrypt_batch(self, keyring, *, batch_size):
        selected = [row for row in self.rows if row["status"] in {"active", "pending"} and row["key_id"] != keyring.active_key_id][:batch_size]
        replacements = []
        for row in selected:
            try:
                plaintext = keyring.decrypt(key_id=row["key_id"], ciphertext=row["ciphertext"])
                encrypted = keyring.encrypt(plaintext)
                assert keyring.decrypt(key_id=encrypted.key_id, ciphertext=encrypted.ciphertext) == plaintext
            except Exception as exc:
                raise ProviderCredentialKeyRotationError("batch failed", details={"credential_id": row["id"]}) from exc
            replacements.append((row, encrypted))
        for row, encrypted in replacements:
            row["key_id"] = encrypted.key_id
            row["ciphertext"] = encrypted.ciphertext
        return len(replacements), tuple(row["id"] for row, _ in replacements)

    def finalize_readiness(self, keyring, *, batch_size):
        if self.before_finalize is not None:
            self.before_finalize()
            self.before_finalize = None
        counts = {}
        for row in self.rows:
            if row["status"] in {"active", "pending"}:
                counts[row["key_id"]] = counts.get(row["key_id"], 0) + 1
        if any(key_id != keyring.active_key_id for key_id in counts):
            return counts, 0
        self.verify_calls += 1
        current = [row for row in self.rows if row["status"] in {"active", "pending"}]
        for row in current:
            keyring.decrypt(key_id=row["key_id"], ciphertext=row["ciphertext"])
        return counts, len(current)


def encrypted_row(row_id, status, keyring, value):
    encrypted = keyring.encrypt(value)
    return {"id": row_id, "status": status, "key_id": encrypted.key_id, "ciphertext": encrypted.ciphertext}


def test_rotation_is_bounded_and_old_key_is_not_removable_until_full_verification():
    old = ProviderCredentialKeyring("old", {"old": "old-secret"})
    rotating = ProviderCredentialKeyring("new", {"new": "new-secret", "old": "old-secret"})
    repo = Repo([encrypted_row(i, "active" if i % 2 else "pending", old, f"secret-{i}") for i in range(1, 6)])
    service = ProviderCredentialKeyRotationService(repo, rotating)

    partial = service.run(batch_size=2, max_batches=1)
    assert partial.migrated == 2 and partial.complete is False
    assert partial.removable_previous_key_ids == () and repo.verify_calls == 0

    complete = service.run(batch_size=2, max_batches=10)
    assert complete.migrated == 3 and complete.complete is True
    assert complete.verified_current == 5
    assert complete.removable_previous_key_ids == ("old",)
    assert complete.remaining_by_key == {"new": 5}


def test_rotation_failure_is_atomic_for_the_claimed_batch():
    old = ProviderCredentialKeyring("old", {"old": "old-secret"})
    rotating = ProviderCredentialKeyring("new", {"new": "new-secret", "old": "old-secret"})
    first = encrypted_row(1, "active", old, "first")
    broken = {"id": 2, "status": "pending", "key_id": "old", "ciphertext": "not-fernet"}
    repo = Repo([first, broken])
    original = dict(first)

    with pytest.raises(ProviderCredentialKeyRotationError):
        ProviderCredentialKeyRotationService(repo, rotating).run(batch_size=2, max_batches=1)

    assert first == original
    assert repo.verify_calls == 0


def test_missing_previous_key_fails_without_modifying_ciphertext():
    old = ProviderCredentialKeyring("old", {"old": "old-secret"})
    row = encrypted_row(1, "active", old, "first")
    repo = Repo([row])
    original = dict(row)
    active_only = ProviderCredentialKeyring("new", {"new": "new-secret"})
    with pytest.raises(ProviderCredentialKeyRotationError):
        ProviderCredentialKeyRotationService(repo, active_only).run()
    assert row == original


def test_destroyed_ciphertext_is_not_reencrypted_or_required_for_key_removal():
    old = ProviderCredentialKeyring("old", {"old": "old-secret"})
    rotating = ProviderCredentialKeyring("new", {"new": "new-secret", "old": "old-secret"})
    destroyed = {"id": 1, "status": "destroyed", "key_id": "old", "ciphertext": ""}
    repo = Repo([destroyed])
    result = ProviderCredentialKeyRotationService(repo, rotating).run()
    assert result.complete and result.migrated == 0 and result.verified_current == 0
    assert result.removable_previous_key_ids == ("old",)


def test_finalization_rejects_stale_key_inserted_at_the_readiness_boundary():
    old = ProviderCredentialKeyring("old", {"old": "old-secret"})
    rotating = ProviderCredentialKeyring("new", {"new": "new-secret", "old": "old-secret"})
    repo = Repo([encrypted_row(1, "active", rotating, "current")])
    repo.before_finalize = lambda: repo.rows.append(encrypted_row(2, "pending", old, "stale"))

    result = ProviderCredentialKeyRotationService(repo, rotating).run()

    assert result.complete is False
    assert result.verified_current == 0
    assert result.removable_previous_key_ids == ()
    assert result.remaining_by_key == {"new": 1, "old": 1}


def test_postgres_finalization_uses_one_write_blocking_transaction():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "data_routing" / "key_rotation.py").read_text()
    method = source[source.index("def finalize_readiness"):]
    assert "pg_advisory_xact_lock(hashtext(%s))" in method
    assert "LOCK TABLE qd_provider_credentials IN SHARE MODE" in method
    assert method.index("LOCK TABLE") < method.index("SELECT encryption_key_id,COUNT(*)")


def test_postgres_rotation_sanitizes_database_exception_context():
    from app.services.data_routing.key_rotation import PostgresCredentialKeyRotationRepository

    secret = "gAAAAAB-sensitive-ciphertext"

    class Cursor:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError(f"database rejected ciphertext {secret}")

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            pass

    class Manager:
        def __enter__(self):
            return Connection()

        def __exit__(self, exc_type, exc, traceback):
            self.seen = exc
            return False

    manager = Manager()
    repository = PostgresCredentialKeyRotationRepository(lambda: manager)
    keyring = ProviderCredentialKeyring("active", {"active": "key-secret"})

    with pytest.raises(ProviderCredentialKeyRotationError) as caught:
        repository.finalize_readiness(keyring, batch_size=10)

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert isinstance(manager.seen, ProviderCredentialKeyRotationError)
