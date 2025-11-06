# Database Setup Scripts

This directory contains scripts for initializing and managing the Grocery Optimizer database.

## Files

- **`init_db.sql`** - Main database schema initialization
- **`seed_sample_data.sql`** - Sample data for testing
- **`run_db_setup.py`** - Python script to execute SQL files
- **`test-db-connection.py`** - Test database connectivity
- **`requirements.txt`** - Python dependencies for scripts

## Prerequisites

1. **Neon.tech Account**
   - Create a project at [neon.tech](https://neon.tech)
   - Enable TimescaleDB extension in project settings
   - Copy your connection string

2. **Environment Variables**
   - Copy `.env.example` to `.env`
   - Add your `DATABASE_URL` from Neon.tech
   - Add your `REDIS_URL` from Upstash (optional for initial setup)

3. **Python Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

## Quick Start

### Option 1: Full Setup with Sample Data (Recommended for Testing)

```bash
python scripts/run_db_setup.py --seed
```

This will:
1. Create all tables and indexes
2. Enable TimescaleDB hypertable for price_snapshots
3. Create views and helper functions
4. Insert sample data (users, stores, deals, recipes)

### Option 2: Schema Only (Production)

```bash
python scripts/run_db_setup.py
```

Creates the schema without sample data.

### Option 3: Manual Setup

If you prefer to run SQL directly:

```bash
# Using psql
psql $DATABASE_URL -f scripts/init_db.sql
psql $DATABASE_URL -f scripts/seed_sample_data.sql

# Or using your SQL client
# Copy contents from init_db.sql and execute
```

## Verification

After setup, verify everything is working:

```bash
# Check tables and data
python scripts/run_db_setup.py --verify-only

# Test connection and extensions
python scripts/test-db-connection.py
```

## Database Schema Overview

### Core Tables

| Table | Purpose | Key Features |
|-------|---------|--------------|
| `users` | User accounts and preferences | Dietary restrictions stored as JSONB |
| `stores` | Grocery store locations | Indexed by postal code |
| `price_snapshots` | **TimescaleDB hypertable** for price history | 90-day retention policy |
| `deals` | Current active deals | Automatically calculated discount % |
| `recipes` | AI-generated recipes | JSONB ingredients with GIN index |
| `shopping_lists` | Consolidated shopping lists | Links to multiple recipes |
| `api_usage` | API cost tracking | Tracks tokens and estimated cost |
| `agent_logs` | LangGraph agent execution logs | For debugging and monitoring |

### Key Indexes

Performance-optimized indexes on:
- Postal codes (for location-based queries)
- Date ranges (for active deals)
- JSONB fields (using GIN indexes)
- TimescaleDB time-bucket indexes

### Views

- **`active_deals_with_stores`** - Current deals joined with store info
- **`user_recipe_stats`** - Recipe statistics per user
- **`api_cost_by_user`** - API usage and costs aggregated by user

## TimescaleDB Features

### Hypertable

`price_snapshots` is configured as a TimescaleDB hypertable:
- 7-day chunks for optimal performance
- Automatic data retention (90 days)
- Continuous aggregate for daily price averages

### Continuous Aggregates

`price_snapshots_daily` provides pre-computed daily statistics:
- Average prices per product per store
- Min/max prices
- Updated hourly

## Sample Data Details

When using `--seed`, the following test data is inserted:

### Users (5)
- alice@example.com - Vegetarian, Toronto (M5V3A8)
- bob@example.com - Gluten-free, Montreal (H2X1Y4)
- charlie@example.com - No restrictions, Vancouver (V6B1A1)
- diana@example.com - Vegan, Toronto (M5V3A8)
- eve@example.com - Kosher, Montreal (H2X1Y4)

### Stores (10)
Covering three test postal codes:
- **Toronto (M5V3A8)**: Loblaws, Metro, No Frills, Sobeys
- **Montreal (H2X1Y4)**: IGA, Metro, Provigo
- **Vancouver (V6B1A1)**: Safeway, Save-On-Foods, T&T

### Deals (~50)
Active deals valid for 7 days from current date, including:
- Meat & Poultry (chicken, pork, beef)
- Produce (vegetables, fruits)
- Pantry staples (pasta, rice, sauces)
- Dairy & Eggs

### Price History
30 days of historical price data for key products showing:
- Price variations over time
- Sale cycles (typically 2-3 days per week)

## Troubleshooting

### Issue: "TimescaleDB extension not found"

**Solution:**
1. Log into Neon.tech console
2. Navigate to your project → Settings → Extensions
3. Enable TimescaleDB extension
4. Wait a few minutes for activation
5. Re-run the setup script

### Issue: "Permission denied"

**Solution:**
Check that your DATABASE_URL user has sufficient privileges:

```sql
-- Grant necessary permissions
GRANT ALL PRIVILEGES ON DATABASE groceryoptimizer TO your_user;
GRANT ALL ON SCHEMA public TO your_user;
```

### Issue: "Connection refused"

**Solution:**
1. Verify DATABASE_URL format: `postgresql://user:pass@host/db`
2. Check if IP is whitelisted (if using IP restrictions)
3. Verify Neon.tech project is active (not suspended)

### Issue: "Table already exists"

**Solution:**
The scripts use `IF NOT EXISTS` clauses, so re-running is safe. To reset:

```sql
-- WARNING: This deletes all data!
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO your_user;
```

Then re-run `run_db_setup.py`.

## Maintenance Scripts

### Check Database Size

```sql
SELECT
    pg_size_pretty(pg_database_size(current_database())) as db_size;
```

### Check Table Sizes

```sql
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

### Clean Old Data

```sql
-- Delete deals older than 30 days
DELETE FROM deals WHERE valid_until < CURRENT_DATE - INTERVAL '30 days';

-- TimescaleDB automatically drops old price_snapshots based on retention policy
```

## Next Steps

After successful database setup:

1. **Test Connection**
   ```bash
   python scripts/test-db-connection.py
   ```

2. **Build FastAPI Application**
   - Create `app/` directory structure
   - Implement API endpoints from README.md

3. **Implement LangGraph Agents**
   - Follow `agents.md` implementation guide
   - Set up Ollama with SmolLM models

4. **Set Up MLflow**
   ```bash
   mlflow ui --backend-store-uri sqlite:///mlflow.db
   ```

## Security Notes

- **Never commit `.env` file** - It's in `.gitignore`
- Use environment-specific credentials for production
- Consider implementing row-level security (RLS) for multi-tenant setup
- Rotate database credentials regularly
- Use read-only database users for analytics queries

## References

- [Neon.tech Documentation](https://neon.tech/docs)
- [TimescaleDB Documentation](https://docs.timescale.com)
- [PostgreSQL JSONB Documentation](https://www.postgresql.org/docs/current/datatype-json.html)
