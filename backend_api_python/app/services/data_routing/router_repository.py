"""Short-lived Provider credential resolution for the Router execution plane."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable

from app.utils.credential_crypto import ProviderCredentialKeyring, load_provider_credential_keyring

from .errors import DataRoutingNotReadyError
from .snapshot import SecretHandle


class PostgresRouterSecretResolver:
    def __init__(
        self,
        connection_factory: Callable[[], Any] | None = None,
        *,
        keyring: ProviderCredentialKeyring | None = None,
    ):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory
        self.keyring = keyring or load_provider_credential_keyring()

    @contextmanager
    def resolve(self, handle: SecretHandle):
        credential_id = handle.resolve_credential_id()
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT encryption_key_id,ciphertext FROM qd_provider_credentials
                       WHERE id=%s AND status='active'""",
                    (credential_id,),
                )
                row = cur.fetchone()
            finally:
                cur.close()
        if not row:
            raise DataRoutingNotReadyError("Pinned Provider credential is no longer active")
        try:
            plaintext = self.keyring.decrypt(
                key_id=row["encryption_key_id"], ciphertext=row["ciphertext"]
            )
            credentials = json.loads(plaintext)
            if not isinstance(credentials, dict):
                raise ValueError("credential payload is not an object")
        except Exception as exc:
            raise DataRoutingNotReadyError("Pinned Provider credential cannot be resolved") from exc
        try:
            yield credentials
        finally:
            credentials.clear()
