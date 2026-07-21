# China A-Share History Rollback

The China A-share history feature is disabled by default and is controlled by
two independent runtime switches:

```dotenv
CN_HISTORY_ENABLED=false
CN_HISTORY_SYNC_ENABLED=false
```

## Non-Destructive Rollback

1. Set both switches to `false`.
2. Stop or revoke pending `cn_market_history` Celery tasks.
3. Restart only the backend and Celery services that load the changed config.
4. Confirm Strategy API V2 no longer selects the local CN history path.
5. Leave all `qd_cn_*` tables in place for audit and a later retry.

Disabling the feature does not modify existing market-data, strategy, or
backtest tables. The dated migration and the matching section in `init.sql`
use additive `CREATE TABLE IF NOT EXISTS` statements.

## Schema Removal

Do not drop history tables as part of a normal application rollback. Removing
them deletes collected market data, quality evidence, synchronization audit,
and provenance needed to explain prior backtests.

If permanent removal is explicitly approved, back up the `qd_cn_*` tables,
stop every writer, and drop them in reverse foreign-key order. Shared project
data, Docker caches, PostgreSQL volumes, and unrelated tables must not be
deleted.

## Verification Record

On 2026-07-20, `migrations/20260720_cn_market_history.sql` executed
successfully against the existing PostgreSQL database inside a transaction and
was rolled back. This validated the forward DDL without changing the live
schema.
