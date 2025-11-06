# Grocery Optimizer - Setup Guide

Complete guide to get the Grocery Optimizer PoC up and running.

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [Environment Setup](#environment-setup)
3. [Database Configuration](#database-configuration)
4. [Local Development](#local-development)
5. [Verification](#verification)
6. [Next Steps](#next-steps)

---

## Prerequisites

### Required Accounts

1. **Neon.tech** (PostgreSQL + TimescaleDB)
   - Sign up at https://neon.tech
   - Free tier includes 0.5 GB storage, sufficient for PoC

2. **Upstash** (Redis)
   - Sign up at https://upstash.com
   - Free tier: 10,000 commands/day

3. **Hugging Face** (AI Models)
   - Sign up at https://huggingface.co
   - Get API token from https://huggingface.co/settings/tokens
   - Free tier includes 30,000 requests/month

### Local Requirements

- **Python 3.11+**
- **Git**
- **Ollama** (for local LLM serving)
  - Download from https://ollama.ai
  - Models needed: `smollm:1.7b`, `smollm:360m`

---

## Environment Setup

### 1. Clone and Install Dependencies

```bash
# Clone the repository
cd grocery-optimizer

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r scripts/requirements.txt
```

### 2. Configure Environment Variables

```bash
# Copy the example file
cp .env.example .env

# Edit .env with your credentials
# Use your favorite text editor
```

Required variables for database setup:
```env
DATABASE_URL=postgresql://user:password@host/database
REDIS_URL=redis://default:password@host:port
```

---

## Database Configuration

### Step 1: Create Neon.tech Project

1. Go to https://console.neon.tech
2. Click **"New Project"**
3. Project name: `grocery-optimizer`
4. Region: Choose closest to you
5. PostgreSQL version: Latest (15+)

### Step 2: Enable TimescaleDB

1. In your project dashboard, go to **Settings** → **Extensions**
2. Find **TimescaleDB** in the list
3. Click **Enable**
4. Wait 2-3 minutes for activation

### Step 3: Get Connection String

1. In project dashboard, click **Connection Details**
2. Copy the connection string
3. Format: `postgresql://user:password@host/database?sslmode=require`
4. Paste into `.env` as `DATABASE_URL`

### Step 4: Run Database Setup

```bash
# Full setup with sample data (recommended for testing)
python scripts/run_db_setup.py --seed

# Schema only (for production)
python scripts/run_db_setup.py
```

Expected output:
```
============================================================
GROCERY OPTIMIZER - Database Setup
============================================================
✅ Connected to database
✅ TimescaleDB version 2.13.0 is installed
🔧 Running Database initialization...
✅ Database initialization completed successfully
✅ Database setup verified successfully

📊 Tables found: 8
   ✅ users
   ✅ stores
   ✅ price_snapshots
   ✅ deals
   ✅ recipes
   ✅ shopping_lists
   ✅ api_usage
   ✅ agent_logs
```

### Step 5: Verify Setup

```bash
# Test database connection
python scripts/test-db-connection.py
```

Expected output:
```
🔧 Testing Database Connections...

✅ PostgreSQL connected
✅ TimescaleDB version: 2.13.0
✅ Tables created: ['users', 'stores', 'deals', ...]
✅ Redis connected
✅ Redis test: Hello from Grocery Optimizer!

==================================================
✅ All database connections successful!
```

---

## Local Development

### 1. Install Ollama and Models

```bash
# Install Ollama from https://ollama.ai

# Pull required models
ollama pull smollm:1.7b      # Chef Orchestrator (1.7B params)
ollama pull smollm:360m      # SousChef & Nutritionist (360M params)

# Optional: smaller model for shopping optimizer
ollama pull smollm:135m      # Shopping Optimizer (135M params)

# Verify models
ollama list
```

### 2. Set Up MLflow (Optional but Recommended)

```bash
# Install MLflow
pip install mlflow

# Start MLflow server
mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5000
```

Access MLflow at: http://localhost:5000

### 3. Start Development Server

```bash
# Install additional dependencies for FastAPI
pip install -r scripts/requirements.txt

# Run the application (once built)
uvicorn app.main:app --reload --port 8000
```

---

## Verification

### Check Database Tables

```bash
# Using psql
psql $DATABASE_URL -c "\dt"

# Or using Python
python -c "
import os
import psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute('SELECT table_name FROM information_schema.tables WHERE table_schema = %s', ('public',))
print('Tables:', [row[0] for row in cur.fetchall()])
"
```

### Query Sample Data

```sql
-- View active deals
SELECT * FROM active_deals_with_stores LIMIT 10;

-- Check users
SELECT user_id, email, postal_code, budget FROM users;

-- View recent recipes
SELECT recipe_id, name, total_cost, servings FROM recipes;
```

### Test Redis Connection

```bash
# Using redis-cli (if installed)
redis-cli -u $REDIS_URL ping

# Or using Python
python -c "
import os
import redis
from dotenv import load_dotenv
load_dotenv()
r = redis.from_url(os.getenv('REDIS_URL'))
print('Redis:', r.ping())
"
```

---

## Next Steps

### 1. Build FastAPI Application

Create the application structure:

```bash
mkdir -p app/{routes,models,services,agents,db}
touch app/__init__.py
touch app/main.py
```

Implement core endpoints (from README.md):
- `POST /api/v1/users/register`
- `POST /api/v1/postal-code/discover`
- `POST /api/v1/recipes/generate`
- `GET /api/v1/shopping-list/{user_id}`

### 2. Implement LangGraph Agents

Follow the comprehensive guide in `agents.md`:

```bash
# Create agent files
mkdir -p app/agents
touch app/agents/{state,chef_orchestrator,sous_chef,nutritionist,graph,prompts}.py
```

Key components:
- **Chef Orchestrator** (SmolLM-1.7B): Plans ingredient groups
- **SousChef Agents** (SmolLM-360M): Generate recipes in parallel
- **Nutritionist** (SmolLM-360M): Validates recipes with feedback loop

### 3. Integrate Flipp API

Once Flipp API credentials are obtained:

```python
# app/services/flipp_api.py
import httpx

async def fetch_deals_for_postal_code(postal_code: str):
    # Implement Flipp API integration
    pass
```

### 4. Set Up Background Jobs

Configure Celery for asynchronous deal fetching:

```bash
# Start Celery worker
celery -A workers.celery_app worker --loglevel=info
```

---

## Troubleshooting

### Database Connection Issues

**Problem:** `psycopg2.OperationalError: could not connect`

**Solutions:**
1. Verify DATABASE_URL format
2. Check Neon.tech project is active (not suspended)
3. Ensure firewall allows outbound connections
4. Try adding `?sslmode=require` to connection string

### TimescaleDB Not Found

**Problem:** `WARNING: TimescaleDB extension not found`

**Solutions:**
1. Enable in Neon.tech console: Settings → Extensions → TimescaleDB
2. Wait 2-3 minutes for extension to activate
3. Refresh project and re-run setup script
4. Note: Some Neon.tech free tier regions may not support TimescaleDB

### Redis Connection Timeout

**Problem:** `redis.exceptions.ConnectionError`

**Solutions:**
1. Verify REDIS_URL format: `redis://default:password@host:port`
2. Check Upstash dashboard for connection details
3. Ensure password is URL-encoded if it contains special characters
4. For PoC, Redis is optional - can proceed without it

### Ollama Model Errors

**Problem:** `ollama.exceptions.ModelNotFound`

**Solutions:**
1. Verify Ollama is running: `ollama serve`
2. Pull models: `ollama pull smollm:1.7b`
3. List available models: `ollama list`
4. Check OLLAMA_BASE_URL in .env (default: http://localhost:11434)

### Import Errors

**Problem:** `ModuleNotFoundError: No module named 'psycopg2'`

**Solutions:**
1. Ensure virtual environment is activated
2. Install dependencies: `pip install -r scripts/requirements.txt`
3. For Windows, may need: `pip install psycopg2-binary`

---

## Development Workflow

### Daily Workflow

1. **Activate environment**
   ```bash
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   ```

2. **Start services**
   ```bash
   # Terminal 1: Ollama
   ollama serve

   # Terminal 2: MLflow (optional)
   mlflow ui

   # Terminal 3: Application
   uvicorn app.main:app --reload
   ```

3. **Run tests**
   ```bash
   pytest app/tests/
   ```

### Git Workflow

```bash
# Check status
git status

# Add changes
git add .

# Commit
git commit -m "feat: implement recipe generation"

# Push
git push origin main
```

### Database Migrations

For schema changes after initial setup:

1. Modify `scripts/init_db.sql`
2. Create migration script: `scripts/migrations/001_add_column.sql`
3. Run migration:
   ```bash
   psql $DATABASE_URL -f scripts/migrations/001_add_column.sql
   ```

---

## Production Deployment Checklist

- [ ] Use production DATABASE_URL with connection pooling
- [ ] Set `ENVIRONMENT=production` in .env
- [ ] Generate secure `SECRET_KEY`
- [ ] Enable HTTPS/SSL certificates
- [ ] Configure CORS for frontend
- [ ] Set up monitoring (Sentry, Prometheus)
- [ ] Configure log aggregation
- [ ] Enable database backups
- [ ] Implement rate limiting
- [ ] Set up CI/CD pipeline
- [ ] Document API with OpenAPI/Swagger

---

## Resources

- **Project Documentation**: `README.md`
- **Agent Implementation Guide**: `agents.md`
- **Database Scripts**: `scripts/README.md`
- **API Reference**: Will be available at `/docs` when FastAPI is running

## Support

For issues or questions:
1. Check troubleshooting section above
2. Review relevant documentation
3. Check GitHub issues
4. Create new issue with details and error logs

---

**Last Updated:** January 2025
**Version:** 1.0 - PoC Setup Guide
