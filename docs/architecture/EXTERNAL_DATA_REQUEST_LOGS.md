# External data request logs

This capability records a Routed Data Request summary and its ordered,
sanitized Provider Attempts rather than third-party-library HTTP packets. It
never stores credentials, URL query strings, response bodies, or stack traces.

## Record model

- One Routed Data Request has a stable request ID, Calling Feature, Capability,
  pinned policy revision, final cache/provider/failure selection, freshness,
  warning count, and sanitized explanation.
- Each policy entry produces an ordered Attempt record, including disabled,
  quarantined, circuit-open, constraint-ineligible, quota-exhausted, quality
  rejected, failed, and successful outcomes.
- Bounded retries to the same provider are summarized on one Attempt. They are
  not represented as another Provider or another policy entry.
- Fresh or eligible stale Routing Cache hits may complete with zero outbound
  Attempts while retaining the original safe provenance.

Provider-specific helpers must not instantiate the legacy `ProviderAttempt`
context. Router observability is the single owner of routed Attempt records.
Logging remains fail-open for data delivery and emits degraded-observability
health/metrics when persistence fails; management audit remains fail-closed.

## Retention

Successful Attempt details default to 30 days. Failed, rejected, disabled, and
skipped Attempts and Routed Data Request summaries default to 90 days. The
registered cleanup Task Definition deletes records in bounded batches.

Only administrators with `data_sources:view` may browse these records. Ordinary
users may see safe provenance for their own response, but cannot query request
summaries, Attempt details, other users/background work, route order, raw
parameters, internal errors, health, quota, or credentials.
