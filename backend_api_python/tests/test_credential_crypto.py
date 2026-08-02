import json

import pytest

from app.utils.credential_crypto import (
    ProviderCredentialKeyring,
    decrypt_credential_blob,
    decrypt_provider_credential_blob,
    encrypt_credential_blob,
    encrypt_provider_credential_blob,
    load_provider_credential_keyring,
    provider_credential_comparison_tag,
)


def test_dedicated_credential_key_survives_secret_key_rotation(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "credential-key-a")
    monkeypatch.setenv("SECRET_KEY", "session-key-a")
    encrypted = encrypt_credential_blob('{"exchange_id":"alpaca"}')

    monkeypatch.setenv("SECRET_KEY", "session-key-b")

    assert decrypt_credential_blob(encrypted) == '{"exchange_id":"alpaca"}'


def test_legacy_secret_key_ciphertext_remains_readable(monkeypatch):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "legacy-session-key")
    encrypted = encrypt_credential_blob("legacy-secret")

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "new-credential-key")

    assert decrypt_credential_blob(encrypted) == "legacy-secret"


def test_provider_keyring_records_key_id_and_reads_previous_decrypt_only_key():
    old = ProviderCredentialKeyring("old-2026-07", {"old-2026-07": "old-secret"})
    stored = encrypt_provider_credential_blob('{"token":"value"}', keyring=old)
    rotating = ProviderCredentialKeyring(
        "new-2026-08",
        {"new-2026-08": "new-secret", "old-2026-07": "old-secret"},
    )

    assert stored.key_id == "old-2026-07"
    assert decrypt_provider_credential_blob(stored.ciphertext, key_id=stored.key_id, keyring=rotating) == '{"token":"value"}'
    assert encrypt_provider_credential_blob('{"token":"next"}', keyring=rotating).key_id == "new-2026-08"


def test_provider_keyring_never_falls_back_to_secret_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "legacy-session-secret")
    monkeypatch.delenv("DATA_PROVIDER_CREDENTIAL_KEYS", raising=False)
    monkeypatch.delenv("DATA_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID", raising=False)

    with pytest.raises(ValueError, match="DATA_PROVIDER_CREDENTIAL_KEYS is required"):
        encrypt_provider_credential_blob('{"token":"value"}')


def test_provider_keyring_loader_requires_active_id_and_bounded_previous_keys():
    with pytest.raises(ValueError, match="active key id"):
        load_provider_credential_keyring({"DATA_PROVIDER_CREDENTIAL_KEYS": json.dumps({"old": "secret"})})
    with pytest.raises(ValueError, match="at most"):
        ProviderCredentialKeyring("a", {key: f"secret-{key}" for key in "abcde"})


def test_provider_secret_comparison_tag_is_stable_peppered_and_non_reversible():
    first = provider_credential_comparison_tag('{"token":"secret"}', pepper="pepper-a")
    assert first == provider_credential_comparison_tag('{"token":"secret"}', pepper="pepper-a")
    assert first != provider_credential_comparison_tag('{"token":"secret"}', pepper="pepper-b")
    assert "secret" not in first


def test_provider_secret_records_have_safe_repr():
    from app.services.data_routing.credentials import StoredCredentialSecret

    keyring = ProviderCredentialKeyring(
        "active",
        {"active": "RAW-ACTIVE-SECRET", "previous": "RAW-PREVIOUS-SECRET"},
    )
    encrypted = keyring.encrypt('{"token":"secret-value"}')
    stored = StoredCredentialSecret(1, 1, "1", "active", encrypted.key_id, encrypted.ciphertext)

    rendered = repr((keyring, encrypted, stored))
    assert "RAW-ACTIVE-SECRET" not in rendered
    assert "RAW-PREVIOUS-SECRET" not in rendered
    assert encrypted.ciphertext not in rendered
    assert "active_key_id='active'" in rendered
    assert "previous_key_ids=('previous',)" in rendered
