# External data request logs

This capability records one sanitized, logical provider attempt rather than
third-party-library HTTP packets. It never stores credentials, URL query
strings, response bodies, or stack traces.

## Current provider integration inventory

| Data domain | Provider | Operations | Notes |
| --- | --- | --- | --- |
| Macro | FRED | `series_observations` | Records disabled configuration and HTTP attempts. |
| Macro | BLS | `series` | Records the logical batch request. |
| Macro | BEA | `dataset` | Records disabled configuration and HTTP attempts. |
| Calendar | Trading Economics | `economic_calendar` | Records the primary calendar attempt. |
| Calendar | Finnhub | `economic_calendar` | Records the paid fallback attempt. |
| Calendar | AkShare WallstreetCN | `economic_calendar` | Records the free fallback attempt. |
| Market catalog | CCXT venues | `load_markets` | One log per venue and spot/swap attempt. |
| Market catalog | OKX official API | `load_instruments` | Explicit fallback after an OKX CCXT failure. |
| Quotes | Twelve Data / yfinance / Tiingo | `forex_pairs`, `commodities` | Records each provider tier and its fallback order. |
| K-line | yfinance | `history` | Records the CN/HK K-line fallback, including retries. |
| News | Search service | `financial_news_search` | Records each language/query attempt without storing the query text. |

Further provider adapters are intentionally added at explicit fallback
boundaries. Do not add a global HTTP monkey patch: it loses provider and
fallback semantics and would capture unrelated traffic.

## Retention

`success`, `disabled`, and `skipped` default to 30 days. Other result classes
default to 90 days. The registered `external_data_request_log_cleanup` Task Definition deletes in bounded batches.
