# Grocery Optimizer - Complete Setup Guide

Get your AI-powered grocery optimization system up and running in under 30 minutes.

## Table of Contents
- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Detailed Setup](#detailed-setup)
- [Testing](#testing)
- [Production Deployment](#production-deployment)
- [Troubleshooting](#troubleshooting)

---

## Overview

The Grocery Optimizer is a multi-agent AI system that:
- Fetches real-time grocery deals by postal code
- Generates optimized meal plans using LangGraph agents
- Creates cost-efficient shopping lists
- Tracks price history with TimescaleDB
- Caches data with Redis for performance

**Tech Stack:**
- FastAPI + PostgreSQL (TimescaleDB) + Redis (Upstash)
- LangGraph with Ollama (SmolLM models)
- MLflow for experiment tracking

---

## Prerequisites

### Required Accounts

1. **Neon.tech** - PostgreSQL + TimescaleDB (Free tier: 0.5GB)
   - Sign up: https://neon.tech
   - Enable TimescaleDB extension in project settings

2. **Upstash** - Redis caching (Free tier: 10K commands/day)
   - Sign up: https://upstash.com
   - Create Redis database
   - Copy connection URL

3. **Hugging Face** (Optional - for future features)
   - Sign up: https://huggingface.co
   - Get API token: https://huggingface.co/settings/tokens

### Local Requirements

- **Python 3.11+**
- **Git**
- **Ollama** - Local LLM serving
  - Download: https://ollama.ai
  - Required models: `smollm:1.7b`, `smollm:360m`

---

## Quick Start

### 1. Clone and Install

```bash
# Clone repository
git clone https://github.com/divaadaan/grocery-optimizer.git
cd grocery-optimizer

# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate

# Activate (macOS/Linux)
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy environment template
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Required
DATABASE_URL=postgresql://user:password@host/database
REDIS_URL=redis://default:password@host:port
REDIS_ENABLED=True

# Ollama (local)
OLLAMA_BASE_URL=http://localhost:11434

# Optional
MLFLOW_TRACKING_URI=http://localhost:5000
ENVIRONMENT=development
DEBUG=True
```

### 3. Initialize Database

```bash
# Set up database schema and sample data
python scripts/run_db_setup.py --seed

# Verify connection
python scripts/test-db-connection.py
```

Expected output:
```
✅ PostgreSQL connected
✅ TimescaleDB version: 2.13.0
✅ Redis connected
✅ All database connections successful!
```

### 4. Install Ollama Models

```bash
# Start Ollama server
ollama serve

# In another terminal, pull models
ollama pull smollm:1.7b      # Chef Orchestrator
ollama pull smollm:360m      # SousChefs & Nutritionist

# Verify
ollama list
```

### 5. Start the Application

```bash
# Start FastAPI server
uvicorn app.main:app --reload --port 8000
```

The API will be available at:
- **API**: http://localhost:8000
- **Interactive Docs**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

---

## Detailed Setup

### Database Setup (Neon.tech)

#### 1. Create Project

1. Go to https://console.neon.tech
2. Click "New Project"
3. Name: `grocery-optimizer`
4. Region: Choose closest to you
5. PostgreSQL version: 15+ (latest)

#### 2. Enable TimescaleDB

1. In project dashboard: **Settings** → **Extensions**
2. Find **TimescaleDB** and click **Enable**
3. Wait 2-3 minutes for activation

#### 3. Get Connection String

1. Click **Connection Details**
2. Copy the connection string
3. Format: `postgresql://user:password@host/database?sslmode=require`
4. Add to `.env` as `DATABASE_URL`

#### 4. Initialize Schema

```bash
# Run database setup
python scripts/run_db_setup.py --seed

# This creates:
# - 8 core tables (users, stores, deals, recipes, etc.)
# - TimescaleDB hypertable for price_snapshots
# - Sample data for 3 postal codes
# - Indexes and views
```

**Tables Created:**
- `users` - User accounts with dietary preferences
- `stores` - Grocery store locations
- `price_snapshots` - TimescaleDB hypertable (price history)
- `deals` - Current active deals
- `recipes` - AI-generated recipes
- `shopping_lists` - Optimized shopping lists
- `api_usage` - Cost tracking
- `agent_logs` - LangGraph execution logs

### Redis Setup (Upstash)

#### 1. Create Database

1. Go to https://console.upstash.com
2. Click "Create Database"
3. Name: `grocery-optimizer-cache`
4. Region: Choose same as Neon.tech
5. Click "Create"

#### 2. Get Connection URL

1. In database dashboard, click "Redis Connect"
2. Copy the connection string
3. Format: `redis://default:password@host:port`
4. Add to `.env`:
   ```env
   REDIS_URL=redis://default:password@region.upstash.io:6379
   REDIS_ENABLED=True
   ```

#### 3. Verify Connection

Start the app and check `/health`:
```bash
curl http://localhost:8000/health
```

Should show:
```json
{
  "status": "healthy",
  "database": "healthy",
  "redis": "healthy",
  "ollama": "healthy"
}
```

### Redis Caching Strategy

The application uses Redis to cache:

**Deals** (6 hours TTL):
```python
# Automatic caching in store_service.py
deals = get_current_deals_by_postal_code("M5V3A8")
# Subsequent calls within 6 hours return from cache
```

**Cache Keys:**
- `deals:{postal_code}:all` - All deals for postal code
- `deals:{postal_code}:{category}` - Category-filtered deals

**Benefits:**
- 🚀 Reduced database load
- ⚡ Faster API responses (10-50x improvement)
- 💰 Lower database costs on Neon.tech

### Ollama Setup

#### 1. Install Ollama

**macOS/Linux:**
```bash
curl https://ollama.ai/install.sh | sh
```

**Windows:**
Download from https://ollama.ai

#### 2. Start Ollama Server

```bash
ollama serve
```

Keep this running in a terminal.

#### 3. Pull Models

```bash
# Chef Orchestrator (1.7B parameters)
ollama pull smollm:1.7b

# SousChef agents (360M parameters)
ollama pull smollm:360m

# Optional: Shopping Optimizer (135M parameters)
ollama pull smollm:135m
```

#### 4. Verify Models

```bash
ollama list
```

Should show:
```
NAME                ID              SIZE      MODIFIED
smollm:1.7b         abc123...       1.1 GB    2 minutes ago
smollm:360m         def456...       240 MB    1 minute ago
```

### MLflow Setup (Optional)

For experiment tracking:

```bash
# Install MLflow (already in requirements.txt)
pip install mlflow

# Start MLflow server
mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5000
```

Access MLflow UI at: http://localhost:5000

**Tracked Metrics:**
- Agent execution times
- Token usage
- Recipe approval rates
- Cost per meal
- Ingredient reuse statistics

---

## Testing

### 1. Health Check

```bash
curl http://localhost:8000/health
```

Expected:
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "database": "healthy",
  "redis": "healthy",
  "ollama": "healthy"
}
```

### 2. Register a User

```bash
curl -X POST http://localhost:8000/api/v1/users/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "postal_code": "M5V3A8",
    "budget": 100.00,
    "household_size": 2,
    "dietary_restrictions": ["vegetarian"]
  }'
```

### 3. Discover Deals

```bash
curl -X POST http://localhost:8000/api/v1/postal-code/discover \
  -H "Content-Type: application/json" \
  -d '{"postal_code": "M5V3A8"}'
```

Should return stores and deals count.

### 4. Generate Recipes (Full LangGraph Workflow)

```bash
curl -X POST http://localhost:8000/api/v1/recipes/generate \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "num_meals": 3,
    "preferences": {
      "cuisine_preferences": ["Italian"],
      "meal_types": ["dinner"]
    }
  }'
```

This triggers:
1. Chef Orchestrator analyzes deals
2. 3 SousChefs generate recipes in parallel
3. Nutritionist validates recipes
4. Returns approved recipes with costs

**Expected Response:**
```json
{
  "recipes": [...],
  "total_cost": 42.50,
  "cost_per_meal": 14.17,
  "estimated_savings": 12.30,
  "generation_time": 8.5,
  "status": "completed",
  "warnings": []
}
```

### 5. Test Redis Caching

```bash
# First call (database query)
time curl http://localhost:8000/api/v1/postal-code/deals/M5V3A8

# Second call (cached - should be much faster)
time curl http://localhost:8000/api/v1/postal-code/deals/M5V3A8
```

Second call should be 10-50x faster.

---

## Production Deployment

### Environment Variables

Update `.env` for production:

```env
ENVIRONMENT=production
DEBUG=False
SECRET_KEY=generate-a-secure-random-key-here
LOG_LEVEL=INFO

# Use production database with connection pooling
DATABASE_URL=postgresql://user:password@prod-host/database
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=40

# Production Redis
REDIS_URL=redis://default:password@prod.upstash.io:6379
REDIS_ENABLED=True

# CORS for frontend
CORS_ORIGINS=["https://yourdomain.com"]
```

### Run with Gunicorn

```bash
# Install Gunicorn
pip install gunicorn

# Run with multiple workers
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 120
```

### Docker Deployment

Create `Dockerfile`:
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Build and run:
```bash
docker build -t grocery-optimizer .
docker run -p 8000:8000 --env-file .env grocery-optimizer
```

---

## Troubleshooting

### Database Connection Failed

**Error:** `psycopg2.OperationalError: could not connect`

**Solutions:**
1. Verify `DATABASE_URL` format: `postgresql://user:pass@host/db`
2. Check Neon.tech project is active (not suspended)
3. Ensure IP is whitelisted (if restrictions enabled)
4. Add `?sslmode=require` to URL

### Redis Connection Failed

**Error:** `redis.exceptions.ConnectionError`

**Solutions:**
1. Verify `REDIS_URL` format: `redis://default:pass@host:port`
2. Check Upstash dashboard for correct connection string
3. URL-encode password if it contains special characters
4. Set `REDIS_ENABLED=True` in `.env`

**Temporary Fix:**
```env
REDIS_ENABLED=False  # App will work without cache
```

### Ollama Not Found

**Error:** `ConnectionError: Ollama server not reachable`

**Solutions:**
1. Start Ollama: `ollama serve`
2. Check `OLLAMA_BASE_URL` in `.env` (default: `http://localhost:11434`)
3. Verify models are pulled: `ollama list`
4. Try pulling models again: `ollama pull smollm:1.7b`

### TimescaleDB Extension Not Found

**Error:** `WARNING: TimescaleDB extension not found`

**Solutions:**
1. Enable in Neon.tech console: Settings → Extensions → TimescaleDB
2. Wait 2-3 minutes for activation
3. Re-run setup: `python scripts/run_db_setup.py`

**Note:** Some Neon.tech regions may not support TimescaleDB. The app will work without it, but without time-series optimizations.

### Import Errors

**Error:** `ModuleNotFoundError: No module named 'app'`

**Solutions:**
1. Ensure virtual environment is activated
2. Install dependencies: `pip install -r requirements.txt`
3. Run from project root: `cd grocery-optimizer`

### Port Already in Use

**Error:** `OSError: [Errno 48] Address already in use`

**Solutions:**
```bash
# Use different port
uvicorn app.main:app --port 8001

# Or kill process on port 8000
# macOS/Linux:
lsof -ti:8000 | xargs kill -9

# Windows:
netstat -ano | findstr :8000
taskkill /PID <PID> /F
```

---

## Performance Tuning

### Database Connection Pool

For high load, adjust in `.env`:
```env
DATABASE_POOL_SIZE=20        # Max active connections
DATABASE_MAX_OVERFLOW=40     # Extra connections when pool full
```

### Redis Cache TTLs

Adjust cache expiration times:
```env
CACHE_TTL_DEALS=21600        # 6 hours (deals change frequently)
CACHE_TTL_RECIPES=86400      # 24 hours
CACHE_TTL_STORES=604800      # 7 days (stores rarely change)
```

### Ollama Performance

For faster inference:
```bash
# Use GPU if available (NVIDIA)
OLLAMA_GPU=1 ollama serve

# Adjust context window
OLLAMA_NUM_CTX=4096

# Parallel model loading
OLLAMA_MAX_LOADED_MODELS=2
```

---

## API Endpoints Summary

### Users
- `POST /api/v1/users/register` - Register user
- `GET /api/v1/users/{id}` - Get user
- `PUT /api/v1/users/{id}` - Update preferences

### Stores & Deals (Cached)
- `POST /api/v1/postal-code/discover` - Discover stores
- `GET /api/v1/postal-code/deals/{postal_code}` - Get deals ⚡ Cached
- `GET /api/v1/postal-code/top-deals/{postal_code}` - Top deals
- `GET /api/v1/postal-code/search/{postal_code}` - Search deals

### Recipes (LangGraph)
- `POST /api/v1/recipes/generate` - Generate meal plan with AI agents

### System
- `GET /health` - Health check (database, Redis, Ollama)
- `GET /api/v1/info` - API configuration

---

## Next Steps

1. **Test the complete workflow** - Register → Discover → Generate recipes
2. **Monitor MLflow** - View agent metrics at http://localhost:5000
3. **Check Redis cache** - Use Upstash console to see cache hits
4. **Add more postal codes** - Test with your local area
5. **Customize agents** - Modify prompts in `app/agents/prompts.py`

## Support

- **Documentation**: Check README.md and code comments
- **Issues**: https://github.com/divaadaan/grocery-optimizer/issues
- **Logs**: Check console output for errors

---

**Last Updated:** January 2025
**Version:** 1.0 - Production Ready
