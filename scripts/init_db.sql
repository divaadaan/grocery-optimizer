-- Grocery Optimizer Database Initialization Script
-- PostgreSQL + TimescaleDB
-- Run this script after enabling TimescaleDB extension on your Neon.tech database

-- ============================================================================
-- EXTENSIONS
-- ============================================================================

-- Enable TimescaleDB extension (if not already enabled)
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- USERS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS users (
    user_id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    postal_code VARCHAR(10) NOT NULL,
    budget DECIMAL(10,2) DEFAULT 100.00,
    household_size INTEGER DEFAULT 1 CHECK (household_size > 0),
    dietary_restrictions JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP,
    is_active BOOLEAN DEFAULT true
);

-- Indexes for users
CREATE INDEX IF NOT EXISTS idx_users_postal_code ON users(postal_code);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active) WHERE is_active = true;

-- ============================================================================
-- STORES TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS stores (
    store_id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    chain VARCHAR(100),
    postal_code VARCHAR(10) NOT NULL,
    address TEXT,
    city VARCHAR(100),
    province VARCHAR(50),
    latitude DECIMAL(10,8),
    longitude DECIMAL(11,8),
    last_updated TIMESTAMP DEFAULT NOW(),
    is_active BOOLEAN DEFAULT true
);

-- Indexes for stores
CREATE INDEX IF NOT EXISTS idx_stores_postal_code ON stores(postal_code);
CREATE INDEX IF NOT EXISTS idx_stores_chain ON stores(chain);
CREATE INDEX IF NOT EXISTS idx_stores_active ON stores(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_stores_location ON stores(latitude, longitude);

-- ============================================================================
-- PRICE SNAPSHOTS (TimescaleDB Hypertable)
-- ============================================================================

CREATE TABLE IF NOT EXISTS price_snapshots (
    time TIMESTAMPTZ NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
    product_name VARCHAR(255) NOT NULL,
    brand VARCHAR(100),
    price DECIMAL(10,2) NOT NULL CHECK (price >= 0),
    sale_price DECIMAL(10,2) CHECK (sale_price >= 0),
    unit VARCHAR(50),
    category VARCHAR(100),
    product_code VARCHAR(100),
    is_on_sale BOOLEAN GENERATED ALWAYS AS (sale_price IS NOT NULL AND sale_price < price) STORED
);

-- Convert to hypertable (only if not already converted)
-- This enables time-series optimization
SELECT create_hypertable('price_snapshots', 'time',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '7 days'
);

-- Indexes for price_snapshots
CREATE INDEX IF NOT EXISTS idx_price_snapshots_store_time ON price_snapshots(store_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_product ON price_snapshots(product_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_category ON price_snapshots(category, time DESC);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_on_sale ON price_snapshots(is_on_sale, time DESC) WHERE is_on_sale = true;

-- Create continuous aggregate for average prices (optional optimization)
CREATE MATERIALIZED VIEW IF NOT EXISTS price_snapshots_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time) AS day,
    store_id,
    product_name,
    AVG(price) as avg_price,
    MIN(COALESCE(sale_price, price)) as min_price,
    MAX(price) as max_price,
    COUNT(*) as sample_count
FROM price_snapshots
GROUP BY day, store_id, product_name
WITH NO DATA;

-- Refresh policy for the continuous aggregate
SELECT add_continuous_aggregate_policy('price_snapshots_daily',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- ============================================================================
-- DEALS TABLE (Current Active Deals - Denormalized)
-- ============================================================================

CREATE TABLE IF NOT EXISTS deals (
    deal_id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
    product_name VARCHAR(255) NOT NULL,
    brand VARCHAR(100),
    sale_price DECIMAL(10,2) NOT NULL CHECK (sale_price >= 0),
    regular_price DECIMAL(10,2) NOT NULL CHECK (regular_price >= 0),
    discount_percentage INTEGER GENERATED ALWAYS AS (
        CASE
            WHEN regular_price > 0 THEN ROUND(((regular_price - sale_price) / regular_price * 100)::numeric, 0)::integer
            ELSE 0
        END
    ) STORED,
    unit VARCHAR(50),
    category VARCHAR(100),
    product_code VARCHAR(100),
    deal_description TEXT,
    valid_from DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_until DATE NOT NULL,
    flipp_merchant_id VARCHAR(100),
    flipp_item_id VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT deals_valid_dates CHECK (valid_until >= valid_from),
    CONSTRAINT deals_valid_price CHECK (sale_price < regular_price)
);

-- Indexes for deals
CREATE INDEX IF NOT EXISTS idx_deals_store_valid ON deals(store_id, valid_until) WHERE valid_until >= CURRENT_DATE;
CREATE INDEX IF NOT EXISTS idx_deals_discount ON deals(discount_percentage DESC) WHERE valid_until >= CURRENT_DATE;
CREATE INDEX IF NOT EXISTS idx_deals_category ON deals(category, valid_until) WHERE valid_until >= CURRENT_DATE;
CREATE INDEX IF NOT EXISTS idx_deals_product ON deals(product_name, valid_until);
CREATE INDEX IF NOT EXISTS idx_deals_valid_period ON deals(valid_from, valid_until);

-- ============================================================================
-- RECIPES TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS recipes (
    recipe_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    ingredients JSONB NOT NULL,
    instructions TEXT[] NOT NULL,
    total_cost DECIMAL(10,2) NOT NULL CHECK (total_cost >= 0),
    servings INTEGER NOT NULL CHECK (servings > 0),
    estimated_prep_time INTEGER,
    meal_type VARCHAR(50),
    cuisine_type VARCHAR(50),
    nutrition_facts JSONB,
    health_score DECIMAL(5,2),
    created_at TIMESTAMP DEFAULT NOW(),
    is_approved BOOLEAN DEFAULT false,
    approval_notes TEXT
);

-- Indexes for recipes
CREATE INDEX IF NOT EXISTS idx_recipes_user ON recipes(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recipes_meal_type ON recipes(meal_type);
CREATE INDEX IF NOT EXISTS idx_recipes_cost ON recipes(total_cost);
CREATE INDEX IF NOT EXISTS idx_recipes_approved ON recipes(is_approved) WHERE is_approved = true;

-- GIN index for JSONB searches
CREATE INDEX IF NOT EXISTS idx_recipes_ingredients_gin ON recipes USING GIN (ingredients);
CREATE INDEX IF NOT EXISTS idx_recipes_nutrition_gin ON recipes USING GIN (nutrition_facts);

-- ============================================================================
-- SHOPPING LISTS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS shopping_lists (
    list_id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    recipe_ids INTEGER[] NOT NULL,
    items JSONB NOT NULL,
    total_cost DECIMAL(10,2) NOT NULL CHECK (total_cost >= 0),
    estimated_savings DECIMAL(10,2) DEFAULT 0.00,
    created_at TIMESTAMP DEFAULT NOW(),
    is_completed BOOLEAN DEFAULT false,
    completed_at TIMESTAMP,
    notes TEXT
);

-- Indexes for shopping_lists
CREATE INDEX IF NOT EXISTS idx_shopping_lists_user ON shopping_lists(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shopping_lists_completed ON shopping_lists(is_completed, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shopping_lists_items_gin ON shopping_lists USING GIN (items);

-- ============================================================================
-- API USAGE TRACKING TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS api_usage (
    usage_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    model_name VARCHAR(100) NOT NULL,
    tokens_used INTEGER NOT NULL CHECK (tokens_used >= 0),
    estimated_cost DECIMAL(10,4) NOT NULL CHECK (estimated_cost >= 0),
    endpoint VARCHAR(255) NOT NULL,
    request_type VARCHAR(50),
    response_time_ms INTEGER,
    success BOOLEAN DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for api_usage
CREATE INDEX IF NOT EXISTS idx_api_usage_user ON api_usage(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_usage_model ON api_usage(model_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_usage_endpoint ON api_usage(endpoint, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_usage_date ON api_usage(created_at DESC);

-- ============================================================================
-- AGENT EXECUTION LOGS TABLE (for MLflow-style tracking)
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_logs (
    log_id SERIAL PRIMARY KEY,
    run_id UUID DEFAULT uuid_generate_v4(),
    user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    agent_name VARCHAR(100) NOT NULL,
    action VARCHAR(100) NOT NULL,
    input_data JSONB,
    output_data JSONB,
    tokens_used INTEGER,
    duration_ms INTEGER,
    success BOOLEAN DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for agent_logs
CREATE INDEX IF NOT EXISTS idx_agent_logs_run ON agent_logs(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_logs_user ON agent_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_agent ON agent_logs(agent_name, created_at DESC);

-- ============================================================================
-- DATA RETENTION POLICIES (TimescaleDB)
-- ============================================================================

-- Drop price snapshots older than 90 days
SELECT add_retention_policy('price_snapshots',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for users table
DROP TRIGGER IF EXISTS update_users_updated_at ON users;
CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Trigger for stores table
DROP TRIGGER IF EXISTS update_stores_updated_at ON stores;
CREATE TRIGGER update_stores_updated_at
    BEFORE UPDATE ON stores
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Trigger for deals table
DROP TRIGGER IF EXISTS update_deals_updated_at ON deals;
CREATE TRIGGER update_deals_updated_at
    BEFORE UPDATE ON deals
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

-- View: Current active deals with store info
CREATE OR REPLACE VIEW active_deals_with_stores AS
SELECT
    d.deal_id,
    d.product_name,
    d.brand,
    d.sale_price,
    d.regular_price,
    d.discount_percentage,
    d.unit,
    d.category,
    d.valid_from,
    d.valid_until,
    s.store_id,
    s.name AS store_name,
    s.chain,
    s.postal_code,
    s.address
FROM deals d
JOIN stores s ON d.store_id = s.store_id
WHERE d.valid_until >= CURRENT_DATE
  AND d.valid_from <= CURRENT_DATE
  AND s.is_active = true;

-- View: User recipe statistics
CREATE OR REPLACE VIEW user_recipe_stats AS
SELECT
    u.user_id,
    u.email,
    COUNT(r.recipe_id) AS total_recipes,
    ROUND(AVG(r.total_cost), 2) AS avg_recipe_cost,
    ROUND(SUM(r.total_cost), 2) AS total_spent,
    COUNT(DISTINCT r.meal_type) AS meal_variety
FROM users u
LEFT JOIN recipes r ON u.user_id = r.user_id
GROUP BY u.user_id, u.email;

-- View: API cost summary by user
CREATE OR REPLACE VIEW api_cost_by_user AS
SELECT
    u.user_id,
    u.email,
    COUNT(a.usage_id) AS total_calls,
    SUM(a.tokens_used) AS total_tokens,
    ROUND(SUM(a.estimated_cost), 4) AS total_cost,
    ROUND(AVG(a.estimated_cost), 4) AS avg_cost_per_call,
    MAX(a.created_at) AS last_api_call
FROM users u
LEFT JOIN api_usage a ON u.user_id = a.user_id
GROUP BY u.user_id, u.email;

-- ============================================================================
-- GRANT PERMISSIONS (Adjust based on your setup)
-- ============================================================================

-- Example: Grant appropriate permissions to application user
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO grocery_app_user;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO grocery_app_user;

-- ============================================================================
-- DATABASE INITIALIZATION COMPLETE
-- ============================================================================

-- Insert a success marker
DO $$
BEGIN
    RAISE NOTICE 'Database schema initialized successfully!';
    RAISE NOTICE 'Tables created: users, stores, price_snapshots, deals, recipes, shopping_lists, api_usage, agent_logs';
    RAISE NOTICE 'TimescaleDB hypertable enabled for price_snapshots';
    RAISE NOTICE 'Retention policy: 90 days for price_snapshots';
END $$;
