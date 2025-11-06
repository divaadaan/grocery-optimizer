# Grocery Optimizer - Quick Start Guide

Get your FastAPI application running in 5 minutes.

## Prerequisites

- Python 3.11+
- PostgreSQL database (Neon.tech or local)
- `.env` file configured with `DATABASE_URL`

## Installation

### 1. Install Dependencies

```bash
# Create and activate virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate

# Install packages
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit .env and set AT MINIMUM:
# DATABASE_URL=your_neon_connection_string
```

**Minimum .env for testing:**
```env
DATABASE_URL=postgresql://user:password@host/database
ENVIRONMENT=development
DEBUG=True
SECRET_KEY=dev-secret-key
REDIS_ENABLED=False
```

### 3. Initialize Database

```bash
# Run database setup with sample data
python scripts/run_db_setup.py --seed

# Verify connection
python scripts/test-db-connection.py
```

## Running the API

### Start the Development Server

```bash
# Option 1: Using uvicorn directly
uvicorn app.main:app --reload --port 8000

# Option 2: Using Python
python -m app.main

# Option 3: Using the module
python app/main.py
```

The API will be available at:
- **API**: http://localhost:8000
- **Interactive Docs (Swagger)**: http://localhost:8000/docs
- **Alternative Docs (ReDoc)**: http://localhost:8000/redoc

## Testing the API

### 1. Health Check

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "database": "healthy",
  "redis": null,
  "ollama": "unavailable",
  "timestamp": "2025-01-06T..."
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

### 3. Discover Postal Code

```bash
curl -X POST http://localhost:8000/api/v1/postal-code/discover \
  -H "Content-Type: application/json" \
  -d '{"postal_code": "M5V3A8"}'
```

### 4. Get Deals

```bash
curl http://localhost:8000/api/v1/postal-code/deals/M5V3A8?limit=10
```

### 5. Search Deals

```bash
curl "http://localhost:8000/api/v1/postal-code/search/M5V3A8?q=chicken"
```

## Using the Interactive Docs

Visit http://localhost:8000/docs for a full interactive API explorer:

1. Click on any endpoint to expand it
2. Click "Try it out"
3. Fill in parameters
4. Click "Execute"
5. See the response below

## Project Structure

```
grocery-optimizer/
├── app/
│   ├── main.py              # FastAPI application entry point
│   ├── config.py            # Configuration from environment
│   ├── db/
│   │   └── database.py      # Database connection pool
│   ├── models/
│   │   └── schemas.py       # Pydantic models
│   ├── routes/
│   │   ├── users.py         # User endpoints
│   │   ├── stores.py        # Store & deals endpoints
│   │   ├── recipes.py       # Recipe generation (stub)
│   │   └── shopping_lists.py # Shopping list (stub)
│   ├── services/
│   │   ├── user_service.py  # User database operations
│   │   └── store_service.py # Store/deal operations
│   └── agents/              # LangGraph agents (to be implemented)
├── scripts/
│   ├── init_db.sql          # Database schema
│   ├── seed_sample_data.sql # Sample data
│   └── run_db_setup.py      # Database setup script
├── requirements.txt         # Python dependencies
└── .env                     # Environment variables (create from .env.example)
```

## Available Endpoints

### Users
- `POST /api/v1/users/register` - Register new user
- `GET /api/v1/users/{user_id}` - Get user details
- `PUT /api/v1/users/{user_id}` - Update user preferences
- `DELETE /api/v1/users/{user_id}` - Deactivate user

### Stores & Deals
- `POST /api/v1/postal-code/discover` - Discover stores and deals
- `GET /api/v1/postal-code/deals/{postal_code}` - Get current deals
- `GET /api/v1/postal-code/top-deals/{postal_code}` - Get top deals by discount
- `GET /api/v1/postal-code/search/{postal_code}` - Search deals

### Recipes (Stubs)
- `POST /api/v1/recipes/generate` - Generate meal plan (not implemented)
- `GET /api/v1/recipes/{recipe_id}` - Get recipe details (not implemented)
- `GET /api/v1/recipes/user/{user_id}` - Get user's recipes (not implemented)

### Shopping Lists (Stubs)
- `GET /api/v1/shopping-list/{user_id}` - Get shopping list (not implemented)
- `POST /api/v1/shopping-list/{user_id}/mark-complete` - Mark as complete (not implemented)

### System
- `GET /` - API root information
- `GET /health` - Health check
- `GET /api/v1/info` - API configuration and features

## Common Issues

### "Connection refused" Error

**Problem:** Can't connect to database
**Solution:**
1. Check DATABASE_URL in .env
2. Ensure database is running
3. Verify network connectivity

### "Module not found" Error

**Problem:** Missing dependencies
**Solution:**
```bash
# Ensure virtual environment is activated
pip install -r requirements.txt
```

### "Table does not exist" Error

**Problem:** Database not initialized
**Solution:**
```bash
python scripts/run_db_setup.py --seed
```

### Port Already in Use

**Problem:** Port 8000 is already taken
**Solution:**
```bash
# Use different port
uvicorn app.main:app --reload --port 8001
```

## Development Tips

### Auto-reload on Changes

```bash
# Uvicorn watches for file changes
uvicorn app.main:app --reload
```

### View Logs

Logs are printed to console with timestamps:
```
2025-01-06 10:30:00 - app.db.database - INFO - Database connection pool initialized
2025-01-06 10:30:05 - app.routes.users - INFO - Created user: test@example.com
```

### Format Code

```bash
# Format with black
black app/

# Lint with flake8
flake8 app/
```

### Run Tests

```bash
# Install pytest
pip install pytest pytest-asyncio

# Run tests (when implemented)
pytest app/tests/
```

## Next Steps

Now that your API is running:

1. **Test All Endpoints** - Use `/docs` to try all endpoints
2. **Implement LangGraph Agents** - Follow `agents.md` to build the recipe generation system
3. **Add Flipp API Integration** - Connect to real grocery deals
4. **Set Up MLflow** - Track agent performance

### Implementing Recipe Generation

The recipe generation endpoints are stubs. To implement:

1. Follow the detailed guide in `agents.md`
2. Create agent files in `app/agents/`:
   - `state.py` - State definitions
   - `chef_orchestrator.py` - Chef agent
   - `sous_chef.py` - SousChef workers
   - `nutritionist.py` - Nutritionist validator
   - `graph.py` - LangGraph workflow
   - `prompts.py` - Agent prompts

3. Install Ollama and models:
   ```bash
   ollama pull smollm:1.7b
   ollama pull smollm:360m
   ```

4. Update `app/routes/recipes.py` to use the LangGraph workflow

---

**Questions?** Check the full documentation:
- `README.md` - Project overview
- `SETUP.md` - Complete setup guide
- `agents.md` - LangGraph implementation guide
- `scripts/README.md` - Database setup documentation
