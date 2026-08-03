# Data Source Operations

Data Source Operations is the administrator control surface for unified
external analysis data. It manages code-registered Provider Adapters and Data
Capabilities; administrators cannot upload plugins, arbitrary endpoints, SQL,
shell commands, or routing expressions.

## Administrator workflow

1. Open **Overview** and resolve readiness, registry, credential-key, health,
   circuit, quota, policy, and cutover blockers.
2. Create a draft Provider Instance from a registered Adapter, submit write-only
   credentials, validate its upstream account identity and capabilities, then
   activate it after at least one Capability succeeds.
3. Create or edit a Routing Policy draft for a Capability. Validate and preview
   the complete ordered route, provide a reason, and publish it explicitly.
4. Use diagnostics for isolated Provider Capability Tests. Use quarantine and
   controlled recovery probes for production health; never force an instance
   healthy.
5. Use External Data Request Logs for routed request/Attempt investigation and
   Data Source Settings only for shared non-secret transport settings and legacy
   import status.

Credentials never echo after submission. Rotation writes a pending version,
verifies the same upstream account, atomically activates it, and destroys the
old ciphertext. A different account requires a new Provider Instance.

## New-environment default bootstrap

After database migration and registry materialization, initialize safe default
routes with the explicit idempotent command:

```bash
cd backend_api_python
python -m app.commands.bootstrap_data_routing --dry-run
python -m app.commands.bootstrap_data_routing --actor-user-id 1
```

For a Compose deployment where PostgreSQL is already running:

```bash
docker compose run --rm --no-deps backend \
  python -m app.commands.bootstrap_data_routing --dry-run
docker compose run --rm --no-deps backend \
  python -m app.commands.bootstrap_data_routing --actor-user-id 1
```

The command creates only code-registered Provider Adapters whose credential
schema has no required fields. It stores an encrypted credentialless marker so
runtime snapshots use the same opaque `SecretHandle` path, runs bounded live
Capability diagnostics, publishes policies only for verified eligible
Instances, and explicitly disables Capabilities that have no verified default.
It never imports API keys or reads legacy credential values.

Repeated execution preserves configured Instances, effective policies,
administrator drafts, and manually disabled policies. A Provider Instance that
previously reached `validation_failed` is not probed again unless explicitly
requested:

```bash
python -m app.commands.bootstrap_data_routing --retry-failed --actor-user-id 1
```

`--actor-user-id` is optional for installations that have not created their
first administrator yet; audit rows then retain a null actor while preserving
the bootstrap reason. `DATA_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID`,
`DATA_PROVIDER_CREDENTIAL_KEYS`, and
`DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER` must be configured for a writing
run. `--dry-run` does not require those Provider credential settings.

## User provenance

Ordinary users do not choose Providers. A routed business response may include
`X-Routed-Data-Request-ID`, public Provider name, acquisition time, freshness,
and non-sensitive warning count. This is informational and optional, so existing
Web, strategy, backtest, Agent, and MCP clients continue to parse their existing
payloads.

Users cannot browse internal request summaries or Attempts and cannot see
Provider Instance IDs, route order, policy revisions, quota/health data,
credentials, raw parameters, sanitized internal errors, or other users' and
background requests.

## Operational boundaries

- Provider order and fallback belong only to `DataRouter`.
- Adapter transports may retry only the same provider within a bounded budget.
- Persistent historical, fundamental, corporate-action, and backtest datasets
  are domain repositories, not Routing Cache.
- A-share daily backtests read qualified PostgreSQL history and never make a
  remote request to fill a gap.
- Trading accounts, positions, orders, LLM, notification, payment, OAuth, and
  infrastructure calls are outside Data Source Operations.
- Mobile/H5 has no administrator console. Administration is Web-only.

For activation and incident handling, follow
`docs/deployment/DATA_ROUTING_CUTOVER_RUNBOOK.md`. After activation, repair only through a
new policy, explicit Capability disablement, eligible cache, quarantine, or a
forward application fix.
