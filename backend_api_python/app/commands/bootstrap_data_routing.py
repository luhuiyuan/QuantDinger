"""Bootstrap safe default Provider Instances and routing policies."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from typing import Sequence


def build_default_bootstrap_service(*, dry_run: bool, timeout_seconds: int):
    from app.services.data_routing.bootstrap import load_default_data_routing_registry
    from app.services.data_routing.credential_repository import PostgresProviderCredentialRepository
    from app.services.data_routing.credentials import ProviderCredentialService
    from app.services.data_routing.default_bootstrap import (
        DefaultRoutingBootstrapService,
        PostgresDefaultRoutingBootstrapRepository,
    )
    from app.services.data_routing.policy import RoutingPolicyService
    from app.services.data_routing.policy_repository import PostgresRoutingPolicyRepository
    from app.services.data_routing.provider_instances import (
        PostgresProviderInstanceRepository,
        ProviderInstanceService,
    )
    from app.services.data_routing.repository import PostgresRegistryRepository, RegistryMaterializer
    from app.utils.credential_crypto import load_provider_credential_keyring
    from app.utils.db import init_database

    init_database(strict_migrations=True)
    registry = load_default_data_routing_registry()
    RegistryMaterializer(registry, PostgresRegistryRepository()).sync()
    instance_service = ProviderInstanceService(PostgresProviderInstanceRepository())
    policy_repository = PostgresRoutingPolicyRepository()
    if dry_run:
        credential_service = object()
    else:
        pepper = str(os.getenv("DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER") or "").strip()
        if not pepper:
            raise RuntimeError("DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER is required")
        credential_service = ProviderCredentialService(
            registry,
            PostgresProviderCredentialRepository(),
            keyring=load_provider_credential_keyring(),
            comparison_pepper=pepper,
        )
    return DefaultRoutingBootstrapService(
        registry,
        PostgresDefaultRoutingBootstrapRepository(),
        instance_service,
        credential_service,
        policy_repository,
        RoutingPolicyService(registry, policy_repository),
        timeout_seconds=timeout_seconds,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotently bootstrap verified default external-data routing state."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report missing public Instances without writing data.")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry credentialless Instances already in validation_failed state.",
    )
    parser.add_argument("--actor-user-id", type=int, default=None, help="Optional administrator user ID for audit attribution.")
    parser.add_argument(
        "--timeout-seconds", type=int, default=5,
        help="Per-transport timeout hint stored on newly created public Instances (1-30, default 5).",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, service_factory=build_default_bootstrap_service):
    args = _parser().parse_args(argv)
    if args.actor_user_id is not None and args.actor_user_id <= 0:
        raise SystemExit("--actor-user-id must be a positive integer")
    service = service_factory(dry_run=args.dry_run, timeout_seconds=args.timeout_seconds)
    result = service.bootstrap(
        actor_user_id=args.actor_user_id,
        retry_failed=args.retry_failed,
        dry_run=args.dry_run,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
