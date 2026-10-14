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
   - No extensions to enable beyond `uuid-ossp` and `pg_trgm`, which
     `init_db.sql` creates itself (TimescaleDB is NOT used — see below)
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
2. Create `price_snapshots` as a natively range-partitioned table
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
| `price_snapshots` | Price history, **native PG range partitioning** by month | Trigram GIN on `product_name`; see the partitioning note below |
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
- `price_snapshots(time DESC)` and `(store_id, time DESC)` for time-range scans

### Views

- **`active_deals_with_stores`** - Current deals joined with store info
- **`user_recipe_stats`** - Recipe statistics per user
- **`api_cost_by_user`** - API usage and costs aggregated by user

## Price history partitioning (corrected 2026-10-14)

**This section previously described TimescaleDB hypertables, 7-day chunks, a
90-day retention policy and a `price_snapshots_daily` continuous aggregate.
None of that exists.** `init_db.sql` says so in its own header ("Native
PostgreSQL (Neon-compatible) - No TimescaleDB required"), never calls
`create_hypertable`, and there is no `price_snapshots_daily` object anywhere in
the repo. The claims were documentation drift; they are corrected here because
ingestion now depends on how this table actually behaves.

### What is actually there

`price_snapshots` is a plain PostgreSQL table declared
`PARTITION BY RANGE (time)`, with:

- four named monthly partitions, `2025_09` through `2025_12`;
- `price_snapshots_default`, a catch-all DEFAULT partition.

### Consequences you need to know about

1. **Current data lands in the DEFAULT partition.** The named partitions stop
   at 2026-01, and `create_price_snapshot_partition()` only ever creates
   *next* month — nothing creates the current one. Inserts still succeed (the
   DEFAULT partition catches them), but partition pruning and cheap
   retention-by-DROP deliver nothing.

2. **The missing partitions cannot simply be backfilled.** Once rows for a
   month sit in the DEFAULT partition, creating that month's partition fails
   with `ERROR 23514: updated partition constraint for default partition
   would be violated by some row`. The fix is a DETACH / relocate / CREATE /
   re-insert / ATTACH sequence, which wants its own migration and test.

3. **`cleanup_old_price_partitions()` must not be scheduled.** It drops
   partitions older than 90 days. Today it is harmless by accident — it
   chokes on the name `price_snapshots_default` and a bare
   `EXCEPTION WHEN OTHERS` swallows the error, and all real rows live in that
   partition anyway. Fix (2) without fixing this function and it becomes a job
   that silently deletes the price history that
   `app/services/price_baseline.py` needs 4-8 weeks to accumulate. See
   `migrations/005_warn_cleanup_old_price_partitions.sql`.

Background and measurements: `../sql4e/DESIGN-LOG.md` entries 0.5 and 0.6.

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

**Not applicable.** TimescaleDB is not used and does not need to be enabled;
earlier versions of this file wrongly said it did. `init_db.sql` needs only
`uuid-ossp` and `pg_trgm`, and creates both itself. If you hit an extension
error, it is one of those two.

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

-- NOTE: nothing drops old price_snapshots automatically. There is no
-- TimescaleDB retention policy (see the partitioning section above), and
-- cleanup_old_price_partitions() must NOT be scheduled -- it would destroy
-- the price baseline. Deleting price history is a deliberate manual act.
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
- [PostgreSQL Table Partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html)
- [PostgreSQL JSONB Documentation](https://www.postgresql.org/docs/current/datatype-json.html)
