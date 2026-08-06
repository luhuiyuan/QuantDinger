"""Idempotent bootstrap for safe default external-data routing state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


BOOTSTRAP_DISABLED_REASON = (
    "No verified eligible default Provider Instance; configure credentials or "
    "revalidate a public Provider"
)
BOOTSTRAP_INSTANCE_REASON = "bootstrap default credentialless provider instance"
BOOTSTRAP_POLICY_REASON = "bootstrap verified default routing policy"
CN_A_ROUTE_CAPABILITIES = frozenset({
    "asia_equity_kline", "cn_corporate_actions", "cn_corporate_announcement",
    "cn_equity_history", "cn_fundamental_history", "cn_hk_fundamentals",
    "cn_hk_quote", "cn_market_snapshot", "cn_official_adjustment_reference",
})


DEFAULT_ROUTING_PREFERENCES: Mapping[str, tuple[str, ...]] = {
    "analysis_search": ("gdelt", "jina", "searxng"),
    "asia_equity_kline": ("tencent", "sina", "easy_tdx", "yfinance", "akshare"),
    "cn_corporate_actions": ("easy_tdx",),
    "cn_corporate_announcement": ("cninfo",),
    "cn_equity_history": ("easy_tdx", "sina"),
    "cn_fundamental_history": ("eastmoney",),
    "cn_hk_fundamentals": ("akshare", "sina"),
    "cn_hk_quote": ("tencent", "sina", "easy_tdx", "eastmoney", "akshare"),
    "cn_market_snapshot": ("eastmoney", "akshare", "sina", "tencent", "easy_tdx"),
    "cn_official_adjustment_reference": ("cninfo", "cn_exchange_official"),
    "commodity_quote": ("yfinance",),
    "crypto_derivatives_market_data": ("ccxt_public_market",),
    "crypto_market_snapshot": ("coingecko", "coincap", "yfinance"),
    "crypto_public_market_data": ("ccxt_public_market",),
    "crypto_public_quote": ("binance_public", "bybit_public"),
    "economic_calendar": ("akshare",),
    "forex_market_data": ("yfinance",),
    "forex_quote": ("yfinance",),
    "futures_market_data": ("yfinance",),
    "global_heatmap": ("yfinance",),
    "global_market_overview": ("coingecko", "binance_public"),
    "market_catalog": ("akshare", "sina", "ccxt_public_market", "moex"),
    "market_index_quote": ("yfinance",),
    "market_sentiment": ("cnn_fear_greed", "akshare", "yfinance"),
    "moex_market_data": ("moex",),
    "symbol_master": ("akshare", "sina", "nasdaq_trader", "ccxt_public_market", "moex", "stooq"),
    "symbol_reference": ("tencent", "sina", "yfinance", "moex"),
    "us_equity_market_data": ("yfinance",),
    "us_fundamentals": ("yfinance",),
    "us_macro_release": ("akshare",),
}


@dataclass(frozen=True, slots=True)
class BootstrapProviderInstance:
    instance_id: int
    instance_key: str
    adapter_key: str
    lifecycle_status: str


@dataclass(frozen=True, slots=True)
class DefaultRoutingBootstrapResult:
    dry_run: bool
    planned_adapters: tuple[str, ...] = ()
    created_adapters: tuple[str, ...] = ()
    validated_adapters: tuple[str, ...] = ()
    failed_adapters: tuple[tuple[str, str], ...] = ()
    published_capabilities: tuple[str, ...] = ()
    disabled_capabilities: tuple[str, ...] = ()
    preserved_capabilities: tuple[str, ...] = ()


class BootstrapRepository(Protocol):
    def list_non_retired_instances(self) -> tuple[BootstrapProviderInstance, ...]: ...
    def list_eligible_instances(self) -> Mapping[str, Mapping[str, int]]: ...


class PostgresDefaultRoutingBootstrapRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    def list_non_retired_instances(self) -> tuple[BootstrapProviderInstance, ...]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT id AS instance_id,instance_key,adapter_key,lifecycle_status
                       FROM qd_provider_instances WHERE lifecycle_status<>'retired'
                       ORDER BY adapter_key,
                         CASE WHEN instance_key LIKE 'default_%%' THEN 0 ELSE 1 END,id"""
                )
                return tuple(BootstrapProviderInstance(**dict(row)) for row in (cur.fetchall() or []))
            finally:
                cur.close()

    def list_eligible_instances(self) -> Mapping[str, Mapping[str, int]]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT c.capability_key,i.adapter_key,i.id AS instance_id
                       FROM qd_provider_instance_capabilities c
                       JOIN qd_provider_instances i ON i.id=c.instance_id
                       WHERE c.eligibility_status='eligible' AND i.lifecycle_status='active'
                       ORDER BY c.capability_key,i.adapter_key,i.id"""
                )
                grouped: dict[str, dict[str, int]] = {}
                for row in cur.fetchall() or []:
                    grouped.setdefault(str(row["capability_key"]), {}).setdefault(
                        str(row["adapter_key"]), int(row["instance_id"])
                    )
                return grouped
            finally:
                cur.close()


class DefaultRoutingBootstrapService:
    def __init__(
        self,
        registry,
        bootstrap_repository: BootstrapRepository,
        instance_service,
        credential_service,
        policy_repository,
        policy_service,
        *,
        preferences: Mapping[str, tuple[str, ...]] | None = None,
        timeout_seconds: int = 5,
    ):
        self.registry = registry
        self.bootstrap_repository = bootstrap_repository
        self.instance_service = instance_service
        self.credential_service = credential_service
        self.policy_repository = policy_repository
        self.policy_service = policy_service
        self.preferences = dict(preferences or DEFAULT_ROUTING_PREFERENCES)
        self.timeout_seconds = max(1, min(int(timeout_seconds), 30))

    @staticmethod
    def _public_adapter(adapter) -> bool:
        return not tuple((adapter.credential_schema or {}).get("required") or ())

    def bootstrap(
        self,
        *,
        actor_user_id: int | None = None,
        retry_failed: bool = False,
        dry_run: bool = False,
    ) -> DefaultRoutingBootstrapResult:
        instances = list(self.bootstrap_repository.list_non_retired_instances())
        by_adapter: dict[str, BootstrapProviderInstance] = {}
        for instance in instances:
            by_adapter.setdefault(instance.adapter_key, instance)
        public_adapters = tuple(
            key for key, adapter in sorted(self.registry.adapters.items()) if self._public_adapter(adapter)
        )
        planned = tuple(key for key in public_adapters if key not in by_adapter)
        if dry_run:
            return DefaultRoutingBootstrapResult(dry_run=True, planned_adapters=planned)

        # A credential can be active while only a subset of an Adapter's
        # capabilities has been verified (for example after the catalog
        # grows).  Do not mistake that state for a healthy default route.
        eligible_before = self.bootstrap_repository.list_eligible_instances()

        created: list[str] = []
        validated: list[str] = []
        failures: list[tuple[str, str]] = []
        for adapter_key in public_adapters:
            adapter = self.registry.adapters[adapter_key]
            instance = by_adapter.get(adapter_key)
            if instance is None:
                record = self.instance_service.create_draft(
                    instance_key=f"default_{adapter_key}",
                    adapter_key=adapter_key,
                    display_name=f"Default {adapter.public_name}",
                    config_schema_version=adapter.version,
                    non_secret_config={"timeout_seconds": self.timeout_seconds},
                    actor_user_id=actor_user_id,
                )
                instance = BootstrapProviderInstance(
                    int(record.id), f"default_{adapter_key}", adapter_key, "draft"
                )
                by_adapter[adapter_key] = instance
                created.append(adapter_key)
            # The catalog is user-visible even when this host cannot reach the
            # upstream. Live validation only changes eligibility.
            self.credential_service.ensure_declared_capabilities(instance.instance_id)
            status = self.credential_service.get_status(instance.instance_id)
            all_capabilities_verified = all(
                eligible_before.get(capability_key, {}).get(adapter_key) == instance.instance_id
                for capability_key in adapter.capabilities
            )
            if status.configured and all_capabilities_verified:
                continue
            if instance.lifecycle_status == "validation_failed" and not retry_failed:
                failures.append((adapter_key, "validation_failed_retry_not_requested"))
                continue
            try:
                self.credential_service.submit_and_validate(
                    instance.instance_id,
                    {},
                    actor_user_id=actor_user_id,
                    reason=BOOTSTRAP_INSTANCE_REASON,
                )
                validated.append(adapter_key)
            except Exception as exc:
                failures.append((adapter_key, type(exc).__name__))

        reconciled = self.reconcile_verified_routes(actor_user_id=actor_user_id)
        return DefaultRoutingBootstrapResult(
            dry_run=False,
            planned_adapters=planned,
            created_adapters=tuple(created),
            validated_adapters=tuple(validated),
            failed_adapters=tuple(failures),
            published_capabilities=reconciled.published_capabilities,
            disabled_capabilities=reconciled.disabled_capabilities,
            preserved_capabilities=reconciled.preserved_capabilities,
        )

    def reconcile_verified_routes(
        self,
        *,
        actor_user_id: int | None = None,
        capability_keys: frozenset[str] | None = None,
    ) -> DefaultRoutingBootstrapResult:
        """Reconcile system-managed default policies from active eligible Instances.

        Only policies published by this bootstrap (or disabled by its standard
        no-provider reason) are changed. Explicit operator policies remain
        untouched while newly verified default providers become fallbacks.
        """
        eligible = self.bootstrap_repository.list_eligible_instances()
        published: list[str] = []
        disabled: list[str] = []
        preserved: list[str] = []
        allowed_capabilities = capability_keys or frozenset(self.registry.capabilities)
        for capability_key in sorted(set(self.registry.capabilities) & set(allowed_capabilities)):
            state = self.policy_repository.get_policy(capability_key)
            effective = state.effective_revision if state is not None else None
            managed_effective = effective is not None and getattr(effective, "change_reason", "") == BOOTSTRAP_POLICY_REASON
            managed_disabled = state is not None and not state.enabled and state.disabled_reason == BOOTSTRAP_DISABLED_REASON
            if state is not None and state.draft_revision is not None:
                preserved.append(capability_key)
                continue
            if state is not None and not managed_effective and not managed_disabled:
                preserved.append(capability_key)
                continue
            available = dict(eligible.get(capability_key) or {})
            preferred = [key for key in self.preferences.get(capability_key, ()) if key in available]
            preferred.extend(sorted(key for key in available if key not in preferred))
            current_ids = tuple(entry.instance_id for entry in getattr(effective, "entries", ()))
            desired_ids = tuple(available[key] for key in preferred)
            if desired_ids == current_ids and effective is not None:
                preserved.append(capability_key)
                continue
            if preferred:
                expected_version = state.policy_version if state is not None else None
                draft = self.policy_service.save_draft(
                    capability_key,
                    ({"instance_id": available[key]} for key in preferred),
                    expected_policy_version=expected_version,
                    actor_user_id=actor_user_id,
                )
                self.policy_service.publish_draft(
                    capability_key,
                    expected_policy_version=draft.policy_version,
                    reason=BOOTSTRAP_POLICY_REASON,
                    actor_user_id=actor_user_id,
                )
                published.append(capability_key)
            elif state is None or state.enabled:
                self.policy_service.disable_capability(
                    capability_key,
                    expected_policy_version=state.policy_version if state is not None else None,
                    reason=BOOTSTRAP_DISABLED_REASON,
                    actor_user_id=actor_user_id,
                )
                disabled.append(capability_key)
            else:
                preserved.append(capability_key)
        return DefaultRoutingBootstrapResult(
            dry_run=False,
            published_capabilities=tuple(published),
            disabled_capabilities=tuple(disabled),
            preserved_capabilities=tuple(preserved),
        )
