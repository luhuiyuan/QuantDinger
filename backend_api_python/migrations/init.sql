-- QuantDinger PostgreSQL Schema Initialization
-- This script runs automatically when PostgreSQL container starts for the first time.

-- =============================================================================
-- 1. Users & Authentication
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email VARCHAR(100) UNIQUE,
    nickname VARCHAR(50),
    avatar VARCHAR(255) DEFAULT '/avatar2.jpg',
    status VARCHAR(20) DEFAULT 'active',  -- active/disabled/pending
    role VARCHAR(20) DEFAULT 'user',       -- admin/manager/user/viewer
    credits DECIMAL(20,2) DEFAULT 0,
    vip_expires_at TIMESTAMP,              -- VIP杩囨湡鏃堕棿
    vip_plan VARCHAR(20) DEFAULT '',
    vip_is_lifetime BOOLEAN DEFAULT FALSE,
    vip_monthly_credits_last_grant TIMESTAMP,
    email_verified BOOLEAN DEFAULT FALSE,
    referred_by INTEGER,                   -- 閭€璇蜂汉ID
    notification_settings TEXT DEFAULT '',
    chart_templates TEXT DEFAULT '',      -- 鐢ㄦ埛鍥捐〃妯℃澘 JSON锛堟寚鏍囧竷灞€/鏍峰紡锛?
    timezone VARCHAR(64) DEFAULT '',
    token_version INTEGER DEFAULT 1,
    password_changed_at TIMESTAMP,           -- NULL only prompts when bootstrap password is still 123456
    last_login_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_referred_by ON qd_users(referred_by);

-- Note: Admin user is created automatically by the application on startup
-- using ADMIN_USER and ADMIN_PASSWORD from environment variables

-- =============================================================================

-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_credits_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    action VARCHAR(50) NOT NULL,            -- recharge/consume/refund/admin_adjust/vip_grant
    amount DECIMAL(20,2) NOT NULL,
    balance_after DECIMAL(20,2) NOT NULL,   -- 鍙樺姩鍚庝綑棰?
    feature VARCHAR(50) DEFAULT '',          -- 娑堣垂鐨勫姛鑳斤細ai_analysis/strategy_run/backtest 绛?
    reference_id VARCHAR(100) DEFAULT '',
    remark TEXT DEFAULT '',                  -- 澶囨敞
    operator_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_credits_log_user_id ON qd_credits_log(user_id);
CREATE INDEX IF NOT EXISTS idx_credits_log_action ON qd_credits_log(action);
CREATE INDEX IF NOT EXISTS idx_credits_log_created_at ON qd_credits_log(created_at);

-- =============================================================================

-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_membership_orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    plan VARCHAR(20) NOT NULL,             -- monthly/yearly/lifetime
    price_usd DECIMAL(10,2) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'paid',
    created_at TIMESTAMP DEFAULT NOW(),
    paid_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_membership_orders_user_id ON qd_membership_orders(user_id);

-- =============================================================================
-- 1.56. USDT Orders (multi-chain single-receiving-address + amount-suffix model)
-- =============================================================================
--
-- v3.0.6 reset: replaced xpub-derived per-order addresses with a single fixed
-- receiving address per chain. Orders are identified on-chain by a unique
-- amount suffix in the low decimals (e.g. 19.991234 -> suffix 0.001234).
-- This eliminates the consolidation step (funds land directly in the main
-- wallet) and removes per-sweep TRX/gas costs.
--
-- Supported chains: TRC20 (TRON), BEP20 (BSC), ERC20 (Ethereum), SOL (Solana SPL).
-- Each chain's address is configured via USDT_{CHAIN}_ADDRESS env var.

CREATE TABLE IF NOT EXISTS qd_usdt_orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    plan VARCHAR(20) NOT NULL,                                  -- monthly/yearly/lifetime
    chain VARCHAR(20) NOT NULL DEFAULT 'TRC20',                 -- TRC20/BEP20/ERC20/SOL
    currency VARCHAR(10) NOT NULL DEFAULT 'USDT',
    amount_usdt DECIMAL(20,8) NOT NULL DEFAULT 0,               -- final amount = base + suffix (6 dp typical)
    amount_suffix DECIMAL(20,8) NOT NULL DEFAULT 0,             -- the unique suffix portion used for matching
    address VARCHAR(120) NOT NULL DEFAULT '',                   -- fixed receiving address (per chain)
    payment_uri TEXT NOT NULL DEFAULT '',                       -- full deep link (EIP-681 / Solana Pay / tron URI)
    matched_via VARCHAR(20) NOT NULL DEFAULT 'amount_suffix',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',              -- pending/paid/confirmed/expired/cancelled/failed
    tx_hash VARCHAR(120) DEFAULT '',
    paid_at TIMESTAMP,
    confirmed_at TIMESTAMP,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usdt_orders_user_id ON qd_usdt_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_usdt_orders_status ON qd_usdt_orders(status);
-- v3.0.6 cleanup: drop the legacy unique index on (chain, address) that
-- was used by the per-order xpub-derived address scheme. In the current
-- "single fixed receiving address per chain + amount-suffix matching"
-- model, every active order on the same chain shares the same address,
-- so this old index would falsely reject every second pending order
-- (UniqueViolation on idx_usdt_orders_address_unique). Safe & idempotent.
DROP INDEX IF EXISTS idx_usdt_orders_address_unique;
-- Prevent two active orders on the same chain from claiming the same amount,
-- which is the foundation of the amount-suffix matching scheme.
CREATE UNIQUE INDEX IF NOT EXISTS idx_usdt_orders_amount_active
  ON qd_usdt_orders(chain, amount_usdt)
  WHERE status IN ('pending', 'paid');

-- One-shot cleanup for installs that pre-date v3.0.6. address_index is no
-- longer used; we keep the column where it already exists to avoid breaking
-- old rows, but new installs do not need it. The DO block is idempotent and
-- safe to re-run.
DO $$
BEGIN
    -- amount_suffix
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='qd_usdt_orders' AND column_name='amount_suffix'
    ) THEN
        ALTER TABLE qd_usdt_orders ADD COLUMN amount_suffix DECIMAL(20,8) NOT NULL DEFAULT 0;
    END IF;
    -- payment_uri
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='qd_usdt_orders' AND column_name='payment_uri'
    ) THEN
        ALTER TABLE qd_usdt_orders ADD COLUMN payment_uri TEXT NOT NULL DEFAULT '';
    END IF;
    -- currency
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='qd_usdt_orders' AND column_name='currency'
    ) THEN
        ALTER TABLE qd_usdt_orders ADD COLUMN currency VARCHAR(10) NOT NULL DEFAULT 'USDT';
    END IF;
    -- matched_via
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='qd_usdt_orders' AND column_name='matched_via'
    ) THEN
        ALTER TABLE qd_usdt_orders ADD COLUMN matched_via VARCHAR(20) NOT NULL DEFAULT 'amount_suffix';
    END IF;
    -- widen amount_usdt to (20,8) so suffix at 6+ decimals fits exactly
    BEGIN
        ALTER TABLE qd_usdt_orders ALTER COLUMN amount_usdt TYPE DECIMAL(20,8);
    EXCEPTION WHEN others THEN NULL;
    END;
    -- widen address (TRC20 base58 ~34, Solana ~44; old col was 80)
    BEGIN
        ALTER TABLE qd_usdt_orders ALTER COLUMN address TYPE VARCHAR(120);
    EXCEPTION WHEN others THEN NULL;
    END;
END
$$;

-- =============================================================================

-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_oauth_states (
    state VARCHAR(128) PRIMARY KEY,
    provider VARCHAR(20) NOT NULL,
    redirect TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_expires ON qd_oauth_states(expires_at);

-- =============================================================================

-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_verification_codes (
    id SERIAL PRIMARY KEY,
    email VARCHAR(100) NOT NULL,
    code VARCHAR(10) NOT NULL,
    type VARCHAR(20) NOT NULL,              -- register/login/reset_password/change_email/change_password
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    ip_address VARCHAR(45),
    attempts INTEGER DEFAULT 0,             -- Failed verification attempts (anti-brute-force)
    last_attempt_at TIMESTAMP,              -- Last attempt time
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_verification_codes_email ON qd_verification_codes(email);
CREATE INDEX IF NOT EXISTS idx_verification_codes_type ON qd_verification_codes(type);
CREATE INDEX IF NOT EXISTS idx_verification_codes_expires ON qd_verification_codes(expires_at);

-- =============================================================================
-- 1.7. Login Attempts (鐧诲綍灏濊瘯璁板綍 - 闃茬垎鐮?
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_login_attempts (
    id SERIAL PRIMARY KEY,
    identifier VARCHAR(100) NOT NULL,       -- IP address or username
    identifier_type VARCHAR(10) NOT NULL,   -- 'ip' or 'account'
    attempt_time TIMESTAMP DEFAULT NOW(),
    success BOOLEAN DEFAULT FALSE,
    ip_address VARCHAR(45),
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_identifier ON qd_login_attempts(identifier, identifier_type);
CREATE INDEX IF NOT EXISTS idx_login_attempts_time ON qd_login_attempts(attempt_time);

-- =============================================================================

-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_oauth_links (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES qd_users(id) ON DELETE CASCADE,
    provider VARCHAR(20) NOT NULL,          -- 'google' or 'github'
    provider_user_id VARCHAR(100) NOT NULL,
    provider_email VARCHAR(100),
    provider_name VARCHAR(100),
    provider_avatar VARCHAR(255),
    access_token TEXT,
    refresh_token TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(provider, provider_user_id)
);

CREATE INDEX IF NOT EXISTS idx_oauth_links_user_id ON qd_oauth_links(user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_links_provider ON qd_oauth_links(provider);

-- =============================================================================

-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_security_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    action VARCHAR(50) NOT NULL,            -- login/logout/register/reset_password/oauth_login/etc
    ip_address VARCHAR(45),
    user_agent TEXT,
    details TEXT,                           -- JSON with additional info
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_security_logs_user_id ON qd_security_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_security_logs_action ON qd_security_logs(action);
CREATE INDEX IF NOT EXISTS idx_security_logs_created_at ON qd_security_logs(created_at);

-- =============================================================================
-- 1.10. User MFA (TOTP / Authenticator App)
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_user_mfa (
    user_id INTEGER PRIMARY KEY REFERENCES qd_users(id) ON DELETE CASCADE,
    enabled BOOLEAN DEFAULT FALSE,
    secret_encrypted TEXT NOT NULL,
    recovery_codes_hash TEXT DEFAULT '',
    last_used_counter BIGINT DEFAULT 0,
    confirmed_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qd_mfa_challenges (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    challenge_hash VARCHAR(128) UNIQUE NOT NULL,
    reason VARCHAR(50) DEFAULT 'risk_login',
    ip_address VARCHAR(45),
    user_agent TEXT,
    attempts INTEGER DEFAULT 0,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mfa_challenges_user_id ON qd_mfa_challenges(user_id);
CREATE INDEX IF NOT EXISTS idx_mfa_challenges_expires ON qd_mfa_challenges(expires_at);

-- =============================================================================
-- 2. Trading Strategies
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_strategies_trading (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_name VARCHAR(255) NOT NULL,
    strategy_type VARCHAR(50) DEFAULT 'StrategyV2',
    market_category VARCHAR(50) DEFAULT 'Crypto',
    execution_mode VARCHAR(20) NOT NULL DEFAULT 'signal',
    notification_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) DEFAULT 'stopped',
    symbol VARCHAR(50),
    symbol_canonical VARCHAR(50) DEFAULT '',
    timeframe VARCHAR(10),
    initial_capital DECIMAL(20,8) DEFAULT 1000,
    leverage INTEGER DEFAULT 1,
    market_type VARCHAR(20) DEFAULT 'swap',
    exchange_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    trading_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategies_user_id ON qd_strategies_trading(user_id);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON qd_strategies_trading(status);

-- Script source library: reusable code assets separated from live/runtime strategy rows.
CREATE TABLE IF NOT EXISTS qd_script_sources (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    asset_type VARCHAR(32) NOT NULL DEFAULT 'script',
    template_key VARCHAR(80) DEFAULT '',
    param_schema JSONB DEFAULT '{}'::jsonb,
    source_marketplace_indicator_id INTEGER,
    source_script_source_id INTEGER,
    visibility VARCHAR(32) DEFAULT 'private',
    status VARCHAR(32) DEFAULT 'draft',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Upgrade installations created before reusable script assets were typed.
-- CREATE TABLE IF NOT EXISTS does not add columns to an existing table, so
-- this must run before idx_script_sources_asset_type is created below.
ALTER TABLE qd_script_sources ADD COLUMN IF NOT EXISTS asset_type VARCHAR(32) NOT NULL DEFAULT 'script';

CREATE INDEX IF NOT EXISTS idx_script_sources_user_id ON qd_script_sources(user_id);
CREATE INDEX IF NOT EXISTS idx_script_sources_marketplace ON qd_script_sources(source_marketplace_indicator_id);
CREATE INDEX IF NOT EXISTS idx_script_sources_asset_type ON qd_script_sources(user_id, asset_type);

CREATE TABLE IF NOT EXISTS qd_script_source_versions (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES qd_script_sources(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL DEFAULT '',
    description TEXT DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    template_key VARCHAR(80) DEFAULT '',
    param_schema JSONB DEFAULT '{}'::jsonb,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_script_source_versions_source
ON qd_script_source_versions(source_id, version_no DESC);
CREATE INDEX IF NOT EXISTS idx_script_source_versions_user
ON qd_script_source_versions(user_id);

CREATE TABLE IF NOT EXISTS qd_script_templates (
    id SERIAL PRIMARY KEY,
    template_key VARCHAR(80) UNIQUE NOT NULL,
    asset_type VARCHAR(40) NOT NULL DEFAULT 'script',
    title VARCHAR(255) NOT NULL,
    description TEXT DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    param_schema JSONB DEFAULT '{}'::jsonb,
    tags JSONB DEFAULT '[]'::jsonb,
    icon VARCHAR(64) DEFAULT 'appstore',
    accent VARCHAR(32) DEFAULT 'blue',
    sort_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_script_templates_active
ON qd_script_templates(is_active, sort_order);

-- =============================================================================
-- 3. Strategy Positions
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_strategy_positions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    symbol VARCHAR(50),
    symbol_canonical VARCHAR(50) DEFAULT '',
    side VARCHAR(10),  -- long/short
    size DECIMAL(20,8),
    entry_price DECIMAL(20,8),
    current_price DECIMAL(20,8),
    highest_price DECIMAL(20,8) DEFAULT 0,
    lowest_price DECIMAL(20,8) DEFAULT 0,
    unrealized_pnl DECIMAL(20,8) DEFAULT 0,
    pnl_percent DECIMAL(10,4) DEFAULT 0,
    equity DECIMAL(20,8) DEFAULT 0,
    market_type VARCHAR(20) DEFAULT 'swap',
    credential_id INTEGER DEFAULT 0,
    inst_id VARCHAR(80) DEFAULT '',
    strategy_run_id INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(strategy_id, symbol, side)
);

CREATE INDEX IF NOT EXISTS idx_positions_user_id ON qd_strategy_positions(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_strategy_id ON qd_strategy_positions(strategy_id);

-- =============================================================================
-- 4. Strategy Trades
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_strategy_trades (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    symbol VARCHAR(50),
    symbol_canonical VARCHAR(50) DEFAULT '',
    type VARCHAR(30),  -- open_long, close_short, etc.
    price DECIMAL(20,8),
    amount DECIMAL(20,8),
    value DECIMAL(20,8),
    commission DECIMAL(20,8) DEFAULT 0,
    commission_ccy VARCHAR(20) DEFAULT '',
    commission_quote DECIMAL(24,8),
    profit DECIMAL(20,8) DEFAULT 0,
    close_reason VARCHAR(64) DEFAULT '',
    matched_entry_price DECIMAL(20,8) DEFAULT 0,
    grid_matched_profit DECIMAL(20,8) DEFAULT 0,
    market_type VARCHAR(20) DEFAULT 'swap',
    credential_id INTEGER DEFAULT 0,
    inst_id VARCHAR(80) DEFAULT '',
    fill_source VARCHAR(32) DEFAULT '',
    pending_order_id INTEGER DEFAULT 0,
    strategy_run_id INTEGER DEFAULT 0,
    order_intent_id INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_user_id ON qd_strategy_trades(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_id ON qd_strategy_trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON qd_strategy_trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_symbol_canon ON qd_strategy_trades (strategy_id, market_type, symbol_canonical);
CREATE INDEX IF NOT EXISTS idx_positions_strategy_leg ON qd_strategy_positions (strategy_id, market_type, symbol_canonical, side);

-- Exchange-settled funding cash flow. Positive amount means the strategy
-- received funding; negative means it paid funding.
CREATE TABLE IF NOT EXISTS qd_strategy_funding_fees (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    credential_id INTEGER NOT NULL DEFAULT 0,
    exchange_id VARCHAR(40) NOT NULL DEFAULT '',
    symbol VARCHAR(50) NOT NULL DEFAULT '',
    asset VARCHAR(20) NOT NULL DEFAULT 'USDT',
    amount DECIMAL(24, 8) NOT NULL DEFAULT 0,
    allocation_ratio DECIMAL(20, 12) NOT NULL DEFAULT 1,
    external_id VARCHAR(160) NOT NULL,
    occurred_at TIMESTAMP NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (credential_id, exchange_id, external_id, strategy_id)
);
CREATE INDEX IF NOT EXISTS idx_strategy_funding_strategy_time
ON qd_strategy_funding_fees(strategy_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_strategy_funding_credential
ON qd_strategy_funding_fees(credential_id, exchange_id, external_id);

-- Broker-posted account activities attributed to strategy-generated orders.
-- Amount is the signed cash impact: negative is a fee/interest debit, positive is a credit.
CREATE TABLE IF NOT EXISTS qd_strategy_broker_activities (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    credential_id INTEGER NOT NULL DEFAULT 0,
    broker_id VARCHAR(40) NOT NULL DEFAULT '',
    activity_type VARCHAR(24) NOT NULL DEFAULT '',
    activity_subtype VARCHAR(24) NOT NULL DEFAULT '',
    symbol VARCHAR(50) NOT NULL DEFAULT '',
    currency VARCHAR(16) NOT NULL DEFAULT 'USD',
    amount DECIMAL(24, 8) NOT NULL DEFAULT 0,
    account_amount DECIMAL(24, 8) NOT NULL DEFAULT 0,
    allocation_ratio DECIMAL(20, 12) NOT NULL DEFAULT 1,
    allocation_reason VARCHAR(40) NOT NULL DEFAULT '',
    external_id VARCHAR(180) NOT NULL,
    occurred_at TIMESTAMP NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (credential_id, broker_id, external_id, strategy_id)
);
CREATE INDEX IF NOT EXISTS idx_broker_activity_strategy_time
ON qd_strategy_broker_activities(strategy_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_broker_activity_credential
ON qd_strategy_broker_activities(credential_id, broker_id, external_id);

-- Five-minute mark-to-market history used to calculate a strategy's true
-- local-day equity change, including the change in unrealized P&L on positions
-- carried across midnight.
CREATE TABLE IF NOT EXISTS qd_strategy_equity_snapshots (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    equity DECIMAL(24,8) NOT NULL DEFAULT 0,
    realized_pnl DECIMAL(24,8) NOT NULL DEFAULT 0,
    unrealized_pnl DECIMAL(24,8) NOT NULL DEFAULT 0,
    captured_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_equity_snapshots_boundary
ON qd_strategy_equity_snapshots(strategy_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_equity_snapshots_user
ON qd_strategy_equity_snapshots(user_id, captured_at DESC);

-- Strategy AI review report history.
CREATE TABLE IF NOT EXISTS qd_strategy_review_reports (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    lookback_days INTEGER NOT NULL DEFAULT 30,
    language VARCHAR(20) DEFAULT 'zh-CN',
    include_ai BOOLEAN DEFAULT TRUE,
    ai_status VARCHAR(32) DEFAULT '',
    summary TEXT DEFAULT '',
    total_net_pnl DECIMAL(20,8) DEFAULT 0,
    total_return_pct DECIMAL(20,8) DEFAULT 0,
    win_rate DECIMAL(20,8) DEFAULT 0,
    profit_factor DECIMAL(20,8) DEFAULT 0,
    max_drawdown_pct DECIMAL(20,8) DEFAULT 0,
    report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_review_reports_strategy
    ON qd_strategy_review_reports(strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_review_reports_user
    ON qd_strategy_review_reports(user_id, created_at DESC);

-- L1 account position mirror (exchange truth per credential + inst_id + side)
CREATE TABLE IF NOT EXISTS qd_account_positions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    credential_id INTEGER NOT NULL DEFAULT 0,
    exchange_id VARCHAR(40) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'swap',
    inst_id VARCHAR(80) NOT NULL DEFAULT '',
    symbol VARCHAR(50) NOT NULL DEFAULT '',
    side VARCHAR(10) NOT NULL DEFAULT '',
    size DECIMAL(24, 8) NOT NULL DEFAULT 0,
    entry_price DECIMAL(24, 8) DEFAULT 0,
    mark_price DECIMAL(24, 8) DEFAULT 0,
    unrealized_pnl DECIMAL(24, 8) DEFAULT 0,
    raw_json JSONB DEFAULT '{}'::jsonb,
    synced_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (credential_id, market_type, inst_id, side)
);
CREATE INDEX IF NOT EXISTS idx_account_pos_user ON qd_account_positions(user_id);
CREATE INDEX IF NOT EXISTS idx_account_pos_cred ON qd_account_positions(credential_id, market_type);

-- Grid cell ladder state (P2). Pre-placed limit orders / user-stream driven
-- fills will land here; today only the scaffolding lives in code (see
-- app.services.live_trading.grid_cells).
CREATE TABLE IF NOT EXISTS qd_grid_cells (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    symbol VARCHAR(50) NOT NULL,
    cell_index INTEGER NOT NULL,
    lower_price DECIMAL(20,8) NOT NULL,
    upper_price DECIMAL(20,8) NOT NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'idle',
    leg_size DECIMAL(20,8) DEFAULT 0,
    leg_entry_price DECIMAL(20,8) DEFAULT 0,
    working_order_id VARCHAR(64) DEFAULT '',
    last_event_ts TIMESTAMP DEFAULT NOW(),
    extra JSONB DEFAULT '{}'::jsonb,
    CONSTRAINT uniq_grid_cell UNIQUE(strategy_id, symbol, cell_index)
);
CREATE INDEX IF NOT EXISTS idx_grid_cells_strategy ON qd_grid_cells(strategy_id);
CREATE INDEX IF NOT EXISTS idx_grid_cells_state ON qd_grid_cells(strategy_id, state);

CREATE TABLE IF NOT EXISTS qd_grid_resting_orders (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    symbol VARCHAR(50) NOT NULL,
    cell_index INTEGER NOT NULL DEFAULT 0,
    purpose VARCHAR(24) NOT NULL,
    side VARCHAR(8) NOT NULL,
    pos_side VARCHAR(8) NOT NULL DEFAULT '',
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    price DECIMAL(24, 8) NOT NULL,
    quantity DECIMAL(24, 8) NOT NULL DEFAULT 0,
    quote_amount DECIMAL(24, 8) NOT NULL DEFAULT 0,
    client_order_id VARCHAR(64) NOT NULL DEFAULT '',
    exchange_order_id VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    filled_quantity DECIMAL(24, 8) NOT NULL DEFAULT 0,
    avg_fill_price DECIMAL(24, 8) NOT NULL DEFAULT 0,
    processed_fill_qty DECIMAL(24, 8) NOT NULL DEFAULT 0,
    extra JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_grid_resting_strategy ON qd_grid_resting_orders(strategy_id, status);

-- =============================================================================
-- 5. Pending Orders Queue
-- =============================================================================

CREATE TABLE IF NOT EXISTS pending_orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER REFERENCES qd_strategies_trading(id) ON DELETE SET NULL,
    symbol VARCHAR(50) NOT NULL,
    signal_type VARCHAR(30) NOT NULL,
    signal_ts BIGINT,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    order_intent_id INTEGER NOT NULL DEFAULT 0,
    idempotency_key VARCHAR(180) NOT NULL,
    market_type VARCHAR(20) DEFAULT 'swap',
    order_type VARCHAR(20) DEFAULT 'market',
    amount DECIMAL(20,8) DEFAULT 0,
    price DECIMAL(20,8) DEFAULT 0,
    execution_mode VARCHAR(20) DEFAULT 'signal',
    status VARCHAR(20) DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 10,
    last_error TEXT DEFAULT '',
    payload_json TEXT DEFAULT '',
    dispatch_note TEXT DEFAULT '',
    exchange_id VARCHAR(50) DEFAULT '',
    credential_id INTEGER NOT NULL DEFAULT 0,
    inst_id VARCHAR(80) NOT NULL DEFAULT '',
    exchange_order_id VARCHAR(100) DEFAULT '',
    exchange_response_json TEXT DEFAULT '',
    filled DECIMAL(20,8) DEFAULT 0,
    avg_price DECIMAL(20,8) DEFAULT 0,
    executed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    processed_at TIMESTAMP,
    sent_at TIMESTAMP
);

UPDATE pending_orders
SET idempotency_key = 'pending-order-' || id::text
WHERE idempotency_key IS NULL OR idempotency_key = '';
ALTER TABLE pending_orders ALTER COLUMN idempotency_key SET NOT NULL;
ALTER TABLE pending_orders ALTER COLUMN idempotency_key DROP DEFAULT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_orders_idempotency_key ON pending_orders(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_pending_orders_user_id ON pending_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders(status);
CREATE INDEX IF NOT EXISTS idx_pending_orders_strategy_id ON pending_orders(strategy_id);
CREATE INDEX IF NOT EXISTS idx_pending_orders_strategy_run_id ON pending_orders(strategy_run_id);

-- =============================================================================
-- 6. Strategy Notifications
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_strategy_notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    symbol VARCHAR(50) DEFAULT '',
    signal_type VARCHAR(30) DEFAULT '',
    channels VARCHAR(255) DEFAULT '',
    title VARCHAR(255) DEFAULT '',
    message TEXT DEFAULT '',
    payload_json TEXT DEFAULT '',
    is_read INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON qd_strategy_notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_strategy_id ON qd_strategy_notifications(strategy_id);
CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON qd_strategy_notifications(is_read);

-- =============================================================================
-- 6a. Indicator Signal Alerts
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_indicator_signal_alerts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    indicator_id INTEGER NOT NULL,
    indicator_name VARCHAR(160) DEFAULT '',
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    symbol_name VARCHAR(128) DEFAULT '',
    timeframe VARCHAR(16) NOT NULL DEFAULT '1D',
    signal_keys TEXT DEFAULT '[]',
    channels TEXT DEFAULT '["browser"]',
    target_json TEXT DEFAULT '{}',
    param_json TEXT DEFAULT '{}',
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    last_bar_time VARCHAR(64) DEFAULT '',
    last_fingerprint VARCHAR(255) DEFAULT '',
    last_signal_payload TEXT DEFAULT '{}',
    last_error TEXT DEFAULT '',
    check_count INTEGER NOT NULL DEFAULT 0,
    trigger_count INTEGER NOT NULL DEFAULT 0,
    next_check_at TIMESTAMP DEFAULT NOW(),
    last_checked_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_indicator_signal_alerts_user_id ON qd_indicator_signal_alerts(user_id);
CREATE INDEX IF NOT EXISTS idx_indicator_signal_alerts_status_next ON qd_indicator_signal_alerts(status, next_check_at);
CREATE INDEX IF NOT EXISTS idx_indicator_signal_alerts_indicator_id ON qd_indicator_signal_alerts(indicator_id);

-- =============================================================================
-- 6b. Strategy runtime logs (dashboard / API)
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_strategy_logs (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    level VARCHAR(20) DEFAULT 'info',
    message TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategy_logs_strategy_id ON qd_strategy_logs(strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_logs_timestamp ON qd_strategy_logs(timestamp);

-- =============================================================================
-- 7. Indicator Codes
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_indicator_codes (
   id serial4 NOT NULL,
   user_id int4 DEFAULT 1 NOT NULL,
   is_buy int4 DEFAULT 0 NOT NULL,
   end_time int8 DEFAULT 1 NOT NULL,
   name varchar(255) DEFAULT ''::character varying NOT NULL,
   code text NULL,
   description text DEFAULT ''::text NULL,
   publish_to_community int4 DEFAULT 0 NOT NULL,
   pricing_type varchar(20) DEFAULT 'free'::character varying NOT NULL,
   price numeric(10, 2) DEFAULT 0 NOT NULL,
   is_encrypted int4 DEFAULT 0 NOT NULL,
   preview_image varchar(500) DEFAULT ''::character varying NULL,
   vip_free boolean DEFAULT false,
   createtime int8 NULL,
   updatetime int8 NULL,
   created_at timestamp DEFAULT now(),
   updated_at timestamp DEFAULT now(),
   purchase_count int4 DEFAULT 0 NULL,
   avg_rating numeric(3, 2) DEFAULT 0 NULL,
   rating_count int4 DEFAULT 0 NULL,
   view_count int4 DEFAULT 0 NULL,
   review_status varchar(20) DEFAULT 'approved'::character varying NULL,
   review_note text DEFAULT ''::text NULL,
   reviewed_at timestamp NULL,
   reviewed_by int4 NULL,
   asset_type varchar(32) DEFAULT 'indicator'::character varying NULL,


    source_indicator_id int4 NULL,
    source_script_source_id int4 NULL,
    source_strategy_id int4 NULL,

    -- (zh-CN / en-US / ja-JP 绛?锛沶ame_i18n / description_i18n 鏄?LLM 缈昏瘧鐢熸垚鐨?
    -- JSONB锛岀粨鏋勫舰濡?{"en-US": "...", "zh-CN": "...", ...}銆?

    -- 瑙?app/services/indicator_translator.py 涓?community_service.py:_localize_indicator銆?
    source_language varchar(16) DEFAULT NULL,
    name_i18n        jsonb       DEFAULT NULL,
    description_i18n jsonb       DEFAULT NULL,
    CONSTRAINT qd_indicator_codes_pkey PRIMARY KEY (id),
   CONSTRAINT qd_indicator_codes_user_id_fkey FOREIGN KEY (user_id) REFERENCES qd_users(id) ON DELETE CASCADE

);

-- Upgrade existing installations created before marketplace asset metadata was
-- added. CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS vip_free boolean DEFAULT false;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS review_status varchar(20) DEFAULT 'approved';
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS review_note text DEFAULT '';
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS reviewed_at timestamp NULL;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS reviewed_by int4 NULL;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS asset_type varchar(32) DEFAULT 'indicator';
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS source_indicator_id int4 NULL;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS source_script_source_id int4 NULL;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS source_strategy_id int4 NULL;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS source_language varchar(16) DEFAULT NULL;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS name_i18n jsonb DEFAULT NULL;
ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS description_i18n jsonb DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_indicator_codes_user_id ON qd_indicator_codes USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_indicator_review_status ON qd_indicator_codes USING btree (review_status);
CREATE INDEX IF NOT EXISTS idx_indicator_codes_source ON qd_indicator_codes USING btree (source_indicator_id);
CREATE INDEX IF NOT EXISTS idx_indicator_codes_source_script ON qd_indicator_codes USING btree (source_script_source_id);
CREATE INDEX IF NOT EXISTS idx_indicator_codes_source_strategy ON qd_indicator_codes USING btree (source_strategy_id);

CREATE TABLE IF NOT EXISTS qd_indicator_code_versions (
   id serial4 NOT NULL,
   indicator_id int4 NOT NULL,
   user_id int4 NOT NULL,
   version_no int4 NOT NULL,
   name varchar(255) DEFAULT ''::character varying NOT NULL,
   description text DEFAULT ''::text NULL,
   code text NOT NULL,
   created_at timestamp DEFAULT now(),
   CONSTRAINT qd_indicator_code_versions_pkey PRIMARY KEY (id),
   CONSTRAINT qd_indicator_code_versions_indicator_fkey FOREIGN KEY (indicator_id) REFERENCES qd_indicator_codes(id) ON DELETE CASCADE,
   CONSTRAINT qd_indicator_code_versions_user_fkey FOREIGN KEY (user_id) REFERENCES qd_users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_indicator_code_versions_indicator ON qd_indicator_code_versions USING btree (indicator_id, version_no DESC);
CREATE INDEX IF NOT EXISTS idx_indicator_code_versions_user ON qd_indicator_code_versions USING btree (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_indicator_code_versions_no ON qd_indicator_code_versions USING btree (indicator_id, version_no);

-- =============================================================================
-- 10. Watchlist
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_watchlist (
    id SERIAL PRIMARY KEY,
    user_id INTEGER DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    name VARCHAR(100) DEFAULT '',
    exchange_id VARCHAR(50) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    instrument_id VARCHAR(120) NOT NULL DEFAULT '',
    settle_currency VARCHAR(20) NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_watchlist_asset UNIQUE(user_id, market, symbol)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_user_id ON qd_watchlist(user_id);

ALTER TABLE qd_watchlist ADD COLUMN IF NOT EXISTS exchange_id VARCHAR(50) NOT NULL DEFAULT '';
ALTER TABLE qd_watchlist ADD COLUMN IF NOT EXISTS market_type VARCHAR(20) NOT NULL DEFAULT 'spot';
ALTER TABLE qd_watchlist ADD COLUMN IF NOT EXISTS instrument_id VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE qd_watchlist ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE qd_watchlist DROP CONSTRAINT IF EXISTS qd_watchlist_user_id_market_symbol_key;
DELETE FROM qd_watchlist newer
USING qd_watchlist older
WHERE newer.user_id = older.user_id
  AND newer.market = older.market
  AND newer.symbol = older.symbol
  AND newer.id < older.id;
UPDATE qd_watchlist
SET exchange_id = '', market_type = 'spot', instrument_id = ''
WHERE exchange_id <> '' OR market_type <> 'spot' OR instrument_id <> '';
DROP INDEX IF EXISTS uq_watchlist_market_context;
CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_asset
  ON qd_watchlist(user_id, market, symbol);

-- =============================================================================
-- 10A. Strategy universes and point-in-time membership
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_universes (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES qd_users(id) ON DELETE CASCADE,
    code VARCHAR(80) NOT NULL,
    name VARCHAR(160) NOT NULL DEFAULT '',
    name_i18n_key VARCHAR(160) NOT NULL DEFAULT '',
    market VARCHAR(50) NOT NULL DEFAULT '',
    universe_type VARCHAR(32) NOT NULL,
    source VARCHAR(50) NOT NULL DEFAULT 'manual',
    source_ref VARCHAR(160) NOT NULL DEFAULT '',
    is_system BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_universes_system_code
  ON qd_universes(code) WHERE is_system = TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_universes_user_code
  ON qd_universes(user_id, code) WHERE is_system = FALSE;
CREATE INDEX IF NOT EXISTS idx_universes_user
  ON qd_universes(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS qd_universe_members (
    id BIGSERIAL PRIMARY KEY,
    universe_id INTEGER NOT NULL REFERENCES qd_universes(id) ON DELETE CASCADE,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(80) NOT NULL,
    name VARCHAR(160) NOT NULL DEFAULT '',
    exchange_id VARCHAR(50) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    instrument_id VARCHAR(120) NOT NULL DEFAULT '',
    settle_currency VARCHAR(20) NOT NULL DEFAULT '',
    valid_from DATE NOT NULL DEFAULT DATE '1900-01-01',
    valid_to DATE,
    member_weight DOUBLE PRECISION,
    member_rank INTEGER,
    source_version VARCHAR(120) NOT NULL DEFAULT '',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_universe_member_valid_range
      CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_universe_member_interval
  ON qd_universe_members(
    universe_id, market, symbol, exchange_id, market_type, instrument_id, valid_from
  );
CREATE INDEX IF NOT EXISTS idx_universe_members_asof
  ON qd_universe_members(universe_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_universe_members_symbol
  ON qd_universe_members(market, symbol);

CREATE TABLE IF NOT EXISTS qd_universe_snapshots (
    snapshot_id VARCHAR(36) PRIMARY KEY,
    universe_id INTEGER NOT NULL REFERENCES qd_universes(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    as_of_date DATE NOT NULL,
    source_version VARCHAR(120) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL,
    member_count INTEGER NOT NULL DEFAULT 0,
    members_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_universe_snapshot_content
  ON qd_universe_snapshots(universe_id, user_id, as_of_date, content_hash);
CREATE INDEX IF NOT EXISTS idx_universe_snapshots_lookup
  ON qd_universe_snapshots(user_id, universe_id, as_of_date DESC);

CREATE TABLE IF NOT EXISTS qd_fundamental_snapshots (
    id BIGSERIAL PRIMARY KEY,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(80) NOT NULL,
    period_end DATE NOT NULL,
    available_at DATE NOT NULL,
    frequency VARCHAR(20) NOT NULL DEFAULT 'quarterly',
    currency VARCHAR(20) NOT NULL DEFAULT '',
    revenue DOUBLE PRECISION,
    net_income DOUBLE PRECISION,
    book_value DOUBLE PRECISION,
    shareholder_equity DOUBLE PRECISION,
    total_debt DOUBLE PRECISION,
    free_cash_flow DOUBLE PRECISION,
    shares_outstanding DOUBLE PRECISION,
    market_cap DOUBLE PRECISION,
    pe_ratio DOUBLE PRECISION,
    pb_ratio DOUBLE PRECISION,
    return_on_equity DOUBLE PRECISION,
    revenue_growth DOUBLE PRECISION,
    debt_to_equity DOUBLE PRECISION,
    source VARCHAR(80) NOT NULL DEFAULT 'manual',
    source_version VARCHAR(120) NOT NULL DEFAULT '',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market, symbol, period_end, available_at, source)
);

ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS revenue DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS net_income DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS book_value DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS shareholder_equity DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS total_debt DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS free_cash_flow DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS shares_outstanding DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS market_cap DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS pe_ratio DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS pb_ratio DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS return_on_equity DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS revenue_growth DOUBLE PRECISION;
ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS debt_to_equity DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_fundamental_snapshots_pit
  ON qd_fundamental_snapshots (market, symbol, available_at, period_end);

CREATE TABLE IF NOT EXISTS qd_portfolio_rebalance_plans (
    plan_id VARCHAR(36) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER REFERENCES qd_strategies_trading(id) ON DELETE SET NULL,
    portfolio_id VARCHAR(96) NOT NULL DEFAULT '',
    universe_id INTEGER REFERENCES qd_universes(id) ON DELETE SET NULL,
    universe_snapshot_id VARCHAR(36) NOT NULL DEFAULT '',
    rebalance_group_id VARCHAR(128) NOT NULL,
    execution_mode VARCHAR(24) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'planned',
    signal_time TIMESTAMP NOT NULL,
    scheduled_execution_time TIMESTAMP,
    equity DOUBLE PRECISION NOT NULL DEFAULT 0,
    cash DOUBLE PRECISION NOT NULL DEFAULT 0,
    target_weights_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    current_weights_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    diagnostics_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    notification_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_portfolio_execution_mode
      CHECK (execution_mode IN ('live', 'notify_only'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_rebalance_group
  ON qd_portfolio_rebalance_plans(user_id, strategy_id, rebalance_group_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_rebalance_plans_user
  ON qd_portfolio_rebalance_plans(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_portfolio_rebalance_orders (
    id BIGSERIAL PRIMARY KEY,
    plan_id VARCHAR(36) NOT NULL REFERENCES qd_portfolio_rebalance_plans(plan_id) ON DELETE CASCADE,
    idempotency_key VARCHAR(180) NOT NULL,
    market VARCHAR(50) NOT NULL DEFAULT '',
    symbol VARCHAR(80) NOT NULL,
    exchange_id VARCHAR(50) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    side VARCHAR(10) NOT NULL,
    action VARCHAR(24) NOT NULL,
    quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    reference_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    estimated_notional DOUBLE PRECISION NOT NULL DEFAULT 0,
    estimated_fee DOUBLE PRECISION NOT NULL DEFAULT 0,
    current_weight DOUBLE PRECISION NOT NULL DEFAULT 0,
    target_weight DOUBLE PRECISION NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'planned',
    order_intent_id INTEGER NOT NULL DEFAULT 0,
    pending_order_id BIGINT NOT NULL DEFAULT 0,
    actual_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    actual_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    acknowledged_at TIMESTAMP NULL,
    error_code VARCHAR(120) NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_rebalance_order_key
  ON qd_portfolio_rebalance_orders(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_portfolio_rebalance_orders_plan
  ON qd_portfolio_rebalance_orders(plan_id, status);

INSERT INTO qd_universes
  (code, name_i18n_key, market, universe_type, source, source_ref, is_system, status)
VALUES
  ('watchlist', 'universe.catalog.watchlist', 'Mixed', 'watchlist', 'watchlist', 'current_user', TRUE, 'active'),
  ('csi300', 'universe.catalog.csi300', 'CNStock', 'index', 'provider', '000300.SH', TRUE, 'data_required'),
  ('csi500', 'universe.catalog.csi500', 'CNStock', 'index', 'provider', '000905.SH', TRUE, 'data_required'),
  ('sp500', 'universe.catalog.sp500', 'USStock', 'index', 'provider', 'SPX', TRUE, 'data_required'),
  ('nasdaq100', 'universe.catalog.nasdaq100', 'USStock', 'index', 'provider', 'NDX', TRUE, 'data_required'),
  ('etf_pool', 'universe.catalog.etfPool', 'Mixed', 'etf', 'provider', 'etf_pool', TRUE, 'data_required'),
  ('crypto_top100', 'universe.catalog.cryptoTop100', 'Crypto', 'market_cap', 'provider', 'top100', TRUE, 'data_required'),
  ('hk_equities', 'universe.catalog.hkEquities', 'HKStock', 'market', 'provider', 'hk_equities', TRUE, 'data_required')
ON CONFLICT DO NOTHING;

INSERT INTO qd_universes
  (code, name_i18n_key, market, universe_type, source, source_ref, is_system, status)
VALUES
  ('hk_core', 'universe.catalog.hkCore', 'HKStock', 'market', 'symbol_master', 'HKStock:hot:equity', TRUE, 'active'),
  ('hk_etf', 'universe.catalog.hkEtf', 'HKStock', 'etf', 'symbol_master', 'HKStock:hot:etf', TRUE, 'active'),
  ('us_etf', 'universe.catalog.usEtf', 'USStock', 'etf', 'symbol_master', 'USStock:hot:etf', TRUE, 'active')
ON CONFLICT DO NOTHING;

INSERT INTO qd_universes
  (code, name, name_i18n_key, market, universe_type, source, source_ref, is_system, status)
VALUES
  ('hk_hsi_core50', 'Hang Seng Index Core 50', 'universe.catalog.hkHsiCore50', 'HKStock', 'index', 'public_snapshot', 'HSI_CORE50', TRUE, 'data_required'),
  ('hk_tech30', 'Hang Seng TECH 30', 'universe.catalog.hkTech30', 'HKStock', 'index', 'public_snapshot', 'HSTECH', TRUE, 'data_required'),
  ('hk_china_enterprises50', 'Hang Seng China Enterprises 50', 'universe.catalog.hkChinaEnterprises50', 'HKStock', 'index', 'public_snapshot', 'HSCEI', TRUE, 'data_required'),
  ('hk_high_dividend50', 'Hang Seng High Dividend Yield 50', 'universe.catalog.hkHighDividend50', 'HKStock', 'index', 'public_snapshot', 'HSHDYI', TRUE, 'data_required')
ON CONFLICT DO NOTHING;

UPDATE qd_universes SET source_ref = 'USStock:hot:etf', updated_at = NOW()
WHERE code = 'us_etf' AND is_system = TRUE;

UPDATE qd_universes SET source_ref = 'HKStock:hot:etf', updated_at = NOW()
WHERE code = 'hk_etf' AND is_system = TRUE;

UPDATE qd_universes SET status = 'deprecated', updated_at = NOW()
WHERE code IN ('etf_pool', 'hk_equities', 'hk_core') AND is_system = TRUE;

-- =============================================================================
-- 11. Analysis Tasks
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_analysis_tasks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    model VARCHAR(100) DEFAULT '',
    language VARCHAR(20) DEFAULT 'en-US',
    status VARCHAR(20) DEFAULT 'completed',
    result_json TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_analysis_tasks_user_id ON qd_analysis_tasks(user_id);

CREATE TABLE IF NOT EXISTS qd_ai_strategy_decisions (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER REFERENCES qd_strategies_trading(id) ON DELETE CASCADE,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    decision_key VARCHAR(64) NOT NULL,
    profile_name VARCHAR(80) NOT NULL DEFAULT '',
    model_id VARCHAR(160) NOT NULL DEFAULT '',
    prompt_version VARCHAR(80) NOT NULL DEFAULT '',
    prompt_hash VARCHAR(64) NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    symbol VARCHAR(80) NOT NULL DEFAULT '',
    as_of_time VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(24) NOT NULL,
    output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code VARCHAR(120) NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_strategy_decision_key
  ON qd_ai_strategy_decisions(user_id, strategy_id, decision_key);
CREATE INDEX IF NOT EXISTS idx_ai_strategy_decisions_lookup
  ON qd_ai_strategy_decisions(user_id, strategy_id, symbol, created_at DESC);

-- =============================================================================
-- 12. Backtest Runs
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_backtest_runs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER,
    source_id INTEGER NOT NULL,
    strategy_name VARCHAR(255) DEFAULT '',
    market VARCHAR(50) NOT NULL DEFAULT '',
    symbol VARCHAR(50) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    timeframe VARCHAR(10) NOT NULL DEFAULT '',
    start_date VARCHAR(20) NOT NULL DEFAULT '',
    end_date VARCHAR(20) NOT NULL DEFAULT '',
    initial_capital DECIMAL(20,8) DEFAULT 10000,
    commission DECIMAL(10,6) DEFAULT 0.001,
    slippage DECIMAL(10,6) DEFAULT 0,
    leverage INTEGER DEFAULT 1,
    params_json TEXT NOT NULL DEFAULT '{}',
    manifest_json TEXT NOT NULL DEFAULT '{}',
    engine_version VARCHAR(50) DEFAULT '',
    code_hash VARCHAR(128) DEFAULT '',
    status VARCHAR(20) DEFAULT 'success',
    error_message TEXT DEFAULT '',
    result_json TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW()
);

-- Upgrade pre-v5 backtest history in place. Legacy rows did not carry the
-- Strategy API V2 source/manifest fields; source_id=0 means no reusable source.
ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS source_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS market_type VARCHAR(20) NOT NULL DEFAULT 'spot';
ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS params_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE qd_backtest_runs ADD COLUMN IF NOT EXISTS manifest_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_backtest_runs_user_id ON qd_backtest_runs(user_id);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy_id ON qd_backtest_runs(strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_source_id ON qd_backtest_runs(source_id);

CREATE TABLE IF NOT EXISTS qd_backtest_trades (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    strategy_id INTEGER,
    trade_index INTEGER DEFAULT 0,
    trade_time VARCHAR(64) DEFAULT '',
    trade_type VARCHAR(64) DEFAULT '',
    side VARCHAR(32) DEFAULT '',
    price DOUBLE PRECISION DEFAULT 0,
    amount DOUBLE PRECISION DEFAULT 0,
    profit DOUBLE PRECISION DEFAULT 0,
    balance DOUBLE PRECISION DEFAULT 0,
    reason VARCHAR(64) DEFAULT '',
    payload_json TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_id ON qd_backtest_trades(run_id);

CREATE TABLE IF NOT EXISTS qd_backtest_equity_points (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL,
    point_index INTEGER DEFAULT 0,
    point_time VARCHAR(64) DEFAULT '',
    point_value DOUBLE PRECISION DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_points_run_id ON qd_backtest_equity_points(run_id);

CREATE TABLE IF NOT EXISTS qd_factor_research_runs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL,
    source_name VARCHAR(255) DEFAULT '',
    market VARCHAR(100) DEFAULT '',
    timeframe VARCHAR(10) DEFAULT '',
    start_date VARCHAR(20) NOT NULL DEFAULT '',
    end_date VARCHAR(20) NOT NULL DEFAULT '',
    factor_id VARCHAR(64) NOT NULL DEFAULT '',
    groups_count INTEGER NOT NULL DEFAULT 5,
    holding_period INTEGER NOT NULL DEFAULT 5,
    commission DECIMAL(10,6) DEFAULT 0.001,
    slippage DECIMAL(10,6) DEFAULT 0,
    neutralize_industry BOOLEAN NOT NULL DEFAULT FALSE,
    universe_size INTEGER NOT NULL DEFAULT 0,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    code_hash VARCHAR(128) DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    status VARCHAR(20) DEFAULT 'success',
    error_message TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_factor_research_runs_user_id
  ON qd_factor_research_runs(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_factor_research_runs_source_id
  ON qd_factor_research_runs(source_id, id DESC);

-- =============================================================================
-- 13. Exchange Credentials
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_exchange_credentials (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    name VARCHAR(100) DEFAULT '',
    exchange_id VARCHAR(50) NOT NULL,
    api_key_hint VARCHAR(50) DEFAULT '',
    encrypted_config TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exchange_credentials_user_id ON qd_exchange_credentials(user_id);

-- =============================================================================
-- 14. Manual Positions
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_manual_positions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    name VARCHAR(100) DEFAULT '',
    side VARCHAR(10) DEFAULT 'long',
    quantity DECIMAL(20,8) NOT NULL DEFAULT 0,
    entry_price DECIMAL(20,8) NOT NULL DEFAULT 0,
    entry_time BIGINT,
    notes TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    group_name VARCHAR(100) DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, market, symbol, side, group_name)
);

CREATE INDEX IF NOT EXISTS idx_manual_positions_user_id ON qd_manual_positions(user_id);

-- =============================================================================
-- 15. Position Alerts
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_position_alerts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    position_id INTEGER,
    market VARCHAR(50) DEFAULT '',
    symbol VARCHAR(50) DEFAULT '',
    alert_type VARCHAR(30) NOT NULL,
    threshold DECIMAL(20,8) NOT NULL DEFAULT 0,
    notification_config TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    is_triggered INTEGER DEFAULT 0,
    last_triggered_at TIMESTAMP,
    trigger_count INTEGER DEFAULT 0,
    repeat_interval INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_position_alerts_user_id ON qd_position_alerts(user_id);
CREATE INDEX IF NOT EXISTS idx_position_alerts_position_id ON qd_position_alerts(position_id);

-- =============================================================================
-- 16. Position Monitors
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_position_monitors (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    name VARCHAR(100) DEFAULT '',
    position_ids TEXT DEFAULT '',
    monitor_type VARCHAR(20) DEFAULT 'ai',
    config TEXT DEFAULT '',
    notification_config TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    last_run_at TIMESTAMP,
    next_run_at TIMESTAMP,
    last_result TEXT DEFAULT '',
    run_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_position_monitors_user_id ON qd_position_monitors(user_id);

-- =============================================================================
-- 17. Market Symbols (Seed Data)
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_market_symbols (
    id SERIAL PRIMARY KEY,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    name VARCHAR(255) DEFAULT '',
    exchange VARCHAR(50) DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'spot',
    instrument_id VARCHAR(120) NOT NULL DEFAULT '',
    settle_currency VARCHAR(20) NOT NULL DEFAULT '',
    asset_class VARCHAR(20) NOT NULL DEFAULT 'crypto',
    currency VARCHAR(10) DEFAULT '',
    is_active INTEGER DEFAULT 1,
    is_hot INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(market, symbol, exchange, market_type, instrument_id)
);

CREATE INDEX IF NOT EXISTS idx_market_symbols_market ON qd_market_symbols(market);
CREATE INDEX IF NOT EXISTS idx_market_symbols_is_hot ON qd_market_symbols(market, is_hot);
CREATE INDEX IF NOT EXISTS idx_market_symbols_market_upper_symbol
  ON qd_market_symbols(market, UPPER(symbol));

CREATE TABLE IF NOT EXISTS qd_market_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'manual',
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    result JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_market_sync_runs_running
  ON qd_market_sync_runs ((status)) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_market_sync_runs_started
  ON qd_market_sync_runs(started_at DESC);

ALTER TABLE qd_market_symbols ADD COLUMN IF NOT EXISTS market_type VARCHAR(20) NOT NULL DEFAULT 'spot';
ALTER TABLE qd_market_symbols ADD COLUMN IF NOT EXISTS instrument_id VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE qd_market_symbols ADD COLUMN IF NOT EXISTS settle_currency VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE qd_market_symbols ADD COLUMN IF NOT EXISTS asset_class VARCHAR(20) NOT NULL DEFAULT 'crypto';
UPDATE qd_market_symbols SET asset_class = 'equity'
WHERE market IN ('CNStock', 'HKStock', 'USStock', 'MOEX') AND asset_class = 'crypto';
UPDATE qd_market_symbols SET asset_class = 'forex'
WHERE market = 'Forex' AND asset_class = 'crypto';
UPDATE qd_market_symbols SET asset_class = 'futures'
WHERE market = 'Futures' AND asset_class = 'crypto';
UPDATE qd_market_symbols SET is_hot = 1, sort_order = GREATEST(sort_order, 80)
WHERE market = 'HKStock' AND asset_class = 'etf' AND symbol IN (
  '02800','02801','02823','02828','02840','02846','03032','03033','03037',
  '03040','03067','03075','03088','03110','03188','03191','03416','03437'
);
UPDATE qd_market_symbols SET is_hot = 1, sort_order = GREATEST(sort_order, 80)
WHERE market = 'USStock' AND asset_class = 'etf' AND symbol IN (
  'SPY','QQQ','IWM','DIA','VTI','VOO','IVV','EFA','EEM','AGG','BND','TLT','IEF',
  'GLD','SLV','USO','XLF','XLK','XLE','XLV','XLI','XLY','XLP','XLU','VNQ','ARKK',
  'HYG','LQD','SCHD','VUG','VTV'
);
ALTER TABLE qd_market_symbols DROP CONSTRAINT IF EXISTS qd_market_symbols_market_symbol_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_market_symbols_venue_instrument
  ON qd_market_symbols(market, symbol, exchange, market_type, instrument_id);

UPDATE qd_market_symbols
SET is_active = 0
WHERE market = 'Crypto'
  AND exchange <> ''
  AND exchange NOT IN ('binance', 'bitget', 'bybit', 'okx', 'gate', 'htx');

CREATE TABLE IF NOT EXISTS qd_market_symbol_aliases (
    id SERIAL PRIMARY KEY,
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    alias VARCHAR(255) NOT NULL,
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(market, symbol, alias)
);

CREATE INDEX IF NOT EXISTS idx_market_symbol_aliases_lookup
  ON qd_market_symbol_aliases(market, alias);
CREATE INDEX IF NOT EXISTS idx_market_symbol_aliases_upper_lookup
  ON qd_market_symbol_aliases(market, UPPER(alias));

-- Seed data: Hot symbols for each market
INSERT INTO qd_market_symbols (market, symbol, name, exchange, currency, is_active, is_hot, sort_order) VALUES
-- USStock (US Stocks)
('USStock', 'AAPL', 'Apple Inc.', 'NASDAQ', 'USD', 1, 1, 100),
('USStock', 'MSFT', 'Microsoft Corporation', 'NASDAQ', 'USD', 1, 1, 99),
('USStock', 'GOOGL', 'Alphabet Inc.', 'NASDAQ', 'USD', 1, 1, 98),
('USStock', 'AMZN', 'Amazon.com Inc.', 'NASDAQ', 'USD', 1, 1, 97),
('USStock', 'TSLA', 'Tesla, Inc.', 'NASDAQ', 'USD', 1, 1, 96),
('USStock', 'META', 'Meta Platforms Inc.', 'NASDAQ', 'USD', 1, 1, 95),
('USStock', 'NVDA', 'NVIDIA Corporation', 'NASDAQ', 'USD', 1, 1, 94),
('USStock', 'JPM', 'JPMorgan Chase & Co.', 'NYSE', 'USD', 1, 1, 93),
('USStock', 'V', 'Visa Inc.', 'NYSE', 'USD', 1, 1, 92),
('USStock', 'JNJ', 'Johnson & Johnson', 'NYSE', 'USD', 1, 1, 91),
-- Crypto (major + popular altcoins)
('Crypto', 'BTC/USDT', 'Bitcoin', 'Binance', 'USDT', 1, 1, 100),
('Crypto', 'ETH/USDT', 'Ethereum', 'Binance', 'USDT', 1, 1, 99),
('Crypto', 'BNB/USDT', 'BNB', 'Binance', 'USDT', 1, 1, 98),
('Crypto', 'SOL/USDT', 'Solana', 'Binance', 'USDT', 1, 1, 97),
('Crypto', 'XRP/USDT', 'Ripple', 'Binance', 'USDT', 1, 1, 96),
('Crypto', 'ADA/USDT', 'Cardano', 'Binance', 'USDT', 1, 1, 95),
('Crypto', 'DOGE/USDT', 'Dogecoin', 'Binance', 'USDT', 1, 1, 94),
('Crypto', 'DOT/USDT', 'Polkadot', 'Binance', 'USDT', 1, 1, 93),
('Crypto', 'POL/USDT', 'Polygon', 'Binance', 'USDT', 1, 1, 92),
('Crypto', 'AVAX/USDT', 'Avalanche', 'Binance', 'USDT', 1, 1, 91),
-- Layer 1 / Layer 2
('Crypto', 'LINK/USDT', 'Chainlink', 'Binance', 'USDT', 1, 1, 90),
('Crypto', 'UNI/USDT', 'Uniswap', 'Binance', 'USDT', 1, 1, 89),
('Crypto', 'ATOM/USDT', 'Cosmos', 'Binance', 'USDT', 1, 1, 88),
('Crypto', 'LTC/USDT', 'Litecoin', 'Binance', 'USDT', 1, 1, 87),
('Crypto', 'FIL/USDT', 'Filecoin', 'Binance', 'USDT', 1, 1, 86),
('Crypto', 'NEAR/USDT', 'NEAR Protocol', 'Binance', 'USDT', 1, 1, 85),
('Crypto', 'APT/USDT', 'Aptos', 'Binance', 'USDT', 1, 1, 84),
('Crypto', 'SUI/USDT', 'Sui', 'Binance', 'USDT', 1, 1, 83),
('Crypto', 'ARB/USDT', 'Arbitrum', 'Binance', 'USDT', 1, 1, 82),
('Crypto', 'OP/USDT', 'Optimism', 'Binance', 'USDT', 1, 1, 81),
('Crypto', 'SEI/USDT', 'Sei', 'Binance', 'USDT', 1, 1, 80),
('Crypto', 'TIA/USDT', 'Celestia', 'Binance', 'USDT', 1, 1, 79),
('Crypto', 'INJ/USDT', 'Injective', 'Binance', 'USDT', 1, 1, 78),
('Crypto', 'FTM/USDT', 'Fantom', 'Binance', 'USDT', 1, 1, 77),
('Crypto', 'ALGO/USDT', 'Algorand', 'Binance', 'USDT', 1, 1, 76),
('Crypto', 'HBAR/USDT', 'Hedera', 'Binance', 'USDT', 1, 1, 75),
('Crypto', 'ICP/USDT', 'Internet Computer', 'Binance', 'USDT', 1, 1, 74),
('Crypto', 'VET/USDT', 'VeChain', 'Binance', 'USDT', 1, 1, 73),
('Crypto', 'SAND/USDT', 'The Sandbox', 'Binance', 'USDT', 1, 1, 72),
('Crypto', 'MANA/USDT', 'Decentraland', 'Binance', 'USDT', 1, 1, 71),
-- DeFi
('Crypto', 'AAVE/USDT', 'Aave', 'Binance', 'USDT', 1, 1, 70),
('Crypto', 'MKR/USDT', 'Maker', 'Binance', 'USDT', 1, 1, 69),
('Crypto', 'CRV/USDT', 'Curve DAO', 'Binance', 'USDT', 1, 1, 68),
('Crypto', 'COMP/USDT', 'Compound', 'Binance', 'USDT', 1, 1, 67),
('Crypto', 'SNX/USDT', 'Synthetix', 'Binance', 'USDT', 1, 1, 66),
('Crypto', 'SUSHI/USDT', 'SushiSwap', 'Binance', 'USDT', 1, 1, 65),
('Crypto', 'DYDX/USDT', 'dYdX', 'Binance', 'USDT', 1, 1, 64),
('Crypto', 'LDO/USDT', 'Lido DAO', 'Binance', 'USDT', 1, 1, 63),
('Crypto', 'PENDLE/USDT', 'Pendle', 'Binance', 'USDT', 1, 1, 62),
('Crypto', 'JUP/USDT', 'Jupiter', 'Binance', 'USDT', 1, 1, 61),
-- Meme coins
('Crypto', 'SHIB/USDT', 'Shiba Inu', 'Binance', 'USDT', 1, 1, 60),
('Crypto', 'PEPE/USDT', 'Pepe', 'Binance', 'USDT', 1, 1, 59),
('Crypto', 'WIF/USDT', 'dogwifhat', 'Binance', 'USDT', 1, 1, 58),
('Crypto', 'FLOKI/USDT', 'Floki', 'Binance', 'USDT', 1, 1, 57),
('Crypto', 'BONK/USDT', 'Bonk', 'Binance', 'USDT', 1, 1, 56),
('Crypto', 'MEME/USDT', 'Memecoin', 'Binance', 'USDT', 1, 1, 55),
('Crypto', 'TURBO/USDT', 'Turbo', 'Binance', 'USDT', 1, 1, 54),
('Crypto', 'NEIRO/USDT', 'Neiro', 'Binance', 'USDT', 1, 1, 53),
-- AI / Infra
('Crypto', 'RENDER/USDT', 'Render', 'Binance', 'USDT', 1, 1, 52),
('Crypto', 'FET/USDT', 'Fetch.ai', 'Binance', 'USDT', 1, 1, 51),
('Crypto', 'RNDR/USDT', 'Render Network', 'Binance', 'USDT', 1, 1, 50),
('Crypto', 'TAO/USDT', 'Bittensor', 'Binance', 'USDT', 1, 1, 49),
('Crypto', 'WLD/USDT', 'Worldcoin', 'Binance', 'USDT', 1, 1, 48),
('Crypto', 'AR/USDT', 'Arweave', 'Binance', 'USDT', 1, 1, 47),
('Crypto', 'STX/USDT', 'Stacks', 'Binance', 'USDT', 1, 1, 46),
('Crypto', 'ORDI/USDT', 'ORDI', 'Binance', 'USDT', 1, 1, 45),
-- Others
('Crypto', 'TRX/USDT', 'Tron', 'Binance', 'USDT', 1, 1, 44),
('Crypto', 'ETC/USDT', 'Ethereum Classic', 'Binance', 'USDT', 1, 1, 43),
('Crypto', 'THETA/USDT', 'Theta Network', 'Binance', 'USDT', 1, 1, 42),
('Crypto', 'EOS/USDT', 'EOS', 'Binance', 'USDT', 1, 1, 41),
('Crypto', 'XLM/USDT', 'Stellar', 'Binance', 'USDT', 1, 1, 40),
('Crypto', 'GALA/USDT', 'Gala', 'Binance', 'USDT', 1, 1, 39),
('Crypto', 'IMX/USDT', 'Immutable X', 'Binance', 'USDT', 1, 1, 38),
('Crypto', 'CFX/USDT', 'Conflux', 'Binance', 'USDT', 1, 1, 37),
('Crypto', 'JASMY/USDT', 'JasmyCoin', 'Binance', 'USDT', 1, 1, 36),
('Crypto', 'CHZ/USDT', 'Chiliz', 'Binance', 'USDT', 1, 1, 35),
('Crypto', 'GMT/USDT', 'STEPN', 'Binance', 'USDT', 1, 1, 34),
('Crypto', 'CAKE/USDT', 'PancakeSwap', 'Binance', 'USDT', 1, 1, 33),
('Crypto', '1INCH/USDT', '1inch', 'Binance', 'USDT', 1, 1, 32),
('Crypto', 'ENS/USDT', 'Ethereum Name Service', 'Binance', 'USDT', 1, 1, 31),
('Crypto', 'BLUR/USDT', 'Blur', 'Binance', 'USDT', 1, 1, 30),
-- Forex
('Forex', 'XAUUSD', 'Gold/USD', 'Forex', 'USD', 1, 1, 100),
('Forex', 'XAGUSD', 'Silver/USD', 'Forex', 'USD', 1, 1, 99),
('Forex', 'EURUSD', 'Euro/US Dollar', 'Forex', 'USD', 1, 1, 98),
('Forex', 'GBPUSD', 'British Pound/US Dollar', 'Forex', 'USD', 1, 1, 97),
('Forex', 'USDJPY', 'US Dollar/Japanese Yen', 'Forex', 'USD', 1, 1, 96),
('Forex', 'AUDUSD', 'Australian Dollar/US Dollar', 'Forex', 'USD', 1, 1, 95),
('Forex', 'USDCAD', 'US Dollar/Canadian Dollar', 'Forex', 'USD', 1, 1, 94),
('Forex', 'NZDUSD', 'New Zealand Dollar/US Dollar', 'Forex', 'USD', 1, 1, 93),
('Forex', 'USDCHF', 'US Dollar/Swiss Franc', 'Forex', 'EUR', 1, 1, 92),
('Forex', 'EURJPY', 'Euro/Japanese Yen', 'Forex', 'EUR', 1, 1, 91),
-- Futures
('Futures', 'CL', 'WTI Crude Oil', 'NYMEX', 'USD', 1, 1, 100),
('Futures', 'GC', 'Gold', 'COMEX', 'USD', 1, 1, 99),
('Futures', 'SI', 'Silver', 'COMEX', 'USD', 1, 1, 98),
('Futures', 'NG', 'Natural Gas', 'NYMEX', 'USD', 1, 1, 97),
('Futures', 'HG', 'Copper', 'COMEX', 'USD', 1, 1, 96),
('Futures', 'ZC', 'Corn', 'CBOT', 'USD', 1, 1, 95),
('Futures', 'ZS', 'Soybeans', 'CBOT', 'USD', 1, 1, 94),
('Futures', 'ZW', 'Wheat', 'CBOT', 'USD', 1, 1, 93),
('Futures', 'ES', 'S&P 500 E-mini', 'CME', 'USD', 1, 1, 92),
('Futures', 'NQ', 'NASDAQ 100 E-mini', 'CME', 'USD', 1, 1, 91),
-- A-share hot symbols use the canonical exchange identifier from the symbol master.
('CNStock', '600519', '贵州茅台', 'CN', 'CNY', 1, 1, 100),
('CNStock', '600036', '招商银行', 'CN', 'CNY', 1, 1, 99),
('CNStock', '601318', '中国平安', 'CN', 'CNY', 1, 1, 98),
('CNStock', '600900', '长江电力', 'CN', 'CNY', 1, 1, 97),
('CNStock', '601899', '紫金矿业', 'CN', 'CNY', 1, 1, 96),
('CNStock', '000858', '五粮液', 'CN', 'CNY', 1, 1, 95),
('CNStock', '000333', '美的集团', 'CN', 'CNY', 1, 1, 94),
('CNStock', '002594', '比亚迪', 'CN', 'CNY', 1, 1, 93),
('CNStock', '300750', '宁德时代', 'CN', 'CNY', 1, 1, 92),
('CNStock', '000001', '平安银行', 'CN', 'CNY', 1, 1, 91),
-- Hong Kong hot symbols.
('HKStock', '00700', '腾讯控股', 'HKEX', 'HKD', 1, 1, 100),
('HKStock', '09988', '阿里巴巴-W', 'HKEX', 'HKD', 1, 1, 99),
('HKStock', '03690', '美团-W', 'HKEX', 'HKD', 1, 1, 98),
('HKStock', '01810', '小米集团-W', 'HKEX', 'HKD', 1, 1, 97),
('HKStock', '00939', '建设银行', 'HKEX', 'HKD', 1, 1, 96),
('HKStock', '01299', '友邦保险', 'HKEX', 'HKD', 1, 1, 95),
('HKStock', '02318', '中国平安', 'HKEX', 'HKD', 1, 1, 94),
('HKStock', '00388', '香港交易所', 'HKEX', 'HKD', 1, 1, 93),
('HKStock', '00883', '中国海洋石油', 'HKEX', 'HKD', 1, 1, 92),
('HKStock', '01398', '工商银行', 'HKEX', 'HKD', 1, 1, 91),
-- MOEX (Moscow Exchange) blue chips
-- Tickers are the MOEX ISS instrument codes; resolve_symbol_name() upgrades
-- the display name from MOEX ISS securities/<sym>.json on first lookup.
('MOEX', 'SBER',  'Sberbank',          'MOEX', 'RUB', 1, 1, 100),
('MOEX', 'GAZP',  'Gazprom',           'MOEX', 'RUB', 1, 1, 99),
('MOEX', 'LKOH',  'Lukoil',            'MOEX', 'RUB', 1, 1, 98),
('MOEX', 'ROSN',  'Rosneft',           'MOEX', 'RUB', 1, 1, 97),
('MOEX', 'GMKN',  'Nornickel',         'MOEX', 'RUB', 1, 1, 96),
('MOEX', 'NVTK',  'Novatek',           'MOEX', 'RUB', 1, 1, 95),
('MOEX', 'TATN',  'Tatneft',           'MOEX', 'RUB', 1, 1, 94),
('MOEX', 'VTBR',  'VTB Bank',          'MOEX', 'RUB', 1, 1, 93),
('MOEX', 'MGNT',  'Magnit',            'MOEX', 'RUB', 1, 1, 92),
('MOEX', 'YNDX',  'Yandex',            'MOEX', 'RUB', 1, 1, 91),
('MOEX', 'SBERP', 'Sberbank Preferred','MOEX', 'RUB', 1, 1, 90),
('MOEX', 'PLZL',  'Polyus',            'MOEX', 'RUB', 1, 1, 89),
('MOEX', 'CHMF',  'Severstal',         'MOEX', 'RUB', 1, 1, 88),
('MOEX', 'ALRS',  'Alrosa',            'MOEX', 'RUB', 1, 1, 87),
('MOEX', 'MOEX',  'Moscow Exchange',   'MOEX', 'RUB', 1, 1, 86)
ON CONFLICT (market, symbol, exchange, market_type, instrument_id) DO NOTHING;

-- Remove legacy A-share rows that used venue-specific exchange identifiers.
-- Canonical symbol-master rows use exchange = 'CN'; the old identifiers caused
-- duplicate search results because exchange is part of the uniqueness key.
UPDATE qd_market_symbols canonical
SET is_hot = GREATEST(canonical.is_hot, legacy.is_hot),
    sort_order = GREATEST(canonical.sort_order, legacy.sort_order)
FROM qd_market_symbols legacy
WHERE canonical.market = 'CNStock'
  AND canonical.exchange = 'CN'
  AND legacy.market = canonical.market
  AND legacy.symbol = canonical.symbol
  AND legacy.exchange IN ('SSE', 'SZSE')
  AND legacy.market_type = canonical.market_type
  AND legacy.instrument_id = canonical.instrument_id;

DELETE FROM qd_market_symbols legacy
USING qd_market_symbols canonical
WHERE legacy.market = 'CNStock'
  AND legacy.exchange IN ('SSE', 'SZSE')
  AND canonical.market = legacy.market
  AND canonical.symbol = legacy.symbol
  AND canonical.exchange = 'CN'
  AND canonical.market_type = legacy.market_type
  AND canonical.instrument_id = legacy.instrument_id;

-- =============================================================================
-- 19.5. Analysis Memory (Fast AI Analysis Memory System)
-- =============================================================================
-- Stores AI analysis results for history, feedback, and learning.

CREATE TABLE IF NOT EXISTS qd_analysis_memory (
    id SERIAL PRIMARY KEY,
    user_id INT,                                -- User who created this analysis (for filtering)
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    decision VARCHAR(10) NOT NULL,
    confidence INT DEFAULT 50,
    price_at_analysis DECIMAL(24, 8),
    summary TEXT,
    reasons JSONB,
    scores JSONB,
    indicators_snapshot JSONB,
    raw_result JSONB,                           -- Full analysis result for history replay
    consensus_score DECIMAL(24, 8),
    consensus_abs DECIMAL(24, 8),
    agreement_ratio DECIMAL(10, 6),
    quality_multiplier DECIMAL(10, 6),
    created_at TIMESTAMP DEFAULT NOW(),
    validated_at TIMESTAMP,
    actual_outcome VARCHAR(20),
    actual_return_pct DECIMAL(10, 4),
    was_correct BOOLEAN,
    user_feedback VARCHAR(20),                  -- helpful/not_helpful
    feedback_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_analysis_memory_symbol ON qd_analysis_memory(market, symbol);
CREATE INDEX IF NOT EXISTS idx_analysis_memory_created ON qd_analysis_memory(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_memory_validated ON qd_analysis_memory(validated_at) WHERE validated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_analysis_memory_user ON qd_analysis_memory(user_id);

-- Migration: Add user_id column to existing qd_analysis_memory table
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'qd_analysis_memory' AND column_name = 'user_id'
    ) THEN
        ALTER TABLE qd_analysis_memory ADD COLUMN user_id INT;
        CREATE INDEX IF NOT EXISTS idx_analysis_memory_user ON qd_analysis_memory(user_id);
        RAISE NOTICE 'Added user_id column to qd_analysis_memory';
    END IF;
END $$;

-- =============================================================================
-- 20. Migration: Add token_version for single-client login
-- =============================================================================
-- This migration adds token_version column for enforcing single-client login.
-- When a user logs in from a new device, the token_version is incremented,
-- invalidating all previous tokens and forcing other sessions to logout.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'qd_users' AND column_name = 'token_version'
    ) THEN
        ALTER TABLE qd_users ADD COLUMN token_version INTEGER DEFAULT 1;
        RAISE NOTICE 'Added token_version column to qd_users table';
    END IF;
END $$;

-- =============================================================================
-- 20b. Migration: user profile timezone (IANA)
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'qd_users' AND column_name = 'timezone'
    ) THEN
        ALTER TABLE qd_users ADD COLUMN timezone VARCHAR(64) DEFAULT '';
        RAISE NOTICE 'Added timezone column to qd_users table';
    END IF;
END $$;

-- =============================================================================
-- 20c. Migration: password_changed_at (initial password reminder)
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'qd_users' AND column_name = 'password_changed_at'
    ) THEN
        ALTER TABLE qd_users ADD COLUMN password_changed_at TIMESTAMP NULL;
        -- One-time backfill when upgrading old DBs (skip on fresh installs after bootstrap user exists)
        UPDATE qd_users
        SET password_changed_at = COALESCE(updated_at, created_at, NOW())
        WHERE password_changed_at IS NULL;
        RAISE NOTICE 'Added password_changed_at column to qd_users table (existing users backfilled)';
    END IF;
END $$;

-- =============================================================================
-- 20e. Stateful Strategy API runtime and order intent infrastructure
-- =============================================================================

CREATE TABLE IF NOT EXISTS strategy_runs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    strategy_id INTEGER NOT NULL,
    source_version_id VARCHAR(64) NOT NULL DEFAULT '',
    code_hash VARCHAR(128) NOT NULL DEFAULT '',
    parameter_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    account_id VARCHAR(64) NOT NULL DEFAULT '',
    exchange_id VARCHAR(50) NOT NULL DEFAULT '',
    credential_id INTEGER NOT NULL DEFAULT 0,
    symbol VARCHAR(80) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'swap',
    position_mode VARCHAR(20) NOT NULL DEFAULT '',
    runtime_status VARCHAR(32) NOT NULL DEFAULT 'running',
    runtime_epoch BIGINT NOT NULL DEFAULT 1,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    stopped_at TIMESTAMP,
    stop_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strategy_runs_strategy ON strategy_runs(strategy_id, runtime_status);
CREATE INDEX IF NOT EXISTS idx_strategy_runs_started ON strategy_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS strategy_runtime_state (
    id SERIAL PRIMARY KEY,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    strategy_id INTEGER NOT NULL,
    state_key VARCHAR(128) NOT NULL,
    state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(strategy_run_id, strategy_id, state_key)
);
CREATE INDEX IF NOT EXISTS idx_strategy_runtime_state_strategy ON strategy_runtime_state(strategy_id);

CREATE TABLE IF NOT EXISTS strategy_order_intents (
    id SERIAL PRIMARY KEY,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    strategy_id INTEGER NOT NULL,
    idempotency_key VARCHAR(180) NOT NULL,
    symbol VARCHAR(80) NOT NULL,
    market_type VARCHAR(20) NOT NULL DEFAULT 'swap',
    side VARCHAR(10) NOT NULL,
    position_side VARCHAR(10) NOT NULL DEFAULT '',
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    order_type VARCHAR(24) NOT NULL DEFAULT 'market',
    quantity DECIMAL(28, 12) NOT NULL DEFAULT 0,
    notional DECIMAL(28, 12) NOT NULL DEFAULT 0,
    limit_price DECIMAL(28, 12) NOT NULL DEFAULT 0,
    execution_algo VARCHAR(32) NOT NULL DEFAULT 'market',
    portfolio_id VARCHAR(96) NOT NULL DEFAULT '',
    universe_id VARCHAR(96) NOT NULL DEFAULT '',
    rebalance_group_id VARCHAR(128) NOT NULL DEFAULT '',
    target_weight DECIMAL(18, 10),
    target_notional DECIMAL(28, 12),
    target_position_qty DECIMAL(28, 12),
    status VARCHAR(32) NOT NULL DEFAULT 'intent_created',
    client_order_id VARCHAR(100) NOT NULL DEFAULT '',
    exchange_order_id VARCHAR(100) NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(strategy_run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_strategy_order_intents_strategy ON strategy_order_intents(strategy_id, status);

CREATE TABLE IF NOT EXISTS strategy_order_fills (
    id SERIAL PRIMARY KEY,
    order_intent_id INTEGER NOT NULL DEFAULT 0,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    strategy_id INTEGER NOT NULL DEFAULT 0,
    exchange_id VARCHAR(50) NOT NULL DEFAULT '',
    exchange_order_id VARCHAR(100) NOT NULL DEFAULT '',
    exchange_fill_id VARCHAR(128) NOT NULL DEFAULT '',
    side VARCHAR(10) NOT NULL DEFAULT '',
    position_side VARCHAR(10) NOT NULL DEFAULT '',
    price DECIMAL(28, 12) NOT NULL DEFAULT 0,
    quantity DECIMAL(28, 12) NOT NULL DEFAULT 0,
    notional DECIMAL(28, 12) NOT NULL DEFAULT 0,
    fee DECIMAL(28, 12) NOT NULL DEFAULT 0,
    fee_ccy VARCHAR(20) NOT NULL DEFAULT '',
    filled_at TIMESTAMP NOT NULL DEFAULT NOW(),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_strategy_order_fills_intent ON strategy_order_fills(order_intent_id);
CREATE INDEX IF NOT EXISTS idx_strategy_order_fills_strategy ON strategy_order_fills(strategy_id, filled_at DESC);

CREATE TABLE IF NOT EXISTS strategy_runtime_events (
    id SERIAL PRIMARY KEY,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    strategy_id INTEGER NOT NULL DEFAULT 0,
    event_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_runtime_events_run ON strategy_runtime_events(strategy_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_runtime_locks (
    lock_key VARCHAR(180) PRIMARY KEY,
    strategy_run_id INTEGER NOT NULL DEFAULT 0,
    runtime_epoch BIGINT NOT NULL DEFAULT 1,
    owner VARCHAR(100) NOT NULL DEFAULT '',
    expires_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
-- =============================================================================
-- Durable process roles, strategy commands, runtime leases, and worker health
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_strategy_commands (
    id BIGSERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL DEFAULT 0,
    command_type VARCHAR(24) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMP NOT NULL DEFAULT NOW(),
    claimed_by VARCHAR(160) NOT NULL DEFAULT '',
    claimed_at TIMESTAMP,
    lease_expires_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CHECK (command_type IN ('start', 'stop', 'restart', 'reconcile')),
    CHECK (status IN ('pending', 'processing', 'succeeded', 'failed', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_strategy_commands_claim
    ON qd_strategy_commands(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_strategy_commands_strategy
    ON qd_strategy_commands(strategy_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_commands_active_action
    ON qd_strategy_commands(strategy_id, command_type)
    WHERE status IN ('pending', 'processing');

CREATE TABLE IF NOT EXISTS qd_strategy_runtime_leases (
    strategy_id INTEGER PRIMARY KEY,
    owner_id VARCHAR(160) NOT NULL,
    fencing_token BIGINT NOT NULL DEFAULT 1,
    lease_expires_at TIMESTAMP NOT NULL,
    heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_runtime_leases_expiry
    ON qd_strategy_runtime_leases(lease_expires_at);

CREATE TABLE IF NOT EXISTS qd_worker_heartbeats (
    worker_id VARCHAR(160) PRIMARY KEY,
    role VARCHAR(32) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'running',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CHECK (role IN ('api', 'trading', 'scheduler')),
    CHECK (status IN ('running', 'stopped', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_role
    ON qd_worker_heartbeats(role, heartbeat_at DESC);
DELETE FROM qd_worker_heartbeats WHERE role NOT IN ('api', 'trading', 'scheduler');
ALTER TABLE qd_worker_heartbeats DROP CONSTRAINT IF EXISTS qd_worker_heartbeats_role_check;
ALTER TABLE qd_worker_heartbeats
    ADD CONSTRAINT qd_worker_heartbeats_role_check
    CHECK (role IN ('api', 'trading', 'scheduler'));
CREATE TABLE IF NOT EXISTS qd_process_leases (
    lease_key VARCHAR(128) PRIMARY KEY,
    owner_id VARCHAR(160) NOT NULL,
    lease_expires_at TIMESTAMP NOT NULL,
    heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_process_leases_expiry
    ON qd_process_leases(lease_expires_at);


CREATE TABLE IF NOT EXISTS qd_account_positions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES qd_users(id) ON DELETE CASCADE,
    credential_id INTEGER NOT NULL DEFAULT 0,
    exchange_id VARCHAR(40) NOT NULL DEFAULT '',
    market_type VARCHAR(20) NOT NULL DEFAULT 'swap',
    inst_id VARCHAR(80) NOT NULL DEFAULT '',
    symbol VARCHAR(50) NOT NULL DEFAULT '',
    side VARCHAR(10) NOT NULL DEFAULT '',
    size DECIMAL(24, 8) NOT NULL DEFAULT 0,
    entry_price DECIMAL(24, 8) DEFAULT 0,
    mark_price DECIMAL(24, 8) DEFAULT 0,
    unrealized_pnl DECIMAL(24, 8) DEFAULT 0,
    raw_json JSONB DEFAULT '{}'::jsonb,
    synced_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (credential_id, market_type, inst_id, side)
);
CREATE INDEX IF NOT EXISTS idx_account_pos_user ON qd_account_positions(user_id);
CREATE INDEX IF NOT EXISTS idx_account_pos_cred ON qd_account_positions(credential_id, market_type);

-- =============================================================================
-- 21. Indicator Community Tables
-- =============================================================================


CREATE TABLE IF NOT EXISTS qd_indicator_purchases (
    id SERIAL PRIMARY KEY,
    indicator_id INTEGER NOT NULL REFERENCES qd_indicator_codes(id) ON DELETE CASCADE,
    buyer_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    seller_id INTEGER NOT NULL REFERENCES qd_users(id),
    price DECIMAL(10,2) NOT NULL DEFAULT 0,
    gross_price DECIMAL(10,2),
    platform_fee DECIMAL(10,2) DEFAULT 0,
    seller_amount DECIMAL(10,2),
    fee_rate DECIMAL(10,6) DEFAULT 0,
    asset_name_snapshot VARCHAR(255),
    asset_description_snapshot TEXT,
    asset_code_snapshot TEXT,
    asset_type_snapshot VARCHAR(32),
    asset_preview_image_snapshot VARCHAR(500),
    asset_is_encrypted_snapshot INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(indicator_id, buyer_id)
);

CREATE INDEX IF NOT EXISTS idx_purchases_indicator ON qd_indicator_purchases(indicator_id);
CREATE INDEX IF NOT EXISTS idx_purchases_buyer ON qd_indicator_purchases(buyer_id);
CREATE INDEX IF NOT EXISTS idx_purchases_seller ON qd_indicator_purchases(seller_id);


CREATE TABLE IF NOT EXISTS qd_indicator_comments (
    id SERIAL PRIMARY KEY,
    indicator_id INTEGER NOT NULL REFERENCES qd_indicator_codes(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    rating INTEGER DEFAULT 5 CHECK (rating >= 1 AND rating <= 5),
    content TEXT DEFAULT '',
    parent_id INTEGER REFERENCES qd_indicator_comments(id) ON DELETE CASCADE,
    is_deleted INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comments_indicator ON qd_indicator_comments(indicator_id);
CREATE INDEX IF NOT EXISTS idx_comments_user ON qd_indicator_comments(user_id);

-- =============================================================================
-- Quick Trades (manual / discretionary orders from Quick Trade Panel)
-- =============================================================================
CREATE TABLE IF NOT EXISTS qd_quick_trades (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    credential_id   INTEGER DEFAULT 0,
    exchange_id     VARCHAR(40) NOT NULL DEFAULT '',
    symbol          VARCHAR(60) NOT NULL DEFAULT '',
    side            VARCHAR(10) NOT NULL DEFAULT '',       -- buy / sell
    order_type      VARCHAR(20) NOT NULL DEFAULT 'market', -- market / limit
    amount          DECIMAL(24, 8) DEFAULT 0,
    price           DECIMAL(24, 8) DEFAULT 0,
    leverage        INTEGER DEFAULT 1,
    market_type     VARCHAR(20) DEFAULT 'swap',            -- swap / spot
    tp_price        DECIMAL(24, 8) DEFAULT 0,
    sl_price        DECIMAL(24, 8) DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'submitted',       -- submitted / filled / failed / cancelled
    exchange_order_id VARCHAR(120) DEFAULT '',
    filled_amount   DECIMAL(24, 8) DEFAULT 0,
    avg_fill_price  DECIMAL(24, 8) DEFAULT 0,
    commission      DECIMAL(24, 8) DEFAULT 0,              -- realised trading fee for this fill (best-effort)
    commission_ccy  VARCHAR(16) DEFAULT '',                -- e.g. 'USDT' / 'BNB'; empty when unknown
    commission_quote DECIMAL(24, 8),
    error_msg       TEXT DEFAULT '',
    source          VARCHAR(40) DEFAULT 'manual',          -- ai_radar / ai_analysis / indicator / manual
    raw_result      JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_quick_trades_user    ON qd_quick_trades(user_id);
CREATE INDEX IF NOT EXISTS idx_quick_trades_created ON qd_quick_trades(created_at DESC);

-- Migration: Add commission tracking columns to existing qd_quick_trades.
-- (Introduced in v3.0.8. Pre-existing rows default to 0 / '' which is the

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'qd_quick_trades' AND column_name = 'commission'
    ) THEN
        ALTER TABLE qd_quick_trades ADD COLUMN commission DECIMAL(24, 8) DEFAULT 0;
        ALTER TABLE qd_quick_trades ADD COLUMN commission_ccy VARCHAR(16) DEFAULT '';
        RAISE NOTICE 'Added commission / commission_ccy columns to qd_quick_trades';
    END IF;
END $$;

ALTER TABLE qd_strategy_trades ADD COLUMN IF NOT EXISTS commission_quote DECIMAL(24,8);
ALTER TABLE qd_quick_trades ADD COLUMN IF NOT EXISTS commission DECIMAL(24,8) DEFAULT 0;
ALTER TABLE qd_quick_trades ADD COLUMN IF NOT EXISTS commission_ccy VARCHAR(16) DEFAULT '';
ALTER TABLE qd_quick_trades ADD COLUMN IF NOT EXISTS commission_quote DECIMAL(24,8);
UPDATE qd_strategy_trades
SET commission_quote = commission
WHERE commission_quote IS NULL
  AND UPPER(COALESCE(commission_ccy, '')) IN ('USD', 'USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD');
UPDATE qd_quick_trades
SET commission_quote = commission
WHERE commission_quote IS NULL
  AND UPPER(COALESCE(commission_ccy, '')) IN ('USD', 'USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD');

-- =============================================================================
-- Polymarket (宸茬Щ闄?/ removed in v3.0.7)
-- =============================================================================


DROP TABLE IF EXISTS qd_polymarket_asset_opportunities CASCADE;
DROP TABLE IF EXISTS qd_polymarket_ai_analysis CASCADE;
DROP TABLE IF EXISTS qd_polymarket_markets CASCADE;

-- =============================================================================

-- =============================================================================
-- These tables back the multi-agent runtime (see docs/agent/AI_INTEGRATION_DESIGN.md).
-- They are tenant-scoped via user_id and stay isolated from human JWT sessions.

CREATE TABLE IF NOT EXISTS qd_agent_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    name VARCHAR(80) NOT NULL,
    token_prefix VARCHAR(24) NOT NULL,           -- e.g. "qd_agent_AbCdEf12" (shown to humans/audit only)
    token_hash VARCHAR(128) NOT NULL,            -- sha256(token) hex
    scopes TEXT NOT NULL DEFAULT 'R',            -- comma-separated subset of R,W,B,N,C,T
    markets TEXT NOT NULL DEFAULT '*',           -- comma-separated allowlist or '*'
    instruments TEXT NOT NULL DEFAULT '*',       -- comma-separated allowlist or '*'
    paper_only BOOLEAN NOT NULL DEFAULT TRUE,    -- T-class always starts paper-only
    rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
    status VARCHAR(20) NOT NULL DEFAULT 'active',-- active/revoked/expired
    expires_at TIMESTAMP,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tokens_hash ON qd_agent_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_user ON qd_agent_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_status ON qd_agent_tokens(status);

CREATE TABLE IF NOT EXISTS qd_agent_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(40) NOT NULL UNIQUE,          -- public id (uuid4 hex)
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    agent_token_id INTEGER REFERENCES qd_agent_tokens(id) ON DELETE SET NULL,
    kind VARCHAR(40) NOT NULL,                   -- backtest
    status VARCHAR(20) NOT NULL DEFAULT 'queued',-- queued/running/succeeded/failed/cancelled
    request JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error TEXT,
    progress JSONB,
    idempotency_key VARCHAR(120),
    created_at TIMESTAMP DEFAULT NOW(),
    started_at TIMESTAMP,
    finished_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_user ON qd_agent_jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status ON qd_agent_jobs(status);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_kind ON qd_agent_jobs(kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_idem
    ON qd_agent_jobs(agent_token_id, kind, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS qd_agent_audit (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    agent_token_id INTEGER,
    agent_name VARCHAR(80),
    route VARCHAR(160) NOT NULL,
    method VARCHAR(8) NOT NULL,
    scope_class VARCHAR(4) NOT NULL,             -- R / W / B / N / C / T
    status_code INTEGER NOT NULL,
    idempotency_key VARCHAR(120),
    request_summary JSONB,                       -- redacted (no secrets)
    response_summary JSONB,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_user ON qd_agent_audit(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_audit_token ON qd_agent_audit(agent_token_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_audit_class ON qd_agent_audit(scope_class);

-- Paper-only ledger so trading-class tokens can simulate without ever touching
-- live exchange credentials.  Real-money execution stays gated by paper_only=false
-- AND the existing TradingExecutor code path.
CREATE TABLE IF NOT EXISTS qd_agent_paper_orders (
    id BIGSERIAL PRIMARY KEY,
    order_uid VARCHAR(40) NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    agent_token_id INTEGER REFERENCES qd_agent_tokens(id) ON DELETE SET NULL,
    market VARCHAR(40) NOT NULL,
    symbol VARCHAR(60) NOT NULL,
    side VARCHAR(8) NOT NULL,                    -- buy / sell
    order_type VARCHAR(16) NOT NULL DEFAULT 'market',
    qty DECIMAL(28,10) NOT NULL,
    limit_price DECIMAL(28,10),
    fill_price DECIMAL(28,10),
    fill_value DECIMAL(28,10),
    status VARCHAR(16) NOT NULL DEFAULT 'filled',-- filled / cancelled / rejected
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_paper_orders_user ON qd_agent_paper_orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_paper_orders_token ON qd_agent_paper_orders(agent_token_id);

-- Jobs created before progress JSONB existed (Agent Gateway v3.1)
ALTER TABLE qd_agent_jobs ADD COLUMN IF NOT EXISTS progress JSONB;

-- Strategy API V2 templates are seeded by strategy_v2_templates.sql.

-- =============================================================================
-- Durable China A-share daily history
-- Keep this section aligned with 20260720_cn_market_history.sql.
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_cn_daily_bars (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL,
    code VARCHAR(6) NOT NULL,
    exchange VARCHAR(2) NOT NULL CHECK (exchange IN ('SH', 'SZ')),
    trade_date DATE NOT NULL,
    open DECIMAL(20, 6) NOT NULL CHECK (open > 0),
    high DECIMAL(20, 6) NOT NULL CHECK (high > 0),
    low DECIMAL(20, 6) NOT NULL CHECK (low > 0),
    close DECIMAL(20, 6) NOT NULL CHECK (close > 0),
    volume DECIMAL(28, 4) NOT NULL DEFAULT 0 CHECK (volume >= 0),
    amount DECIMAL(28, 4) NOT NULL DEFAULT 0 CHECK (amount >= 0),
    provider VARCHAR(32) NOT NULL,
    provider_version VARCHAR(32) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL,
    data_version INTEGER NOT NULL DEFAULT 1 CHECK (data_version > 0),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, trade_date, provider),
    CHECK (high >= GREATEST(open, close, low)),
    CHECK (low <= LEAST(open, close, high))
);
CREATE INDEX IF NOT EXISTS idx_cn_daily_bars_range ON qd_cn_daily_bars(instrument, trade_date);
CREATE INDEX IF NOT EXISTS idx_cn_daily_bars_provider_range ON qd_cn_daily_bars(provider, instrument, trade_date);

CREATE TABLE IF NOT EXISTS qd_cn_instruments (
    instrument VARCHAR(32) PRIMARY KEY,
    code VARCHAR(6) NOT NULL,
    exchange VARCHAR(2) NOT NULL CHECK (exchange IN ('SH', 'SZ')),
    name VARCHAR(255) NOT NULL DEFAULT '',
    security_type VARCHAR(24) NOT NULL DEFAULT 'ordinary_share',
    listed_on DATE,
    delisted_on DATE,
    source VARCHAR(32) NOT NULL,
    source_version VARCHAR(32) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (delisted_on IS NULL OR listed_on IS NULL OR delisted_on >= listed_on)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cn_instruments_code_exchange ON qd_cn_instruments(code, exchange);

CREATE TABLE IF NOT EXISTS qd_cn_instrument_classifications (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    classification VARCHAR(32) NOT NULL CHECK (classification IN ('main_board', 'star_board', 'chinext', 'st', 'non_st', 'delisting')),
    effective_start DATE NOT NULL,
    effective_end DATE,
    source VARCHAR(32) NOT NULL,
    source_version VARCHAR(32) NOT NULL DEFAULT '',
    confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, classification, effective_start, source),
    CHECK (effective_end IS NULL OR effective_end >= effective_start)
);
CREATE INDEX IF NOT EXISTS idx_cn_classifications_lookup ON qd_cn_instrument_classifications(instrument, effective_start, effective_end, confirmed);

CREATE TABLE IF NOT EXISTS qd_cn_daily_bar_revisions (
    id BIGSERIAL PRIMARY KEY,
    daily_bar_id BIGINT NOT NULL REFERENCES qd_cn_daily_bars(id) ON DELETE CASCADE,
    instrument VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    provider VARCHAR(32) NOT NULL,
    previous_version INTEGER NOT NULL,
    previous_content_hash VARCHAR(64) NOT NULL,
    previous_payload JSONB NOT NULL,
    replacement_content_hash VARCHAR(64) NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cn_daily_revisions_bar ON qd_cn_daily_bar_revisions(daily_bar_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_corporate_actions (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL,
    code VARCHAR(6) NOT NULL,
    exchange VARCHAR(2) NOT NULL CHECK (exchange IN ('SH', 'SZ')),
    event_date DATE NOT NULL,
    category INTEGER NOT NULL,
    event_name VARCHAR(80) NOT NULL DEFAULT '',
    cash_dividend DECIMAL(20, 8),
    rights_price DECIMAL(20, 8),
    bonus_ratio DECIMAL(20, 10),
    rights_ratio DECIMAL(20, 10),
    consolidation_ratio DECIMAL(20, 10),
    provider VARCHAR(32) NOT NULL,
    provider_version VARCHAR(32) NOT NULL DEFAULT '',
    event_key VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    data_version INTEGER NOT NULL DEFAULT 1 CHECK (data_version > 0),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, provider, event_key)
);
CREATE INDEX IF NOT EXISTS idx_cn_actions_range ON qd_cn_corporate_actions(instrument, event_date);

CREATE TABLE IF NOT EXISTS qd_cn_adjustment_factor_versions (
    factor_version VARCHAR(64) PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL,
    mode VARCHAR(16) NOT NULL CHECK (mode IN ('forward', 'backward')),
    algorithm_version VARCHAR(32) NOT NULL,
    action_data_version VARCHAR(64) NOT NULL,
    anchor_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'invalidated')),
    invalidated_reason TEXT NOT NULL DEFAULT '',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cn_factor_versions_active ON qd_cn_adjustment_factor_versions(instrument, mode, generated_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_adjustment_factors (
    instrument VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    mode VARCHAR(16) NOT NULL CHECK (mode IN ('forward', 'backward')),
    factor DECIMAL(28, 14) NOT NULL CHECK (factor > 0),
    factor_version VARCHAR(64) NOT NULL REFERENCES qd_cn_adjustment_factor_versions(factor_version) ON DELETE CASCADE,
    algorithm_version VARCHAR(32) NOT NULL,
    anchor_date DATE NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument, trade_date, mode, factor_version)
);
CREATE INDEX IF NOT EXISTS idx_cn_factors_lookup ON qd_cn_adjustment_factors(instrument, mode, factor_version, trade_date);

CREATE TABLE IF NOT EXISTS qd_cn_trading_status (
    instrument VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    status VARCHAR(24) NOT NULL CHECK (status IN ('trading', 'suspended', 'not_listed', 'delisted')),
    source VARCHAR(32) NOT NULL,
    source_version VARCHAR(32) NOT NULL DEFAULT '',
    confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument, trade_date, source)
);
CREATE INDEX IF NOT EXISTS idx_cn_trading_status_range ON qd_cn_trading_status(instrument, trade_date, confirmed);

CREATE TABLE IF NOT EXISTS qd_cn_history_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL UNIQUE,
    parent_run_id VARCHAR(40),
    requested_by INTEGER,
    request_kind VARCHAR(24) NOT NULL DEFAULT 'targeted',
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'paused', 'cancelled')),
    target_start DATE NOT NULL,
    target_end DATE NOT NULL,
    total_symbols INTEGER NOT NULL DEFAULT 0,
    succeeded_symbols INTEGER NOT NULL DEFAULT 0,
    failed_symbols INTEGER NOT NULL DEFAULT 0,
    skipped_symbols INTEGER NOT NULL DEFAULT 0,
    current_instrument VARCHAR(32) NOT NULL DEFAULT '',
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error_code VARCHAR(64) NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (target_end >= target_start)
);
CREATE INDEX IF NOT EXISTS idx_cn_sync_runs_status ON qd_cn_history_sync_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_history_sync_targets (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL REFERENCES qd_cn_history_sync_runs(run_id) ON DELETE CASCADE,
    instrument VARCHAR(32) NOT NULL,
    target_start DATE NOT NULL,
    target_end DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'paused', 'cancelled', 'skipped')),
    page_offset INTEGER NOT NULL DEFAULT 0,
    checkpoint_date DATE,
    attempts INTEGER NOT NULL DEFAULT 0,
    bars_written INTEGER NOT NULL DEFAULT 0,
    actions_written INTEGER NOT NULL DEFAULT 0,
    last_error_code VARCHAR(64) NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, instrument),
    CHECK (target_end >= target_start)
);
CREATE INDEX IF NOT EXISTS idx_cn_sync_targets_status ON qd_cn_history_sync_targets(run_id, status, id);

CREATE TABLE IF NOT EXISTS qd_cn_provider_health (
    provider VARCHAR(32) NOT NULL,
    host VARCHAR(255) NOT NULL,
    healthy BOOLEAN NOT NULL DEFAULT FALSE,
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms DECIMAL(12, 3),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error_code VARCHAR(64) NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, host)
);

CREATE TABLE IF NOT EXISTS qd_cn_history_quality_findings (
    id BIGSERIAL PRIMARY KEY,
    fingerprint VARCHAR(64) NOT NULL UNIQUE,
    instrument VARCHAR(32) NOT NULL,
    finding_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('info', 'warning', 'blocking')),
    status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    CHECK (end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS idx_cn_quality_open ON qd_cn_history_quality_findings(instrument, status, severity, start_date);

CREATE TABLE IF NOT EXISTS qd_cn_history_coverage (
    instrument VARCHAR(32) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    adjustment_mode VARCHAR(16) NOT NULL CHECK (adjustment_mode IN ('raw', 'forward', 'backward')),
    first_trade_date DATE,
    last_trade_date DATE,
    expected_sessions INTEGER NOT NULL DEFAULT 0,
    actual_sessions INTEGER NOT NULL DEFAULT 0,
    missing_sessions INTEGER NOT NULL DEFAULT 0,
    blocking_findings INTEGER NOT NULL DEFAULT 0,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    data_version VARCHAR(64) NOT NULL DEFAULT '',
    factor_version VARCHAR(64),
    gaps JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_successful_sync_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument, provider, adjustment_mode)
);
CREATE INDEX IF NOT EXISTS idx_cn_coverage_complete ON qd_cn_history_coverage(complete, updated_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_history_operation_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER,
    action VARCHAR(32) NOT NULL,
    run_id VARCHAR(40),
    request_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_status VARCHAR(20) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cn_history_audit_actor ON qd_cn_history_operation_audit(actor_user_id, created_at DESC);

-- Latest display quote per enabled Shanghai/Shenzhen A-share. This is a
-- latest-state read model, not an append-only intraday history table.
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

-- =============================================================================
-- Completion Notice
-- =============================================================================
DO $$
BEGIN
    RAISE NOTICE 'QuantDinger PostgreSQL schema initialized successfully!';
END $$;
-- External data provider observability. Safe to apply repeatedly.

CREATE TABLE IF NOT EXISTS qd_external_data_request_logs (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider VARCHAR(64) NOT NULL,
    data_domain VARCHAR(48) NOT NULL,
    operation VARCHAR(96) NOT NULL,
    call_source VARCHAR(96) NOT NULL DEFAULT '',
    subject_summary VARCHAR(512) NOT NULL DEFAULT '',
    fallback_index SMALLINT NOT NULL DEFAULT 0 CHECK (fallback_index >= 0),
    retry_count SMALLINT NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    result VARCHAR(32) NOT NULL CHECK (result IN (
        'success', 'timeout', 'rate_limited', 'provider_error', 'network_error',
        'invalid_response', 'disabled', 'skipped'
    )),
    http_status SMALLINT,
    error_summary VARCHAR(1024) NOT NULL DEFAULT '',
    request_id VARCHAR(128) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_occurred
    ON qd_external_data_request_logs (occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_provider_result_time
    ON qd_external_data_request_logs (provider, result, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_domain_time
    ON qd_external_data_request_logs (data_domain, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_retention
    ON qd_external_data_request_logs (result, occurred_at, id);

CREATE TABLE IF NOT EXISTS qd_external_data_cache_stats (
    bucket_date DATE NOT NULL,
    data_domain VARCHAR(48) NOT NULL,
    operation VARCHAR(96) NOT NULL,
    cache_hits BIGINT NOT NULL DEFAULT 0 CHECK (cache_hits >= 0),
    cache_misses BIGINT NOT NULL DEFAULT 0 CHECK (cache_misses >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bucket_date, data_domain, operation)
);

CREATE TABLE IF NOT EXISTS qd_external_data_log_cleanup_runs (
    id BIGSERIAL PRIMARY KEY,
    status VARCHAR(16) NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    deleted_count BIGINT NOT NULL DEFAULT 0 CHECK (deleted_count >= 0),
    error_summary VARCHAR(1024) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_external_log_cleanup_runs_recent
    ON qd_external_data_log_cleanup_runs (started_at DESC, id DESC);
-- Durable annual A-share fundamental history and quality operations.
CREATE TABLE IF NOT EXISTS qd_cn_fundamental_observations (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    available_at DATE NOT NULL,
    frequency VARCHAR(16) NOT NULL DEFAULT 'annual' CHECK (frequency = 'annual'),
    source VARCHAR(48) NOT NULL,
    source_version VARCHAR(96) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    announcement_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    supersedes_observation_id BIGINT REFERENCES qd_cn_fundamental_observations(id),
    observation_version INTEGER NOT NULL DEFAULT 1 CHECK (observation_version > 0),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, period_end, available_at, source, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_observation_pit ON qd_cn_fundamental_observations(instrument, available_at DESC, period_end DESC);
ALTER TABLE qd_cn_fundamental_observations ADD COLUMN IF NOT EXISTS observation_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_field_values (
    id BIGSERIAL PRIMARY KEY,
    observation_id BIGINT NOT NULL REFERENCES qd_cn_fundamental_observations(id) ON DELETE CASCADE,
    field_code VARCHAR(80) NOT NULL,
    value_numeric DECIMAL(30, 8),
    value_text TEXT,
    unit VARCHAR(32) NOT NULL DEFAULT '',
    mapping_version VARCHAR(48) NOT NULL,
    source_field VARCHAR(120) NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (observation_id, field_code)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_fields_lookup ON qd_cn_fundamental_field_values(field_code, observation_id);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_metric_values (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    as_of_period_end DATE NOT NULL,
    available_at DATE NOT NULL,
    metric_code VARCHAR(80) NOT NULL,
    value_numeric DECIMAL(30, 10),
    status VARCHAR(24) NOT NULL CHECK (status IN ('available', 'insufficient', 'invalid')),
    formula_version VARCHAR(48) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, as_of_period_end, available_at, metric_code, formula_version)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_metrics_pit ON qd_cn_fundamental_metric_values(instrument, available_at DESC, metric_code);

CREATE TABLE IF NOT EXISTS qd_cn_industry_classifications (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    taxonomy VARCHAR(48) NOT NULL,
    industry_code VARCHAR(64) NOT NULL,
    industry_name VARCHAR(255) NOT NULL,
    effective_start DATE NOT NULL,
    effective_end DATE,
    source VARCHAR(48) NOT NULL,
    source_version VARCHAR(96) NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (instrument, taxonomy, industry_code, effective_start, source)
);
CREATE INDEX IF NOT EXISTS idx_cn_industry_pit ON qd_cn_industry_classifications(instrument, taxonomy, effective_start, effective_end);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_verification_targets (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    trigger_metric VARCHAR(80) NOT NULL,
    trigger_value DECIMAL(30, 10),
    threshold_value DECIMAL(30, 10),
    status VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'warning', 'blocked', 'unavailable')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (instrument, period_end, trigger_metric)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_targets_status ON qd_cn_fundamental_verification_targets(status, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_reconciliations (
    id BIGSERIAL PRIMARY KEY,
    target_id BIGINT NOT NULL REFERENCES qd_cn_fundamental_verification_targets(id) ON DELETE CASCADE,
    field_code VARCHAR(80) NOT NULL,
    primary_value DECIMAL(30, 8), official_value DECIMAL(30, 8),
    difference_pct DECIMAL(12, 6),
    status VARCHAR(16) NOT NULL CHECK (status IN ('matched', 'warning', 'blocking', 'unavailable')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (target_id, field_code)
);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_quality_results (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    available_at DATE NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('passed_pending_verification', 'excluded', 'insufficient', 'pending_verification', 'blocked', 'verified_with_warnings', 'verified')),
    formula_version VARCHAR(48) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, period_end, available_at, formula_version)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_quality_status ON qd_cn_fundamental_quality_results(status, calculated_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_quality_issues (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE,
    issue_code VARCHAR(80) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('warning', 'blocking')),
    status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
    source VARCHAR(48) NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (instrument, period_end, issue_code, source)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_issues_open ON qd_cn_fundamental_quality_issues(status, severity, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_coverage (
    instrument VARCHAR(32) PRIMARY KEY REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    first_period_end DATE,
    last_period_end DATE,
    observation_count INTEGER NOT NULL DEFAULT 0,
    complete_year_count INTEGER NOT NULL DEFAULT 0,
    missing_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_available_at DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL UNIQUE,
    requested_by INTEGER,
    request_kind VARCHAR(24) NOT NULL CHECK (request_kind IN ('backfill', 'scheduled', 'retry')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'paused', 'cancelled')),
    total_symbols INTEGER NOT NULL DEFAULT 0, succeeded_symbols INTEGER NOT NULL DEFAULT 0,
    failed_symbols INTEGER NOT NULL DEFAULT 0, skipped_symbols INTEGER NOT NULL DEFAULT 0,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb, result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS qd_cn_fundamental_sync_targets (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL REFERENCES qd_cn_fundamental_sync_runs(run_id) ON DELETE CASCADE,
    instrument VARCHAR(32) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'pending',
    checkpoint_period_end DATE, attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '', started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (run_id, instrument)
);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_operation_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER,
    action VARCHAR(64) NOT NULL,
    run_id VARCHAR(40),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_audit_actor ON qd_cn_fundamental_operation_audit(actor_user_id, created_at DESC);

-- =============================================================================
-- Internal finite Task Scheduler control plane
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_task_definitions (
    task_key VARCHAR(120) PRIMARY KEY,
    display_name VARCHAR(255) NOT NULL DEFAULT '',
    definition_version VARCHAR(64) NOT NULL,
    parameter_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority SMALLINT NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 3),
    max_concurrency INTEGER CHECK (max_concurrency IS NULL OR max_concurrency > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE qd_task_definitions
    ADD COLUMN IF NOT EXISTS max_concurrency INTEGER;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'qd_task_definitions_max_concurrency_check'
          AND conrelid = 'qd_task_definitions'::regclass
    ) THEN
        ALTER TABLE qd_task_definitions
            ADD CONSTRAINT qd_task_definitions_max_concurrency_check
            CHECK (max_concurrency IS NULL OR max_concurrency > 0);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS qd_task_schedules (
    id BIGSERIAL PRIMARY KEY,
    task_key VARCHAR(120) NOT NULL REFERENCES qd_task_definitions(task_key),
    cron_expression VARCHAR(120) NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    last_scheduled_at TIMESTAMPTZ,
    next_scheduled_at TIMESTAMPTZ,
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_schedules_due ON qd_task_schedules(enabled, next_scheduled_at);
CREATE INDEX IF NOT EXISTS idx_task_schedules_task ON qd_task_schedules(task_key, enabled);

CREATE TABLE IF NOT EXISTS qd_task_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL UNIQUE,
    task_key VARCHAR(120) NOT NULL REFERENCES qd_task_definitions(task_key),
    definition_version VARCHAR(64) NOT NULL,
    schedule_id BIGINT REFERENCES qd_task_schedules(id) ON DELETE SET NULL,
    owner_user_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    domain_kind VARCHAR(120) NOT NULL DEFAULT '',
    domain_run_id VARCHAR(120) NOT NULL DEFAULT '',
    exclusivity_key VARCHAR(255) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'retry_wait', 'cancel_requested',
        'succeeded', 'failed', 'cancelled'
    )),
    priority SMALLINT NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 3),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt_no INTEGER NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scheduled_at TIMESTAMPTZ,
    stage VARCHAR(120) NOT NULL DEFAULT '',
    progress_current BIGINT NOT NULL DEFAULT 0 CHECK (progress_current >= 0),
    progress_total BIGINT CHECK (progress_total IS NULL OR progress_total >= 0),
    progress_unit VARCHAR(64) NOT NULL DEFAULT '',
    progress_message VARCHAR(500) NOT NULL DEFAULT '',
    checkpoint_ref VARCHAR(255) NOT NULL DEFAULT '',
    result_status VARCHAR(32) NOT NULL DEFAULT '',
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code VARCHAR(120) NOT NULL DEFAULT '',
    error_summary VARCHAR(2000) NOT NULL DEFAULT '',
    heartbeat_at TIMESTAMPTZ,
    last_progress_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retry_of_run_id VARCHAR(64) REFERENCES qd_task_runs(run_id) ON DELETE SET NULL
);
ALTER TABLE qd_task_runs
    ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_task_runs_queue ON qd_task_runs(status, priority, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_status ON qd_task_runs(task_key, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_owner ON qd_task_runs(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_domain ON qd_task_runs(domain_kind, domain_run_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_heartbeat ON qd_task_runs(status, heartbeat_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_schedule_scheduled
    ON qd_task_runs(schedule_id, scheduled_at)
    WHERE schedule_id IS NOT NULL AND scheduled_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_active_exclusivity
    ON qd_task_runs(exclusivity_key)
    WHERE status IN ('queued', 'running', 'retry_wait', 'cancel_requested');

CREATE TABLE IF NOT EXISTS qd_task_events (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(64) REFERENCES qd_task_runs(run_id) ON DELETE CASCADE,
    schedule_id BIGINT REFERENCES qd_task_schedules(id) ON DELETE CASCADE,
    event_type VARCHAR(80) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'info' CHECK (severity IN ('debug', 'info', 'warning', 'error')),
    stage VARCHAR(120) NOT NULL DEFAULT '',
    message VARCHAR(2000) NOT NULL DEFAULT '',
    error_code VARCHAR(120) NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor VARCHAR(32) NOT NULL DEFAULT 'system',
    correlation_id VARCHAR(255) NOT NULL DEFAULT '',
    external_request_id VARCHAR(120) NOT NULL DEFAULT '',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (run_id IS NOT NULL OR schedule_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_task_events_run_time ON qd_task_events(run_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_task_events_schedule_time ON qd_task_events(schedule_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_task_events_type_time ON qd_task_events(event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS qd_task_leases (
    run_id VARCHAR(64) PRIMARY KEY REFERENCES qd_task_runs(run_id) ON DELETE CASCADE,
    holder_id VARCHAR(255) NOT NULL,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_leases_expiry ON qd_task_leases(lease_expires_at);

CREATE TABLE IF NOT EXISTS qd_task_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    action VARCHAR(80) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(120) NOT NULL,
    reason VARCHAR(1000) NOT NULL DEFAULT '',
    before_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_audit_target_time ON qd_task_audit(target_type, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_audit_actor_time ON qd_task_audit(actor_user_id, created_at DESC);


-- Unified external-data routing control plane. Additive and safe to apply repeatedly.

CREATE TABLE IF NOT EXISTS qd_data_adapter_types (
    adapter_key VARCHAR(120) PRIMARY KEY,
    adapter_version VARCHAR(64) NOT NULL,
    public_name VARCHAR(160) NOT NULL,
    config_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    credential_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    registration_status VARCHAR(24) NOT NULL DEFAULT 'registered'
        CHECK (registration_status IN ('registered', 'tombstoned')),
    code_fingerprint VARCHAR(64) NOT NULL,
    tombstoned_at TIMESTAMPTZ,
    materialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (adapter_key ~ '^[a-z][a-z0-9_]{1,119}$'),
    CHECK (octet_length(config_schema::text) <= 65536),
    CHECK (octet_length(credential_schema::text) <= 65536),
    CHECK ((registration_status = 'tombstoned') = (tombstoned_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS qd_data_capabilities (
    capability_key VARCHAR(120) PRIMARY KEY,
    capability_version VARCHAR(64) NOT NULL,
    public_name VARCHAR(160) NOT NULL,
    semantic_family VARCHAR(80) NOT NULL,
    market VARCHAR(40) NOT NULL DEFAULT '',
    normalized_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
    allowed_constraints JSONB NOT NULL DEFAULT '[]'::jsonb,
    hard_quality_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
    cache_key_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
    registration_status VARCHAR(24) NOT NULL DEFAULT 'registered'
        CHECK (registration_status IN ('registered', 'tombstoned')),
    code_fingerprint VARCHAR(64) NOT NULL,
    tombstoned_at TIMESTAMPTZ,
    materialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (capability_key ~ '^[a-z][a-z0-9_]{1,119}$'),
    CHECK (octet_length(normalized_contract::text) <= 65536),
    CHECK (octet_length(allowed_constraints::text) <= 32768),
    CHECK (octet_length(hard_quality_contract::text) <= 65536),
    CHECK (octet_length(cache_key_contract::text) <= 32768),
    CHECK ((registration_status = 'tombstoned') = (tombstoned_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS qd_provider_instances (
    id BIGSERIAL PRIMARY KEY,
    instance_key VARCHAR(120) NOT NULL UNIQUE,
    adapter_key VARCHAR(120) NOT NULL REFERENCES qd_data_adapter_types(adapter_key),
    display_name VARCHAR(160) NOT NULL,
    lifecycle_status VARCHAR(24) NOT NULL DEFAULT 'draft' CHECK (lifecycle_status IN (
        'draft', 'validation_failed', 'active', 'draining', 'disabled', 'migration_required', 'retired'
    )),
    provider_account_identity VARCHAR(255),
    account_identity_verified_at TIMESTAMPTZ,
    config_schema_version VARCHAR(64) NOT NULL,
    non_secret_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    config_version INTEGER NOT NULL DEFAULT 1 CHECK (config_version > 0),
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    activated_at TIMESTAMPTZ,
    disabled_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (instance_key ~ '^[a-z][a-z0-9_]{1,119}$'),
    CHECK (octet_length(non_secret_config::text) <= 65536),
    CHECK (provider_account_identity IS NULL OR length(provider_account_identity) BETWEEN 1 AND 255),
    CHECK (lifecycle_status <> 'retired' OR retired_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_instances_nonretired_account
    ON qd_provider_instances(adapter_key, provider_account_identity)
    WHERE provider_account_identity IS NOT NULL AND lifecycle_status <> 'retired';
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_instances_single_unidentified_active
    ON qd_provider_instances(adapter_key)
    WHERE provider_account_identity IS NULL AND lifecycle_status IN ('active', 'draining');
CREATE INDEX IF NOT EXISTS idx_provider_instances_lifecycle
    ON qd_provider_instances(lifecycle_status, adapter_key, id);

CREATE TABLE IF NOT EXISTS qd_provider_credentials (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    credential_version INTEGER NOT NULL CHECK (credential_version > 0),
    credential_schema_version VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'active', 'destroyed')),
    encryption_key_id VARCHAR(120) NOT NULL,
    ciphertext TEXT NOT NULL,
    secret_comparison_tag VARCHAR(128) NOT NULL,
    validation_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMPTZ,
    destroyed_at TIMESTAMPTZ,
    UNIQUE (instance_id, credential_version),
    CHECK (octet_length(validation_summary::text) <= 32768),
    CHECK ((status IN ('pending', 'active') AND length(ciphertext) > 0 AND destroyed_at IS NULL)
        OR (status = 'destroyed' AND ciphertext = '' AND destroyed_at IS NOT NULL)),
    CHECK (status <> 'active' OR activated_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_credentials_one_active
    ON qd_provider_credentials(instance_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_credentials_one_pending
    ON qd_provider_credentials(instance_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_credentials_secret_reuse
    ON qd_provider_credentials(secret_comparison_tag);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_key_rotation
    ON qd_provider_credentials(encryption_key_id, status, id)
    WHERE status IN ('active', 'pending');

CREATE TABLE IF NOT EXISTS qd_provider_instance_capabilities (
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE CASCADE,
    capability_key VARCHAR(120) NOT NULL REFERENCES qd_data_capabilities(capability_key),
    eligibility_status VARCHAR(24) NOT NULL DEFAULT 'unverified' CHECK (eligibility_status IN (
        'unverified', 'eligible', 'ineligible', 'disabled', 'migration_required'
    )),
    adapter_version VARCHAR(64) NOT NULL,
    capability_version VARCHAR(64) NOT NULL,
    verification_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_verified_at TIMESTAMPTZ,
    next_verification_at TIMESTAMPTZ,
    disabled_reason VARCHAR(1000) NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instance_id, capability_key),
    CHECK (octet_length(verification_evidence::text) <= 65536),
    CHECK (eligibility_status <> 'eligible' OR last_verified_at IS NOT NULL),
    CHECK (eligibility_status <> 'disabled' OR length(disabled_reason) > 0)
);
CREATE INDEX IF NOT EXISTS idx_instance_capabilities_route
    ON qd_provider_instance_capabilities(capability_key, eligibility_status, instance_id);
CREATE INDEX IF NOT EXISTS idx_instance_capabilities_reverify
    ON qd_provider_instance_capabilities(next_verification_at, instance_id)
    WHERE eligibility_status IN ('eligible', 'ineligible', 'migration_required');

CREATE TABLE IF NOT EXISTS qd_provider_quota_states (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE CASCADE,
    capability_key VARCHAR(120) REFERENCES qd_data_capabilities(capability_key),
    bucket_key VARCHAR(120) NOT NULL,
    unit VARCHAR(40) NOT NULL,
    configured_limit NUMERIC(28, 8),
    observed_limit NUMERIC(28, 8),
    consumed NUMERIC(28, 8) NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    reserved NUMERIC(28, 8) NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    reset_at TIMESTAMPTZ,
    observation_source VARCHAR(40) NOT NULL DEFAULT 'unknown',
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (configured_limit IS NULL OR configured_limit >= 0),
    CHECK (observed_limit IS NULL OR observed_limit >= 0),
    CHECK (octet_length(metadata::text) <= 32768)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_quota_scope_bucket
    ON qd_provider_quota_states(instance_id, COALESCE(capability_key, ''), bucket_key);
CREATE INDEX IF NOT EXISTS idx_provider_quota_reset
    ON qd_provider_quota_states(reset_at, instance_id, id);

CREATE TABLE IF NOT EXISTS qd_provider_quota_reservations (
    reservation_id VARCHAR(64) PRIMARY KEY,
    quota_state_id BIGINT NOT NULL REFERENCES qd_provider_quota_states(id) ON DELETE CASCADE,
    holder_id VARCHAR(160) NOT NULL,
    amount NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'consumed', 'released', 'expired')),
    purpose VARCHAR(120) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CHECK ((status = 'reserved' AND completed_at IS NULL) OR status <> 'reserved')
);
CREATE INDEX IF NOT EXISTS idx_quota_reservations_active
    ON qd_provider_quota_reservations(quota_state_id, expires_at, reservation_id)
    WHERE status = 'reserved';
CREATE INDEX IF NOT EXISTS idx_quota_reservations_holder
    ON qd_provider_quota_reservations(holder_id, status, expires_at);

CREATE TABLE IF NOT EXISTS qd_provider_health_states (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE CASCADE,
    capability_key VARCHAR(120) REFERENCES qd_data_capabilities(capability_key),
    health_status VARCHAR(20) NOT NULL DEFAULT 'unknown'
        CHECK (health_status IN ('unknown', 'healthy', 'degraded', 'unhealthy', 'quarantined')),
    circuit_state VARCHAR(20) NOT NULL DEFAULT 'closed'
        CHECK (circuit_state IN ('closed', 'open', 'probe_pending', 'half_open')),
    circuit_reason VARCHAR(1000) NOT NULL DEFAULT '',
    circuit_opened_at TIMESTAMPTZ,
    circuit_until TIMESTAMPTZ,
    quarantine_reason VARCHAR(1000) NOT NULL DEFAULT '',
    quarantined_at TIMESTAMPTZ,
    failure_window JSONB NOT NULL DEFAULT '{}'::jsonb,
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    last_evidence_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(failure_window::text) <= 32768),
    CHECK (health_status <> 'quarantined' OR (quarantined_at IS NOT NULL AND length(quarantine_reason) > 0)),
    CHECK (circuit_state = 'closed' OR length(circuit_reason) > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_health_scope
    ON qd_provider_health_states(instance_id, COALESCE(capability_key, ''));
CREATE INDEX IF NOT EXISTS idx_provider_health_circuit
    ON qd_provider_health_states(circuit_state, circuit_until, instance_id);

CREATE TABLE IF NOT EXISTS qd_provider_health_evidence (
    id BIGSERIAL PRIMARY KEY,
    health_state_id BIGINT NOT NULL REFERENCES qd_provider_health_states(id) ON DELETE CASCADE,
    routed_request_id VARCHAR(64),
    evidence_kind VARCHAR(40) NOT NULL,
    permanence VARCHAR(16) NOT NULL CHECK (permanence IN ('success', 'transient', 'permanent', 'unknown')),
    sanitized_summary VARCHAR(2000) NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(metadata::text) <= 32768)
);
CREATE INDEX IF NOT EXISTS idx_health_evidence_scope_time
    ON qd_provider_health_evidence(health_state_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_health_evidence_retention
    ON qd_provider_health_evidence(occurred_at, id);

CREATE TABLE IF NOT EXISTS qd_data_routing_policies (
    id BIGSERIAL PRIMARY KEY,
    capability_key VARCHAR(120) NOT NULL UNIQUE REFERENCES qd_data_capabilities(capability_key),
    policy_version BIGINT NOT NULL DEFAULT 1 CHECK (policy_version > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    disabled_reason VARCHAR(1000) NOT NULL DEFAULT '',
    effective_revision_id BIGINT,
    draft_revision_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (enabled OR length(disabled_reason) > 0)
);

CREATE TABLE IF NOT EXISTS qd_data_routing_revisions (
    id BIGSERIAL PRIMARY KEY,
    policy_id BIGINT NOT NULL REFERENCES qd_data_routing_policies(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'superseded', 'discarded')),
    based_on_revision_id BIGINT REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT,
    quality_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    impact_preview JSONB NOT NULL DEFAULT '{}'::jsonb,
    change_reason VARCHAR(1000) NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    published_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    UNIQUE (policy_id, revision_number),
    CHECK (octet_length(quality_profile::text) <= 32768),
    CHECK (octet_length(impact_preview::text) <= 65536),
    CHECK (status NOT IN ('published', 'superseded') OR (published_at IS NOT NULL AND length(change_reason) > 0))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_revisions_one_draft
    ON qd_data_routing_revisions(policy_id) WHERE status = 'draft';
CREATE INDEX IF NOT EXISTS idx_routing_revisions_history
    ON qd_data_routing_revisions(policy_id, revision_number DESC, id DESC);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_routing_policy_effective_revision_fk') THEN
        ALTER TABLE qd_data_routing_policies ADD CONSTRAINT qd_routing_policy_effective_revision_fk
            FOREIGN KEY (effective_revision_id) REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_routing_policy_draft_revision_fk') THEN
        ALTER TABLE qd_data_routing_policies ADD CONSTRAINT qd_routing_policy_draft_revision_fk
            FOREIGN KEY (draft_revision_id) REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS qd_data_routing_entries (
    id BIGSERIAL PRIMARY KEY,
    revision_id BIGINT NOT NULL REFERENCES qd_data_routing_revisions(id) ON DELETE CASCADE,
    position SMALLINT NOT NULL CHECK (position > 0),
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    eligibility_requirements JSONB NOT NULL DEFAULT '{}'::jsonb,
    stricter_quality_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (revision_id, position),
    UNIQUE (revision_id, instance_id),
    CHECK (octet_length(eligibility_requirements::text) <= 32768),
    CHECK (octet_length(stricter_quality_profile::text) <= 32768)
);
CREATE INDEX IF NOT EXISTS idx_routing_entries_instance
    ON qd_data_routing_entries(instance_id, revision_id);

CREATE TABLE IF NOT EXISTS qd_routed_data_requests (
    routed_request_id VARCHAR(64) PRIMARY KEY,
    capability_key VARCHAR(120) NOT NULL REFERENCES qd_data_capabilities(capability_key),
    policy_revision_id BIGINT REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT,
    calling_feature VARCHAR(160) NOT NULL,
    request_mode VARCHAR(24) NOT NULL CHECK (request_mode IN ('interactive', 'background', 'backtest')),
    subject_summary VARCHAR(512) NOT NULL DEFAULT '',
    constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
    deadline_at TIMESTAMPTZ,
    final_outcome VARCHAR(32) NOT NULL CHECK (final_outcome IN (
        'fresh_cache', 'provider', 'stale_cache', 'capability_disabled', 'failed'
    )),
    selected_instance_id BIGINT REFERENCES qd_provider_instances(id) ON DELETE SET NULL,
    selected_cache_key_hash VARCHAR(64) NOT NULL DEFAULT '',
    provider_public_name VARCHAR(160) NOT NULL DEFAULT '',
    acquired_at TIMESTAMPTZ,
    freshness VARCHAR(16) NOT NULL DEFAULT 'none' CHECK (freshness IN ('fresh', 'stale', 'none')),
    quality_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    explanation JSONB NOT NULL DEFAULT '{}'::jsonb,
    observability_degraded BOOLEAN NOT NULL DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(constraints::text) <= 32768),
    CHECK (octet_length(quality_warnings::text) <= 32768),
    CHECK (octet_length(explanation::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_routed_requests_capability_time
    ON qd_routed_data_requests(capability_key, created_at DESC, routed_request_id);
CREATE INDEX IF NOT EXISTS idx_routed_requests_outcome_retention
    ON qd_routed_data_requests(final_outcome, created_at, routed_request_id);
CREATE INDEX IF NOT EXISTS idx_routed_requests_instance_time
    ON qd_routed_data_requests(selected_instance_id, created_at DESC, routed_request_id);

ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS routed_request_id VARCHAR(64);
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS provider_instance_id BIGINT;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS policy_revision_id BIGINT;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS attempt_order SMALLINT;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS skip_reason VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS retry_summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS quality_outcome JSONB NOT NULL DEFAULT '{}'::jsonb;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_routed_request_fk') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_routed_request_fk
            FOREIGN KEY (routed_request_id) REFERENCES qd_routed_data_requests(routed_request_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_provider_instance_fk') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_provider_instance_fk
            FOREIGN KEY (provider_instance_id) REFERENCES qd_provider_instances(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_policy_revision_fk') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_policy_revision_fk
            FOREIGN KEY (policy_revision_id) REFERENCES qd_data_routing_revisions(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_attempt_order_check') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_attempt_order_check
            CHECK (attempt_order IS NULL OR attempt_order > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_retry_summary_size_check') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_retry_summary_size_check
            CHECK (octet_length(retry_summary::text) <= 32768);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_quality_outcome_size_check') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_quality_outcome_size_check
            CHECK (octet_length(quality_outcome::text) <= 32768);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_external_logs_routed_attempt
    ON qd_external_data_request_logs(routed_request_id, attempt_order, id);
CREATE INDEX IF NOT EXISTS idx_external_logs_instance_time
    ON qd_external_data_request_logs(provider_instance_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_logs_revision_time
    ON qd_external_data_request_logs(policy_revision_id, occurred_at DESC, id DESC);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'qd_provider_health_evidence_routed_request_id_fkey'
          AND conrelid = 'qd_provider_health_evidence'::regclass
    ) THEN
        ALTER TABLE qd_provider_health_evidence
            ADD CONSTRAINT qd_provider_health_evidence_routed_request_id_fkey
            FOREIGN KEY (routed_request_id) REFERENCES qd_routed_data_requests(routed_request_id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS qd_data_source_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    action VARCHAR(80) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(120) NOT NULL,
    reason VARCHAR(1000) NOT NULL,
    before_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id VARCHAR(128) NOT NULL,
    outcome VARCHAR(24) NOT NULL CHECK (outcome IN ('succeeded', 'rejected', 'failed')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(before_summary::text) <= 65536),
    CHECK (octet_length(after_summary::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_data_source_audit_target_time
    ON qd_data_source_audit(target_type, target_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_data_source_audit_actor_time
    ON qd_data_source_audit(actor_user_id, occurred_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS qd_data_source_step_up_proofs (
    proof_hash VARCHAR(64) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    verification_method VARCHAR(16) NOT NULL CHECK (verification_method IN ('password', 'mfa')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_data_source_step_up_user_expiry
    ON qd_data_source_step_up_proofs(user_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_source_step_up_expiry
    ON qd_data_source_step_up_proofs(expires_at, proof_hash);

CREATE TABLE IF NOT EXISTS qd_provider_diagnostics (
    diagnostic_id VARCHAR(64) PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    capability_key VARCHAR(120) NOT NULL REFERENCES qd_data_capabilities(capability_key),
    requested_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    sanitized_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    quota_reservation_id VARCHAR(64) REFERENCES qd_provider_quota_reservations(reservation_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CHECK (octet_length(sanitized_result::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_provider_diagnostics_instance_time
    ON qd_provider_diagnostics(instance_id, capability_key, created_at DESC, diagnostic_id);

CREATE TABLE IF NOT EXISTS qd_provider_recovery_probes (
    probe_id VARCHAR(64) PRIMARY KEY,
    health_state_id BIGINT NOT NULL REFERENCES qd_provider_health_states(id) ON DELETE CASCADE,
    requested_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    trigger VARCHAR(24) NOT NULL CHECK (trigger IN ('scheduled', 'administrator', 'configuration_change')),
    status VARCHAR(20) NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sanitized_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CHECK (octet_length(sanitized_result::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_recovery_probes_due
    ON qd_provider_recovery_probes(status, not_before, probe_id);
CREATE INDEX IF NOT EXISTS idx_recovery_probes_health_time
    ON qd_provider_recovery_probes(health_state_id, created_at DESC, probe_id);

CREATE TABLE IF NOT EXISTS qd_data_routing_cutovers (
    id BIGSERIAL PRIMARY KEY,
    singleton_key SMALLINT NOT NULL DEFAULT 1 CHECK (singleton_key = 1),
    target_application_version VARCHAR(64) NOT NULL UNIQUE,
    status VARCHAR(24) NOT NULL DEFAULT 'preflight'
        CHECK (status IN ('preflight', 'maintenance', 'activated', 'aborted', 'failed')),
    requested_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    activated_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    reason VARCHAR(1000) NOT NULL,
    inventory_digest VARCHAR(64) NOT NULL,
    gate_result_digest VARCHAR(64) NOT NULL DEFAULT '',
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (status <> 'activated' OR (activated_at IS NOT NULL AND activated_by IS NOT NULL AND length(gate_result_digest) = 64))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_data_routing_one_activated_cutover
    ON qd_data_routing_cutovers(singleton_key) WHERE status = 'activated';
CREATE INDEX IF NOT EXISTS idx_data_routing_cutovers_status_time
    ON qd_data_routing_cutovers(status, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS qd_data_routing_cutover_gates (
    id BIGSERIAL PRIMARY KEY,
    cutover_id BIGINT NOT NULL REFERENCES qd_data_routing_cutovers(id) ON DELETE CASCADE,
    gate_key VARCHAR(80) NOT NULL,
    mandatory BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'passed', 'failed')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at TIMESTAMPTZ,
    UNIQUE (cutover_id, gate_key),
    CHECK (octet_length(evidence::text) <= 65536),
    CHECK (status = 'pending' OR evaluated_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_cutover_gates_blocking
    ON qd_data_routing_cutover_gates(cutover_id, mandatory, status, gate_key);

CREATE TABLE IF NOT EXISTS qd_data_source_legacy_imports (
    adapter_key VARCHAR(80) PRIMARY KEY REFERENCES qd_data_adapter_types(adapter_key),
    instance_id BIGINT UNIQUE REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    status VARCHAR(24) NOT NULL DEFAULT 'not_imported' CHECK (status IN ('not_imported','imported','failed')),
    source_names JSONB NOT NULL DEFAULT '[]'::jsonb,
    imported_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    imported_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(source_names::text) <= 8192)
);
