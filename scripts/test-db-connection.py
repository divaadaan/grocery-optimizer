# scripts/test_db_connection.py
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import redis

def test_postgres():
    """Test PostgreSQL + TimescaleDB connection"""
    try:
        # Secrets are automatically available as env vars
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
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
    
    # Check if we're in Codespaces
    if os.environ.get("CODESPACES"):
        print("📍 Running in GitHub Codespaces\n")
    
    # Check for required env vars
    required_vars = ["DATABASE_URL", "REDIS_URL"]
    missing = [var for var in required_vars if not os.environ.get(var)]
    
    if missing:
        print(f"⚠️  Missing environment variables: {missing}")
        print("Add them via: Settings → Secrets and variables → Codespaces")
        exit(1)
    
    postgres_ok = test_postgres()
    print()
    redis_ok = test_redis()
    
    print("\n" + "="*50)
    if postgres_ok and redis_ok:
        print("✅ All database connections successful!")
    else:
        print("⚠️ Some connections failed. Check your secrets.")