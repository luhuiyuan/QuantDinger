# Unified External Data Routing — Regression Preflight Report

**Status:** NO-GO (baseline analysis in progress)  
**Date:** 2026-08-04  
**Scope:** Regression from pre-transform commit `894322e` through unified-routing commit `7802dab` and bootstrap `618ee48`.

## Executive finding

The change was marked complete without an executable, complete, real-request preflight proving that each migrated business Calling Feature still works through its routed Provider policy. The live control plane has disabled and unverified capabilities, while routed requests have failed in production. It is therefore not eligible for a forward-only cutover.

## Evidence sources

- OpenSpec proposal/design/tasks for `unify-external-data-source-operations`.
- Runtime mapping: `app/services/data_routing/gateway.py`.
- Inventory: `backend_api_python/data_routing_artifacts/EXTERNAL_DATA_OUTBOUND_INVENTORY.json`.
- Request fixtures: `backend_api_python/tests/fixtures/routed_calling_feature_contracts.json`.
- Live PostgreSQL control-plane and routed-request records, read on 2026-08-04.

## Coverage gap (blocking)

| Evidence | Observed state | Consequence |
|---|---:|---|
| Runtime Calling Feature → Capability mappings | 34 | Runtime truth |
| Business request fixtures | 30 | 4 mapped features have no preflight request contract |
| Inventory in-scope paths | 30 | Every entry is marked `routed`, but its `calling_feature`, `capability_key`, and `adapter_key` fields are null |

Missing request fixtures:

- `market.cn_stock.core_indices`
- `market.cn_stock.snapshot`
- `task.cn_history.daily_bars`
- `task.cn_history.provider_actions`

The inventory cannot currently prove the requirement in tasks 1.2/1.5/20.7: each in-scope source path must map to an executable Calling Feature and capability.

## Completed missing business-request contracts

The following contracts were derived from the actual Calling Feature code, not from
an admin diagnostic. They are the four entries absent from the existing fixture
file; this report records them as preflight inputs only and does **not** alter the
runtime fixtures or application behavior.

| Calling Feature | Capability | Owner/source path | Mode / timeout | Exact subject | Constraints | Minimum successful result contract |
|---|---|---|---|---|---|---|
| `market.cn_stock.snapshot` | `cn_market_snapshot` | `app/services/market/cn_stock_market.py:fetch_cn_market_snapshot` | interactive / 12 s | `{"market":"CNStock","subject":"all"}` | `{"market":"CNStock"}` | object with a non-empty `rows` collection; each accepted row is later normalized as a CN A-share snapshot |
| `market.cn_stock.core_indices` | `cn_hk_quote` | `app/services/market/cn_stock_market.py:fetch_core_indices` | interactive / 12 s | `{"market":"CNStock","symbols":["sh000001","sz399001","sz399006"],"index_definitions":[["000001.SH","上证指数","sh000001"],["399001.SZ","深证成指","sz399001"],["399006.SZ","创业板指","sz399006"]]}` | `{"market":"CNStock"}` | list (the caller preserves it as a list for the index view) |
| `task.cn_history.daily_bars` | `cn_equity_history` | `app/services/cn_market_history/routed_tdx_provider.py:iter_daily_pages` | background pinned stream / 30 s | `{"operation":"daily_page","instrument":"SH:600000","start_date":"2026-07-01","end_date":"2026-07-31","start_offset":0}` | `{"market":"CN","adjustment":"raw"}` | a `DailyBarPage` object; a valid page has the page protocol used by the iterator (`reached_start`, `next_offset`, and bars) |
| `task.cn_history.provider_actions` | `cn_corporate_actions` | `app/services/cn_market_history/routed_tdx_provider.py:fetch_corporate_actions` | background pinned stream / 30 s | `{"operation":"corporate_actions","instrument":"SH:600000"}` | `{"market":"CN"}` | list (empty is a domain-valid result only if the Provider explicitly returns a successful empty action set) |

For the history features, the preflight must first pin the capability stream as the
business service does; it must not call `execute` as an interactive substitute.
The fixed July 2026 date window is bounded and avoids relying on an in-progress
current trading session.

## Live control-plane findings (blocking)

| Condition | Observed state | Severity |
|---|---|---|
| Disabled capability policies | Multiple, including CN history, CN snapshot, US stock, FX, futures, macro and commodities | P0 |
| Capability eligibility | Unverified entries remain, including a policy entry for `market_catalog` and `symbol_master` | P0 |
| Effective policies | 17 effective policies; all other registered capabilities are disabled | P0 |
| 24h routed outcomes | `cn_hk_quote`: 2,948 failed; `economic_calendar`: 4 failed | P0 |

A disabled policy with a reason is not evidence that its dependent business feature remains available. The current cutover evaluator accepts this state, which conflicts with the design requirement for full capability preflight.

## Confirmed implementation regressions

1. **Quota schema mismatch:** catalog adapters use top-level `unknown_limit_safety_budget`, whereas quota reservation required bucket-local `unknown_safety_limit`. This produced `quota_model_invalid` before a Provider request was attempted.
2. **Eligibility/bootstrap mismatch:** active credentials/instances did not imply every declared capability was verified, but bootstrap treated them as ready enough to preserve disabled policies.
3. **Diagnostic/request-contract mismatch:** generic diagnostics can call a K-line provider without the Calling Feature's required symbol/timeframe, producing a false `empty_provider_result` result.
4. **Gate weakness:** capability existence, an explicit disable reason, and mocked matrix evidence can pass the cutover evidence shape without a successful real business request.

## Full real routed preflight — 2026-08-04

**Execution:** 34/34 runtime Calling Features, one Router request at a time, from the running `quantdinger-backend` container. Interactive samples used a 12-second Router deadline; the two history stream samples were pinned as their business service does and used 30 seconds. No policy, credential, Provider instance, default bootstrap, or application configuration was changed.

**Outcome:** 6 Provider-transport accepted; 5 accepted from fresh Router cache (**not external-transport proof**); 18 failed; 5 blocked. Therefore **28/34 features lack the required direct successful Provider evidence** and the global decision remains **NO-GO**.

**Baseline caveat:** the running container/worktree contains the earlier uncommitted routing-code edits listed in this report. These results are live control-plane evidence only; they are not proof that commit `7802dab` was correct, nor a comparison against `894322e`.

| # | Calling Feature → Capability | Owner/source path | Mode / sample contract | Router evidence (request ID / Provider trace) | Result shape or failure | Verdict |
|---:|---|---|---|---|---|---|
| 1 | `analysis.sentiment.adanos` → `us_equity_sentiment` | `app/data_providers/adanos_sentiment.py` | interactive / 12s<br>`{"tickers":["AAPL"]}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 2 | `market.commodities.overview` → `commodity_quote` | `app/data_providers/commodities.py` | interactive / 12s<br>`{"operation":"overview"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 3 | `market.crypto.overview` → `crypto_market_snapshot` | `app/data_providers/crypto.py` | interactive / 12s<br>`{"operation":"overview"}`<br>constraints `{}` | `4c390622-096c-4abf-ae8f-508e79990951`<br>Coingecko / coingecko:accepted | array; n=30 | **PASS—transport accepted** |
| 4 | `market.economic_calendar` → `economic_calendar` | `app/data_providers/economic_calendar.py` | interactive / 12s<br>`{"operation":"calendar_payload"}`<br>constraints `{}` | `5afb63a8-4443-473b-a7fb-d3382105444c`<br>Akshare / fresh cache | object; n=5 | **PASS—cached only** |
| 5 | `market.forex.overview` → `forex_quote` | `app/data_providers/forex.py` | interactive / 12s<br>`{"operation":"overview"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 6 | `market.global_heatmap` → `global_heatmap` | `app/data_providers/heatmap.py` | interactive / 12s<br>`{"operation":"heatmap"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 7 | `market.indices.overview` → `market_index_quote` | `app/data_providers/indices.py` | interactive / 12s<br>`{"operation":"overview"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 8 | `market.macro.series` → `macro_series` | `app/data_providers/macro_series.py` | interactive / 12s<br>`{"operation":"fred_series","series_id":"DGS10"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 9 | `analysis.opportunities` → `equity_opportunity` | `app/services/global_market_data.py` | interactive / 12s<br>`{"operation":"aggregate"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 10 | `analysis.market_sentiment` → `market_sentiment` | `app/services/market_data_collector.py`<br>`app/services/global_market_data.py` | interactive / 12s<br>`{"operation":"aggregate"}`<br>constraints `{}` | `7e970b26-0475-498d-9872-96d0e3a718f6`<br>Cnn Fear Greed / fresh cache | object; n=2 | **PASS—cached only** |
| 11 | `market.asia_stock.kline` → `asia_equity_kline` | `app/data_sources/hk_stock.py`<br>`app/data_sources/cn_stock.py` | interactive / 12s<br>`{"symbol":"SH600000","timeframe":"1D"}`<br>constraints `{}` | `682b82ad-e42f-4630-8afc-b8d5bded1a84`<br>Tencent / fresh cache | array; n=301 | **PASS—cached only** |
| 12 | `task.cn_fundamental_history.eastmoney` → `cn_fundamental_history` | `app/services/cn_fundamental_history/routed_provider.py` | interactive / 12s<br>`{"symbol":"600000"}`<br>constraints `{}` | `47e2d79d-c60c-445f-8477-cabc8af3483f`<br>eastmoney:deadline_exceeded | `routed_data_unavailable` — No eligible Provider returned data that passed hard quality gates | **BLOCKED** |
| 13 | `market.cn_hk.fundamentals` → `cn_hk_fundamentals` | `app/services/market_data_collector.py` | interactive / 12s<br>`{"market":"CNStock","symbol":"600000"}`<br>constraints `{}` | `532d986d-eec7-4cb4-835e-32db82115f4b`<br>akshare:deadline_exceeded | `routed_data_unavailable` — No eligible Provider returned data that passed hard quality gates | **BLOCKED** |
| 14 | `task.cn_fundamental_history.cninfo_verification` → `cn_corporate_announcement` | Not located by literal reference; runtime mapping only | interactive / 12s<br>`{"symbol":"600000"}`<br>constraints `{}` | `2089ec1c-e1e8-4ac4-be8e-1f5841bd8e12`<br>cninfo:provider_error | `routed_data_unavailable` — No eligible Provider returned data that passed hard quality gates | **BLOCKED** |
| 15 | `market.crypto.kline` → `crypto_public_market_data` | `app/data_sources/crypto.py` | interactive / 12s<br>`{"operation":"kline","symbol":"BTC/USDT"}`<br>constraints `{}` | `3c973d71-2258-4162-82de-7c57747924f6`<br>Ccxt Public Market / fresh cache | array; n=300 | **PASS—cached only** |
| 16 | `market.forex.quote_kline` → `forex_market_data` | `app/data_sources/forex.py` | interactive / 12s<br>`{"operation":"ticker","symbol":"EURUSD"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 17 | `market.futures.quote_kline` → `futures_market_data` | `app/data_sources/futures.py` | interactive / 12s<br>`{"operation":"ticker","symbol":"GC"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 18 | `market.moex.quote_kline` → `moex_market_data` | Not located by literal reference; runtime mapping only | interactive / 12s<br>`{"operation":"ticker","symbol":"SBER"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 19 | `market.cn_hk.quote_snapshot` → `cn_hk_quote` | `app/data_sources/hk_stock.py`<br>`app/data_sources/cn_stock.py` | interactive / 12s<br>`{"market":"CNStock","symbol":"600000"}`<br>constraints `{}` | `d2175f50-a920-4a55-98b9-e2fb0cdfd5ed`<br>Tencent / tencent:accepted | object; n=10 | **PASS—transport accepted** |
| 20 | `market.us_stock.quote_kline` → `us_equity_market_data` | `app/data_sources/us_stock.py` | interactive / 12s<br>`{"operation":"ticker","symbol":"AAPL"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 21 | `ai_chat.macro_nonfarm_context` → `us_macro_release` | `app/routes/ai_chat.py` | interactive / 12s<br>`{"indicator":"US_NONFARM_PAYROLLS"}`<br>constraints `{}` | `45de8fe4-869e-4866-970f-163868d83d39`<br>akshare:deadline_exceeded | `routed_data_unavailable` — No eligible Provider returned data that passed hard quality gates | **BLOCKED** |
| 22 | `quick_trade.public_price_conversion` → `crypto_public_quote` | `app/routes/quick_trade.py` | interactive / 12s<br>`{"operation":"ticker","symbol":"BTC/USDT"}`<br>constraints `{}` | `4d337d0e-04f9-4208-8a8c-b9070493e201`<br>Bybit Public / bybit_public:accepted | object; n=2 | **PASS—transport accepted** |
| 23 | `admin.data_source_connection_test` → `us_equity_quote` | `app/routes/settings.py` | interactive / 12s<br>`{"operation":"ticker","symbol":"AAPL"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 24 | `task.cn_history.provider_actions` → `cn_corporate_actions` | `app/services/cn_market_history/routed_tdx_provider.py` | background / 30s<br>`{"operation":"corporate_actions","instrument":"SH:600000"}`<br>constraints `{"market":"CN"}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 25 | `task.cn_history.official_adjustments` → `cn_official_adjustment_reference` | `app/services/cn_market_history/routed_official_adjustments.py` | interactive / 12s<br>`{"symbol":"600000"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 26 | `task.cn_history.daily_bars` → `cn_equity_history` | `app/services/cn_market_history/routed_tdx_provider.py` | background / 30s<br>`{"operation":"daily_page","instrument":"SH:600000","start_date":"2026-07-01","end_date":"2026-07-31","start_offset":0}`<br>constraints `{"market":"CN","adjustment":"raw"}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 27 | `analysis.us_fundamentals` → `us_fundamentals` | `app/services/market_data_collector.py` | interactive / 12s<br>`{"symbol":"AAPL"}`<br>constraints `{}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 28 | `market.cn_stock.snapshot` → `cn_market_snapshot` | `app/services/market/cn_stock_market.py` | interactive / 12s<br>`{"market":"CNStock","subject":"all"}`<br>constraints `{"market":"CNStock"}` | `—`<br>no attempt | `data_routing_not_ready` — Data Capability has no effective routing revision | **FAIL** |
| 29 | `market.cn_stock.core_indices` → `cn_hk_quote` | `app/services/market/cn_stock_market.py` | interactive / 12s<br>`{"market":"CNStock","symbols":["sh000001","sz399001","sz399006"],"index_definitions":[["000001.SH","上证指数","sh000001"],["399001.SZ","深证成指","sz399001"],["399006.SZ","创业板指","sz399006"]]}`<br>constraints `{"market":"CNStock"}` | `81cdb89c-0047-49d8-b4c1-e8c330a745be`<br>Tencent / tencent:accepted | array; n=3 | **PASS—transport accepted** |
| 30 | `market.symbol_search` → `market_catalog` | `app/routes/market.py`<br>`app/services/symbol_master_sync.py` | interactive / 12s<br>`{"market":"USStock","query":"Apple"}`<br>constraints `{}` | `bd120c31-b452-4580-bd49-bb59bca7a727`<br>akshare:capability_ineligible; ccxt_public_market:quality_gate_failed | `routed_data_unavailable` — No eligible Provider returned data that passed hard quality gates | **BLOCKED** |
| 31 | `analysis.market_data_collection` → `global_market_overview` | `app/services/market_data_collector.py` | interactive / 12s<br>`{"operation":"overview","symbol":"BTC"}`<br>constraints `{}` | `d88e5132-d3e8-419f-af0f-9184b2107eac`<br>Coingecko / coingecko:accepted | object; n=3 | **PASS—transport accepted** |
| 32 | `analysis.news_search` → `analysis_search` | `app/services/search.py`<br>`app/data_providers/news.py` | interactive / 12s<br>`{"operation":"search","query":"market"}`<br>constraints `{}` | `0e4f14a0-a918-4901-bcaa-34b42c8953c3`<br>Gdelt / fresh cache | object; n=2 | **PASS—cached only** |
| 33 | `task.symbol_master_sync` → `symbol_master` | `app/services/symbol_master_sync.py`<br>`app/services/market_catalog_sync.py` | interactive / 12s<br>`{"operation":"full_sync","market":"USStock"}`<br>constraints `{}` | `aa505a28-c762-4c7e-9f5b-0fe6945e87be`<br>Nasdaq Trader / akshare:skipped; nasdaq_trader:accepted | array; n=13056 | **PASS—transport accepted** |
| 34 | `market.symbol_name` → `symbol_reference` | `app/services/symbol_name.py` | interactive / 12s<br>`{"market":"USStock","symbol":"AAPL"}`<br>constraints `{}` | `4d544997-9ac3-4cf2-b640-991229c5e4a0`<br>Tencent / fresh cache | string; n=0 (violates non-empty-name contract) | **FAIL** |

### Failure clusters established by the run

- **No effective routing revision (18):** the request was rejected before Provider selection. This affects sentiment, commodities, forex, heatmap, indices, macro series, opportunities, forex/futures/MOEX/US quote paths, the admin connection test, all three CN-history paths, US fundamentals, and CN market snapshot. This is a direct cutover regression/control-plane failure, not a Provider outage.

- **Eligible route still cannot deliver (5):** CN fundamental history, CN/HK fundamentals, CNINFO verification, macro nonfarm context, and symbol search reached a policy but ended in `routed_data_unavailable`. Four cases contain deadline/provider failures; `market.symbol_search` specifically shows an ineligible AkShare first choice followed by CCXT `capability:empty_provider_result`.

- **Contract violation despite accepted route (1):** `market.symbol_name` accepted Tencent but returned an empty string, violating the Calling Feature’s non-empty name contract.

- **Deadline enforcement defect:** two nominal 12-second calls exceeded it materially: CN fundamental history 13.321 s, CN/HK fundamentals 15.044 s, and macro nonfarm 68.097 s. Router deadline is not an end-to-end hard cancellation boundary for those transports; that needs a separate design/fix, not an increased preflight timeout.

- **Direct Provider evidence exists only for 6 features:** crypto overview (Coingecko), CN/HK quote snapshot (Tencent), public price conversion (Bybit), CN core indices (Tencent), market-data collection (Coingecko), and symbol-master sync (Nasdaq Trader after AkShare was skipped). The remaining five successful statuses were fresh-cache returns and must be re-run with an explicit, safe cache-bypass/invalidation test design before they can satisfy the design’s “real request” gate.


## Required preflight report phases

1. **Contract completion:** derive/approve a request fixture for every runtime Calling Feature; attach owner module, capability, expected result shape, acceptable domain error and a bounded timeout.
2. **Baseline comparison:** execute the same samples against the pre-transform behavior in an isolated environment and retain sanitized result fingerprints.
3. **Routed real preflight:** execute each sample through the current Router, one request at a time, recording selected Provider, fallback chain, quota decision, normalized result fingerprint, quality outcome and routed request id.
4. **Decision:** a feature is PASS only when routed behavior meets its old business contract. Disabled, unverified, skipped, empty, mock-only or contract-incompatible results are FAIL. Any FAIL is global NO-GO.

## Safety rules for the next phase

- No policy auto-publish/disable, credential rotation, bootstrap, or Provider configuration mutation.
- No empty generic diagnostics used as business evidence.
- External calls are single-threaded, bounded by the existing per-feature timeout, and recorded with sanitized evidence only.
- Current uncommitted code changes are explicitly excluded from the transformed baseline until separately reviewed.

## A-share full-market snapshot remediation scope (2026-08-04)

`cn_market_snapshot` must not be modeled as an AkShare-only capability. The
implementation remediation retains the distinction between Adapter support,
per-environment Instance eligibility, and published routability. Candidate
channels are Eastmoney direct and AkShare-Eastmoney (the same `eastmoney`
upstream failure domain), Sina, Tencent batch quote, and EasyTDX batch quote.
A channel is added to the Capability only after it can produce the same
normalized full-market `rows` contract under bounded batching and quality gates;
per-environment 403, remote disconnect, or node reachability evidence only
changes that Instance's eligibility and does not erase registered support.
