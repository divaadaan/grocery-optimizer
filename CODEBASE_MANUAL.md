# Grocery Optimizer — Comprehensive Codebase Manual

This document is an exhaustive reference for the Grocery Optimizer codebase. It is intended to give any developer or AI assistant full context to work across any part of the system without needing to read every file first.

---

## Table of Contents

1. [Project Summary](#1-project-summary)
2. [Directory Structure](#2-directory-structure)
3. [Architecture Overview](#3-architecture-overview)
4. [Request Lifecycle: End-to-End](#4-request-lifecycle-end-to-end)
5. [FastAPI Application (`app/main.py`)](#5-fastapi-application-appmainpy)
6. [Configuration (`app/config.py`)](#6-configuration-appconfigpy)
7. [Database Layer](#7-database-layer)
8. [Pydantic Schemas (`app/models/schemas.py`)](#8-pydantic-schemas-appmodelsschemspy)
9. [Routes](#9-routes)
10. [Services Layer](#10-services-layer)
11. [LangGraph Multi-Agent Workflow](#11-langgraph-multi-agent-workflow)
12. [Agent Implementations](#12-agent-implementations)
13. [Workflow State (`app/agents/state.py`)](#13-workflow-state-appagentsstatepy)
14. [Prompt Templates (`app/agents/prompts.py`)](#14-prompt-templates-appagentspromptspy)
15. [Graph Construction & Node Wiring (`app/agents/graph.py`)](#15-graph-construction--node-wiring-appagentsgraphpy)
16. [Recipe Generation Entry Point (`app/main_recipe_generation.py`)](#16-recipe-generation-entry-point-appmain_recipe_generationpy)
17. [Redis Cache Service](#17-redis-cache-service)
18. [MLflow Logger](#18-mlflow-logger)
19. [Database Schema (SQL)](#19-database-schema-sql)
20. [Tests](#20-tests)
21. [Scripts](#21-scripts)
22. [Docker & Deployment](#22-docker--deployment)
23. [Dependencies](#23-dependencies)
24. [What Is Done vs. Not Done](#24-what-is-done-vs-not-done)
25. [Known Issues & Gotchas](#25-known-issues--gotchas)
26. [Cross-File Dependency Map](#26-cross-file-dependency-map)
27. [Migration Plan Summary](#27-migration-plan-summary)

---

## 1. Project Summary

Grocery Optimizer is an AI-powered meal planning system. A user provides their postal code, budget, household size, and dietary restrictions. The system:

1. Fetches real-time grocery deals from the database for the user's postal code
2. Uses a multi-agent LangGraph workflow with local LLMs (Ollama) to generate cost-optimized recipes
3. Validates recipes nutritionally, retrying rejected ones
4. Saves approved recipes to PostgreSQL
5. Tracks the entire process with MLflow

The tech stack is:
- **API**: FastAPI 0.109.0, served by Uvicorn
- **Database**: PostgreSQL (originally Neon.tech, migrating to local) with psycopg2 connection pooling
- **Cache**: Redis (Upstash or local) with optional enable/disable
- **AI**: Ollama for local LLM inference (SmolLM models), orchestrated by LangGraph
- **Tracking**: MLflow for experiment metrics
- **Task Queue**: Celery (declared in dependencies but not yet integrated into the workflow)

---

## 2. Directory Structure

```
/workspace/grocery-optimizer/
├── app/
│   ├── __init__.py                    # version = "0.1.0"
│   ├── main.py                        # FastAPI app setup, lifespan, middleware, health check
│   ├── main_recipe_generation.py      # Entry point for LangGraph workflow invocation
│   ├── config.py                      # Pydantic BaseSettings (all env vars)
│   │
│   ├── db/
│   │   ├── __init__.py                # Exports: db, get_db, init_db, close_db
│   │   └── database.py                # Database class with SimpleConnectionPool
│   │
│   ├── models/
│   │   ├── __init__.py                # Exports all schema classes
│   │   └── schemas.py                 # 16 Pydantic models (requests, responses, errors)
│   │
│   ├── routes/
│   │   ├── __init__.py                # Empty
│   │   ├── users.py                   # User CRUD endpoints
│   │   ├── stores.py                  # Store/deal discovery endpoints
│   │   ├── recipes.py                 # Recipe generation endpoint
│   │   └── shopping_lists.py          # Shopping list endpoints (NOT IMPLEMENTED)
│   │
│   ├── services/
│   │   ├── __init__.py                # Exports: UserService, StoreService
│   │   ├── database.py                # DatabaseService (deal fetching, recipe saving)
│   │   ├── cache_service.py           # CacheService (Redis wrapper + @cached decorator)
│   │   ├── mlflow_logger.py           # MLflowLogger (static methods for experiment tracking)
│   │   ├── user_service.py            # UserService (CRUD with soft delete)
│   │   └── store_service.py           # StoreService (deal queries with caching)
│   │
│   ├── agents/
│   │   ├── __init__.py                # Module docstring only
│   │   ├── state.py                   # RecipeGenerationState TypedDict (~70 fields)
│   │   ├── prompts.py                 # PromptTemplates class (5 prompt templates)
│   │   ├── graph.py                   # LangGraph StateGraph construction (10 nodes)
│   │   ├── chef_orchestrator.py       # ChefOrchestrator agent (SmolLM 1.7B)
│   │   ├── sous_chef.py              # SousChef agent + node function (SmolLM 360M)
│   │   └── nutritionist.py           # Nutritionist agent (SmolLM 360M)
│   │
│   ├── library/
│   │   └── flipp.py                   # Empty placeholder for Flipp API integration
│   │
│   └── tests/
│       ├── __init__.py                # Empty
│       └── test_graph.py              # 3 tests (1 implemented, 2 stubs)
│
├── scripts/
│   ├── init_db.sql                    # Full schema (8 tables, views, triggers, functions)
│   ├── seed_sample_data.sql           # Sample data (5 users, 10 stores, ~50 deals)
│   ├── run_db_setup.py                # Schema runner + optional seeding
│   ├── test-db-connection.py          # PostgreSQL + Redis connectivity test
│   ├── README.md                      # Database setup guide
│   └── ToDO.txt                       # Security TODOs (PgBouncer, RLS, etc.)
│
├── .env.example                       # 35+ environment variables with documentation
├── requirements.txt                   # 28 Python packages
├── Dockerfile                         # Python 3.11-slim with uv
├── docker-compose.yml                 # Backend service with health check
├── CLAUDE.md                          # AI assistant instructions
└── MIGRATION_PLAN.md                  # 2-phase migration (local PG + Claude Haiku swap)
```

---

## 3. Architecture Overview

### High-Level Data Flow

```
User Request (POST /api/v1/recipes/generate)
    │
    ▼
FastAPI Route (app/routes/recipes.py)
    │  Validates user exists via UserService
    │  Extracts user profile (budget, postal_code, dietary_restrictions)
    │
    ▼
run_recipe_generation() (app/main_recipe_generation.py)
    │  Builds initial RecipeGenerationState (~70 fields)
    │  Creates compiled LangGraph
    │
    ▼
LangGraph Workflow (app/agents/graph.py) — 10 nodes
    │
    ├─► Node 1: ChefOrchestrator.initialize()
    │     Fetches deals from DB, starts MLflow run
    │
    ├─► Node 2: ChefOrchestrator.plan_ingredient_groups()
    │     LLM call (SmolLM 1.7B) → 3 ingredient groups
    │
    ├─► Node 3: generate_recipes_parallel()
    │     Fan-out via Send() to 3 parallel SousChef nodes
    │
    ├─► Node 4 (×3): sous_chef_generate_node()
    │     Each SousChef (SmolLM 360M) generates recipes from its ingredient group
    │
    ├─► Node 5: aggregate_recipes()
    │     Sums costs, calculates cost_per_meal, budget_remaining
    │
    ├─► Node 6: Nutritionist.validate_recipes()
    │     LLM validates each recipe (SmolLM 360M, temp=0.3)
    │     Approves or rejects with feedback
    │
    ├─► [Conditional Routing]
    │     All approved → finalize
    │     Some rejected & retries left → retry loop
    │     Max retries & ≥60% approved → finalize (partial)
    │     Max retries & <60% approved → failure
    │
    ├─► Node 7: ChefOrchestrator.handle_rejections()
    │     Sets retry strategy, maps rejected recipes to feedback
    │
    ├─► Node 8: retry_generation()
    │     Regenerates rejected recipes with nutritionist feedback
    │     └─► Back to Node 6 (re-validate)
    │
    ├─► Node 9: finalize_meal_plan()
    │     Saves approved recipes to PostgreSQL
    │     Logs final metrics to MLflow
    │
    └─► Node 10: handle_failure()
          Logs failure, sets status="failed"
    │
    ▼
FastAPI Route converts results → RecipeGenerationResponse
    │
    ▼
JSON Response to User
```

### Three-Layer Architecture

1. **API Layer** (`app/routes/`): FastAPI routers handle HTTP, validate input with Pydantic schemas, delegate to services
2. **Service Layer** (`app/services/`): Business logic, database queries, caching, MLflow logging
3. **Agent Layer** (`app/agents/`): LangGraph workflow with LLM-powered agents for recipe generation

### Key Singletons

These are instantiated once at module level and reused:
- `settings = Settings()` in `app/config.py` — all configuration
- `db = Database()` in `app/db/database.py` — connection pool
- `cache = CacheService()` in `app/services/cache_service.py` — Redis client
- `chef = ChefOrchestrator()` in `app/agents/graph.py` — reused across workflow invocations
- `nutritionist = Nutritionist()` in `app/agents/graph.py` — reused across workflow invocations

---

## 4. Request Lifecycle: End-to-End

This section traces a complete `POST /api/v1/recipes/generate` request through every layer.

### Step 1: HTTP Request Arrives

```json
POST /api/v1/recipes/generate
{
  "user_id": 1,
  "num_meals": 7,
  "preferences": {"cuisine_preferences": ["Italian"]}
}
```

### Step 2: Middleware Processing (`app/main.py`)

1. **CORS Middleware** checks origin against `settings.cors_origins`
2. **Request Timing Middleware** records `start_time = time.time()`
3. Request dispatched to matching route

### Step 3: Route Handler (`app/routes/recipes.py`)

1. Pydantic validates request body as `RecipeGenerationRequest`
2. `UserService.get_user_by_id(request.user_id)` fetches user from PostgreSQL
3. If user not found → `404 HTTPException`
4. Extracts from user record: `postal_code`, `budget`, `household_size`, `dietary_restrictions`
5. Calls `run_recipe_generation(user_id, postal_code, budget, household_size, dietary_restrictions, num_meals, preferences)`

### Step 4: Workflow Entry (`app/main_recipe_generation.py`)

1. Builds `RecipeGenerationState` dict with ~70 fields (most initialized to empty defaults)
2. Calls `create_recipe_generation_graph()` to get compiled LangGraph
3. Calls `graph.invoke(initial_state)` — this executes the entire multi-agent pipeline synchronously
4. Returns `final_state` dict

### Step 5: LangGraph Execution (`app/agents/graph.py` + agent files)

Described in detail in [Section 11](#11-langgraph-multi-agent-workflow).

### Step 6: Response Construction (`app/routes/recipes.py`)

1. Checks `result["status"]` — if not "completed", raises 500
2. Iterates `result["generated_recipes"]`, filters to `approved_recipe_ids` only
3. For each approved recipe, merges `nutrition_facts` and `health_score` from `validation_results`
4. Constructs `RecipeGenerationResponse` with recipes, costs, timing, warnings
5. Returns JSON response

### Step 7: Middleware Post-Processing

1. **Request Timing Middleware** adds `X-Process-Time` header
2. Response sent to client

---

## 5. FastAPI Application (`app/main.py`)

### App Initialization

```python
app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="...",
    openapi_url=f"{settings.api_prefix}/openapi.json"
)
```

### Lifespan (Startup/Shutdown)

**Startup:**
1. `init_db()` — initializes psycopg2 SimpleConnectionPool
2. `init_cache()` — initializes Redis client (graceful fallback if unavailable)
3. Logs environment and database info

**Shutdown:**
1. `close_db()` — closes all pooled database connections
2. `close_cache()` — closes Redis connection

### Middleware Stack (applied in order)

1. **CORS**: Origins from `settings.cors_origins`, all methods/headers allowed, credentials enabled
2. **Request Timing**: Adds `X-Process-Time` response header

### Exception Handlers

- `RequestValidationError` → 422 with detailed error array and request body
- `Exception` (catch-all) → 500 with safe error message, full traceback logged

### Built-in Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | App info (name, version, docs URLs) |
| GET | `/health` | Health check (database, Redis, Ollama status) |
| GET | `/api/v1/info` | Feature flags, rate limits, cache TTLs |

### Registered Routers

All under prefix `/api/v1`:
- `users.router` — User CRUD
- `stores.router` — Store/deal discovery
- `recipes.router` — Recipe generation
- `shopping_lists.router` — Shopping list management

---

## 6. Configuration (`app/config.py`)

The `Settings` class extends `pydantic_settings.BaseSettings` and loads all values from environment variables (with `.env` file support).

### All Configuration Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `app_name` | str | "Grocery Optimizer API" | Application display name |
| `version` | str | "0.1.0" | API version |
| `environment` | str | "development" | dev/staging/production |
| `debug` | bool | True | Debug mode toggle |
| `api_prefix` | str | "/api/v1" | URL prefix for all routes |
| `secret_key` | str | "dev-secret-key..." | For future auth use |
| `database_url` | str | **REQUIRED** | PostgreSQL connection string |
| `database_pool_size` | int | 10 | Max pool connections |
| `database_max_overflow` | int | 20 | Overflow connections (unused currently) |
| `redis_url` | Optional[str] | None | Redis connection string |
| `redis_enabled` | bool | False | Master toggle for caching |
| `cache_ttl_deals` | int | 21600 (6h) | Deal cache duration in seconds |
| `cache_ttl_recipes` | int | 86400 (24h) | Recipe cache duration in seconds |
| `cache_ttl_stores` | int | 604800 (7d) | Store cache duration in seconds |
| `ollama_base_url` | str | "http://localhost:11434" | Ollama server URL |
| `ollama_chef_model` | str | "smollm:1.7b" | Chef orchestrator model |
| `ollama_sous_chef_model` | str | "smollm:360m" | SousChef model |
| `ollama_nutritionist_model` | str | "smollm:360m" | Nutritionist model |
| `mlflow_tracking_uri` | str | "http://localhost:5000" | MLflow server URL |
| `mlflow_experiment_name` | str | "grocery-meal-planner" | MLflow experiment name |
| `huggingface_api_key` | Optional[str] | None | HuggingFace API key |
| `flipp_api_key` | Optional[str] | None | Flipp grocery API key |
| `flipp_api_url` | str | "https://api.flipp.com/v2" | Flipp API base URL |
| `enable_cost_tracking` | bool | True | Track LLM inference costs |
| `cost_smollm_1_7b` | float | 0.001 | Cost per 1M tokens (1.7B) |
| `cost_smollm_360m` | float | 0.0005 | Cost per 1M tokens (360M) |
| `cost_smollm_135m` | float | 0.0002 | Cost per 1M tokens (135M) |
| `api_rate_limit` | int | 100 | Requests per minute |
| `cors_origins` | list[str] | ["localhost:3000", "localhost:8000"] | Allowed CORS origins |
| `log_level` | str | "INFO" | Logging level |

**Global Instance**: `settings = Settings()` — imported throughout the app.

**Important**: The agent files (`chef_orchestrator.py`, `sous_chef.py`, `nutritionist.py`) currently **hardcode** model names instead of reading from `settings`. The migration plan addresses this.

---

## 7. Database Layer

### Connection Pool (`app/db/database.py`)

**Class: `Database`**

Uses `psycopg2.pool.SimpleConnectionPool` with min 1, max `settings.database_pool_size` (default 10) connections.

**Key Methods:**

```python
# Initialize pool (called once on startup)
db.initialize()

# Get a connection (auto-commit on success, rollback on exception)
with db.get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT ...")

# Get a cursor directly (returns RealDictCursor — rows as dicts)
with db.get_cursor() as cursor:
    cursor.execute("SELECT ...")
    results = cursor.fetchall()  # List[Dict]
```

**Critical Behavior:**
- `get_connection()` lazily initializes the pool if not yet created
- Connections are returned to the pool in a `finally` block
- On exception: transaction is rolled back, then connection returned to pool
- All cursors use `RealDictCursor` by default (rows returned as Python dicts, not tuples)

**Exports from `app/db/__init__.py`:**
- `db` — the singleton Database instance
- `get_db()` — FastAPI dependency injection function
- `init_db()` — calls `db.initialize()`
- `close_db()` — calls `db.close()`

### Database Service (`app/services/database.py`)

**Class: `DatabaseService`**

A thin wrapper providing domain-specific query methods:

1. **`fetch_current_deals(postal_code: str) -> List[Dict]`**
   - Joins `deals` and `stores` tables
   - Filters: `postal_code` match, `valid_from <= TODAY <= valid_until`
   - Orders by `discount_percentage DESC`
   - Limits to 200 results
   - Returns: list of dicts with keys: `deal_id`, `product_name`, `sale_price`, `regular_price`, `discount_percentage`, `store_name`, `store_id`, `chain`

2. **`save_recipes(user_id: int, recipes: List[Dict]) -> List[int]`**
   - Inserts into `recipes` table
   - Serializes `ingredients` list to JSON for JSONB column
   - Returns: list of auto-generated `recipe_id` values

---

## 8. Pydantic Schemas (`app/models/schemas.py`)

All request/response models. These are critical because they define the API contract.

### User Schemas

| Schema | Type | Fields |
|--------|------|--------|
| `UserCreate` | Request | `email` (EmailStr), `postal_code` (6-10 chars, auto-uppercase), `budget` (Decimal, 0-10000, default 100), `household_size` (int, 1-20, default 1), `dietary_restrictions` (List[str], default []) |
| `UserResponse` | Response | All of UserCreate + `user_id` (int), `created_at` (datetime), `is_active` (bool) |
| `UserUpdate` | Request | `postal_code`, `budget`, `household_size`, `dietary_restrictions` — all Optional |

### Store & Deal Schemas

| Schema | Type | Fields |
|--------|------|--------|
| `StoreInfo` | Response | `store_id`, `name`, `chain`, `postal_code`, `address`, `city`, `province` |
| `DealInfo` | Response | `deal_id`, `product_name`, `brand`, `sale_price`, `regular_price`, `discount_percentage`, `unit`, `category`, `valid_from` (date), `valid_until` (date), `store_name`, `chain` |
| `PostalCodeDiscoveryRequest` | Request | `postal_code` (6-10 chars) |
| `PostalCodeDiscoveryResponse` | Response | `postal_code`, `stores_found`, `deals_count`, `stores` (List[StoreInfo]), `job_id` (Optional), `message` |

### Recipe Schemas

| Schema | Type | Fields |
|--------|------|--------|
| `IngredientInfo` | Nested | `name`, `quantity`, `unit`, `price` |
| `RecipeInfo` | Response | `recipe_id`, `name`, `ingredients` (List[dict]), `instructions` (List[str]), `total_cost`, `servings`, `estimated_prep_time`, `meal_type`, `cuisine_type`, `nutrition_facts` (dict), `health_score`, `created_at` |
| `RecipeGenerationRequest` | Request | `user_id` (int), `num_meals` (1-21, default 7), `preferences` (Dict) |
| `RecipeGenerationResponse` | Response | `recipes` (List[RecipeInfo]), `total_cost`, `cost_per_meal`, `estimated_savings`, `generation_time` (float), `status`, `warnings` (List[str]) |

### Shopping List Schemas

| Schema | Type | Fields |
|--------|------|--------|
| `ShoppingListItem` | Nested | `product`, `quantity`, `store`, `price`, `category` |
| `ShoppingListResponse` | Response | `list_id`, `user_id`, `recipe_ids`, `items`, `total_cost`, `estimated_savings`, `stores`, `created_at`, `is_completed` |

### Utility Schemas

| Schema | Type | Fields |
|--------|------|--------|
| `ErrorResponse` | Response | `error`, `detail` (Optional), `timestamp` |
| `HealthCheckResponse` | Response | `status`, `version`, `database`, `redis` (Optional), `ollama` (Optional), `timestamp` |
| `APIUsageStats` | Response | `total_calls`, `total_tokens`, `total_cost`, `avg_cost_per_call`, `by_model`, `last_api_call` |
| `UserMetricsResponse` | Response | `user_id`, `email`, `api_usage`, `recipe_stats`, `total_savings` |

---

## 9. Routes

### Users Router (`app/routes/users.py`)

| Method | Path | Status | Request Body | Response | Notes |
|--------|------|--------|--------------|----------|-------|
| POST | `/api/v1/users/register` | 201 | `UserCreate` | `UserResponse` | Catches duplicate email |
| GET | `/api/v1/users/{user_id}` | 200 | — | `UserResponse` | 404 if not found or inactive |
| PUT | `/api/v1/users/{user_id}` | 200 | `UserUpdate` | `UserResponse` | Dynamic query (only non-None fields) |
| DELETE | `/api/v1/users/{user_id}` | 204 | — | — | Soft delete (sets is_active=false) |

### Stores Router (`app/routes/stores.py`)

| Method | Path | Status | Parameters | Response | Notes |
|--------|------|--------|------------|----------|-------|
| POST | `/api/v1/postal-code/discover` | 200 | Body: `PostalCodeDiscoveryRequest` | `PostalCodeDiscoveryResponse` | Finds stores + deal count |
| GET | `/api/v1/postal-code/deals/{postal_code}` | 200 | Query: `category`, `limit` (1-200, default 50) | `List[DealInfo]` | Cached via Redis |
| GET | `/api/v1/postal-code/top-deals/{postal_code}` | 200 | Query: `limit` (1-50, default 20) | `List[DealInfo]` | Sorted by discount |
| GET | `/api/v1/postal-code/search/{postal_code}` | 200 | Query: `q` (min 2 chars) | `List[DealInfo]` | ILIKE search on name/brand |

### Recipes Router (`app/routes/recipes.py`)

| Method | Path | Status | Request Body | Response | Notes |
|--------|------|--------|--------------|----------|-------|
| POST | `/api/v1/recipes/generate` | 200 | `RecipeGenerationRequest` | `RecipeGenerationResponse` | **Main feature** — triggers full LangGraph workflow |
| GET | `/api/v1/recipes/{recipe_id}` | 501 | — | `RecipeInfo` | **NOT IMPLEMENTED** |
| GET | `/api/v1/recipes/user/{user_id}` | 501 | Query: `limit` | `List[RecipeInfo]` | **NOT IMPLEMENTED** |

### Shopping Lists Router (`app/routes/shopping_lists.py`)

| Method | Path | Status | Notes |
|--------|------|--------|-------|
| GET | `/api/v1/shopping-list/{user_id}` | 501 | **NOT IMPLEMENTED** |
| POST | `/api/v1/shopping-list/{user_id}/mark-complete` | 501 | **NOT IMPLEMENTED** |

---

## 10. Services Layer

### UserService (`app/services/user_service.py`)

All methods are `@staticmethod`. Uses `app.db` for queries.

| Method | Signature | Description |
|--------|-----------|-------------|
| `create_user` | `(user_data: UserCreate) -> Dict` | INSERT into users. Catches duplicate email constraint violations and raises `ValueError`. Returns user dict. |
| `get_user_by_id` | `(user_id: int) -> Optional[Dict]` | SELECT by user_id WHERE is_active=true |
| `get_user_by_email` | `(email: str) -> Optional[Dict]` | SELECT by email WHERE is_active=true |
| `update_user` | `(user_id: int, user_data: UserUpdate) -> Optional[Dict]` | Dynamic UPDATE (only non-None fields). Converts budget to float, serializes dietary_restrictions to JSON. |
| `update_last_login` | `(user_id: int)` | Sets last_login = NOW() |
| `deactivate_user` | `(user_id: int) -> bool` | Soft delete: sets is_active=false |

### StoreService (`app/services/store_service.py`)

All methods are `@staticmethod`. Uses `app.db` for queries, `cache` for Redis.

| Method | Signature | Cached? | Description |
|--------|-----------|---------|-------------|
| `get_stores_by_postal_code` | `(postal_code) -> List[Dict]` | No | SELECT from stores WHERE postal_code and is_active |
| `get_current_deals_by_postal_code` | `(postal_code, category=None) -> List[Dict]` | **Yes** | Cache key: `deals:{postal_code}:{category\|'all'}`. TTL: `settings.cache_ttl_deals` (6h). Joins deals+stores, filters by date validity. |
| `get_deals_by_category` | `(postal_code, categories: List[str]) -> Dict[str, List]` | No | Uses `ANY(%(categories)s)` for array query. Groups by category in Python. |
| `get_top_deals` | `(postal_code, limit=20) -> List[Dict]` | No | Top N by discount_percentage DESC |
| `search_deals` | `(postal_code, search_term) -> List[Dict]` | No | ILIKE on product_name and brand, limit 50 |
| `get_deal_statistics` | `(postal_code) -> Dict` | No | Aggregate stats: count, avg/max discount, min/max price |

### DatabaseService (`app/services/database.py`)

Described in [Section 7](#7-database-layer). Two methods: `fetch_current_deals()` and `save_recipes()`.

### CacheService (`app/services/cache_service.py`)

Described in detail in [Section 17](#17-redis-cache-service).

### MLflowLogger (`app/services/mlflow_logger.py`)

Described in detail in [Section 18](#18-mlflow-logger).

---

## 11. LangGraph Multi-Agent Workflow

This is the core feature. The workflow is a directed graph with 10 nodes, defined in `app/agents/graph.py`.

### Workflow Nodes (in execution order)

| # | Node Name | Function | Agent | Description |
|---|-----------|----------|-------|-------------|
| 1 | `initialize_chef` | `chef.initialize()` | ChefOrchestrator | Fetch deals, start MLflow, init state |
| 2 | `plan_ingredient_groups` | `chef.plan_ingredient_groups()` | ChefOrchestrator | LLM creates 3 ingredient groups |
| 3 | `generate_recipes_parallel` | `generate_recipes_parallel()` | (routing) | Fan-out to 3 parallel SousChef nodes via `Send()` |
| 4 | `sous_chef_generate` | `sous_chef_generate_node()` | SousChef (×3) | Each generates recipes from assigned ingredients |
| 5 | `aggregate_recipes` | `aggregate_recipes()` | (computation) | Sum costs, calculate per-meal cost |
| 6 | `nutritionist_validate` | `nutritionist.validate_recipes()` | Nutritionist | Validate each recipe, approve/reject |
| 7 | `handle_rejections` | `chef.handle_rejections()` | ChefOrchestrator | Determine retry strategy |
| 8 | `retry_generation` | `retry_generation()` | SousChef (new instance) | Regenerate rejected recipes with feedback |
| 9 | `finalize_meal_plan` | `finalize_meal_plan()` | (persistence) | Save to DB, log to MLflow |
| 10 | `handle_failure` | `handle_failure()` | (error handling) | Log failure, set status="failed" |

### Edge Definitions

```
START → initialize_chef → plan_ingredient_groups → generate_recipes_parallel
                                                          │
                                              ┌───────────┼───────────┐
                                              ▼           ▼           ▼
                                         sous_chef_1  sous_chef_2  sous_chef_3
                                              │           │           │
                                              └───────────┼───────────┘
                                                          ▼
                                                  aggregate_recipes
                                                          │
                                                          ▼
                                               nutritionist_validate
                                                          │
                                              ┌───────────┼───────────┐
                                              ▼           ▼           ▼
                                    ┌─────────────┐  ┌────────┐  ┌─────────┐
                                    │ finalize (all│  │ retry  │  │ failure │
                                    │ approved or  │  │ loop   │  │ (<60%   │
                                    │ ≥60% + max   │  │        │  │ after   │
                                    │ retries)     │  │        │  │ max)    │
                                    └──────┬──────┘  └───┬────┘  └────┬────┘
                                           │             │            │
                                           ▼             ▼            ▼
                                          END    handle_rejections   END
                                                       │
                                                       ▼
                                                retry_generation
                                                       │
                                                       ▼
                                              nutritionist_validate
                                                  (loop back)
```

### Conditional Routing Logic (`route_after_validation()`)

After the Nutritionist validates all recipes, routing is determined by:

```python
num_rejected = len(state["rejected_recipe_ids"])
num_approved = len(state["approved_recipe_ids"])
num_requested = state["num_meals"]

if num_rejected == 0:
    return "finalize"                          # All approved!
elif state["iteration_count"] >= state["max_iterations"]:
    if num_approved >= num_requested * 0.6:    # 60% threshold
        return "finalize_partial"              # Good enough
    else:
        return "handle_failure"                # Not enough recipes
else:
    return "retry"                             # Try again
```

Both `"finalize"` and `"finalize_partial"` route to `finalize_meal_plan` (same node).

### Parallel Execution via Send()

Node 3 (`generate_recipes_parallel()`) uses LangGraph's `Send()` mechanism:

```python
# Distributes work to 3 SousChef instances
sends = []
for i in range(3):
    recipes_for_this_chef = (num_meals // 3) + (1 if i < num_meals % 3 else 0)
    sends.append(Send("sous_chef_generate", {
        **state,
        "chef_id": f"sous_chef_{i+1}",
        "ingredient_group": state["ingredient_groups"][i],
        "target_recipe_count": recipes_for_this_chef
    }))
return sends
```

Each `Send()` creates a parallel execution of the `sous_chef_generate` node with different inputs. Results are automatically merged back into the shared state.

### Retry Loop Detail

1. **Nutritionist rejects recipes** → routes to `handle_rejections`
2. **`handle_rejections()`** increments `iteration_count`, sets `retry_strategy`:
   - Iteration 1: `"reassign_chef"` — same ingredients, new attempt
   - Iteration 2+: `"new_ingredients"` — (incomplete, falls through to empty dict)
3. **`retry_generation()`** creates a new SousChef instance and calls `regenerate_with_feedback()` for each rejected recipe, passing the Nutritionist's specific feedback
4. **Back to Nutritionist** — only validates NEW recipes (skips already-validated ones via `if recipe_id in state["validation_results"]: continue`)
5. Approved/rejected lists **accumulate** across iterations via `extend()` (not replace)

**Max iterations is hardcoded to 2** in `ChefOrchestrator.initialize()`.

---

## 12. Agent Implementations

### ChefOrchestrator (`app/agents/chef_orchestrator.py`)

**LLM**: `ChatOllama(model="smollm:1.7b", temperature=0.7, format="json")`

| Method | LangGraph Node | Purpose |
|--------|---------------|---------|
| `initialize(state)` | Node 1 | Fetches deals from DB via `DatabaseService.fetch_current_deals()`. Builds `deal_index` (dict mapping product_name → deal dict for O(1) lookups). Starts MLflow run. Sets `max_iterations=2`, clears all output lists. |
| `plan_ingredient_groups(state)` | Node 2 | Formats `CHEF_INGREDIENT_PLANNING` prompt with deals (limited to first 100), budget, household_size, num_meals, dietary_restrictions. Calls LLM. Parses JSON response to get 3 `ingredient_groups` and `ingredient_reuse_map`. Validates exactly 3 groups returned. |
| `handle_rejections(state)` | Node 7 | Increments `iteration_count`. If iteration 1: maps each rejected recipe_id to its validation feedback. If iteration 2+: sets empty `recipes_pending_retry` (incomplete implementation). |

**Error Handling**: `plan_ingredient_groups` catches all exceptions, appends to `state["errors"]`, sets `status="failed"`. Other methods have no explicit error handling.

### SousChef (`app/agents/sous_chef.py`)

**LLM**: `ChatOllama(model="smollm:360m", temperature=0.8, format="json")`

| Method | Purpose |
|--------|---------|
| `generate_recipes(chef_id, ingredient_group, target_recipe_count, household_size, dietary_restrictions)` | Formats `SOUS_CHEF_RECIPE_GENERATION` prompt. Calls LLM. Parses JSON array of recipes. Assigns UUID `recipe_id` to each. Returns `List[Recipe]` or empty list on error. |
| `regenerate_with_feedback(chef_id, original_recipe, feedback, ingredient_group, household_size, dietary_restrictions)` | Formats `SOUS_CHEF_RETRY_WITH_FEEDBACK` prompt with original recipe and feedback. Returns single new `Recipe` or None on error. |

**Node Function**: `sous_chef_generate_node(state)` — creates a new SousChef instance, calls `generate_recipes()`, stores results in `state["generated_recipes"]` and `state["sous_chef_assignments"]`.

**Important**: Each recipe gets a new UUID on generation AND on regeneration. The old recipe_id is not preserved.

### Nutritionist (`app/agents/nutritionist.py`)

**LLM**: `ChatOllama(model="smollm:360m", temperature=0.3, format="json")`

| Method | LangGraph Node | Purpose |
|--------|---------------|---------|
| `validate_recipes(state)` | Node 6 | Iterates all `generated_recipes`. **Skips recipes already in `validation_results`** (important for retry loops). For each new recipe: formats `NUTRITIONIST_VALIDATION` prompt, calls LLM, parses `ValidationResult`. Categorizes as approved or rejected. On parse error: recipe is **rejected** (graceful degradation). |

**Key Design**: Approved/rejected lists are accumulated via `extend()`, not replaced. This means after retry, the lists contain results from all iterations.

### Ollama Invocation Pattern (all agents)

All agents follow the same pattern:

```python
self.llm = ChatOllama(
    model="<model_name>",
    temperature=<float>,
    format="json"       # Forces JSON output from Ollama
)

# Invocation
response = self.llm.invoke([HumanMessage(content=prompt_string)])

# Parse
result = json.loads(response.content)
```

Temperature settings by role:
- **Chef** (0.7): Moderate — balanced planning
- **SousChef** (0.8): High — creative recipe generation
- **Nutritionist** (0.3): Low — consistent, reliable validation

---

## 13. Workflow State (`app/agents/state.py`)

The entire workflow operates on a single `RecipeGenerationState` TypedDict with ~70 fields. This dict is passed to every node and mutated in place.

### State Field Groups

**Input Configuration (set once at entry):**
```python
user_id: int
postal_code: str
budget: float
household_size: int
dietary_restrictions: List[str]
num_meals: int
preferences: Dict
```

**Deal Data (set by ChefOrchestrator.initialize):**
```python
available_deals: List[Dict]        # Raw deals from database
deal_index: Dict[str, Dict]       # {product_name: deal_dict} for O(1) lookup
```

**Chef Orchestration (set by ChefOrchestrator.plan_ingredient_groups):**
```python
ingredient_groups: List[List[Dict]]     # 3 groups, one per SousChef
ingredient_reuse_map: Dict[str, int]    # Tracks ingredient reuse across groups
target_ingredients_per_group: int       # UNUSED — defined but never read
```

**SousChef Outputs (set by sous_chef_generate_node):**
```python
generated_recipes: Dict[str, Recipe]     # Keyed by recipe_id (UUID)
sous_chef_assignments: Dict[str, str]    # Maps recipe_id → chef_id (sous_chef_1/2/3)
```

**Nutritionist Validation (set by Nutritionist.validate_recipes):**
```python
validation_results: Dict[str, ValidationResult]  # Keyed by recipe_id
approved_recipe_ids: List[str]                    # Accumulated across iterations
rejected_recipe_ids: List[str]                    # Accumulated across iterations
```

**Retry Mechanism:**
```python
iteration_count: int                              # Starts at 0, incremented per retry
max_iterations: int                               # Hardcoded to 2
retry_strategy: Literal["reassign_chef", "new_ingredients"]
recipes_pending_retry: Dict[str, str]             # Maps recipe_id → feedback
```

**Cost Tracking (set by aggregate_recipes):**
```python
total_cost: float
cost_per_meal: float
estimated_savings: float          # Always 0.0 (placeholder)
budget_remaining: float
```

**MLflow Tracking:**
```python
mlflow_run_id: str
agent_call_log: List[Dict]        # Appended to by each agent
```

**Workflow Control:**
```python
status: Literal["initializing", "planning", "generating", "validating", "retrying", "completed", "failed"]
errors: List[str]
warnings: List[str]
```

### TypedDict Definitions

**`Recipe`**:
```python
class Recipe(TypedDict):
    recipe_id: str           # UUID
    name: str
    ingredients: List[Dict]  # [{name, quantity, unit, price, store_id}]
    instructions: List[str]
    servings: int
    total_cost: float
    estimated_prep_time: int
    meal_type: str
    cuisine_type: Optional[str]
```

**`ValidationResult`**:
```python
class ValidationResult(TypedDict):
    recipe_id: str
    approved: bool
    feedback: str
    nutrition_facts: Dict    # {calories, protein_g, carbs_g, fat_g, vitamins}
    dietary_compliance: Dict # {allergen_free: bool, meets_restrictions: bool}
    health_score: float      # 0-100
```

---

## 14. Prompt Templates (`app/agents/prompts.py`)

The `PromptTemplates` class contains 5 prompt templates as class-level string constants with `.format()` placeholders.

### 1. CHEF_INGREDIENT_PLANNING

**Used by**: `ChefOrchestrator.plan_ingredient_groups()`

**Input variables**: `deals_json`, `budget`, `household_size`, `num_meals`, `dietary_restrictions`, `preferences`

**Instructs the LLM to**:
- Analyze available grocery deals
- Create exactly 3 optimized ingredient groups
- Maximize ingredient reuse across groups
- Balance cost across groups
- Ensure complementary ingredients (protein + starch + vegetables)
- Respect dietary restrictions

**Expected JSON output**:
```json
{
  "ingredient_groups": [[], [], []],
  "ingredient_reuse_map": {"ingredient_name": reuse_count},
  "rationale": "explanation string"
}
```

### 2. SOUS_CHEF_RECIPE_GENERATION

**Used by**: `SousChef.generate_recipes()`

**Input variables**: `ingredients_json`, `target_recipe_count`, `servings`, `dietary_restrictions`

**Instructs the LLM to**:
- Generate recipes using only assigned ingredients (plus basic pantry staples)
- Calculate exact costs from ingredient prices
- Provide clear cooking instructions

**Expected JSON output**: Array of recipe objects

### 3. SOUS_CHEF_RETRY_WITH_FEEDBACK

**Used by**: `SousChef.regenerate_with_feedback()`

**Input variables**: `original_recipe_json`, `feedback`, `ingredients_json`, `servings`

**Instructs the LLM to**: Create a NEW recipe addressing the specific feedback from the Nutritionist.

### 4. NUTRITIONIST_VALIDATION

**Used by**: `Nutritionist.validate_recipes()`

**Input variables**: `recipe_json`, `dietary_restrictions`

**Validation checklist**:
1. Allergen safety
2. Dietary compliance
3. Nutritional balance (macros)
4. Practical cooking (instructions clarity)
5. Ingredient coherence

**Expected JSON output**:
```json
{
  "approved": true/false,
  "feedback": "specific issues or praise",
  "nutrition_facts": {
    "calories_per_serving": 0,
    "protein_g": 0,
    "carbs_g": 0,
    "fat_g": 0,
    "fiber_g": 0,
    "vitamins": []
  },
  "dietary_compliance": {
    "allergen_free": true/false,
    "meets_restrictions": true/false,
    "violations": []
  },
  "health_score": 0-100
}
```

### 5. CHEF_NEW_INGREDIENTS_SELECTION

**Used by**: Not currently used (defined but not called anywhere).

**Input variables**: `rejection_feedback`, `remaining_deals_json`, `budget_remaining`, `dietary_restrictions`, `recipes_needed`

**Purpose**: Would select alternative ingredient combinations when initial ones fail validation repeatedly.

---

## 15. Graph Construction & Node Wiring (`app/agents/graph.py`)

### Global Instances (top of file)

```python
chef = ChefOrchestrator()      # Single instance, reused across all invocations
nutritionist = Nutritionist()  # Single instance, reused across all invocations
db_service = DatabaseService()  # Single instance for recipe persistence
```

### `create_recipe_generation_graph()` Function

Creates and returns a compiled `StateGraph(RecipeGenerationState)`.

**Node registration** (10 nodes):
```python
workflow.add_node("initialize_chef", chef.initialize)
workflow.add_node("plan_ingredient_groups", chef.plan_ingredient_groups)
workflow.add_node("generate_recipes_parallel", generate_recipes_parallel)
workflow.add_node("sous_chef_generate", sous_chef_generate_node)
workflow.add_node("aggregate_recipes", aggregate_recipes)
workflow.add_node("nutritionist_validate", nutritionist.validate_recipes)
workflow.add_node("handle_rejections", chef.handle_rejections)
workflow.add_node("retry_generation", retry_generation)
workflow.add_node("finalize_meal_plan", finalize_meal_plan)
workflow.add_node("handle_failure", handle_failure)
```

**Edge registration**:
```python
workflow.set_entry_point("initialize_chef")
workflow.add_edge("initialize_chef", "plan_ingredient_groups")
workflow.add_edge("plan_ingredient_groups", "generate_recipes_parallel")
# generate_recipes_parallel returns Send() objects → sous_chef_generate
workflow.add_edge("aggregate_recipes", "nutritionist_validate")
workflow.add_conditional_edges(
    "nutritionist_validate",
    route_after_validation,
    {
        "finalize": "finalize_meal_plan",
        "retry": "handle_rejections",
        "finalize_partial": "finalize_meal_plan",
        "handle_failure": "handle_failure"
    }
)
workflow.add_edge("handle_rejections", "retry_generation")
workflow.add_edge("retry_generation", "nutritionist_validate")
workflow.add_edge("finalize_meal_plan", END)
workflow.add_edge("handle_failure", END)
```

### Helper Node Functions (defined in graph.py)

**`generate_recipes_parallel(state)`**: Calculates recipes per chef, returns list of `Send("sous_chef_generate", ...)` objects.

**`aggregate_recipes(state)`**: Sums `total_cost` from all generated recipes. Calculates `cost_per_meal = total_cost / num_recipes`. Sets `budget_remaining = budget - total_cost`. Sets `estimated_savings = 0.0` (placeholder).

**`retry_generation(state)`**: Creates new SousChef. For each recipe in `recipes_pending_retry`, calls `sous_chef.regenerate_with_feedback()` with original recipe, feedback, and ingredient group. Replaces old recipe in `generated_recipes` with new one. Clears `recipes_pending_retry`.

**`finalize_meal_plan(state)`**: Filters to approved recipes only. Calls `db_service.save_recipes()`. Logs final metrics to MLflow. Sets `status="completed"`.

**`handle_failure(state)`**: Sets `status="failed"`. Appends error message. Logs failure to MLflow.

---

## 16. Recipe Generation Entry Point (`app/main_recipe_generation.py`)

### `run_recipe_generation()` Function

**Signature:**
```python
def run_recipe_generation(
    user_id: int,
    postal_code: str,
    budget: float,
    household_size: int,
    dietary_restrictions: list,
    num_meals: int,
    preferences: dict = None
) -> dict
```

**What it does:**
1. Constructs `initial_state` dict with all ~70 fields of `RecipeGenerationState`, mostly initialized to empty defaults
2. Calls `create_recipe_generation_graph()` to get the compiled graph
3. Calls `graph.invoke(initial_state)` — synchronous execution of the entire workflow
4. Returns `final_state` dict

**Return value** is the complete final state dict, which includes:
- `status`: "completed" or "failed"
- `generated_recipes`: Dict of all recipe objects (approved and rejected)
- `approved_recipe_ids`: List of approved recipe UUIDs
- `validation_results`: Dict with nutrition_facts and health_score per recipe
- `total_cost`, `cost_per_meal`, `estimated_savings`
- `errors`, `warnings`

---

## 17. Redis Cache Service

### File: `app/services/cache_service.py`

### CacheService Class

**Initialization behavior:**
- Checks `settings.redis_enabled` and `settings.redis_url`
- If both present: creates `redis.Redis` client with `decode_responses=True`, 5-second timeouts
- If Redis connection fails: logs warning, sets `self.client = None` — all operations gracefully return None/False
- If `redis_enabled` is False: no client created

**Core Methods:**

| Method | Signature | Returns | Behavior on Error |
|--------|-----------|---------|-------------------|
| `get` | `(key: str)` | `Any \| None` | Returns None, logs error |
| `set` | `(key: str, value: Any, ttl: int = None)` | `bool` | Returns False, logs error |
| `delete` | `(key: str)` | `bool` | Returns False, logs error |
| `delete_pattern` | `(pattern: str)` | `int` | Returns 0, logs error |
| `clear` | `()` | `bool` | Returns False, logs error |
| `exists` | `(key: str)` | `bool` | Returns False, logs error |
| `get_ttl` | `(key: str)` | `int \| None` | Returns None, logs error |
| `increment` | `(key: str, amount: int = 1)` | `int \| None` | Returns None, logs error |
| `close` | `()` | None | Logs error |

All values are JSON-serialized/deserialized. TTL is set via Redis `SETEX` command.

### `@cached` Decorator

```python
@cached(ttl=3600, key_prefix="deals")
def some_function(postal_code: str):
    ...
```

- Constructs cache key from: `{key_prefix}:{arg1}:{arg2}:{sorted_kwarg_pairs}`
- On cache hit: returns deserialized cached value (skips function)
- On cache miss: executes function, caches result with TTL, returns result

### Cache Key Patterns Used in the App

| Pattern | TTL | Used By |
|---------|-----|---------|
| `deals:{postal_code}:{category\|all}` | 6 hours (21600s) | `StoreService.get_current_deals_by_postal_code()` |

### Global Instance

```python
cache = CacheService()  # Singleton, created at import time
```

### Lifecycle Functions

- `init_cache()`: Called on FastAPI startup (currently a no-op, init happens in constructor)
- `close_cache()`: Called on FastAPI shutdown, calls `cache.close()`

---

## 18. MLflow Logger

### File: `app/services/mlflow_logger.py`

All methods are `@staticmethod` — no instance needed.

### Methods

| Method | When Called | What It Logs |
|--------|------------|--------------|
| `start_run(user_id, num_meals, budget, dietary_restrictions)` | Node 1 (initialize) | Creates MLflow run. Logs params: user_id, num_meals, budget, dietary_restrictions, timestamp. Returns run_id. |
| `log_agent_call(agent_name, tokens, duration, model, success, error)` | After each LLM call | Metrics: `{agent}_tokens`, `{agent}_duration_sec`. Param: `{agent}_error` if failed. |
| `log_ingredient_groups(groups, reuse_map)` | Node 2 (plan) | Metrics: group_count, avg_ingredients_per_group, total_unique, max/avg_reuse. Artifact: `ingredient_reuse_map.json` |
| `log_validation_results(validation_results)` | Node 6 (validate) | Metrics: validated/approved/rejected counts, approval_rate, avg_health_score. Artifact: `rejection_reasons.txt` |
| `log_final_metrics(total_cost, cost_per_meal, estimated_savings, iterations, recipe_count, success)` | Node 9 or 10 (finalize/failure) | All cost metrics + workflow_success param |
| `finalize_run(state)` | Node 9 or 10 | Artifacts: approved_recipes JSON, agent_call_log JSON. Calls `mlflow.end_run()`. |

### Dependencies

Requires a running MLflow tracking server at `settings.mlflow_tracking_uri` (default: `http://localhost:5000`). If MLflow is unavailable, calls will fail silently or raise (no explicit error handling in the logger itself).

---

## 19. Database Schema (SQL)

### File: `scripts/init_db.sql` (344 lines)

### Required Extensions

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- UUID generation
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- Trigram text search
```

### Tables

#### `users`
```sql
user_id SERIAL PRIMARY KEY
email VARCHAR(255) UNIQUE NOT NULL
postal_code VARCHAR(10) NOT NULL
budget DECIMAL(10,2) DEFAULT 100.00
household_size INTEGER DEFAULT 1
dietary_restrictions JSONB DEFAULT '[]'
is_active BOOLEAN DEFAULT true
last_login TIMESTAMP
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

#### `stores`
```sql
store_id SERIAL PRIMARY KEY
name VARCHAR(255) NOT NULL
chain VARCHAR(255)
postal_code VARCHAR(10) NOT NULL
address TEXT
latitude DECIMAL(10,7)
longitude DECIMAL(10,7)
is_active BOOLEAN DEFAULT true
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

#### `price_snapshots` (PARTITIONED BY RANGE on `time`)
```sql
snapshot_id BIGSERIAL
time TIMESTAMPTZ NOT NULL
store_id INTEGER REFERENCES stores(store_id)
product_name VARCHAR(255) NOT NULL
brand VARCHAR(255)
price DECIMAL(10,2) NOT NULL
sale_price DECIMAL(10,2)
unit VARCHAR(50)
category VARCHAR(100)
PRIMARY KEY (snapshot_id, time)
```

**Partitions defined**: 2025-09 through 2025-12 only.
**CRITICAL BUG**: Missing partitions for 2026 months. Inserting data with 2026 timestamps will fail. See [Known Issues](#25-known-issues--gotchas).

#### `deals`
```sql
deal_id SERIAL PRIMARY KEY
store_id INTEGER REFERENCES stores(store_id)
product_name VARCHAR(255) NOT NULL
brand VARCHAR(255)
sale_price DECIMAL(10,2) NOT NULL
regular_price DECIMAL(10,2)
discount_percentage DECIMAL(5,2)
unit VARCHAR(50)
category VARCHAR(100)
valid_from DATE
valid_until DATE
is_active BOOLEAN DEFAULT true
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

#### `recipes`
```sql
recipe_id SERIAL PRIMARY KEY
user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE
name VARCHAR(255) NOT NULL
ingredients JSONB NOT NULL
instructions TEXT[] NOT NULL
total_cost DECIMAL(10,2)
servings INTEGER DEFAULT 2
prep_time INTEGER  -- minutes
cook_time INTEGER  -- minutes
cuisine_type VARCHAR(100)
meal_type VARCHAR(50)
nutritional_info JSONB
allergen_info JSONB
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

#### `shopping_lists`
```sql
list_id SERIAL PRIMARY KEY
user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE
recipe_ids INTEGER[]
items JSONB NOT NULL
total_cost DECIMAL(10,2)
estimated_savings DECIMAL(10,2)
regular_total DECIMAL(10,2)
is_completed BOOLEAN DEFAULT false
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

#### `api_usage`
```sql
usage_id SERIAL PRIMARY KEY
user_id INTEGER REFERENCES users(user_id)
model_name VARCHAR(100) NOT NULL
tokens_used INTEGER NOT NULL
estimated_cost DECIMAL(10,6)
endpoint VARCHAR(255)
execution_time_ms INTEGER
status VARCHAR(20) DEFAULT 'success'
error_message TEXT
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

#### `agent_logs`
```sql
log_id SERIAL PRIMARY KEY
user_id INTEGER REFERENCES users(user_id)
agent_name VARCHAR(100) NOT NULL
task_type VARCHAR(100)
input_data JSONB
output_data JSONB
execution_time_ms INTEGER
tokens_used INTEGER
status VARCHAR(20) DEFAULT 'success'
error_message TEXT
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

### Materialized Views

- **`best_deals_by_category`**: Deals with ≥20% discount, current and future, ordered by discount DESC
- **`price_trends`**: 30-day price statistics (avg, min, max, data points) per product/category

### Triggers

- `update_users_updated_at`: Auto-sets `updated_at = NOW()` on user UPDATE
- `update_stores_updated_at`: Auto-sets `updated_at = NOW()` on store UPDATE
- `update_deals_updated_at`: Auto-sets `updated_at = NOW()` on deal UPDATE

### Functions

- `create_price_snapshot_partition()`: Creates next month's partition for `price_snapshots`
- `cleanup_old_price_partitions()`: Drops partitions older than 90 days
- `get_active_deals(p_postal_code)`: Returns current deals for a postal code

---

## 20. Tests

### File: `app/tests/test_graph.py` (66 lines)

Only one test is fully implemented:

**`test_basic_workflow()`**: Creates a `RecipeGenerationState` with mock parameters (user_id=999, postal_code="M5V3A8", budget=50, household_size=2, dietary_restrictions=["vegetarian"], num_meals=3). Invokes the full graph. Asserts:
- Status is "completed" or "failed"
- Agent call log has entries
- Iteration count ≤ max_iterations
- If completed: approved recipes exist and total_cost ≤ budget

**`test_rejection_retry()`**: Stub — marked as requiring LLM response mocking.

**`test_parallel_execution()`**: Stub — intended to verify parallel timing.

**To run**: `pytest app/tests/` or `pytest app/tests/test_graph.py`

---

## 21. Scripts

### `scripts/run_db_setup.py`

Entry point for database initialization.

```bash
python scripts/run_db_setup.py          # Schema only
python scripts/run_db_setup.py --seed   # Schema + sample data
```

**Note**: Uses `psycopg` (v3), not `psycopg2`. This is a different library from what the app uses.

### `scripts/test-db-connection.py`

Connectivity test for PostgreSQL and Redis:

```bash
python scripts/test-db-connection.py
```

Tests:
1. PostgreSQL connection + TimescaleDB extension check + table listing
2. Redis ping + test set/get with 60s TTL

### `scripts/init_db.sql`

Full schema. Described in [Section 19](#19-database-schema-sql).

### `scripts/seed_sample_data.sql`

Inserts sample data:
- 5 users (Toronto, Montreal, Vancouver with various dietary restrictions)
- 10+ stores (Loblaws, Metro, No Frills, Sobeys, IGA, Safeway, T&T, etc.)
- ~50 deals (valid for 7 days from CURRENT_DATE)
- 60+ price snapshots (30-day history for chicken breast and bananas)
- 3 sample recipes with JSONB ingredients and nutritional info
- 1 sample shopping list
- 50 API usage records

---

## 22. Docker & Deployment

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc
RUN pip install --no-cache-dir uv
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Uses `uv` for faster package installation. No `--reload` flag (production-ready default).

### docker-compose.yml

Single service (`backend`):
- Builds from local Dockerfile
- Maps port 8000
- Loads `.env` file
- Overrides Redis URL to `redis://192.168.3.104:6379` (hardcoded local network IP)
- Health check: `GET /health` every 30s, 15s startup grace, 10s timeout, 3 retries
- Restart policy: `unless-stopped`
- **No PostgreSQL or Redis services defined** — assumes external

### .dockerignore

Excludes: `.git`, `.idea`, `__pycache__`, `*.pyc`, `*.pyo`, `venv`, `.env`, `.dockerignore`

---

## 23. Dependencies

### `requirements.txt` — 28 packages

| Category | Package | Version |
|----------|---------|---------|
| **Web** | fastapi | 0.109.0 |
| | uvicorn | 0.27.0 |
| | pydantic[email] | ≥2.7.4 |
| | pydantic-settings | 2.1.0 |
| | python-multipart | 0.0.6 |
| **Uvicorn Extras** | websockets | ≥12.0 |
| | httptools | ≥0.6.0 |
| | uvloop | ≥0.17.0 |
| | watchfiles | ≥0.21.0 |
| **Database** | psycopg2-binary | ≥2.9.0 |
| | python-dotenv | 1.0.0 |
| **Cache** | redis | 5.0.1 |
| **HTTP** | httpx | ≥0.27.0 |
| | requests | 2.31.0 |
| **Config** | pyyaml | ≥6.0.2 |
| **AI/LangGraph** | langgraph | ≥0.2.0 |
| | langchain-core | ≥0.3.0 |
| | langchain-ollama | ≥0.1.0 |
| **Tracking** | mlflow | ≥2.9.0 |
| **Task Queue** | celery | 5.3.4 |
| **Utilities** | python-dateutil | 2.8.2 |
| | pytz | 2023.3 |
| **Dev/Testing** | pytest | 7.4.4 |
| | pytest-asyncio | 0.21.1 |
| | black | 23.12.1 |
| | flake8 | 7.0.0 |

**Notable**: Celery is declared but not yet integrated into the app code.

---

## 24. What Is Done vs. Not Done

### Fully Implemented

| Feature | Files |
|---------|-------|
| FastAPI app with lifespan, middleware, error handling | `app/main.py` |
| Configuration via environment variables | `app/config.py` |
| PostgreSQL connection pooling | `app/db/database.py` |
| User CRUD (create, read, update, soft delete) | `app/routes/users.py`, `app/services/user_service.py` |
| Store discovery by postal code | `app/routes/stores.py`, `app/services/store_service.py` |
| Deal querying (by postal code, category, search, top deals) | `app/routes/stores.py`, `app/services/store_service.py` |
| Redis caching with TTL and `@cached` decorator | `app/services/cache_service.py` |
| Full LangGraph 10-node workflow | `app/agents/graph.py` |
| ChefOrchestrator (deal fetching, ingredient grouping) | `app/agents/chef_orchestrator.py` |
| SousChef (recipe generation, retry with feedback) | `app/agents/sous_chef.py` |
| Nutritionist (recipe validation, approve/reject) | `app/agents/nutritionist.py` |
| MLflow experiment tracking | `app/services/mlflow_logger.py` |
| Recipe persistence to database | `app/services/database.py` |
| Recipe generation API endpoint | `app/routes/recipes.py` |
| Database schema (8 tables, views, triggers) | `scripts/init_db.sql` |
| Sample data seeding | `scripts/seed_sample_data.sql` |
| Health check endpoint | `app/main.py` |
| Docker containerization | `Dockerfile`, `docker-compose.yml` |
| Basic workflow test | `app/tests/test_graph.py` |

### Not Implemented (Stubs/Placeholders)

| Feature | Location | Status |
|---------|----------|--------|
| `GET /api/v1/recipes/{recipe_id}` | `app/routes/recipes.py` | Returns 501 |
| `GET /api/v1/recipes/user/{user_id}` | `app/routes/recipes.py` | Returns 501 |
| `GET /api/v1/shopping-list/{user_id}` | `app/routes/shopping_lists.py` | Returns 501 |
| `POST /api/v1/shopping-list/{user_id}/mark-complete` | `app/routes/shopping_lists.py` | Returns 501 |
| Flipp API integration | `app/library/flipp.py` | Empty file |
| `estimated_savings` calculation | `app/agents/graph.py:60` | Always 0.0 |
| "new_ingredients" retry strategy | `app/agents/chef_orchestrator.py:147-152` | Empty recipes_pending_retry on 2nd iteration |
| `CHEF_NEW_INGREDIENTS_SELECTION` prompt usage | `app/agents/prompts.py` | Defined but never called |
| `target_ingredients_per_group` state field | `app/agents/state.py` | Defined but never used |
| Celery background task integration | `requirements.txt` | Dependency declared only |
| Rejection retry test | `app/tests/test_graph.py` | Stub |
| Parallel execution test | `app/tests/test_graph.py` | Stub |
| Rate limiting enforcement | `app/config.py` | Config exists, no middleware |
| Authentication/authorization | — | Not started |
| PgBouncer connection pooling | `scripts/ToDO.txt` | TODO |
| Row-level security | `scripts/ToDO.txt` | TODO |

### Partially Implemented

| Feature | What Works | What Doesn't |
|---------|------------|--------------|
| Retry loop | First iteration (reassign_chef) works | Second iteration (new_ingredients) creates empty pending dict, effectively skipping retries |
| Agent model config | `app/config.py` has `ollama_*_model` settings | Agents hardcode model names instead of reading settings |
| Deal caching | `StoreService.get_current_deals_by_postal_code()` uses cache | Other StoreService methods don't cache |

---

## 25. Known Issues & Gotchas

### Critical

1. **Missing price_snapshots partitions for 2026**: The schema only creates partitions through 2025-12. Any INSERT into `price_snapshots` with a 2026 timestamp will fail with "no partition of relation found for row". Fix: Add 2026 partitions to `init_db.sql` (see MIGRATION_PLAN.md Phase 1, Step 1.3).

2. **Agents hardcode model names**: Despite `settings.ollama_chef_model` etc. existing in config, the agents hardcode `"smollm:1.7b"` and `"smollm:360m"` directly in their constructors. Changing the config has no effect.

3. **psycopg version mismatch**: `run_db_setup.py` uses `psycopg` (v3) while the entire app uses `psycopg2`. The v3 library is not in `requirements.txt`.

### Important

4. **Incomplete retry strategy**: On the second retry iteration, `handle_rejections()` sets `recipes_pending_retry = {}` (empty dict), so `retry_generation()` does nothing, and the workflow exits via the 60% threshold or failure path.

5. **estimated_savings is always 0.0**: The `aggregate_recipes()` function has a placeholder comment but never calculates actual savings from deal prices.

6. **MLflow error handling**: `MLflowLogger` has no try/except. If the MLflow server is unavailable, agent calls will raise exceptions that propagate up.

7. **Global agent instances**: `chef` and `nutritionist` in `graph.py` are module-level singletons. In a multi-worker deployment, this is fine (each worker gets its own instances), but state is not shared between the agent's `self.db` instance and the workflow's state-passing pattern.

8. **Recipe ID changes on retry**: When a recipe is regenerated, it gets a new UUID. The old recipe_id is removed from `generated_recipes` but may linger in `rejected_recipe_ids`. The Nutritionist skips already-validated recipes by checking `validation_results`, so this works, but the rejected_recipe_ids list accumulates stale IDs.

### Minor

9. **`database_max_overflow` unused**: Defined in config (default 20) but never referenced in `Database` class.

10. **Deal query limit discrepancy**: `DatabaseService.fetch_current_deals()` limits to 200 results. The Chef prompt receives first 100 (`available_deals[:100]`). If there are more than 100 deals, some are silently dropped for planning.

11. **Docker Redis IP hardcoded**: `docker-compose.yml` hardcodes `redis://192.168.3.104:6379`, which is a specific local network address.

12. **No input validation on workflow state**: The `RecipeGenerationState` TypedDict provides type hints but no runtime validation. Bad input (e.g., negative budget, empty postal_code) passes through unchecked until it hits a database query or LLM prompt.

---

## 26. Cross-File Dependency Map

This shows which files import from which other files, critical for understanding impact when modifying any file.

```
app/config.py
  └── Imported by: app/db/database.py, app/services/cache_service.py,
      app/services/store_service.py, app/main.py

app/db/database.py (and app/db/__init__.py)
  └── Imported by: app/services/database.py, app/services/user_service.py,
      app/services/store_service.py, app/main.py

app/models/schemas.py
  └── Imported by: app/routes/users.py, app/routes/stores.py,
      app/routes/recipes.py, app/routes/shopping_lists.py,
      app/services/user_service.py, app/main.py

app/services/cache_service.py
  └── Imported by: app/services/store_service.py, app/main.py

app/services/user_service.py
  └── Imported by: app/routes/users.py, app/routes/recipes.py

app/services/store_service.py
  └── Imported by: app/routes/stores.py

app/services/database.py (DatabaseService)
  └── Imported by: app/agents/chef_orchestrator.py, app/agents/graph.py

app/services/mlflow_logger.py
  └── Imported by: app/agents/chef_orchestrator.py, app/agents/sous_chef.py,
      app/agents/nutritionist.py, app/agents/graph.py

app/agents/state.py
  └── Imported by: app/agents/graph.py, app/agents/chef_orchestrator.py,
      app/agents/sous_chef.py, app/agents/nutritionist.py,
      app/main_recipe_generation.py, app/tests/test_graph.py

app/agents/prompts.py
  └── Imported by: app/agents/chef_orchestrator.py, app/agents/sous_chef.py,
      app/agents/nutritionist.py

app/agents/graph.py
  └── Imported by: app/main_recipe_generation.py, app/tests/test_graph.py

app/main_recipe_generation.py
  └── Imported by: app/routes/recipes.py

app/routes/*.py
  └── Imported by: app/main.py (router registration)
```

### Impact Analysis

If you modify **`app/agents/state.py`** (add/rename/remove fields):
- Must update: `app/main_recipe_generation.py` (initial state construction), every agent file, `app/agents/graph.py`, `app/tests/test_graph.py`

If you modify **`app/config.py`** (add/rename settings):
- Must update: `.env.example`, any file that reads the new setting

If you modify **`app/models/schemas.py`** (change request/response shapes):
- Must update: corresponding route handlers, service methods that return data matching old shapes

If you modify **`app/db/database.py`** (change pool behavior):
- Affects: all services that use `db.get_cursor()` or `db.get_connection()`

If you modify **`scripts/init_db.sql`** (change table columns):
- Must update: all SQL queries in `app/services/database.py`, `app/services/user_service.py`, `app/services/store_service.py`, `scripts/seed_sample_data.sql`

---

## 27. Migration Plan Summary

The `MIGRATION_PLAN.md` describes two sequential phases:

### Phase 1: Migrate to Local PostgreSQL

**What changes:**
- `.env`: Update `DATABASE_URL` to local PostgreSQL (remove Neon.tech-specific SSL params)
- `scripts/init_db.sql`: Add missing 2026 partition definitions for `price_snapshots`
- Install `psycopg[binary]` for the setup script (v3)
- Set up monthly cron to create new partitions

**No code changes required** — the app only uses `DATABASE_URL` from config.

### Phase 2: Replace Ollama with Claude Haiku

**What changes:**

| File | Change |
|------|--------|
| `requirements.txt` | Add `langchain-anthropic>=0.3.0` |
| `app/config.py` | Add `anthropic_api_key`, `anthropic_chef_model`, `anthropic_sous_chef_model`, `anthropic_nutritionist_model` settings |
| `app/agents/chef_orchestrator.py` | Replace `ChatOllama` import with `ChatAnthropic`. Update constructor. Add `extract_json()` helper (strips markdown fences from Claude responses). Replace `json.loads()` with `extract_json()`. |
| `app/agents/sous_chef.py` | Same changes as chef_orchestrator |
| `app/agents/nutritionist.py` | Same changes as chef_orchestrator |
| `app/agents/prompts.py` | (Optional) Append "Return ONLY raw JSON" to templates |
| `.env` / `.env.example` | Add `ANTHROPIC_API_KEY`, `ANTHROPIC_*_MODEL` vars |

**Key technical detail**: Claude sometimes wraps JSON in markdown fences (` ```json ... ``` `), which breaks `json.loads()`. The `extract_json()` helper strips these fences before parsing:

```python
def extract_json(text: str):
    text = text.strip()
    fenced = re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)
```

---

## Appendix A: How to Add a New Route

1. Create or edit a file in `app/routes/`
2. Define an `APIRouter(prefix="/your-prefix", tags=["YourTag"])`
3. Add Pydantic request/response models to `app/models/schemas.py`
4. Add any needed service methods to `app/services/`
5. Register the router in `app/main.py`: `app.include_router(your_router, prefix=settings.api_prefix)`

## Appendix B: How to Add a New Agent Node

1. Create or edit agent class in `app/agents/`
2. Add any new state fields to `RecipeGenerationState` in `app/agents/state.py`
3. Add prompt template to `app/agents/prompts.py` if needed
4. Register the node in `create_recipe_generation_graph()` in `app/agents/graph.py`
5. Add edges to wire it into the workflow
6. Update `initial_state` in `app/main_recipe_generation.py` with new field defaults
7. Update `app/tests/test_graph.py` to include the new fields in test state

## Appendix C: How to Add a New Database Table

1. Add `CREATE TABLE` statement to `scripts/init_db.sql`
2. Add sample data to `scripts/seed_sample_data.sql` if appropriate
3. Add service methods in `app/services/` with SQL queries
4. Add Pydantic models in `app/models/schemas.py` for the API layer
5. Run `python scripts/run_db_setup.py` to apply (NOTE: this drops and recreates everything)

## Appendix D: Environment Setup Quick Reference

```bash
# 1. Virtual environment
python -m venv venv && source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env: set DATABASE_URL, optionally REDIS_URL + REDIS_ENABLED=True

# 4. Database
python scripts/run_db_setup.py --seed

# 5. Ollama (separate terminal)
ollama serve
ollama pull smollm:1.7b && ollama pull smollm:360m

# 6. Run
uvicorn app.main:app --reload --port 8000

# 7. Test
pytest app/tests/

# 8. Lint
black app/ && flake8 app/
```
