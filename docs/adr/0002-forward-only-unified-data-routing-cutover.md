# Forward-only unified external-data routing cutover

**Status: accepted**

QuantDinger will finish and validate the unified external-data control plane,
execution plane, Provider Adapters, Data Capabilities, and every in-scope
Calling Feature before changing production routing behavior. Production will
then enter a full-application maintenance window: inbound entry points and
workers stop, active Provider Attempts and background capability streams drain
to safe boundaries, the final non-bypassable gate is evaluated, and one
cutover record activates the target version. That version contains only the
unified Data Router for in-scope data access. It has no per-domain switch,
runtime legacy fallback, or supported route back to feature-owned provider
selection.

After activation, recovery is forward-only. Operators may publish a corrected
Data Routing Policy, disable a capability, use an eligible Routing Cache entry,
quarantine a Provider Instance, or deploy an application fix. They must not
restore legacy provider/fallback code or roll the data-routing state back to an
older application release. Activation is therefore a point of no return and is
permitted only after the inventory, schema, credentials, policy, health,
preflight, background rehearsal, compatibility, and explicit Go/No-Go gates
all pass. No administrator permission bypasses a failed mandatory gate.

## Considered options

- Migrate domains gradually in production behind per-domain old/new switches.
- Keep unified and legacy routing as long-lived dual paths, with runtime
  fallback to the legacy implementation.
- Duplicate production requests as shadow traffic before gradually changing
  the primary route.
- Implement domains sequentially, validate them without changing normal
  production traffic, then stop the full application and activate one
  forward-only cutover after every mandatory gate passes (chosen).

## Consequences

The chosen approach avoids split routing truth, hidden quota consumption,
incomparable health evidence, and indefinite compatibility branches. It also
makes the release operationally high risk: an incomplete inventory, invalid
credential migration, missing policy, or broken Adapter can affect multiple
product areas at once, and rollback to the previous runtime is intentionally
not a mitigation. The implementation must therefore keep an exact
machine-checked outbound-call inventory, deploy additive schema ahead of the
maintenance window, exercise real credentials through bounded diagnostics,
rehearse non-committing background workflows, and abort before activation on
any failed gate. Once activated, incident response must remain entirely within
the new routing system and forward application releases.
