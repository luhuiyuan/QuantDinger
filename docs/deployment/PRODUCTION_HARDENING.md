# Production Hardening

## Preflight

Create the project-root `.env` and `backend_api_python/.env`, then replace every default credential. Validate them before deployment:

```bash
python backend_api_python/scripts/check_production_config.py \
  --env-file .env \
  --env-file backend_api_python/.env
```

The guard rejects default database, administrator, Grafana, JWT, and credential-encryption secrets.

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
