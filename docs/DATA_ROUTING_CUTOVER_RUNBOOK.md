# Unified Data Routing Cutover Runbook

This runbook activates unified external-data routing once for the whole application. It does not provide a per-domain switch, shadow fallback, version rollback, or post-activation abort. After activation, recover only by deploying a forward fix that continues to use the unified Router.

## Preconditions

1. Deploy the additive database migration while the old application is still available.
2. Configure `DATA_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID`, `DATA_PROVIDER_CREDENTIAL_KEYS`, and `DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER` outside source control.
3. Import every detected supported legacy credential through Data Source Operations using Step-up authentication. Repair failed imports or create a Provider Instance manually.
4. Remove imported legacy secret values from the deployment environment. Keep LLM, trading, notification, payment, OAuth, and infrastructure secrets in their existing boundaries.
5. Validate every enabled Capability against at least one active, eligible, healthy Provider Instance. Publish an effective policy or explicitly disable the Capability with a reason.
6. Run the backend, Vue, Agent, MCP, inventory, security, policy, cache, quota, health, and cutover test matrix. Write the verified result to `docs/architecture/DATA_ROUTING_TEST_MATRIX.json`.
7. Generate the server-owned preflight report. A `NO_GO` decision is final for that attempt; administrators cannot submit replacement evidence.

For Compose deployments, run the migration as a single dedicated process after
building or pulling the target backend image:

```bash
COMPOSE_PARALLEL_LIMIT=1 docker compose run --rm --no-deps migration
```

The migration applies the idempotent Schema and then materializes the trusted
Adapter/Capability registry. It does not create Provider Instances, import
credentials, publish Routing Policies, or activate cutover. The backend image
contains release copies of the inventory and verified test-matrix artifacts;
tests require them to match their canonical files under `docs/architecture/`.

## Maintenance And Drain

1. Announce the maintenance window and stop external traffic at the ingress.
2. Create the cutover record for the exact target application version with Step-up proof and a reason.
3. Evaluate gates through the Data Source Operations API. Do not enter maintenance unless every mandatory gate is `passed`.
4. Enter maintenance. Normal HTTP entrypoints return `503`; health, auth, and Data Source Operations remain available.
5. The scheduler observes maintenance, stops its task executor at the safe boundary, exits, and records a stopped heartbeat.
6. Wait until the server reports zero incomplete Routed Data Requests, zero running/cancel-requested Task Runs, and zero running scheduler heartbeats.
7. Re-evaluate operational conditions. Abort is allowed only before activation.

## Activation

1. Apply any final additive data migration required by the target version. Do not run destructive rollback SQL.
2. Activate the unique cutover record. The database transaction records the forward-only activation and management audit together.
3. Start the target backend and Vue versions. Do not start an older backend version and do not enable a legacy route switch.
4. Verify readiness, health, authentication, Data Source Operations, one representative request per enabled Capability, Agent contract, MCP contract, and A-share backtest local-history behavior.
5. Reopen ingress only after those checks pass.

## Forward Fix

If a fault appears after activation, keep maintenance active or reapply ingress maintenance, diagnose the unified Router/Adapter/policy path, deploy a newer application version, and repeat readiness checks. Do not restore old provider/fallback code, old environment secret lookup, or a domain-specific bypass.

## Secret Cleanup

After successful import, remove `FINNHUB_API_KEY`, `TWELVE_DATA_API_KEY`, `TIINGO_API_KEY`, `FRED_API_KEY`, `BLS_API_KEY`, `BEA_API_KEY`, Trading Economics credentials, CoinGlass/CryptoQuant keys, Tavily/Bing search keys, and Alpha Vantage keys from `.env`, Compose secrets, service managers, and host environment files. Never include their values in tickets, logs, screenshots, audits, or the test matrix artifact.
