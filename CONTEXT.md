# QuantDinger

QuantDinger provides market data, analysis, strategy operation, and trading-adjacent workflows. This context records the project-specific language used to keep those domains distinct.

## Language

### Data observability

**External Data Request**:
A request initiated by the QuantDinger backend to an external market-data, macro-data, news, or market-catalog provider. It excludes inbound client API calls, broker/exchange trading requests, and Agent audit events.
_Avoid_: API request, user request, trading request

**External Data Request Log**:
One durable record of an External Data Request and its classified outcome, including successful and failed requests in the same chronological stream. It contains a sanitized request summary only and excludes cache hits. An External Data Request has zero or one log record.
_Avoid_: normal log, error log

**Provider Attempt**:
One logical attempt by an External Data Request to obtain a dataset from one named provider. Fallback attempts are separate Provider Attempts, rather than hidden retries inside one record.
_Avoid_: HTTP packet, transport trace

## Example dialogue

**Developer**: “This External Data Request to the quote provider timed out.”  
**Domain expert**: “Create a timeout External Data Request Log for that Provider Attempt, but do not mix it with an inbound user API request or an exchange order.”
