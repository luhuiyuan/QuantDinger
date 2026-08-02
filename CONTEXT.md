# QuantDinger

QuantDinger provides market data, analysis, strategy operation, and trading-adjacent workflows. This context records the project-specific language used to keep those domains distinct.

## Language

### Data observability

**Data Source Operations Console**:
An administrator-only control surface for inspecting and operating configured external data providers, their routing policies, and their health. It is distinct from end-user data provenance and from the raw External Data Request Log stream.
_Avoid_: data-source settings form, public provider dashboard

**Data Source Settings**:
The administrator settings surface for shared non-secret transport parameters and legacy-configuration migration status. Provider Instance credentials belong to the instance lifecycle and are not edited here; this surface is reached from the Data Source Operations Console but is not the place to manage routing or observe provider health.
_Avoid_: Data Source Operations Console

**Data Capability**:
A code-defined kind of external data qualified by its market and only the additional characteristics required for provider compatibility, such as intraday versus daily K-lines or spot versus swap instruments. It is not defined by a frontend page or Calling Feature.
_Avoid_: page data type, endpoint capability, provider feature

**Provider Adapter**:
A code-registered integration contract for one external provider type. It declares supported Data Capabilities and configuration requirements but does not represent deployment credentials, health, quota, or routing priority.
_Avoid_: provider account, provider configuration, routing entry

**Provider Instance**:
A deployment-specific, administrator-managed use of one Provider Adapter with its own identity, settings, quota, health, and routing eligibility. Before activation its draft may be discarded; after activation its identity and history are preserved, so it may be disabled or permanently retired but not deleted. Multiple instances of one adapter represent distinct verified upstream accounts; the same upstream account may not back more than one non-retired instance.
_Avoid_: adapter type, API key when referring to the configured provider as a whole

**Provider Account Identity**:
A stable, non-secret upstream account or quota-owner identity established through Provider Adapter validation and used to prevent duplicate Provider Instances for the same account. When a provider cannot establish such an identity, QuantDinger cannot claim that separately entered credentials have independent quota.
_Avoid_: API key, credential ciphertext, Provider Instance ID

**Data Routing Policy**:
An administrator-managed, deployment-wide validated ordered list of Provider Instances for one Data Capability. It applies consistently to user requests, strategies, backtests, and background tasks, and determines eligible primary and fallback Provider Attempts; it never contains arbitrary endpoints, executable code, secrets, or per-user overrides.
_Avoid_: provider URL list, custom request script

**Data Eligibility Constraint**:
A non-relaxable requirement attached to a data request, such as execution venue, adjustment mode, maximum delay, or backtest eligibility. Automatic fallback may only select Provider Instances that satisfy every constraint; it does not weaken constraints to obtain some data.
_Avoid_: provider preference, fallback priority, user routing override

**Runtime Data Quality Gate**:
The minimum validity contract a provider result must pass before routing may accept it, including capability-specific structural, temporal, and semantic checks. A gate failure rejects that Provider Attempt and permits fallback; lesser quality warnings may accompany accepted data, while cross-provider reconciliation and correction remain outside this runtime gate.
_Avoid_: full data governance, provider health check, informational validation warning

**Routing Cache**:
QuantDinger's reusable copy of a previously accepted routed-data result, keyed by the request characteristics that determine its meaning and retaining its original provider provenance and acquisition time. Freshness and stale eligibility are evaluated against the current request; managed historical or backtest datasets and provider-side caches are not Routing Cache entries.
_Avoid_: historical data store, backtest dataset, provider CDN cache

**Data Request Mode**:
The execution posture of a routed data request. An interactive request permits only a short quota wait before cache use or fallback, while a background batch may wait or defer within a bounded task budget to preserve source consistency; neither mode permits unbounded retries.
_Avoid_: Data Capability, Calling Feature, provider priority

**Provider Health**:
The routing-relevant availability state of a Provider Instance, represented at both instance scope and instance-plus-capability scope. Capability failures normally affect only that capability; transport, authentication, account, or provider-wide failures may affect the whole instance.
_Avoid_: one undifferentiated provider status, individual request result, data quality score

**Provider Capability Test**:
An administrator-triggered diagnostic of one Provider Instance and one Data Capability that bypasses routing and fallback while preserving normal credentials, quotas, normalization, quality gates, sanitization, and logging. Its result is recorded separately and does not directly alter production circuit state; recovery probing is an explicit operation.
_Avoid_: routed data request, automatic health probe, unrestricted provider call

**Routed Data Request**:
One request to QuantDinger's data routing capability for a dataset under a Data Capability, Data Eligibility Constraints, and Data Request Mode. It owns the complete routing outcome and contains zero or more ordered Provider Attempts.
_Avoid_: inbound API request, individual provider call, trading request

**Routed Data Request ID**:
The immutable identifier shared by the routing summary and every Provider Attempt in that fallback chain. It is the preferred reference for explaining why a particular provider or cache result was selected.
_Avoid_: External Data Request Log ID, provider request ID

**External Data Request**:
A logical outbound request initiated by the QuantDinger backend to an external market-data, macro-data, news, or market-catalog provider. It belongs to one Provider Attempt and excludes inbound client API calls, broker/exchange trading requests, and Agent audit events.
_Avoid_: API request, user request, trading request

**External Data Request Log**:
One durable record of a Provider Attempt and its classified outcome, including successful, failed, disabled, and skipped attempts in the same chronological stream. It contains a sanitized summary only and excludes cache hits; attempts that perform outbound work correspond to one logical External Data Request.
_Avoid_: normal log, error log

**External Data Request Log ID**:
The immutable, unique identifier of one External Data Request Log. It is the primary reference a user supplies when reporting an external-data issue.
_Avoid_: request ID, provider request ID

**Calling Feature**:
The user-facing QuantDinger feature that initiated an External Data Request, stored as a stable feature code and presented in the current UI language. It is distinct from the provider adapter, internal module, and inbound API route.
_Avoid_: call source when referring to a technical module, provider name

**Provider Attempt**:
One ordered attempt within a Routed Data Request to obtain a dataset from one Provider Instance. Fallback attempts are separate Provider Attempts, while bounded transport retries remain summarized inside the attempt.
_Avoid_: HTTP packet, transport trace

### Task execution

**Task Scheduler**:
A QuantDinger capability that determines when a finite background Task Run
should become eligible, including recurring Cron schedules and administrator
controls. It is distinct from the mechanism that executes a Task Run.
_Avoid_: Domain Scheduler, timer thread

**Domain Scheduler**:
The long-lived process that owns portfolio monitoring, payment scans, signal
alerts, and other domain loops requiring renewable ownership and controlled
shutdown. It is not the owner of finite background Task Runs.
_Avoid_: finite Task Scheduler, short-lived Task Run

**Task Run**:
One durable execution instance of a finite background task, with an explicit
lifecycle, progress, retry history, and operator-visible outcome.
_Avoid_: thread, cron entry, transient callback

**Task Definition**:
The code-registered contract for a finite background task, including its
validated parameter schema, execution capabilities, version, and domain
integration. Database records materialize this contract but do not redefine it.
_Avoid_: arbitrary job, shell command, database-only task

**Exclusivity Key**:
A registered-task-generated identity that limits how many active Task Runs may
coexist. It may be global, scoped to a domain object, or scoped to a user; it
is not user-entered free text.
_Avoid_: queue name, priority, task key

**Task Event**:
A sanitized, structured, operator-visible record of a Task Run lifecycle,
progress, retry, cancellation, or error transition. Raw process output and
secrets are not Task Events.
_Avoid_: stdout, stack dump, provider response body

### Candidate selection

**Candidate Pool Rule**:
A versioned, structured set of deterministic eligibility and financial-quality conditions that selects a Candidate Pool from an investment universe. Its human-readable expression is a presentation of the structured rule, not executable user-supplied code.
_Avoid_: strategy script, executable expression

**Initial A-share Scope**:
The first Candidate Pool universe: active ordinary shares listed on the Shanghai or Shenzhen stock exchanges. Beijing Stock Exchange instruments are outside this initial scope.
_Avoid_: all China-listed securities, full A-share market when referring to the first scope

**Fundamental History**:
Point-in-time financial observations for an instrument, with the reporting period, public availability date, source, and data version retained separately. The first scope backfills every available annual observation for Shanghai and Shenzhen instruments, including delisted instruments; the current Candidate Pool separately filters to active instruments. Every received revision and its source evidence is retained rather than overwritten. It is the evidence base for historical Candidate Pool evaluation.
_Avoid_: latest fundamentals, current snapshot

**Fundamental Data Quality State**:
The evaluation state of an instrument's required Fundamental History for a Candidate Pool Rule: eligible evidence, rule-failing evidence, or insufficient/unverified evidence. Insufficient or unverified evidence is distinct from a rule failure and never silently admits an instrument to the Candidate Pool.
_Avoid_: data failure when referring to a failed quality rule

**Standardized Fundamental Metric**:
A versioned metric calculated by QuantDinger from retained raw Fundamental History according to an explicit formula. In the first quality screen, long-term gross margin and long-term net margin are each the simple average of the latest five complete annual periods; profit and equity metrics use the parent-attributable amount. Provider-calculated ratios are retained as source evidence and quality checks, but do not directly decide Candidate Pool membership.
_Avoid_: provider metric as the canonical rule input

**Fundamental Verification Target**:
An instrument selected for official-source verification after its all-market primary-source screen. In the first scope, targets are preliminary passes and instruments within the default 10% relative boundary of a Candidate Pool Rule threshold; a rule may override that boundary. Unverified targets do not enter the formal Candidate Pool.
_Avoid_: all-market verification, informal spot check

**Fundamental Source Reconciliation**:
The comparison of primary-source and official-source observations using matched reporting period and standardized field mappings. In the first scope, a difference above 1% is recorded as a warning; a difference above 5% blocks formal Candidate Pool admission until resolved.
_Avoid_: replacing a source without recording the difference

**Fundamental Incremental Sync**:
The daily process that collects new annual disclosures and source revisions, recalculates standardized metrics, and creates Fundamental Verification Targets. It runs at 20:30 in the Asia/Shanghai timezone and never overlaps a historical backfill.
_Avoid_: on-demand candidate calculation, concurrent history sync

**Fundamental Historical Backfill**:
An administrator-started, sequential import of available annual Fundamental
History. It is checkpointed at instrument level and may be safely cancelled,
retried as a new Task Run, and inspected. It does not pause or resume in place,
and deployment must not silently restart it.
_Avoid_: deployment-time bulk import, untracked batch job, in-place resume

**Fundamental Source Evidence**:
The retained structured source fields, source identifiers, request context, announcement reference, and content hash that support a Fundamental History version. The first scope stores this evidence in PostgreSQL but does not bulk-archive source PDF files.
_Avoid_: unreferenced source link, bulk local PDF archive

**Share-count Quality Rule**:
The Candidate Pool Rule that measures five-year total-share growth. In the first Candidate Pool version, growth above the configured threshold is a rule failure; merger-driven dilution is not an exemption.
_Avoid_: presumed merger exemption, automatic waiver

**Industry Classification**:
A versioned, effective-dated classification of an A-share instrument. The China Securities Regulatory Commission industry taxonomy is the primary classification for Candidate Pool Rules; provider-specific labels, including Eastmoney labels, are retained as mappings and verification evidence rather than used as the canonical rule input.
_Avoid_: current industry text, provider industry name

**Industry Rule Exemption**:
A rule-specific treatment based on the primary Industry Classification. In the first quality screen, only banks and insurers skip the interest-coverage rule; all other rules still apply, and no other industry exemption is implied.
_Avoid_: whole-financial-sector exemption, unclassified exemption

**Interest-Coverage Data Requirement**:
The raw annual EBIT and separately mapped interest-expense inputs required for the interest-coverage Candidate Pool Rule. For a non-exempt instrument, a missing interest-expense input is insufficient evidence rather than a financial-expense proxy or an implicit pass.
_Avoid_: finance-expense approximation, omitted coverage check

**Free-Cash-Flow Quality Metric**:
The standardized annual free cash flow used by the first quality screen: net cash generated by operating activities minus cash paid to acquire or construct fixed assets, intangible assets, and other long-term assets. The rule evaluates the sum of the latest five complete annual periods.
_Avoid_: provider free-cash-flow field, total investing cash flow

## Example dialogue

**Developer**: “This External Data Request to the quote provider timed out.”  
**Domain expert**: “Create a timeout External Data Request Log for that Provider Attempt, but do not mix it with an inbound user API request or an exchange order.”
