"""Fail-fast database migration entrypoint for deployments."""

from __future__ import annotations


def _sync_data_routing_registry():
    from app.services.data_routing.bootstrap import load_default_data_routing_registry
    from app.services.data_routing.repository import PostgresRegistryRepository, RegistryMaterializer

    return RegistryMaterializer(
        load_default_data_routing_registry(),
        PostgresRegistryRepository(),
    ).sync()


def main() -> None:
    from app.utils.db import init_database
    from app.utils.logger import get_logger

    init_database(strict_migrations=True)
    report = _sync_data_routing_registry()
    logger = get_logger(__name__)
    logger.info(
        "Data routing registry synchronized: adapters=%d capabilities=%d tombstones=%d migrated_instances=%d",
        len(report.adapters_created_or_updated),
        len(report.capabilities_created_or_updated),
        len(report.adapters_tombstoned) + len(report.capabilities_tombstoned),
        len(report.instances_migrated),
    )
    if not report.ready:
        logger.warning(
            "Data routing registry synchronized with %d readiness blocker(s); resolve them in Data Source Operations",
            len(report.readiness_issues),
        )


if __name__ == "__main__":
    main()
