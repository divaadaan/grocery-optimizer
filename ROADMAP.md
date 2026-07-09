# Development Roadmap

Canonical plan as of 2026-07-09. Supersedes `NEXT_STEPS.md` (see git history
for the WSL-migration log and verification notes it contained).

## Done

- psycopg3 migration, dockerization, WSL environment — verified end-to-end.
- LangGraph workflow fixed and verified: conditional-edge fan-out via `Send`,
  reducers on parallel channels, 3 parallel SousChefs, Nutritionist validation
  with both retry strategies, both terminal paths.
- LLM output hardening: Pydantic schemas for all agent JSON
  (`app/agents/llm_output.py`) with validation-retry; recipe costs computed in
  Python from `deal_index` (`app/agents/costing.py`); 26 deterministic unit
  tests + Nutritionist regression corpus (`pytest -m llm`).
- **React frontend** (`frontend/`): Vite + React 19 + TypeScript,
  react-query + react-router. Covers profile setup, deal discovery/browse/
  search, meal-plan generation (with long-running-job UX), and a shopping-list
  page wired against the 501 stub so it lights up when the backend lands.
  See `frontend/README.md` for architecture and run instructions.
- **Pre-launch review fixes (2026-07-09).** A fresh-install walkthrough
  (clean Postgres, seeded from `scripts/init_db.sql`, real browser session)
  found and fixed four blockers: `users.is_active`/`last_login` missing from
  the schema (registration and every user endpoint 500'd), `search_deals`/
  `get_top_deals` omitting columns the `DealInfo` response model requires
  (both endpoints 500'd), cache SETs failing on Decimal/date serialization
  (deals cache silently did nothing), and `POST /recipes/generate` stalling
  ~4 minutes when dependencies were down — MLflow's default retry backoff,
  not Ollama, was the culprit. Generation now preflights Ollama (503 in
  ~60ms with an actionable message), caps MLflow retries, and puts a
  configurable timeout (`OLLAMA_REQUEST_TIMEOUT`) on all agent LLM calls.

## Next

1. **Recipe persistence.** Save approved recipes from
   `run_recipe_generation` (route currently returns `recipe_id=0`); implement
   `GET /recipes/{id}` and `GET /recipes/user/{id}` (501 stubs). Unblocks a
   recipe-history view in the UI.

2. **Async generation API.** `POST /recipes/generate` is synchronous and takes
   minutes with local models — it will hit browser/proxy timeouts. Move to a
   job model: POST returns `job_id`; progress via polling or SSE streamed from
   LangGraph node events ("SousChef 2/3 cooking…"). Frontend impact is
   contained: swap `useGenerateRecipes` in `frontend/src/hooks/queries.ts` and
   feed real stages to `GenerationProgress`.

3. **Shopping Optimizer agent + endpoints.** Consolidate approved-recipe
   ingredients, assign items to stores by best price, persist to
   `shopping_lists`. The frontend success path is already built
   (`features/shopping/ShoppingListPage.tsx`).

4. **Real deal ingestion (Flipp).** Replace seed data; `flipp_api_key`/
   `flipp_api_url` already exist in settings. Add a scheduled refresh and wire
   `POST /postal-code/discover` to it (currently it only reads what's in the DB).

5. **Model experimentation + SFT.** Cheap `.env` swaps first: `qwen2.5:3b` /
   `llama3.2:3b` chef, `qwen2.5:1.5b` vs `smollm:360m` sous. Then fine-tune
   SmolLM on Food.com (per arXiv:2502.02028) targeting the two observed failure
   modes: structured-JSON compliance and dietary-restriction adherence in chef
   grouping (the `xfail` chef-grouping test is the acceptance bar). Training
   scaffolding lives in `training/`.

   *Progress (2026-06-14):* paper-repro scaffold validated end-to-end. SmolLM-135M
   full-FT (100k-sample corpus, 3 epochs) trained on a rented vast.ai 4090 and
   evaluated vs the untuned baseline — large fluency/format gains (perplexity
   12.3→4.7, BLEU 2.4→12.1, judge overall 2.64→3.52) but **allergen-safety judge
   score stayed flat** (2.86→2.86). That's empirical confirmation that Food.com
   SFT teaches recipe *form*, not the dietary/allergen failure mode — i.e. the
   real lever is the step-2 dataset (app `PromptTemplates` + JSON schema), not
   bigger paper-repro runs. *Next:* full 231k-corpus 360M + 1.7B QLoRA runs (size
   sweep, configs ready, log TensorBoard; QLoRA → needs `merge_lora` before
   eval/export), then pivot to the step-2 app-targeted dataset.

## Launch readiness (from the 2026-07-09 review)

Items 1–4 above are the launch gate: recipe persistence, the async
generation API, the Shopping Optimizer, and real deal data. Two scoping
calls to make explicitly:

- **Shopping Optimizer is the namesake feature.** The UI stub degrades
  gracefully, but shipping "grocery optimizer" without item 3 needs a
  deliberate decision (soft-launch as a meal planner vs. hold for the
  optimizer).
- **Seed data only covers 3 postal codes.** Any other postal code renders
  an empty app. Until Flipp ingestion (item 4) lands, launch messaging has
  to match the covered areas.

Deployment/operational gaps to close before anything public:

- **Mobile layout breaks.** At phone widths the header nav stacks
  vertically and pushes content down; the profile chip is clipped. Content
  cards adapt fine — it's the `Layout` header that needs a responsive pass.
- **No CI.** Add a workflow that runs `pytest -m "not llm"` and
  `npm run build` (typecheck) per PR — the fresh-install bugs fixed above
  are exactly the class of drift CI against a clean schema would catch.
- **No production serving story for the frontend.** It isn't in
  docker-compose and only has the Vite dev server documented; decide
  static hosting vs. serving `dist/` from FastAPI and wire it up.
- **No LICENSE file.**
- **SETUP.md has drifted** (psycopg2 troubleshooting, Windows-only venv
  activation, response examples that predate the current API). Re-verify
  it against a clean machine once the above land.
- **DB hardening** (tracked in `scripts/ToDO.txt`): least-privilege app
  role, read-only analytics role, row-level security, PgBouncer,
  credential rotation.

## Engineering debt / smaller items

- `estimated_savings` for shopping lists hardcoded to 0.0 pending item 3.
- No auth — the frontend "session" is a localStorage user profile, and
  "load profile by user ID" means any profile is loadable by anyone;
  acceptable for a closed beta, revisit before opening writes to the
  public.
- Frontend deal categories are derived from the loaded page of deals; add a
  `GET /postal-code/categories/{postal}` endpoint if filtering needs to be
  exhaustive.
