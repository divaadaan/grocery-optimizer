-- ============================================================================
-- Grocery Optimizer Database Schema
-- Native PostgreSQL (Neon-compatible) - No TimescaleDB required
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ============================================================================
-- Core Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS users (
    user_id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    postal_code VARCHAR(10) NOT NULL,
    budget DECIMAL(10,2),
    household_size INTEGER DEFAULT 1,
    dietary_restrictions JSONB DEFAULT '[]'::jsonb,
    is_active BOOLEAN DEFAULT true,
    last_login TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_postal_code ON users(postal_code);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS stores (
    store_id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    chain VARCHAR(100),
    postal_code VARCHAR(10) NOT NULL,
    address TEXT,
    city VARCHAR(100),
    province VARCHAR(10),
    latitude DECIMAL(10,8),
    longitude DECIMAL(11,8),
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_stores_postal_code ON stores(postal_code);
CREATE INDEX IF NOT EXISTS idx_stores_chain ON stores(chain);
CREATE INDEX IF NOT EXISTS idx_stores_is_active ON stores(is_active);

-- ============================================================================
-- Price Snapshots (Partitioned by Month)
-- ============================================================================

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id BIGSERIAL,
    time TIMESTAMPTZ NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
    product_name VARCHAR(255) NOT NULL,
    brand VARCHAR(100),
    price DECIMAL(10,2) NOT NULL,
    sale_price DECIMAL(10,2),
    unit VARCHAR(50),
    category VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_id, time)
) PARTITION BY RANGE (time);

-- Partitions: 2025-09 through 2026-12
DO $$
DECLARE
    partition_start DATE;
    partition_end DATE;
    partition_name TEXT;
BEGIN
    FOR i IN 0..15 LOOP
        partition_start := DATE_TRUNC('month', DATE '2025-09-01' + (i || ' months')::INTERVAL)::DATE;
        partition_end := (partition_start + INTERVAL '1 month')::DATE;
        partition_name := 'price_snapshots_' || TO_CHAR(partition_start, 'YYYY_MM');

        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = partition_name) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF price_snapshots FOR VALUES FROM (%L) TO (%L)',
                partition_name, partition_start, partition_end
            );
        END IF;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_price_snapshots_time ON price_snapshots(time DESC);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_store_time ON price_snapshots(store_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_product ON price_snapshots(product_name);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_category ON price_snapshots(category);

-- ============================================================================
-- Current Deals
-- ============================================================================

CREATE TABLE IF NOT EXISTS deals (
    deal_id SERIAL PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
    product_name VARCHAR(255) NOT NULL,
    brand VARCHAR(100),
    sale_price DECIMAL(10,2) NOT NULL,
    regular_price DECIMAL(10,2),
    discount_percentage INTEGER,
    unit VARCHAR(50),
    category VARCHAR(100),
    valid_from DATE NOT NULL,
    valid_until DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_deals_store ON deals(store_id);
CREATE INDEX IF NOT EXISTS idx_deals_valid_dates ON deals(valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_deals_category ON deals(category);
CREATE INDEX IF NOT EXISTS idx_deals_product_name ON deals(product_name);
CREATE INDEX IF NOT EXISTS idx_deals_discount ON deals(discount_percentage DESC);
CREATE INDEX IF NOT EXISTS idx_deals_active ON deals(valid_until, valid_from);

-- ============================================================================
-- Recipes
-- ============================================================================

CREATE TABLE IF NOT EXISTS recipes (
    recipe_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    name VARCHAR(255) NOT NULL,
    ingredients JSONB NOT NULL,
    instructions TEXT[] NOT NULL,
    total_cost DECIMAL(10,2),
    servings INTEGER DEFAULT 4,
    estimated_prep_time INTEGER,
    cook_time INTEGER,
    cuisine_type VARCHAR(100),
    meal_type VARCHAR(50),
    nutrition_facts JSONB,
    allergen_info JSONB,
    health_score DECIMAL(5,2),
    is_approved BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recipes_user ON recipes(user_id);
CREATE INDEX IF NOT EXISTS idx_recipes_meal_type ON recipes(meal_type);
CREATE INDEX IF NOT EXISTS idx_recipes_cuisine ON recipes(cuisine_type);
CREATE INDEX IF NOT EXISTS idx_recipes_cost ON recipes(total_cost);
CREATE INDEX IF NOT EXISTS idx_recipes_ingredients_gin ON recipes USING gin(ingredients);

-- ============================================================================
-- Shopping Lists
-- ============================================================================

CREATE TABLE IF NOT EXISTS shopping_lists (
    list_id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    recipe_ids INTEGER[] NOT NULL,
    items JSONB NOT NULL,
    total_cost DECIMAL(10,2) NOT NULL,
    estimated_savings DECIMAL(10,2),
    regular_total DECIMAL(10,2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_completed BOOLEAN DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_shopping_lists_user ON shopping_lists(user_id);
CREATE INDEX IF NOT EXISTS idx_shopping_lists_created ON shopping_lists(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shopping_lists_items_gin ON shopping_lists USING gin(items);

-- ============================================================================
-- API Usage Tracking
-- ============================================================================

CREATE TABLE IF NOT EXISTS api_usage (
    usage_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    model_name VARCHAR(100) NOT NULL,
    tokens_used INTEGER NOT NULL,
    estimated_cost DECIMAL(10,4) NOT NULL,
    endpoint VARCHAR(255),
    request_type VARCHAR(10),
    execution_time_ms INTEGER,
    response_time_ms INTEGER,
    success BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_api_usage_user ON api_usage(user_id);
CREATE INDEX IF NOT EXISTS idx_api_usage_created ON api_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_usage_model ON api_usage(model_name);

-- ============================================================================
-- Agent Logs
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_logs (
    log_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    agent_name VARCHAR(100) NOT NULL,
    task_type VARCHAR(100),
    input_data JSONB,
    output_data JSONB,
    execution_time_ms INTEGER,
    tokens_used INTEGER,
    status VARCHAR(50),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_logs_user ON agent_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_agent_logs_agent ON agent_logs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_logs_status ON agent_logs(status);
CREATE INDEX IF NOT EXISTS idx_agent_logs_created ON agent_logs(created_at DESC);

-- ============================================================================
-- Views
-- ============================================================================

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
    s.city,
    s.province
FROM deals d
JOIN stores s ON d.store_id = s.store_id
WHERE d.valid_until >= CURRENT_DATE
  AND d.valid_from <= CURRENT_DATE
  AND s.is_active = true;

-- ============================================================================
-- Materialized Views
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_matviews WHERE matviewname = 'best_deals_by_category') THEN
        EXECUTE '
        CREATE MATERIALIZED VIEW best_deals_by_category AS
        SELECT
            category,
            product_name,
            brand,
            store_id,
            sale_price,
            regular_price,
            discount_percentage,
            valid_until
        FROM deals
        WHERE valid_until >= CURRENT_DATE AND discount_percentage >= 20
        ORDER BY category, discount_percentage DESC';

        CREATE INDEX idx_best_deals_category ON best_deals_by_category(category);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_matviews WHERE matviewname = 'price_trends') THEN
        EXECUTE '
        CREATE MATERIALIZED VIEW price_trends AS
        SELECT
            product_name,
            category,
            AVG(sale_price) as avg_sale_price,
            MIN(sale_price) as min_sale_price,
            MAX(sale_price) as max_sale_price,
            COUNT(*) as price_count,
            MAX(time) as last_updated
        FROM price_snapshots
        WHERE time >= NOW() - INTERVAL ''30 days''
        GROUP BY product_name, category';

        CREATE INDEX idx_price_trends_product ON price_trends(product_name);
        CREATE INDEX idx_price_trends_category ON price_trends(category);
    END IF;
END $$;

-- ============================================================================
-- Triggers
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_users_updated_at') THEN
        CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_stores_updated_at') THEN
        CREATE TRIGGER update_stores_updated_at BEFORE UPDATE ON stores
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_deals_updated_at') THEN
        CREATE TRIGGER update_deals_updated_at BEFORE UPDATE ON deals
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END $$;

-- ============================================================================
-- Partition Management Functions
-- ============================================================================

CREATE OR REPLACE FUNCTION create_price_snapshot_partition()
RETURNS void AS $$
DECLARE
    partition_date DATE;
    partition_name TEXT;
    start_date TEXT;
    end_date TEXT;
BEGIN
    partition_date := DATE_TRUNC('month', CURRENT_DATE + INTERVAL '1 month');
    partition_name := 'price_snapshots_' || TO_CHAR(partition_date, 'YYYY_MM');
    start_date := TO_CHAR(partition_date, 'YYYY-MM-DD');
    end_date := TO_CHAR(partition_date + INTERVAL '1 month', 'YYYY-MM-DD');

    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = partition_name) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF price_snapshots FOR VALUES FROM (%L) TO (%L)',
            partition_name, start_date, end_date
        );
        RAISE NOTICE 'Created partition: %', partition_name;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION cleanup_old_price_partitions()
RETURNS void AS $$
DECLARE
    partition_record RECORD;
    cutoff_date DATE;
BEGIN
    cutoff_date := CURRENT_DATE - INTERVAL '90 days';

    FOR partition_record IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public' AND tablename LIKE 'price_snapshots_%'
    LOOP
        DECLARE
            partition_month DATE;
        BEGIN
            partition_month := TO_DATE(
                SUBSTRING(partition_record.tablename FROM 'price_snapshots_(.*)'),
                'YYYY_MM'
            );

            IF partition_month < DATE_TRUNC('month', cutoff_date) THEN
                EXECUTE format('DROP TABLE IF EXISTS %I', partition_record.tablename);
                RAISE NOTICE 'Dropped old partition: %', partition_record.tablename;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Could not process partition: %', partition_record.tablename;
        END;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION get_active_deals(p_postal_code VARCHAR)
RETURNS TABLE (
    deal_id INTEGER,
    store_name VARCHAR,
    product_name VARCHAR,
    sale_price DECIMAL,
    regular_price DECIMAL,
    discount_percentage INTEGER,
    category VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        d.deal_id,
        s.name,
        d.product_name,
        d.sale_price,
        d.regular_price,
        d.discount_percentage,
        d.category
    FROM deals d
    JOIN stores s ON d.store_id = s.store_id
    WHERE s.postal_code = p_postal_code
        AND d.valid_until >= CURRENT_DATE
        AND d.valid_from <= CURRENT_DATE
        AND s.is_active = true
    ORDER BY d.discount_percentage DESC;
END;
$$ LANGUAGE plpgsql;
