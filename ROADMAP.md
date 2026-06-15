# Development Roadmap

Canonical plan as of 2026-06-12. Supersedes `NEXT_STEPS.md` (see git history
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
   bigger paper-repro runs.

   *Progress (2026-06-15):* (a) **Step-2 dataset builder shipped** —
   `training/data_app/` renders examples through the app's real `PromptTemplates`
   and gates every completion through the real Pydantic schemas. Hybrid labelling:
   chef-grouping + nutritionist verdicts programmatic (zero label noise on the two
   failure modes), SousChef prose distilled from `qwen2.5:7b`. Verified build: 115
   examples, 0 dropped by the schema gate, 0 chef forbidden-product selections.
   Plan/caveats in `training/data_app/NEXT_STEPS.md`. (b) **Phase-0 export check
   started** — SmolLM-360M QLoRA (full 231k corpus, 1 epoch, train_loss 1.54)
   trained on vast.ai and pulled local to `training/runs/smollm-360m-qlora`;
   adapter verified loadable. **Phase-0 export path now validated end-to-end:**
   `merge_lora` → GGUF (llama.cpp `convert_hf_to_gguf`, f16, 724 MB) → HF Hub
   (`danieldsachs/smollm-360m-recipe`) → `ollama run hf.co/...` into the app's
   Ollama instance, producing coherent on-format recipes (the greedy-decoding
   repetition loop seen in `evaluate.py` does not appear under Ollama's default
   sampling). *Next:* Phase 2 (train_sft `prompt_completion` + completion-only
   loss masking) and Phase 3 (`evaluate_app.py`; flip the two `xfail` acceptance
   tests).

## Engineering debt / smaller items

- `estimated_savings` for shopping lists hardcoded to 0.0 pending item 3.
- No auth — the frontend "session" is a localStorage user profile; revisit
  when the API grows write endpoints worth protecting.
- Frontend deal categories are derived from the loaded page of deals; add a
  `GET /postal-code/categories/{postal}` endpoint if filtering needs to be
  exhaustive.
