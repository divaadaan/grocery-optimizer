# Pickup Point — WSL Migration (2026-06-12)

Status snapshot for resuming work after cloning into the WSL Ubuntu filesystem.
Previous environment was Windows-native (git bash); Python there resolved to the
Microsoft Store stub and Docker was not installed, so **none of the changes below
have been runtime-verified yet**. Verification is step 1.

## What was just done (committed but unverified)

1. **psycopg2 → psycopg3 migration completed.** The Nov 2025 commit
   ("mess with version compatability") had switched `requirements.txt` to psycopg3
   but left the code on psycopg2, so the app couldn't start on a fresh install.
   - `app/db/database.py`: rewritten on `psycopg_pool.ConnectionPool`, rows are
     dicts via `row_factory=dict_row` (replaces `RealDictCursor`). Public API
     unchanged (`db.get_cursor()` / `db.get_connection()`).
   - `app/services/database.py`: dead psycopg2 import removed; fixed latent bug
     `fetchone()[0]` → `fetchone()["recipe_id"]` (was broken under RealDictCursor too).
   - `scripts/test-db-connection.py`: migrated to psycopg3.
   - `requirements.txt`: `psycopg[binary,pool]>=3.2.0`; removed unused `celery`.

2. **Agents now read model config from `app/config.py` settings** instead of
   hardcoding `smollm:*` and the default localhost Ollama URL. `ChatOllama` gets
   `base_url=settings.ollama_base_url` — required for Docker networking. Models
   are switchable via `.env` (`OLLAMA_CHEF_MODEL`, `OLLAMA_SOUS_CHEF_MODEL`,
   `OLLAMA_NUTRITIONIST_MODEL`).

3. **Dockerized.** `Dockerfile` (python:3.11-slim, uvicorn) + `docker-compose.yml`
   with services:
   - `api` — the FastAPI app; `OLLAMA_BASE_URL`/`MLFLOW_TRACKING_URI` overridden
     to in-network service names; everything else from `.env`
   - `ollama` — model server with a named volume for weights
   - `ollama-init` — one-shot, pulls the three configured models on `up`
   - `mlflow` — tracking server (SQLite backend, named volume)
   - Postgres and Redis intentionally **not** containerized — stay on Neon/Upstash via `.env`

## WSL environment setup

```bash
# 1. Base tooling
sudo apt update
sudo apt install -y python3-venv python3-pip git

# 2. Docker Engine (official repo — no Docker Desktop needed)
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER   # then close and reopen the WSL terminal

# 3. Clone into the WSL filesystem (NOT /mnt/c — much faster I/O)
git clone https://github.com/divaadaan/grocery-optimizer.git ~/projects/grocery-optimizer
cd ~/projects/grocery-optimizer

# 4. Secrets — .env is gitignored, copy it from the Windows checkout
cp /mnt/c/Users/thesa/projects/python/grocery-optimizer/.env .
```

## Step 1: verify the migration (do this first)

```bash
# Fresh venv install — proves requirements resolve
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Import smoke test (dummy DB URL is fine; nothing connects at import time)
DATABASE_URL=postgresql://x:y@localhost/db python -c "import app.main; print('app.main OK')"
DATABASE_URL=postgresql://x:y@localhost/db python -c "import app.agents.graph; print('graph OK')"

# Against the real Neon DB (uses .env)
python scripts/test-db-connection.py

# Full stack
docker compose up --build
# wait for ollama-init to finish pulling models, then:
curl http://localhost:8000/health
# expect: database healthy, redis healthy (if REDIS_ENABLED), ollama healthy
```

## Task roadmap (from the 2026-06-12 code review)

1. ~~Finish psycopg3 migration + dockerize~~ → **verify in WSL (above)**
2. **Fix the LangGraph workflow** — known-broken:
   - `generate_recipes_parallel` returns `Send` objects but is wired as a regular
     node (`app/agents/graph.py`). `Send` only works from a **conditional edge**.
   - `RecipeGenerationState` has no reducers (`Annotated[..., merge]`), so 3
     parallel SousChefs writing `generated_recipes` will raise `InvalidUpdateError`.
   - Nodes mutate state in place instead of returning partial updates.
   - Make MLflow optional/lazy: `app/main_recipe_generation.py` calls
     `mlflow.set_experiment()` at module import (inside the first request) — first
     recipe request fails if MLflow is down.
3. **Harden LLM output + model experimentation**
   - Pydantic-validate agent JSON output; retry on parse failure; compute recipe
     cost in Python from `deal_index` instead of trusting model arithmetic.
   - Context from the motivating paper (https://arxiv.org/html/2502.02028v1,
     "Fine-tuning Language Models for Recipe Generation"): SmolLM-360M ≈
     SmolLM-1.7B **after fine-tuning on Food.com**, and bigger ≠ better (Phi-2
     degraded). Caveat: the paper used *fine-tuned* models generating free-text
     recipes; this project asks *raw* SmolLM for structured JSON + nutrition
     validation — a much harder ask. Suggested experiments (just `.env` changes now):
     - `qwen2.5:3b` / `llama3.2:3b` as chef — strong JSON compliance
     - keep `smollm:360m` sous chefs, compare against `qwen2.5:1.5b`
     - longer term: fine-tune SmolLM on Food.com per the paper to earn the
       small-model cost profile back
4. **Fill in stub endpoints** — recipe retrieval (`GET /recipes/{id}`), user
   recipe history, shopping lists (all currently 501); Shopping Optimizer agent
   doesn't exist; `estimated_savings` hardcoded to 0.0.
5. **Real deal ingestion** — replace seed data (Flipp API config exists in
   settings but no integration).

## Editor notes

- VSCode: open with `code .` from the WSL repo dir (Remote-WSL, already set up).
- Run Claude Code from a WSL terminal in the repo so its tools execute in Linux.
- Delete this file once the WSL environment is verified and work resumes.
