# CLAUDE.md

Project-level instructions for agents working in this repo.

## Environment

The checkout is at `/home/divad/projects/grocery-optimizer` in WSL (Ubuntu), and
Claude Code runs as the native Linux build there. Run git, python, pytest and
npm directly — no wrapper.

- **Python** is the project venv: `.venv/bin/python` (3.11). Do not create a new
  one. There is no `pyproject.toml`/`setup.py`; nothing is installed as an
  editable package, so imports resolve from the working directory.
- **Node** needs nvm sourced explicitly: `. ~/.nvm/nvm.sh` before `npm`/`npx`.

## The regression oracle

```
.venv/bin/python -m pytest -q -m "not llm"
```

This must stay green. The deselected `-m llm` tests need a live Ollama and a
populated database; do not run them casually and do not treat them as part of
the acceptance bar.

Deterministic tests only: no live network calls, no live LLM. Use recorded
fixtures under `app/tests/fixtures/`.

## Parallel agents: use worktrees

When fanning work out to multiple agents, give each one its own git worktree
(`isolation: "worktree"` on the Agent tool). Hand-partitioning file ownership
across agents that share one checkout is brittle — they contend over a single
`HEAD` and index, and one `git add -A` sweeps another agent's half-finished work
into the wrong commit.

A fresh worktree contains **tracked files only**, and three load-bearing things
here are gitignored, so bootstrap before doing anything else:

```
ln -s ~/projects/grocery-optimizer/.env .env
ln -s ~/projects/grocery-optimizer/frontend/node_modules frontend/node_modules
~/projects/grocery-optimizer/.venv/bin/python -m pytest -q -m "not llm"
```

Reusing the main `.venv` by absolute path is safe precisely because nothing is
installed as a package: the worktree's code wins on `sys.path` and the main venv
supplies site-packages. Untracked files do not propagate into a worktree.

### Worktrees isolate code, not state

Every worktree still talks to the **same Postgres** and the **same local
Ollama**. Agents that write to the database collide no matter how isolated their
checkouts are, and LLM-heavy agents contend over the single Ollama instance.
Serialize database-writing agents, or give them separate databases.

### If agents must share one checkout

Never `git add -A`, `git add .`, or `git commit -a` — stage only your own files
explicitly by path. Never `git checkout`, `git switch`, `git stash`, or
`git reset`: those move the shared `HEAD` out from under the other agent.

## Known environment gotchas

- `starlette` 0.35.1's `TestClient` is incompatible with the installed `httpx`
  0.28.1 (`Client.__init__() got an unexpected keyword argument 'app'`). Drive
  the app through `httpx.ASGITransport` rather than changing the pins.
- `scripts/README.md` and `SETUP.md` describe `price_snapshots` as a TimescaleDB
  hypertable with a retention policy and a `price_snapshots_daily` aggregate.
  That is false — it is a native PostgreSQL partitioned table. Trust
  `scripts/init_db.sql` over those docs.
- **The app's database is Neon**, via `DATABASE_URL` in `.env` — not the
  `sql4e` study container that `migrations/README.md` describes `make migrate`
  applying to. Migrations that the running app needs must reach Neon, which is
  a deliberate, owner-approved step; record each in `schema_migrations`.
- Neon closes idle connections. A long-running `uvicorn` will throw
  `SSL connection has been closed unexpectedly` on its first query after a few
  idle hours; restart the server rather than retrying writes into a dead pool.
- `cleanup_old_price_partitions()` in `scripts/init_db.sql` drops partitions
  older than 90 days. Nothing schedules it, and nothing should: the project
  accumulates long-run price history deliberately.

## Product framing

The hero metric is **meeting dietary goals within the spend limit the user
sets** — not savings against a regular price. Deal data mostly lacks a reliable
regular price, so savings is a secondary metric and must degrade to absent
rather than to a fabricated number.

Dietary compliance is a deterministic code path, never learned or LLM-judged
behavior. That conclusion cost an entire SFT track — do not reintroduce an LLM
as the compliance judge.

It is decided **only from curated product attributes** (`contains_meat/fish/
dairy/egg/nuts/animal_byproduct` on `products`; the restriction → attribute map
is `RESTRICTION_ATTRIBUTES` in `app/services/products.py`). Two readers, one
authority:

- Serving: `app/services/deal_gate.py` — a restricted user gets a deal only if
  every product linked to it (`deal_products`) is confirmed and clears.
- Recipes: `app/agents/dietary.py` — every SousChef ingredient cites a `deal_id`
  or a staple `product_id`; the verdict is a lookup on the cited products.

**No name, keyword or substring matching anywhere downstream of product review**
— not as a filter, a veto, a review alarm or a backfill heuristic. An ingredient
or deal *name* never decides anything. NULL attributes fail closed. A diet the
attributes can't express (halal, kosher, gluten-free) needs a new attribute, not
a word list.

## Conventions

- `ROADMAP.md` is the canonical plan and its shipped-log entries are worth
  reading, but it lags the code. Trust the code, and say so when they disagree.
- Do not update `ROADMAP.md` from a subagent; the coordinator maintains it.
