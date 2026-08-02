# Data Source Operations Requirements Checkpoint

> Status: requirements discovery in progress
> Last updated: 2026-08-01
> Purpose: persistent recovery point for the current requirements discussion. This document is not yet an approved OpenSpec or implementation plan.

## Recovery protocol

If conversational context is lost, resume from this document instead of asking the user to repeat the requirements:

1. Read the overall goal, confirmed decisions, and open decisions below.
2. Cross-check the terminology in [`../CONTEXT.md`](../CONTEXT.md).
3. Treat only items under **Confirmed decisions** as settled.
4. Resume with the single question under **Next question**.
5. After every answer, update this checkpoint immediately before asking another question.

## Decision protocol

Ask the user only about consequential product scope, compatibility, security boundaries, irreversible operations, and material operational trade-offs. Resolve low-risk, reversible, and implementation-level details using repository conventions and the recommended safe default, record those decisions here, and surface them later for review rather than interrupting the user with individual questions.

## Overall goal

Build a unified external-data provider management, routing, fallback, and observability capability for QuantDinger, together with an administrator-only **Data Source Operations Console**.

The capability should let administrators operate configured provider instances and global routing policies while ensuring that all consumers—interactive product features, strategies, backtests, and background tasks—use consistent routing behavior.

## Intended capabilities

### 1. Code-defined provider integrations

- A **Provider Adapter** represents the integration contract for one external provider type.
- Adapters declare their supported **Data Capabilities** and configuration requirements.
- Adapter types are registered by trusted application code, not created dynamically by administrators.

### 2. Configured provider instances

- A **Provider Instance** represents an actual deployment configuration of a Provider Adapter.
- An instance has its own identity, settings, quota, health, and routing eligibility.
- The model permits multiple instances of the same adapter only for distinct verified upstream accounts; when account identity cannot be verified, only one active instance is allowed for that adapter.

### 3. Global capability-based routing

- Administrators manage an ordered list of eligible Provider Instances for each Data Capability.
- Routing is deployment-wide and applies consistently to user requests, strategies, backtests, and background tasks.
- Routing does not provide per-user provider priority overrides.

### 4. Constraint-preserving fallback

- Routing may fall back after failure, timeout, rate limiting, unavailability, or rejection by a Runtime Data Quality Gate.
- Fallback must not weaken non-relaxable Data Eligibility Constraints such as market, venue, adjustment mode, maximum delay, or backtest eligibility.
- Interactive requests and background batches may use different bounded waiting behavior, but neither may retry without limits.

### 5. Health and diagnostics

- Health is represented both for a Provider Instance as a whole and for an instance serving a particular Data Capability.
- Administrators can run a direct Provider Capability Test against one instance and one capability.
- A diagnostic test bypasses routing and fallback but retains normal credentials, quota enforcement, normalization, quality gates, sanitization, and logging.
- A single diagnostic result does not directly rewrite production circuit state.

### 6. End-to-end routing observability

- One logical routed request has a stable **Routed Data Request ID**.
- Each primary or fallback selection is recorded as an ordered **Provider Attempt**.
- Under normal observability operation, tooling should explain why a provider result, cache result, or final failure was selected; a fail-open logging outage is surfaced explicitly as degraded observability rather than silently claiming a complete record.

### 7. Separation of administrative concerns

- **Data Source Operations Console**: provider instances, routing policies, health, diagnostics, and operational explanation.
- **Data Source Settings**: shared non-secret transport parameters and legacy-configuration migration status.
- **External Data Request Logs**: sanitized chronological records of provider attempts.

## Explicit non-goals

- A third-party adapter marketplace or runtime plugin loader.
- Administrator-supplied executable code, scripts, arbitrary endpoints, Shell, Python, or SQL.
- Per-user routing policies or frontend-page-specific provider priority lists.
- Relaxing data eligibility constraints merely to return some data.
- Full cross-provider reconciliation, correction, or general data-governance workflows.
- Unifying exchange/broker account or order-execution operations into this external-data routing scope; public market-data calls through the same vendor or SDK remain in scope.

## Confirmed decisions

### Provider Adapter registration

Only trusted application code may register Provider Adapters. Configuration, database records, and administrator actions may configure instances of registered adapters, but may not introduce a new adapter type or executable integration.

### Provider Instance management

Administrators create and manage Provider Instances through the Data Source Operations Console. Deployment configuration is not the routine source of Provider Instances, and no configuration-to-console ownership handoff is required. Platform-level bootstrap settings remain deployment concerns rather than Provider Instances.

### Provider Instance lifecycle

A draft Provider Instance may be discarded while it has never been activated, included in an effective routing policy, or acquired operational history. After activation, an instance may be reversibly disabled or permanently retired, but it may not be hard-deleted. Retirement removes it from future routing while preserving its identity and request, routing, health, and audit history.

### Provider credential storage

Administrators submit Provider Instance credentials through the console, and QuantDinger stores the values encrypted in the database using a dedicated credential-encryption key. Provider credentials are not routinely sourced from deployment environment variables or external Secret references, and the system does not support competing credential-source precedence. The design may reuse the existing persisted-credential cryptography boundary, subject to provider-specific schemas and strict redaction.

### Provider credential visibility

Provider credentials are write-only after submission. Neither the console nor an API may return stored plaintext, including to a super-administrator. Administrative views expose only configuration presence, last-update metadata, and safe provider-account metadata obtained independently when available. Credential rotation requires submitting replacement values and produces an audit event.

### Provider credential rotation activation

Replacement credentials are encrypted as a pending version while the currently active credentials continue serving production requests. QuantDinger validates the replacement schema and runs the Adapter-supported credential or capability test. Success atomically activates the replacement; failure leaves it non-active for correction or discard and does not interrupt production routing.

### Provider credential history

After a successful atomic rotation, QuantDinger immediately destroys the previous credential ciphertext. It retains only non-secret audit metadata such as credential version, timestamps, operator identity, and validation outcome. Historical credential plaintext or recoverable ciphertext is never retained for rollback.

### Provider credential ownership

Each Provider Instance exclusively owns one adapter-defined credential bundle. All Data Capabilities served by that instance may use the bundle, but no other instance shares its stored credential record. Credential storage, rotation, audit, disablement, and retirement remain isolated to that instance even when several distinct accounts use the same Adapter.

### Provider account uniqueness

One upstream provider account or quota owner may back at most one non-retired Provider Instance for a given Adapter. Instance validation derives a stable, non-secret Provider Account Identity when the provider supports it and rejects activation when that identity already belongs to another non-retired instance. Exact credential reuse is also rejected without persisting a reversible fingerprint. When an Adapter cannot reliably establish upstream account identity, QuantDinger permits only one active instance for that Adapter rather than pretending separately entered credentials have independent quota.

### Data Routing Policy publication

Administrators edit a Data Routing Policy in a draft revision. Before publication, the backend validates the complete ordered route, including instance existence, lifecycle state, declared capability support, and required constraints, and the console presents the affected capability and resulting primary/fallback order. An explicit publish action atomically replaces the effective revision and creates an immutable audit record; incomplete or invalid drafts never affect live requests.

### Data Routing Policy restoration

Published revisions are immutable. To restore earlier routing, an administrator selects a previous revision as the basis of a new draft. QuantDinger revalidates that draft against current Adapter capabilities and Provider Instance lifecycle states, then publishes it as a new revision. Restoration never rewinds, overwrites, or bypasses the audit history.

### Data Capability disablement

A capability may be explicitly disabled with an operator reason and audit record. An enabled policy must contain at least one valid Provider Instance; an empty ordered route is invalid rather than an implicit disablement. Disablement prevents new Provider Attempts and returns a stable `capability_disabled` outcome when no eligible cache result can satisfy the request.

### Cache use during capability disablement

Explicit capability disablement blocks outbound Provider Attempts but does not invalidate otherwise eligible cached data. Fresh cache may be returned, and stale cache may be returned only when the Data Request Mode and all Data Eligibility Constraints allow it. A consistency-sensitive request, including a backtest request, never receives cache data that fails its constraints merely because the capability is disabled.

### Routing Cache boundary

Routing Cache is QuantDinger's reusable copy of a previously accepted routed-data result. Its key includes all request characteristics that determine data meaning, and the entry retains original Provider Instance provenance, acquisition time, and quality status. A fresh entry still satisfies the current request's freshness constraints; a stale entry exceeds normal freshness but remains within an explicitly permitted fallback window. Managed historical or backtest datasets and provider-side caches are not Routing Cache entries. A cache hit performs no outbound work and may therefore produce a Routed Data Request with zero Provider Attempts.

### Interactive cache and fallback order

An interactive request first returns eligible fresh Routing Cache without an outbound attempt. Otherwise it tries validated Provider Instances in policy order within the interactive time budget. Eligible stale Routing Cache is considered only after no Provider Instance succeeds, and the response explicitly identifies the stale-cache outcome rather than presenting it as fresh provider data. This order was reconfirmed after the Routing Cache boundary was explicitly defined.

### Background batch provider consistency

For each Data Capability consumed by a logical background task, the task selects one eligible Provider Instance before substantive progress in that capability stream and remains pinned to it. A workflow that coordinates distinct capabilities—such as price history and official corporate-action references—may pin a different instance for each capability, but it may not silently mix providers within one capability's dataset/checkpoint scope. A mid-stream failure may wait, retry, or defer using the same instance; switching source requires an explicit new Provider Attempt or Task Run from a domain-safe checkpoint with the provenance change recorded.

### Provider Health failure scope

Health impact follows the failure's proven scope. Data-shape, semantic, endpoint-specific, capability quality, and capability-specific quota failures affect only the Provider Instance plus Data Capability state. Authentication failure, account suspension, account-wide quota exhaustion, provider-wide transport outage, and other failures proven to affect all declared capabilities affect the whole instance. An unknown failure starts capability-scoped and escalates only when cross-capability evidence establishes a wider impact.

### Routing circuit opening

Classified permanent failures, including invalid authentication, account suspension, permanently invalid configuration, or an explicit provider declaration that a capability is unavailable, open the circuit immediately at their proven scope. Transient transport, server, rate-limit, and quality failures open the circuit only after adapter/capability-appropriate rolling thresholds and a minimum sample count are met. The circuit transition, scope, threshold evidence, and supporting Provider Attempts are visible to administrators.

### Routing circuit recovery

Transient-failure circuits recover through dedicated low-volume probes after a bounded cooldown, not through normal user traffic. Required recovery evidence closes the circuit; failure reopens it with bounded backoff. Administrators may trigger the same controlled probe early. A circuit caused by a permanent credential, account, or configuration failure is not repeatedly probed until the underlying configuration changes. Provider Capability Tests remain diagnostic and do not directly mutate production circuit state.

### Routing circuit manual controls

Administrators may manually quarantine an instance or capability, extend an open circuit, and trigger controlled recovery probing, but may not directly mark an unhealthy circuit healthy or force production traffic through it. Re-entry requires successful recovery evidence. Urgent service restoration uses another eligible instance or an audited routing-policy publication rather than bypassing health safeguards.

### Runtime Data Quality Gate authority

Trusted code defines non-disableable universal invariants and Data Capability-specific hard gates. Provider Adapters normalize upstream output but may not weaken or bypass those gates. Administrators may select only code-declared stricter profiles or raise bounded thresholds; they may not lower requirements below the capability contract, upload validation code, or disable a hard gate.

### Runtime data-quality warnings

A hard-gate failure rejects the Provider Attempt and permits fallback. A result that passes every hard gate but carries only non-fatal warnings is accepted from the current Provider Instance and returns structured warning metadata; warnings alone do not trigger another provider request. A stricter Data Request Mode or policy profile expresses an unacceptable condition as a hard gate rather than reinterpreting a warning ad hoc. Repeated warnings remain observable and may inform policy changes or code-defined health thresholds.

### Routed-request retention

Detailed successful Provider Attempt records are retained for 30 days by default. Failed, rejected, disabled, and skipped attempt records are retained for 90 days by default, and compact Routed Data Request summaries are retained for 90 days. Retention remains bounded and configurable within safe limits. Longer-term health and quota trends use aggregate metrics rather than retaining individual request details indefinitely.

### User-visible routed-data provenance

The response to a user's own request may include its Routed Data Request ID, the selected provider's public display name, acquisition time, freshness or stale status, and non-sensitive quality warnings. Ordinary users cannot browse Routed Data Request summaries, Provider Attempt details, other users' or background tasks' requests, routing order, internal health or quota state, credentials, raw parameters, or error details. Operational investigation remains administrator-only.

### First-delivery console structure

The administrator-only Data Source Operations Console has three core tabs: **Overview**, **Provider Instances**, and **Routing Policies**. Overview presents readiness, active incidents, open circuits, fallback trends, and operator actions. Provider Instances owns lifecycle, credential submission/status/rotation, capability health and quota, diagnostics, quarantine, and recovery probes. Routing Policies owns capability routes, drafts, validation, publication, explicit disablement, and immutable revision history. Existing Data Source Settings and External Data Request Logs remain separate destinations linked from the console rather than duplicated as console tabs; Data Source Settings no longer owns per-instance credentials after migration.

### First-delivery migration scope

The first delivery migrates every external-data adapter within the agreed Data Source Operations scope into the unified Provider Adapter, Provider Instance, Data Capability, routing, health, quality, cache, and observability contracts. This is not an A/H-share-only pilot and does not leave feature-specific hard-coded provider fallback chains as accepted production paths. Implementation may proceed sequentially by domain, but the first delivery is incomplete until full in-scope coverage is verified.

### External-data scope boundary

The all-source scope includes every backend integration whose purpose is to obtain market, K-line, quote, index, fundamental, financial-statement, corporate-action, market-catalog, security-master, macroeconomic, economic-calendar, commodity, news, sentiment-input, or other analysis data. It includes public market-data calls made through exchange SDKs such as CCXT and search or library calls used to obtain these datasets. It excludes order execution, broker/exchange account and trading data, LLM or AI inference, notifications, payments, OAuth, and unrelated infrastructure calls. Classification follows the call's purpose, so one vendor may have both in-scope data and out-of-scope trading integrations.

### Production cutover

All in-scope domains are implemented and verified before production changes routing behavior. Production then performs one coordinated cutover from legacy feature-specific selection and fallback paths to unified routing. Domains do not cut over incrementally in production, and the target release contains no long-lived new/legacy dual-routing mode. The release is incomplete if any in-scope outbound data path still bypasses unified routing.

### Pre-cutover live validation

Before cutover, user-facing production traffic remains on legacy paths while administrators run bounded preflight diagnostics through every new Adapter, Provider Instance, and Data Capability contract in the real deployment. The gate combines fixture and contract tests, staging workflows, real credential validation, low-frequency Provider Capability Tests, non-committing background workflow rehearsals, and a complete outbound-call inventory. QuantDinger does not duplicate every production request as shadow traffic because that would consume quota and contaminate health observations.

### Forward-only cutover recovery

After the coordinated production cutover activates unified routing, QuantDinger does not restore the previous release, retain a runtime legacy-routing switch, or fall back to old feature-specific paths. Incidents are mitigated and repaired entirely within the new system through eligible Provider Instances, routing-policy publication, capability disablement, Routing Cache where constraints permit, and forward application fixes. The decision requires non-bypassable pre-cutover gates because activation is the point of no return.

### Non-bypassable cutover gate

The coordinated cutover requires: a complete in-scope outbound-call inventory with zero bypasses; successful contract, fixture, migration, security, and targeted domain workflow tests; successful real-deployment preflight for every enabled capability; at least one healthy eligible Provider Instance for every enabled capability; explicit audited disablement of intentionally unavailable optional capabilities; valid published routing revisions; verified credential-encryption readiness; successful non-committing background workflow rehearsals; and an explicit administrator Go/No-Go confirmation. No administrator role may force activation while a mandatory gate fails.

### Data Capability registration

Only trusted code registers stable Data Capability types and defines their market/data meaning, allowed eligibility dimensions, normalized output contract, non-disableable quality gates, Routing Cache semantics, and permitted stricter profiles. Administrators configure Provider Instances, routing, explicit disablement, and bounded stricter settings for registered capabilities, but cannot create arbitrary capability keys or redefine their semantics.

### Data Capability granularity

A separate Data Capability exists only when provider compatibility, normalized output, hard quality gates, or Routing Cache semantics materially differ. Markets and major semantic families such as intraday versus end-of-day data or spot versus swap instruments may be separate capabilities. Frontend pages, API routes, individual symbols, ordinary timeframes, and adjustment choices remain Calling Features or Data Eligibility Constraints rather than multiplying capability keys.

### Provider Instance capability eligibility

A Provider Adapter declares the maximum set of Data Capabilities it can support. Provider Instance activation validates its credential/configuration and performs capability-specific checks to establish the subset actually usable by that account, plan, region, and deployment. Administrators may disable a verified capability for an instance but may not manually grant eligibility for an unsupported or unverified capability.

### Provider Instance activation threshold

Activation requires valid Adapter configuration and at least one successfully verified Data Capability. An instance with zero verified capabilities remains a draft or validation-failed instance and cannot enter any routing policy. Activation grants routing eligibility only for the verified subset; additional capabilities become eligible only after their own verification succeeds. The instance is not required to verify every capability in the Adapter's maximum set.

### Provider Instance capability revalidation

Capability eligibility is revalidated after credential or capability-relevant configuration changes, after classified evidence of a provider plan, permission, or regional-access change, and through bounded low-frequency periodic checks. Ordinary transient availability failures affect Provider Health and circuit state rather than erasing eligibility. Confirmed permission loss removes routing eligibility for that capability until verification later succeeds.

### Provider Instance quota model

Provider Adapter code defines supported quota dimensions, measurement units, weights, and reset semantics. Administrators configure account or plan limits when the provider cannot report them, while trustworthy response metadata updates runtime observations such as remaining units and reset time. Routing uses the most conservative currently valid view when sources disagree and does not infer unlimited capacity merely because telemetry is absent.

### Unknown Provider Instance quota

When a provider limit cannot be configured or discovered, the Adapter must define a conservative safety budget for unknown-limit operation. The instance may route within that budget while the console displays an unknown or unverified quota warning. If the Adapter cannot define a safe budget, the instance is not routing-eligible until a limit becomes available. Unknown never means unlimited.

### Interactive quota exhaustion

When a known quota reset falls within the small interactive wait budget and total request deadline, routing may wait once and then attempt the instance. Otherwise it does not issue an outbound call known to exceed quota, records a skipped Provider Attempt with a quota-exhausted reason, and continues to the next eligible instance. After all providers fail or are skipped, the confirmed stale Routing Cache rule applies.

### Background batch quota planning

Provider Adapters conservatively estimate the quota cost of the next commit-safe chunk for each capability stream. Execution verifies or reserves sufficient budget before starting that chunk, remains pinned to the same Provider Instance for that stream, and waits or defers across quota resets within the bounded task budget when capacity is insufficient. A stream does not intentionally begin a chunk expected to exhaust quota before its checkpoint and does not silently switch providers.

### Data Routing Policy shape

Each Data Capability has one administrator-ordered Provider Instance list. Code-defined compatibility and request Data Eligibility Constraints filter that list at runtime. Administrators cannot author time-, symbol-, user-, feature-, or arbitrary expression-based routing branches. If filtering leaves no eligible instance, the request may use only eligible Routing Cache or fail explicitly.

### In-flight routing revision

Each Routed Data Request snapshots the effective Data Routing Policy revision at creation and uses it for the entire fallback chain. A background task records the policy revision and pinned Provider Instance for each capability stream it begins. Publishing a new revision affects only new requests and new capability streams and never changes provider order underneath in-flight work.

### Provider Instance disablement and in-flight work

Disablement or quarantine immediately prevents new Routed Data Requests and not-yet-started Provider Attempts from selecting the instance, even when their policy snapshot contains it. An already-issued outbound call may finish and is logged. A background capability stream pinned to the instance completes its current commit-safe chunk, then stops and defers or fails rather than starting another chunk or switching providers. Other independently pinned capability streams follow their own instance state. Already committed data is not automatically rolled back.

### Provider Instance retirement gate

Retirement requires the instance to be disabled, removed from every effective routing policy, free of active Provider Attempts and pinned background capability streams, and free of pending credential rotations, recovery probes, or management actions. The console lists blockers and provides no force-retire bypass. After the gate passes, retirement destroys current credential ciphertext while preserving non-secret instance identity and request, health, routing, and audit history.

### Provider Adapter removal gate

Provider Adapter code may be removed only after all of its Provider Instances are retired, all effective policies are migrated, and no related operation remains active. If a deployment lacks an Adapter still referenced by a non-retired instance or effective policy, unified data routing does not enter Ready state or accept traffic. QuantDinger does not auto-retire or silently skip the reference and preserves a non-executable metadata tombstone for historical explanation.

### Provider Adapter versioning and instance migration

Each Provider Adapter has a stable key and explicit contract/configuration version. Code supplies deterministic migrations for safely transformable non-secret settings. A change requiring new credentials or administrator judgment places the affected Provider Instance in a migration-required, non-routing state until corrected and revalidated. An effective policy referencing an unresolved instance blocks routing readiness; an unreferenced instance may await migration without blocking unrelated routes. QuantDinger does not retain multiple legacy Adapter implementations indefinitely.

### Credential-encryption key rotation

Credential encryption supports a bounded dual-key transition. A new active key encrypts all new writes while the previous key remains decrypt-only. A bounded background task decrypts existing ciphertext in memory and immediately re-encrypts it with the active key without exposing plaintext through APIs, logs, Task Events, or audit records. The previous key is removed only after every active and pending credential verifies under the new key. Missing required keys or unverifiable ciphertext prevents affected routing from becoming Ready.

### Data Source Operations permissions

Backend authorization separates operations viewing, Provider Instance lifecycle management, credential submission/rotation, routing publication, and diagnostic/recovery actions. Existing super-administrators may receive every permission by default, but APIs do not rely on one undifferentiated frontend-only administrator flag. Every mutating operation is authorized server-side and audited.

### Operator reasons and audit

Routing publication/restoration, capability disablement, manual quarantine, circuit extension, credential rotation, Provider Instance disablement/retirement, and the forward-only production cutover require an operator-supplied reason. Draft edits and read-only diagnostics are audited without mandatory prose justification. Audit records contain sanitized before/after metadata, actor, timestamp, correlation ID, reason where required, and outcome, and never contain credential plaintext or recoverable ciphertext.

### Step-up authentication

Recent password or MFA step-up is required for initial Provider credential submission, credential rotation, Data Source Operations permission assignment, Provider Instance retirement, and the irreversible production cutover. Routine routing publication, disablement, quarantine, recovery probing, and diagnostics rely on their specific backend permission plus audit unless deployment policy adopts a stricter rule.

### Existing API compatibility

The all-source routing migration preserves existing application routes, request parameters, success payload contracts, and established domain error expectations. Routed Data Request ID, safe provider provenance, freshness state, and quality warnings are added only through backward-compatible optional fields or headers. Internal provider selection may change completely, but existing frontend, strategy, backtest, Agent, and MCP consumers do not require a coordinated breaking rewrite unless an existing contract is demonstrably unsafe.

### Legacy credential import

QuantDinger provides an explicit, administrator-confirmed one-time import for supported legacy environment-variable and local-configuration credentials. After step-up confirmation, values flow directly into encrypted Provider Instance records without being displayed, and each instance/capability is validated. Audit records contain only sanitized import metadata. Required import or manual replacement failures block cutover. After cutover, legacy credential lookup is removed and operators are instructed to remove the old values from deployment configuration.

### Full-application cutover maintenance

The coordinated production cutover occurs during a bounded full-application maintenance window. New user traffic and background work stop, active operations drain, the complete non-bypassable gate is rechecked, unified routing activates and verifies, and the application resumes only on the new system. Any failure before activation aborts the maintenance attempt; after activation, recovery is forward-only.

### Operational logging failure behavior

User and background data retrieval remains fail-open when sanitized Routed Data Request or Provider Attempt operational-log persistence fails, while emitting health metrics and alerts for degraded observability. Security and management mutations—including routing publication, credential changes, Provider Instance lifecycle changes, permission changes, and production cutover—remain fail-closed when their required durable audit record cannot be written.

### Last-known-good control-plane snapshot

During a temporary configuration-database outage, an already-running process may continue for a bounded degradation window using its validated last-known-good in-memory policy, Provider Instance configuration, and credential material. Management mutations freeze and health surfaces identify stale control-plane state. A new or restarted process without a valid snapshot does not become Ready, and no legacy environment-configuration or feature-specific routing fallback is reintroduced.

## Open decisions

No blocking product decision is currently identified. Remaining reversible details—exact time budgets, polling intervals, threshold defaults, maintenance timeout, pagination, UI copy, and bounded batch sizes—are implementation defaults to be derived from existing repository conventions and verified during OpenSpec planning.

## Next question

Are the reviewed requirements ready to be consolidated into a formal OpenSpec change?

Current recommended answer: yes. Preserve this checkpoint as the reviewed discovery record, create the OpenSpec proposal/design/spec/task set for the complete all-source migration, and capture the coordinated forward-only cutover as an ADR-worthy decision.
