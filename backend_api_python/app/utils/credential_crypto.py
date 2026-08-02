"""Fernet encryption for persisted credentials and MFA secrets.

New installations use ``CREDENTIAL_ENCRYPTION_KEY`` so rotating the JWT/session
``SECRET_KEY`` does not make broker credentials unreadable. Ciphertexts created
by older releases remain readable through the legacy ``SECRET_KEY`` fallback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken


def _secret_key() -> str:
    secret = (os.getenv("SECRET_KEY") or "").strip()
    if not secret:
        try:
            from app.config.settings import Config

            secret = str(Config.SECRET_KEY or "").strip()
        except Exception:
            secret = ""
    return secret


def _credential_key() -> str:
    return (os.getenv("CREDENTIAL_ENCRYPTION_KEY") or "").strip()


def _fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _encryption_secret() -> str:
    secret = _credential_key() or _secret_key()
    if not secret:
        raise ValueError(
            "CREDENTIAL_ENCRYPTION_KEY or SECRET_KEY must be set to encrypt persisted credentials"
        )
    return secret


def encrypt_credential_blob(plaintext_json: str) -> str:
    """Encrypt JSON text for storage in encrypted_config."""
    if plaintext_json is None:
        plaintext_json = ""
    f = _fernet(_encryption_secret())
    return f.encrypt(plaintext_json.encode("utf-8")).decode("ascii")


def decrypt_credential_blob(stored: Any) -> str:
    """
    Decrypt DB value to JSON text. Empty / None yields empty string.
    """
    if stored is None:
        return ""
    s = stored.decode("utf-8") if isinstance(stored, (bytes, bytearray)) else str(stored)
    s = s.strip()
    if not s:
        return ""
    secrets = []
    for candidate in (_credential_key(), _secret_key()):
        if candidate and candidate not in secrets:
            secrets.append(candidate)
    if not secrets:
        raise ValueError(
            "CREDENTIAL_ENCRYPTION_KEY or SECRET_KEY must be set to decrypt persisted credentials"
        )
    for secret in secrets:
        try:
            return _fernet(secret).decrypt(s.encode("ascii")).decode("utf-8")
        except InvalidToken:
            continue
    raise ValueError(
        "Cannot decrypt persisted credential with the configured encryption keys"
    )


@dataclass(frozen=True, slots=True)
class EncryptedProviderCredential:
    key_id: str
    ciphertext: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderCredentialKeyring:
    """One encrypt/decrypt key and at most three previous decrypt-only keys."""

    active_key_id: str
    keys: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        active = str(self.active_key_id or "").strip()
        normalized = {str(key).strip(): str(value).strip() for key, value in dict(self.keys).items()}
        if not active or active not in normalized or not normalized[active]:
            raise ValueError("Provider credential keyring active key id is missing")
        if len(normalized) > 4:
            raise ValueError("Provider credential keyring supports at most one active and three previous keys")
        if any(not key or not secret for key, secret in normalized.items()):
            raise ValueError("Provider credential keyring contains an empty key id or secret")
        object.__setattr__(self, "active_key_id", active)
        object.__setattr__(self, "keys", normalized)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(active_key_id={self.active_key_id!r}, "
            f"previous_key_ids={self.previous_key_ids!r})"
        )

    @property
    def previous_key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(key for key in self.keys if key != self.active_key_id))

    def encrypt(self, plaintext_json: str) -> EncryptedProviderCredential:
        plaintext = "" if plaintext_json is None else str(plaintext_json)
        ciphertext = _fernet(self.keys[self.active_key_id]).encrypt(plaintext.encode("utf-8")).decode("ascii")
        return EncryptedProviderCredential(self.active_key_id, ciphertext)

    def decrypt(self, *, key_id: str, ciphertext: Any) -> str:
        selected = str(key_id or "").strip()
        secret = self.keys.get(selected)
        if not secret:
            raise ValueError(f"Provider credential encryption key id is unavailable: {selected or '<empty>'}")
        stored = ciphertext.decode("utf-8") if isinstance(ciphertext, (bytes, bytearray)) else str(ciphertext or "")
        try:
            return _fernet(secret).decrypt(stored.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Cannot decrypt Provider credential with its recorded key id") from exc


def load_provider_credential_keyring(environ: Mapping[str, str] | None = None) -> ProviderCredentialKeyring:
    """Load the Provider-only keyring without consulting ``SECRET_KEY``."""

    env = os.environ if environ is None else environ
    active_key_id = str(env.get("DATA_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID") or "").strip()
    raw = str(env.get("DATA_PROVIDER_CREDENTIAL_KEYS") or "").strip()
    if not raw:
        raise ValueError("DATA_PROVIDER_CREDENTIAL_KEYS is required for new Provider credentials")
    try:
        keys = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("DATA_PROVIDER_CREDENTIAL_KEYS must be a JSON object") from exc
    if not isinstance(keys, dict):
        raise ValueError("DATA_PROVIDER_CREDENTIAL_KEYS must be a JSON object")
    return ProviderCredentialKeyring(active_key_id, keys)


def encrypt_provider_credential_blob(
    plaintext_json: str, *, keyring: ProviderCredentialKeyring | None = None
) -> EncryptedProviderCredential:
    return (keyring or load_provider_credential_keyring()).encrypt(plaintext_json)


def decrypt_provider_credential_blob(
    ciphertext: Any, *, key_id: str, keyring: ProviderCredentialKeyring | None = None
) -> str:
    return (keyring or load_provider_credential_keyring()).decrypt(key_id=key_id, ciphertext=ciphertext)


def provider_credential_comparison_tag(canonical_secret_json: str, *, pepper: str | None = None) -> str:
    """Return a stable non-reversible equality tag which must never be displayed."""

    selected = str(pepper if pepper is not None else os.getenv("DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER") or "")
    if not selected:
        raise ValueError("DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER is required")
    return hmac.new(selected.encode("utf-8"), str(canonical_secret_json).encode("utf-8"), hashlib.sha256).hexdigest()
