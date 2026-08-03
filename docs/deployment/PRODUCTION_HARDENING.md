# Production Hardening

## Preflight

Create the project-root `.env` and `backend_api_python/.env`, then replace every default credential. Validate them before deployment:

```bash
python backend_api_python/scripts/check_production_config.py \
  --env-file .env \
  --env-file backend_api_python/.env
```

The guard rejects default database, administrator, Grafana, JWT, and credential-encryption secrets.

Unified external-data credentials use a dedicated keyring. Configure
`DATA_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID`, `DATA_PROVIDER_CREDENTIAL_KEYS`, and
`DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER` outside source control before
creating or importing Provider Instances. Do not use the Flask `SECRET_KEY` as
the normal Provider credential key.

Legacy data-source environment keys are import-only. Import them once through
Data Source Operations with Step-up proof, verify the resulting capabilities,
then remove their values from Compose, service-manager, and host environment
files. Trading, LLM, notification, payment, OAuth, and infrastructure secrets
remain in their existing boundaries.

Schema migration deliberately does not seed Provider eligibility or effective
routing revisions because those facts require live validation in the target
network. After migrations complete, run:

```bash
python -m app.commands.bootstrap_data_routing --dry-run
python -m app.commands.bootstrap_data_routing
```

The command is idempotent and preserves administrator-managed policies. Use
`--retry-failed` only during an explicit maintenance action because it performs
new outbound diagnostics for credentialless Providers.

## Locked runtime

The production override runs backend processes as UID/GID `10001`, drops Linux capabilities, makes the root filesystem read-only, constrains memory/CPU, and mounts the backend environment file read-only:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  -f docker-compose.observability.yml \
  up -d --build
```

Prepare both backend secrets before the first locked start because the non-root containers cannot generate or persist them. Run migrations through the bundled migration service; do not run migrations concurrently from API workers.

## Redis cache and task durability

`redis` is a disposable cache with `allkeys-lru`. Internal schedules, Runs, leases, progress, events, audit records, and domain references are stored in PostgreSQL; no durable task queue Redis exists. Back up PostgreSQL and monitor Task Scheduler/Task executor health through the worker heartbeat and Task Management APIs. Cache data does not require backup.

Keep PostgreSQL and Redis ports on their default loopback bindings. Public access should terminate at a TLS reverse proxy in front of the frontend and backend only.

## Unified routing cutover

Follow `docs/DATA_ROUTING_CUTOVER_RUNBOOK.md`. Activation is a coordinated,
forward-only cutover: all mandatory server-owned gates must pass, the whole
application enters maintenance, workers drain at safe boundaries, and the
unique target-version cutover record is activated once. There is no per-domain
legacy switch, administrator gate bypass, post-activation abort, or deployment
rollback to old routing code.
