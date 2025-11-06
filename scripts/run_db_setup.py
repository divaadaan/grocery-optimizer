#!/usr/bin/env python3
"""
Database Setup Script for Grocery Optimizer
Runs init_db.sql and optionally seed_sample_data.sql
"""

import os
import sys
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from pathlib import Path
import argparse
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def get_db_connection():
    """Create database connection from environment variable."""
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        print("❌ ERROR: DATABASE_URL environment variable not set")
        print("Please set DATABASE_URL in your .env file or environment")
        sys.exit(1)

    try:
        conn = psycopg2.connect(database_url)
        return conn
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        sys.exit(1)


def run_sql_file(conn, sql_file_path, description="SQL script"):
    """Execute a SQL file."""
    print(f"\n🔧 Running {description}...")
    print(f"   File: {sql_file_path}")

    if not os.path.exists(sql_file_path):
        print(f"❌ ERROR: File not found: {sql_file_path}")
        return False

    try:
        with open(sql_file_path, 'r', encoding='utf-8') as f:
            sql = f.read()

        cursor = conn.cursor()

        # Split by semicolons but handle complex cases
        # Execute the entire script at once to handle procedural blocks
        cursor.execute(sql)
        conn.commit()

        cursor.close()

        print(f"✅ {description} completed successfully")
        return True

    except Exception as e:
        print(f"❌ ERROR executing {description}: {e}")
        conn.rollback()
        return False


def check_timescaledb(conn):
    """Check if TimescaleDB extension is available."""
    print("\n🔍 Checking TimescaleDB availability...")

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")
        result = cursor.fetchone()
        cursor.close()

        if result:
            print(f"✅ TimescaleDB version {result[0]} is installed")
            return True
        else:
            print("⚠️  WARNING: TimescaleDB extension not found")
            print("   You may need to enable it in your Neon.tech console")
            print("   Navigate to: Project Settings → Extensions → Enable TimescaleDB")
            return False

    except Exception as e:
        print(f"⚠️  Could not check TimescaleDB: {e}")
        return False


def verify_setup(conn):
    """Verify that tables were created successfully."""
    print("\n🔍 Verifying database setup...")

    expected_tables = [
        'users', 'stores', 'price_snapshots', 'deals',
        'recipes', 'shopping_lists', 'api_usage', 'agent_logs'
    ]

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """)

        tables = [row[0] for row in cursor.fetchall()]

        print(f"\n📊 Tables found: {len(tables)}")
        for table in tables:
            status = "✅" if table in expected_tables else "ℹ️"
            print(f"   {status} {table}")

        # Check for missing tables
        missing = set(expected_tables) - set(tables)
        if missing:
            print(f"\n⚠️  Missing expected tables: {missing}")
            return False

        # Check views
        cursor.execute("""
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema = 'public'
            ORDER BY table_name;
        """)

        views = [row[0] for row in cursor.fetchall()]
        if views:
            print(f"\n📈 Views created: {len(views)}")
            for view in views:
                print(f"   ✅ {view}")

        cursor.close()

        print("\n✅ Database setup verified successfully")
        return True

    except Exception as e:
        print(f"❌ Verification failed: {e}")
        return False


def count_sample_data(conn):
    """Count rows in key tables after seeding."""
    print("\n📊 Sample Data Summary:")

    tables_to_check = [
        ('users', 'Users'),
        ('stores', 'Stores'),
        ('deals', 'Active Deals'),
        ('price_snapshots', 'Price Snapshots'),
        ('recipes', 'Recipes'),
        ('shopping_lists', 'Shopping Lists'),
        ('api_usage', 'API Usage Records')
    ]

    try:
        cursor = conn.cursor()

        for table, label in tables_to_check:
            cursor.execute(f"SELECT COUNT(*) FROM {table};")
            count = cursor.fetchone()[0]
            print(f"   {label}: {count}")

        cursor.close()

    except Exception as e:
        print(f"⚠️  Could not count sample data: {e}")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description='Initialize Grocery Optimizer database'
    )
    parser.add_argument(
        '--seed',
        action='store_true',
        help='Also load sample data after initialization'
    )
    parser.add_argument(
        '--seed-only',
        action='store_true',
        help='Only load sample data (skip initialization)'
    )
    parser.add_argument(
        '--verify-only',
        action='store_true',
        help='Only verify existing setup without making changes'
    )

    args = parser.parse_args()

    print("=" * 60)
    print("GROCERY OPTIMIZER - Database Setup")
    print("=" * 60)

    # Get connection
    conn = get_db_connection()

    # Set isolation level for DDL operations
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    print(f"✅ Connected to database")

    # Check TimescaleDB
    has_timescaledb = check_timescaledb(conn)

    if args.verify_only:
        # Just verify and exit
        verify_setup(conn)
        count_sample_data(conn)
        conn.close()
        return

    # Get script paths
    script_dir = Path(__file__).parent
    init_sql = script_dir / "init_db.sql"
    seed_sql = script_dir / "seed_sample_data.sql"

    success = True

    # Run initialization unless seed-only
    if not args.seed_only:
        if not has_timescaledb:
            print("\n⚠️  WARNING: Proceeding without TimescaleDB")
            print("   Some features may not work optimally")
            response = input("Continue anyway? (y/N): ")
            if response.lower() != 'y':
                print("❌ Setup cancelled")
                conn.close()
                sys.exit(1)

        success = run_sql_file(conn, init_sql, "Database initialization")

        if success:
            verify_setup(conn)

    # Run sample data if requested
    if (args.seed or args.seed_only) and success:
        success = run_sql_file(conn, seed_sql, "Sample data insertion")

        if success:
            count_sample_data(conn)

    conn.close()

    print("\n" + "=" * 60)
    if success:
        print("✅ DATABASE SETUP COMPLETE!")
        print("=" * 60)
        print("\nNext steps:")
        print("  1. Test connection: python scripts/test-db-connection.py")
        print("  2. Start building the FastAPI application")
        print("  3. Implement the LangGraph agents")
    else:
        print("❌ SETUP FAILED")
        print("=" * 60)
        print("\nPlease fix the errors above and try again")
        sys.exit(1)


if __name__ == "__main__":
    main()
