"""
Database Setup Script
Run: python scripts/run_db_setup.py [--seed]
"""
import os
import sys
import argparse
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in environment")
    sys.exit(1)


def schema_exists(cursor):
    """Check if the schema has already been applied."""
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'users'
        )
    """)
    return cursor.fetchone()['exists']


def run_migrations(cursor):
    """Apply any missing columns to existing tables."""
    migrations = [
        ("users", "is_active",   "ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT true"),
        ("users", "last_login",  "ALTER TABLE users ADD COLUMN last_login TIMESTAMP"),
        ("stores", "city",       "ALTER TABLE stores ADD COLUMN city VARCHAR(100)"),
        ("stores", "province",   "ALTER TABLE stores ADD COLUMN province VARCHAR(10)"),
        ("recipes", "estimated_prep_time", "ALTER TABLE recipes ADD COLUMN estimated_prep_time INTEGER"),
        ("recipes", "nutrition_facts",     "ALTER TABLE recipes ADD COLUMN nutrition_facts JSONB"),
        ("recipes", "health_score",        "ALTER TABLE recipes ADD COLUMN health_score DECIMAL(5,2)"),
        ("recipes", "is_approved",         "ALTER TABLE recipes ADD COLUMN is_approved BOOLEAN DEFAULT false"),
        ("api_usage", "request_type",      "ALTER TABLE api_usage ADD COLUMN request_type VARCHAR(10)"),
        ("api_usage", "response_time_ms",  "ALTER TABLE api_usage ADD COLUMN response_time_ms INTEGER"),
        ("api_usage", "success",           "ALTER TABLE api_usage ADD COLUMN success BOOLEAN DEFAULT true"),
    ]

    applied = 0
    for table, column, sql in migrations:
        cursor.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            )
        """, (table, column))
        if not cursor.fetchone()['exists']:
            cursor.execute(sql)
            print(f"  + Added {table}.{column}")
            applied += 1

    if applied:
        print(f"Migrations applied: {applied} column(s) added.")
    else:
        print("Schema up to date, no migrations needed.")


def data_exists(cursor):
    """Check if seed data has already been loaded."""
    cursor.execute("SELECT COUNT(*) AS count FROM stores")
    return cursor.fetchone()['count'] > 0


def run_schema(cursor):
    """Execute schema SQL file."""
    print("Running schema...")
    schema_path = os.path.join(os.path.dirname(__file__), 'init_db.sql')
    with open(schema_path, 'r') as f:
        cursor.execute(f.read())
    print("Schema applied.")


def seed_data(cursor):
    """Execute seed SQL file."""
    print("Seeding sample data...")
    seed_path = os.path.join(os.path.dirname(__file__), 'seed_sample_data.sql')
    with open(seed_path, 'r') as f:
        cursor.execute(f.read())
    print("Seed data loaded.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', action='store_true', help='Add sample data')
    parser.add_argument('--force-seed', action='store_true', help='Re-run seed even if data exists')
    args = parser.parse_args()

    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        if schema_exists(cursor):
            print("Schema already exists, skipping.")
            run_migrations(cursor)
        else:
            run_schema(cursor)

        if args.seed or args.force_seed:
            if data_exists(cursor) and not args.force_seed:
                print("Seed data already present, skipping. Use --force-seed to re-run.")
            else:
                seed_data(cursor)

        conn.commit()

        cursor.execute("SELECT COUNT(*) as count FROM stores")
        store_count = cursor.fetchone()['count']
        cursor.execute("SELECT COUNT(*) as count FROM deals")
        deal_count = cursor.fetchone()['count']
        print(f"Setup complete — {store_count} stores, {deal_count} deals")

        cursor.close()
        conn.close()

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
