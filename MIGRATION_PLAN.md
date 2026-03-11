# Migration Plan

Two sequential phases: swap the cloud database for a local PostgreSQL instance, then swap Ollama/SmolLM for Claude Haiku.

---

## Phase 1: Local PostgreSQL

### Background

The app connects to PostgreSQL solely through `DATABASE_URL` in `.env`. The connection pool in `app/db/database.py` passes `dsn=settings.database_url` directly to `psycopg2.SimpleConnectionPool` — no Neon-specific drivers or SSL parameters exist in application code. This is almost entirely an operational change, with one schema fix required.

---

### Step 1.1 — Provision the database

On your Pi (or whatever Postgres host you're using), create the database and role:

```sql
-- run as postgres superuser
CREATE ROLE groceryapp WITH LOGIN PASSWORD 'choose_a_password';
CREATE DATABASE groceryoptimizer OWNER groceryapp;
\c groceryoptimizer
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
```

Verify both extensions are available:

```sql
SELECT name, installed_version FROM pg_available_extensions
WHERE name IN ('uuid-ossp', 'pg_trgm');
```

Both must show a non-null `installed_version`. If `pg_trgm` is missing, install `postgresql-contrib`.

Minimum PostgreSQL version: **14** (required for `PARTITION BY RANGE` syntax used in the schema).

---

### Step 1.2 — Update `.env`

Change `DATABASE_URL` to point at your local instance. Remove any `?sslmode=require` or Neon query-string parameters:

```
DATABASE_URL=postgresql://groceryapp:choose_a_password@<host-ip>:5432/groceryoptimizer
```

Also update the test database URL:

```
TEST_DATABASE_URL=postgresql://groceryapp:choose_a_password@<host-ip>:5432/groceryoptimizer_test
```

---

### Step 1.3 — Fix missing partitions in `scripts/init_db.sql`

**Problem:** The schema creates partitions for `price_snapshots` covering only `2025-09` through `2025-12`. Any insert or query with a `time` value in 2026 will fail with `no partition of relation found for row`.

**Fix:** After line 72 (the `price_snapshots_2025_12` partition), add three new partition declarations before the first `CREATE INDEX` block:

```sql
CREATE TABLE price_snapshots_2026_01 PARTITION OF price_snapshots
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');

CREATE TABLE price_snapshots_2026_02 PARTITION OF price_snapshots
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');

CREATE TABLE price_snapshots_2026_03 PARTITION OF price_snapshots
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
```

**Going forward:** The schema already includes a `create_price_snapshot_partition()` function that creates next month's partition. Schedule it monthly on your Pi:

```
# /etc/cron.d/grocery-partitions (runs 25th of each month)
0 3 25 * * postgres psql -d groceryoptimizer -c "SELECT create_price_snapshot_partition();"
```

---

### Step 1.4 — Run setup and verify

```bash
pip install psycopg2-binary "psycopg[binary]"
python scripts/run_db_setup.py --seed
```

Verify partitions exist:

```sql
SELECT tablename FROM pg_tables
WHERE tablename LIKE 'price_snapshots_%'
ORDER BY tablename;
```

Expected: `price_snapshots_2025_09` through `price_snapshots_2026_03`.

---

### Step 1.5 — Boot check

```bash
uvicorn app.main:app --reload
```

Watch for `Database connection pool initialized` in the logs, then hit `GET /health`. If database shows healthy, Phase 1 is done.

---

## Phase 2: Swap Ollama for Claude Haiku

### Background

All three agents (`chef_orchestrator.py`, `sous_chef.py`, `nutritionist.py`) hardcode `ChatOllama` with the model name and an Ollama-specific `format="json"` parameter. LangChain's `ChatAnthropic` implements the same `BaseChatModel` interface, so `.invoke()` and `response.content` stay identical. Two things need careful handling:

- **`format="json"` must be removed** — it's Ollama-only and will error on `ChatAnthropic`.
- **JSON fence stripping** — Claude sometimes wraps JSON output in markdown fences (` ```json ``` `). The bare `json.loads(response.content)` calls in each agent will break if that happens. A sanitizer function handles this.

---

### Step 2.1 — `requirements.txt`

Add `langchain-anthropic` after the existing LangChain lines:

```
langchain-anthropic>=0.3.0
```

Keep `langchain-ollama` for now. Then:

```bash
pip install "langchain-anthropic>=0.3.0"
```

---

### Step 2.2 — `app/config.py`

After the Ollama block (line 43), add a new Anthropic block:

```python
    # Anthropic
    anthropic_api_key: Optional[str] = Field(default=None, env="ANTHROPIC_API_KEY")
    anthropic_chef_model: str = Field(default="claude-haiku-4-5", env="ANTHROPIC_CHEF_MODEL")
    anthropic_sous_chef_model: str = Field(default="claude-haiku-4-5", env="ANTHROPIC_SOUS_CHEF_MODEL")
    anthropic_nutritionist_model: str = Field(default="claude-haiku-4-5", env="ANTHROPIC_NUTRITIONIST_MODEL")
```

`Optional` is already imported. No other changes to this file.

---

### Step 2.3 — Add JSON sanitizer to each agent file

Claude doesn't guarantee raw JSON output — it may wrap it in markdown fences. Add this helper near the top of each of the three agent files (after the imports):

```python
import re

def extract_json(text: str):
    """Strip optional markdown fences and parse JSON."""
    text = text.strip()
    fenced = re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)
```

---

### Step 2.4 — `app/agents/chef_orchestrator.py`

1. **Line 1** — swap import:
   ```python
   # remove:
   from langchain_ollama import ChatOllama
   # add:
   from langchain_anthropic import ChatAnthropic
   ```

2. Add `from ..config import settings` to imports (not currently present).

3. Add `import re` and the `extract_json` helper after imports.

4. **Lines 15–19** — replace constructor:
   ```python
   # remove:
   self.llm = ChatOllama(model="smollm:1.7b", temperature=0.7, format="json")
   # replace with:
   self.llm = ChatAnthropic(
       model=settings.anthropic_chef_model,
       temperature=0.7,
       api_key=settings.anthropic_api_key
   )
   ```

5. **Line 82** — replace `json.loads`:
   ```python
   # remove:
   result = json.loads(response.content)
   # replace with:
   result = extract_json(response.content)
   ```

6. **Line 103** — update MLflow model name string:
   ```python
   # remove:
   model="smollm:1.7b",
   # replace with:
   model=settings.anthropic_chef_model,
   ```

---

### Step 2.5 — `app/agents/sous_chef.py`

1. **Line 1** — swap import (same as above, `ChatOllama` → `ChatAnthropic`).

2. Add `from ..config import settings`, `import re`, and `extract_json` helper.

3. **Lines 14–19** — replace constructor:
   ```python
   # remove:
   self.llm = ChatOllama(model="smollm:360m", temperature=0.8, format="json")
   # replace with:
   self.llm = ChatAnthropic(
       model=settings.anthropic_sous_chef_model,
       temperature=0.8,
       api_key=settings.anthropic_api_key
   )
   ```

4. **Line 48** — replace `json.loads` in `generate_recipes`:
   ```python
   recipes_data = extract_json(response.content)
   ```

5. **Line 73** — update MLflow model name:
   ```python
   model=settings.anthropic_sous_chef_model,
   ```

6. **Line 107** — replace second `json.loads` in `regenerate_with_feedback`:
   ```python
   recipe_data = extract_json(response.content)
   ```

---

### Step 2.6 — `app/agents/nutritionist.py`

1. **Line 1** — swap import.

2. Add `from ..config import settings`, `import re`, and `extract_json` helper.

3. **Lines 13–18** — replace constructor:
   ```python
   # remove:
   self.llm = ChatOllama(model="smollm:360m", temperature=0.3, format="json")
   # replace with:
   self.llm = ChatAnthropic(
       model=settings.anthropic_nutritionist_model,
       temperature=0.3,
       api_key=settings.anthropic_api_key
   )
   ```

4. **Line 46** — replace `json.loads`:
   ```python
   validation_data = extract_json(response.content)
   ```

---

### Step 2.7 — `app/agents/prompts.py` (optional but recommended)

The existing prompts already include JSON output format instructions. To reduce how often Claude wraps output in fences, append this line to the output format section of each of the four prompt templates (`CHEF_INGREDIENT_PLANNING`, `SOUS_CHEF_RECIPE_GENERATION`, `SOUS_CHEF_RETRY_WITH_FEEDBACK`, `NUTRITIONIST_VALIDATION`):

```
Return ONLY the raw JSON object with no markdown fences or additional text.
```

This is defense-in-depth — `extract_json` handles fences even without this, but the instruction reduces noise.

---

### Step 2.8 — `.env` and `.env.example`

Add to both files:

```
# ============================================================================
# ANTHROPIC CONFIGURATION
# ============================================================================
ANTHROPIC_API_KEY=sk-ant-your-key-here
ANTHROPIC_CHEF_MODEL=claude-haiku-4-5
ANTHROPIC_SOUS_CHEF_MODEL=claude-haiku-4-5
ANTHROPIC_NUTRITIONIST_MODEL=claude-haiku-4-5
```

The `OLLAMA_*` variables can be left in place or commented out — they're read by config but unused once the agents are updated.

---

### Step 2.9 — Smoke test

Trigger a recipe generation request. Watch for:

- No `JSONDecodeError` → sanitizer is working
- No `AuthenticationError` → API key loaded correctly
- MLflow logs showing `model=claude-haiku-4-5` → config-driven model names are working

If a `JSONDecodeError` does appear, add this debug line immediately before the failing `extract_json` call to see what Claude actually returned:

```python
print(f"[DEBUG] Raw LLM response: {repr(response.content[:500])}")
```

---

## Summary of all file changes

| File | Phase | Change |
|---|---|---|
| `scripts/init_db.sql` | 1 | Add 3 missing `price_snapshots` partitions after line 72 |
| `.env` | 1 + 2 | Update `DATABASE_URL`; add `ANTHROPIC_*` vars |
| `.env.example` | 1 + 2 | Same template updates |
| `requirements.txt` | 2 | Add `langchain-anthropic>=0.3.0` |
| `app/config.py` | 2 | Add `anthropic_*` settings fields after line 43 |
| `app/agents/chef_orchestrator.py` | 2 | Swap import + constructor; add `extract_json`; fix line 82 + 103 |
| `app/agents/sous_chef.py` | 2 | Swap import + constructor; add `extract_json`; fix lines 48, 73, 107 |
| `app/agents/nutritionist.py` | 2 | Swap import + constructor; add `extract_json`; fix line 46 |
| `app/agents/prompts.py` | 2 | Append raw-JSON instruction to 4 prompt templates (optional) |
