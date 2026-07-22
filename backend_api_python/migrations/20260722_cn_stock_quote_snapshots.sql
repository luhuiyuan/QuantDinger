-- Persistent latest display quote for Shanghai/Shenzhen A-shares.
-- Safe to re-run; no append-only intraday history is created.

CREATE TABLE IF NOT EXISTS qd_cn_stock_quote_snapshots (
    instrument VARCHAR(32) PRIMARY KEY,
    market VARCHAR(16) NOT NULL DEFAULT 'CNStock',
    symbol VARCHAR(16) NOT NULL,
    code VARCHAR(8) NOT NULL,
    exchange VARCHAR(4) NOT NULL CHECK (exchange IN ('SH', 'SZ')),
    latest DECIMAL(20, 6) NOT NULL,
    previous_close DECIMAL(20, 6),
    change_value DECIMAL(20, 6),
    change_percent DECIMAL(16, 6),
    open_price DECIMAL(20, 6),
    high_price DECIMAL(20, 6),
    low_price DECIMAL(20, 6),
    volume DECIMAL(28, 4),
    amount DECIMAL(28, 4),
    quote_time TIMESTAMPTZ NOT NULL,
    source VARCHAR(48) NOT NULL,
    refresh_run_id VARCHAR(40) NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cn_quote_symbol ON qd_cn_stock_quote_snapshots(symbol);
CREATE INDEX IF NOT EXISTS idx_cn_quote_change ON qd_cn_stock_quote_snapshots(exchange, change_percent DESC NULLS LAST, code);
CREATE INDEX IF NOT EXISTS idx_cn_quote_volume ON qd_cn_stock_quote_snapshots(exchange, volume DESC NULLS LAST, code);
CREATE INDEX IF NOT EXISTS idx_cn_quote_amount ON qd_cn_stock_quote_snapshots(exchange, amount DESC NULLS LAST, code);
CREATE INDEX IF NOT EXISTS idx_cn_quote_time ON qd_cn_stock_quote_snapshots(quote_time DESC);

CREATE TABLE IF NOT EXISTS qd_cn_stock_quote_refresh_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL UNIQUE,
    trigger_kind VARCHAR(24) NOT NULL DEFAULT 'scheduled',
    status VARCHAR(20) NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'skipped')),
    source VARCHAR(32) NOT NULL DEFAULT '',
    planned_symbols INTEGER NOT NULL DEFAULT 0,
    succeeded_symbols INTEGER NOT NULL DEFAULT 0,
    failed_symbols INTEGER NOT NULL DEFAULT 0,
    missing_symbols INTEGER NOT NULL DEFAULT 0,
    skip_reason VARCHAR(64) NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cn_quote_runs_started ON qd_cn_stock_quote_refresh_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cn_quote_runs_status ON qd_cn_stock_quote_refresh_runs(status, started_at DESC);
