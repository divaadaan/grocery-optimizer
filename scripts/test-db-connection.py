# scripts/test_db_connection.py
import os

import psycopg
from psycopg.rows import dict_row
import redis
from dotenv import load_dotenv

load_dotenv()


def test_postgres():
    """Test PostgreSQL + TimescaleDB connection"""
    try:
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor(row_factory=dict_row)

        # Check TimescaleDB
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")
        result = cur.fetchone()
        print(f"✅ PostgreSQL connected")
        print(f"✅ TimescaleDB version: {result['extversion'] if result else 'Not installed'}")

        # Check tables
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name;
        """)
        tables = cur.fetchall()
        print(f"✅ Tables created: {[t['table_name'] for t in tables]}")

        cur.close()
        conn.close()
        return True

    except Exception as e:
        print(f"❌ PostgreSQL error: {e}")
        return False


def test_redis():
    """Test Redis connection"""
    try:
        r = redis.from_url(os.environ["REDIS_URL"])
        r.ping()
        print("✅ Redis connected")

        # Test basic operations
        r.set("test_key", "Hello from Grocery Optimizer!", ex=60)
        value = r.get("test_key")
        print(f"✅ Redis test: {value.decode('utf-8')}")

        return True

    except Exception as e:
        print(f"❌ Redis error: {e}")
        return False


if __name__ == "__main__":
    print("🔧 Testing Database Connections...\n")

    if not os.environ.get("DATABASE_URL"):
        print("⚠️  DATABASE_URL is not set (checked environment and .env)")
        exit(1)

    postgres_ok = test_postgres()
    print()

    redis_enabled = os.environ.get("REDIS_ENABLED", "False").lower() in ("true", "1", "yes")
    if redis_enabled:
        redis_ok = test_redis()
    else:
        redis_ok = True
        print("⏭️  Redis disabled (REDIS_ENABLED is not true) — skipping")

    print("\n" + "=" * 50)
    if postgres_ok and redis_ok:
        print("✅ All database connections successful!")
    else:
        print("⚠️ Some connections failed. Check your secrets.")
        exit(1)
