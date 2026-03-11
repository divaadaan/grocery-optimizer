# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Grocery Optimizer is an AI-powered meal planning system using a multi-agent LangGraph workflow to generate cost-optimized recipes based on real-time grocery deals. The backend is FastAPI + PostgreSQL (Neon.tech with TimescaleDB) + Redis (Upstash) + Ollama for local LLM inference.

## Commands

### Setup
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in credentials
```

### Database
```bash
python scripts/run_db_setup.py --seed   # setup schema + sample data
python scripts/test-db-connection.py    # verify connection
```

### Running
```bash
ollama serve                             # start local LLM server (separate terminal)
ollama pull smollm:1.7b && ollama pull smollm:360m  # download models on first run
uvicorn app.main:app --reload --port 8000
```

### Testing
```bash
pytest app/tests/
pytest app/tests/test_graph.py          # run single test file
```

### Linting & Formatting
```bash
black app/
flake8 app/
```

## Architecture

### Multi-Agent LangGraph Workflow (`app/agents/`)

The core feature is a 10-node LangGraph workflow in `app/agents/graph.py`:

1. **Chef Orchestrator** (`chef_orchestrator.py`, SmolLM 1.7B via Ollama): Fetches live grocery deals and groups ingredients into 3 clusters optimized for budget.
2. **3 Parallel SousChef Agents** (`sous_chef.py`, SmolLM 360M): Generate recipes independently for each ingredient group.
3. **Aggregation Node**: Combines outputs and calculates total/per-meal costs.
4. **Nutritionist Agent** (`nutritionist.py`, SmolLM 360M): Validates recipes; rejected recipes loop back for retry (up to `max_iterations`).
5. **Finalize**: Saves approved recipes to PostgreSQL via `app/services/database.py`.

Workflow state is defined as a large TypedDict (`app/agents/state.py`, ~70 fields) covering input parameters, per-agent outputs, cost calculations, retry tracking, and MLflow metadata.

### Request Flow

```
POST /api/v1/recipes/generate
  → app/routes/recipes.py
  → app/main_recipe_generation.py (LangGraph workflow entry)
  → app/agents/graph.py (orchestrates all agents)
  → app/services/database.py (persist results)
```

### Data Layer

- **PostgreSQL** (`app/db/database.py`): psycopg2 SimpleConnectionPool (1–10 connections). Schema in `scripts/init_db.sql`. 8 tables including a TimescaleDB hypertable (`price_snapshots`) with 90-day retention.
- **Redis cache** (`app/services/cache_service.py`): TTL-based via `@cached` decorator. Cache keys follow pattern `deals:{postal_code}:{category}` with 6-hour TTL.
- **MLflow** (`app/services/mlflow_logger.py`): Tracks agent call counts, latencies, and experiment metrics.

### Configuration

All settings in `app/config.py` use Pydantic `BaseSettings` loaded from environment variables. See `.env.example` for all required variables: `DATABASE_URL`, `REDIS_URL`, `HUGGINGFACE_API_KEY`, Ollama model names, MLflow URI, and cache TTL overrides.

### Incomplete Endpoints

These routes exist but are not implemented: `GET /api/v1/recipes/{id}`, `GET /api/v1/recipes/user/{id}`, and all shopping list CRUD operations.
