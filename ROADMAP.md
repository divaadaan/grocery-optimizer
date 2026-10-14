# Development Roadmap

Canonical plan, last revised 2026-10-14 (originally 2026-06-12). Dated
`*Shipped:*` / `*Progress:*` entries below are historical records — the test
counts inside them were true when written and are deliberately not rewritten.
Current suite: **69 passed, 11 deselected** on `feat/async-recipe-generation`,
**214 passed, 11 deselected** on `feat/flipp-ingestion` (`pytest -m "not llm"`).

Supersedes `NEXT_STEPS.md` (see git history
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

## Product framing (2026-10-14)

The hero metric is **meeting dietary goals within the spend limit the user
sets** — not savings against a regular price. Flyer data supplies a pre-promo
price on only ~17% of items (measured live), so `estimated_savings` degrades on
real data and must degrade to *absent* rather than to a fabricated number. The
replacement is a self-derived baseline accumulated in `price_snapshots`
(`observed_sale_median`, delta `discount_vs_typical_sale`), which needs ~4-8
weeks of runs before it says anything. Budget adherence needs no history, so it
is the day-one metric and savings is secondary.

One gap this exposes, not yet closed: `users.budget` is documented as a
*monthly* budget (`app/routes/users.py:36`) but `app/agents/graph.py:69`
applies it as the ceiling for a *single* generation, with no accumulation
against completed `shopping_lists.total_cost`. `mark-complete` is the natural
spend-recording signal. Until that is wired, "within your spend limit" is
aspirational.

## Next

1. **Recipe persistence.** Save approved recipes from
   `run_recipe_generation` (route currently returns `recipe_id=0`); implement
   `GET /recipes/{id}` and `GET /recipes/user/{id}` (501 stubs). Unblocks a
   recipe-history view in the UI.

   *Shipped (2026-07-15):* **Done.** `finalize_meal_plan` already persisted
   recipes; the returned DB ids are now threaded back through
   `state.saved_recipe_ids` so `POST /recipes/generate` returns real ids
   instead of `recipe_id=0`. Both GET stubs implemented over new
   `DatabaseService.get_recipe`/`get_user_recipes`. (Also fixed a pre-existing
   schema drift found while verifying: `users.last_login`/`is_active` were
   referenced by `UserService` but missing from `init_db.sql` and the DB.)

2. **Async generation API.** `POST /recipes/generate` is synchronous and takes
   minutes with local models — it will hit browser/proxy timeouts. Move to a
   job model: POST returns `job_id`; progress via polling or SSE streamed from
   LangGraph node events ("SousChef 2/3 cooking…"). Frontend impact is
   contained: swap `useGenerateRecipes` in `frontend/src/hooks/queries.ts` and
   feed real stages to `GenerationProgress`.

   *Shipped (2026-10-14):* **Done**, two commits on
   `feat/async-recipe-generation`. The framing above understated the defect: the
   synchronous `graph.invoke()` ran inside an `async def` endpoint with no
   executor, so one generation pinned the event loop for *every* client, not just
   the caller — an availability bug, not a UX wart. `0b1d1f6` fixes only that
   (`asyncio.to_thread`), landed separately on purpose. `6d6db31` adds the job
   model: `POST /recipes/generate` → `202` + `job_id`; `GET
   /recipes/generate/status/{job_id}` → state, `stage`, `stage_index`/
   `stage_total`, a server-supplied `pipeline` checklist, and the **unchanged**
   `RecipeGenerationResponse` (real `recipe_id`s preserved) when terminal. User
   validation stays in the POST so a bad `user_id` is still a 404 up front.
   Progress is real, not simulated: `run_recipe_generation` moved to
   `graph.stream(stream_mode=["updates","values"])` (the last `values` chunk
   equals what `invoke` returned) and the registry counts the three parallel
   SousChefs to produce "SousChef 2/3 cooking…". `stage_index` is a position in
   a 6-step checklist, not a monotonic percentage — the retry loop revisits
   step 5. Storage is an in-process dict + 2-worker `ThreadPoolExecutor`;
   concurrency capped at 2 because the single local Ollama is the real
   bottleneck. Limits, all documented in `app/services/recipe_jobs.py`: lost on
   restart, wrong for >1 uvicorn worker, eviction past 200 jobs drops only
   terminal ones, and no ownership check on a `job_id` beyond uuid4
   unguessability (there is no auth). Tests 46 → 69.
   *Caveat:* no non-`llm` test drives the real compiled graph, so the
   `stream_mode` contract rests on a stub graph plus a standalone langgraph
   experiment. The first live generation is the real check on stage ordering.

3. **Shopping Optimizer agent + endpoints.** Consolidate approved-recipe
   ingredients, assign items to stores by best price, persist to
   `shopping_lists`. The frontend success path is already built
   (`features/shopping/ShoppingListPage.tsx`).

   *Shipped (2026-07-15):* **Done.** `app/services/shopping_optimizer.py` is a
   deterministic (DB-/LLM-free) v1: consolidate ingredients across a user's
   recipes (merge duplicates by normalized token-set → `xN` quantity) and
   assign each to the cheapest store carrying a matching deal, reusing
   `costing.py`'s token matcher for parity; unmatched items / pantry staples
   fall to an `ANY_STORE` sentinel. `estimated_savings` (regular − sale) is
   computed here, retiring the hardcoded-0.0 debt. Endpoints: `GET
   /shopping-list/{id}` returns the user's latest persisted list, lazily
   building one only on first call (idempotent — no row-per-GET); `POST
   /shopping-list/{id}/generate` is the explicit regenerate action; `POST
   .../mark-complete` closes a list. The persisted row has no `stores` column,
   so the read path re-derives it from items via `stores_from_items`. Verified
   end-to-end against the live DB (cross-store cheapest assignment, JSON/array
   round-trips, GET idempotency). Follow-up left as debt below (v2 =
   minimize-trips; `mark-complete` targets latest-by-user, not a `list_id`).

4. **Real deal ingestion (Flipp).** Replace seed data; `flipp_api_key`/
   `flipp_api_url` already exist in settings. Add a scheduled refresh and wire
   `POST /postal-code/discover` to it (currently it only reads what's in the DB).

   *Shipped (2026-10-14):* **Done**, `6d8b306` on `feat/flipp-ingestion`, and
   verified end-to-end against the live provider and the live Neon database.

   The `flipp_api_key` premise was false: there is no official Flipp API and no
   credentials ever existed — `.env` held the `.env.example` placeholder
   verbatim, and `https://api.flipp.com/v2` corresponds to nothing. The setting
   is retired. The source is the unofficial, keyless backend
   `backflipp.wishabi.com/flipp/items/search`. Because it is search-by-term and
   cannot enumerate a flyer, ingestion is **staples-driven**: 50 `(term,
   category)` pairs in `app/services/flipp_staples.py`, capped at 60 so an edit
   cannot turn a refresh into a crawl. Politeness is built in: identifying UA,
   10s timeout, 2 retries with jittered backoff on transport errors and 429/5xx
   only (403/404 fail fast), a 1.5s minimum inter-request interval held under a
   lock, and a process-wide refresh lock. Category comes from the search term —
   `_L1`/`_L2` are useless (every grocery item is `Food, Beverages & Tobacco`)
   and `_L1` is used only as a negative filter. `brand` is unobtainable: the
   payload carries opaque `brand_ids` with no names.

   **Price history is now written.** `price_snapshots` was dead-but-ready schema
   that no Python touched (it did hold 60 seeded rows). Ingestion appends to it,
   with a `price_basis` column distinguishing `sale_only` /
   `regular_and_sale` / `regular_only` / `unknown`, because "we only got one
   number" and "shelf price equals sale price" are different facts and guessing
   between them corrupts everything downstream. `price` became nullable so a
   caller with only a promo price is not forced to invent a shelf price.

   Idempotency: deals keyed `(store_id, source, source_item_id, valid_from)` —
   `valid_from` is in the key deliberately, or next week's flyer would overwrite
   this week's row instead of accumulating history. Snapshots keyed the same with
   `time` truncated to UTC midnight so two runs in a day collapse. A merchant can
   print the identical offer twice in one flyer with distinct `flyer_item_id`s
   (10 such pairs observed live across two terms); `dedupe()` collapses them and
   picks the representative by **lowest `source_item_id`, not encounter order**,
   because encounter order follows unstable relevance ranking and would silently
   insert a second row next week.

   Two latent bugs found and fixed: every `UPDATE` on `stores` raised `42703`
   (the shared `update_updated_at_column()` trigger assigns `NEW.updated_at`, a
   column `stores` does not have — it has `last_updated`; nothing had ever
   updated a store row so it never fired), and `fetch_current_deals` ordered by
   `discount_percentage DESC` where Postgres sorts NULLs **first**, so
   mostly-NULL real discounts would have filled the `LIMIT 200` with
   unmeasurable deals.

   Migrations `001`–`005` **applied to Neon on 2026-10-14**, each validated
   first by executing against the real schema inside a rolled-back transaction
   (individually and as a sequence). A `schema_migrations` ledger was created on
   Neon, which had none — `make migrate` in `../sql4e` targets only the study DB.
   `005` is `COMMENT ON FUNCTION` only and deliberately adds **no** current
   `price_snapshots` partitions: `cleanup_old_price_partitions()`'s 90-day cutoff
   is safe *by accident* today, since the loop globs `price_snapshots_%`,
   `TO_DATE('default','YYYY_MM')` raises, and the inner `EXCEPTION WHEN OTHERS`
   swallows it — so with no current-month partitions all real rows live in
   `default` and only the four empty 2025 partitions are reachable. Adding
   partitions would convert that into a live hazard, and a naive `CREATE TABLE
   ... PARTITION OF` fails with `23514` against a populated default partition.
   **Do not schedule or invoke that function.**

   Live verification: run 1 wrote 35 stores / 98 deals / 98 snapshots; an
   identical re-run inserted **zero** (98 deals updated, 98 snapshots skipped),
   with zero natural-key duplicates in either table and zero `price_basis`
   invariant violations. Measured `regular_price` coverage **0.173**.
   `sale_baseline` correctly returns `None` at one observed week, and
   `discount_vs_typical_sale` returns `None` rather than raising.

   `scripts/README.md` and `SETUP.md` claimed `price_snapshots` was a TimescaleDB
   hypertable with 7-day chunks, 90-day retention and a `price_snapshots_daily`
   continuous aggregate. None of that exists — it is native PG range
   partitioning. Both corrected, guarded by `test_migrations.py`.

   *Deferred, deliberately:* dietary compliance. `app/agents/dietary.py` matches
   forbidden terms by naive casefold substring, which real flyer names break both
   ways — false positives ("Beefsteak Tomatoes" → `beef`) and, worse, false
   negatives (`prosciutto`, `kielbasa`, `capicollo`, `gelatin`, `lard`,
   `marzipan` contain no forbidden term). Gated instead: two flags, both default
   off (`DEAL_INGESTION_ENABLED`, `INGESTED_DEALS_FOR_RESTRICTED_DIETS`), and a
   user with any restriction is served only `source IN ('seed','manual')` deals,
   filtered in SQL before the `LIMIT`. `test_deal_gate.py` characterises the gap
   rather than hiding it. **See the debt list — this gate is currently vacuous.**

   *No scheduler.* `POST /postal-code/refresh` is the manual trigger (503 while
   disabled, 409 if already running); `POST /postal-code/discover?refresh=true`
   auto-ingests only on a genuine cold start, rate-limited to one attempt per
   postal code per hour; `scripts/ingest_deals.py` is the CLI. Refresh weekly
   when flyers turn over.

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

   *Progress (2026-06-16):* **Phase 2 + Phase 3 shipped; the small-model SFT track
   is concluded.** `train_sft.py` gained a `dataset_format: conversational` path
   with completion-only loss masking — the app serves these agents via
   `ChatOllama`, so the dataset emits user/assistant turns rather than raw
   concatenation (which also fixed a BPE `:`+`{` merge masking the opening brace).
   A first SmolLM-360M run produced garbage and exposed a base bug:
   `SmolLM-360M-Instruct` (v1) has a 2048-token context but chef prompts are
   ~2.8k, so they overflowed and RoPE extrapolated into noise (a healthy eval_loss
   1.57 hid it). Fixed by moving to **SmolLM2** (8192 ctx) + 12 epochs. Built
   `evaluate_app.py` (Phase 3): deterministic per-task scoring (JSON-validity,
   chef dietary-violation, nutritionist confusion matrix + false-rejection rate)
   with HF and app-faithful Ollama paths, porting the two `xfail` assertions.
   **Result: both xfail proxies still fail.** SmolLM2-360M chef emits the wrong
   top-level JSON shape; the nutritionist produces 6/6 valid JSON yet
   **false-rejects 100% of compliant recipes by hallucinating violations**
   ("avocado violates vegan", "milk violates vegetarian") — it learned the
   rejection *template*, not the dietary *semantics*, despite zero-noise
   programmatic labels. Decisive lesson: valid JSON ≠ correct, and ~360M params
   can't internalize the compliance mapping from this corpus.
   **Decision: stop SFT'ing the small SmolLM models. The next attempt is a more
   capable base — `qwen2.5:1.5b-instruct` (already pulled) — evaluated *untuned*
   through `evaluate_app.py --ollama-model` first**, since world knowledge alone
   may clear the bar (it already "knows" avocado is vegan). If that still falls
   short, the next lever is retrieval (item 6 / Phase 4), not more SFT.

   *Progress (2026-07-15):* **Untuned `qwen2.5:1.5b-instruct` evaluated through the
   app-faithful Ollama path — falls short; the base-swap escape hatch is closed.**
   `evaluate_app.py --ollama-model qwen2.5:1.5b-instruct` (10-example test split,
   seed 0): sous_chef 2/2 valid JSON, but **both xfail proxies still fail** — chef
   0/2 valid & dietary-clean (the one valid plan grouped Eggs/Ground Beef/Milk into
   a vegetarian plan), and the nutritionist **false-rejects 5/5 APPROVE controls**
   (false_rejection_rate 1.0) — the *same* failure mode as the SFT'd SmolLM2, now on
   a capable untuned model. Root-caused via two cheap probes: (a) the model's own
   verdict reasoning is factually wrong ("Milk 2% is not vegetarian-friendly",
   "Tomato Sauce contains tomatoes, which are not vegan"), so it is hallucinating
   violations, not legitimately rejecting incoherent fixtures; (b) plain-form Q&A
   isolates *two compounding causes* — a genuine knowledge gap (qwen answers "No" to
   "is milk/cheddar vegetarian?" even in plain form — it conflates vegetarian with
   vegan) AND prompt-induced degradation (it answers "tomatoes are vegan" correctly
   in plain form but confabulates the opposite under the app's 5-point checklist +
   `format=json`). Decisive: small models get dietary compliance wrong whether tuned
   or untuned-but-capable, confirming the item-6 thesis that compliance must be a
   deterministic **code path**, not learned/LLM-judged behavior. **Base-swap sweep
   confirms the ceiling isn't about capacity:** the same eval on `llama3.1:8b` and
   `mistral:7b-instruct` also false-rejects 5/5 APPROVE controls (rate 1.0) —
   scaling 1.5B→8B moved the nutritionist metric by zero. `deepseek-r1:8b` is a
   non-starter for the app path (0/6 valid JSON; its `<think>` reasoning can't
   coexist with `ChatOllama(format="json")` without a think-stripping layer). The
   base-swap lever is exhausted. **Prompt-hardening diagnostic also negative:**
   `NUTRITIONIST_VALIDATION` was hardened with an authoritative dietary reference
   mirroring the `forbidden_terms_for` oracle (vegetarian ALLOWS dairy/eggs, plant
   foods never violate, etc.) plus an approval rule that bars style/coherence
   rejections. Re-running the test's nutritionist rows through the live template:
   **llama3.1:8b and qwen2.5:1.5b both still false-reject 5/5 (rate unchanged at
   1.0).** Inspection shows the models don't *use* the reference — they carry a hard
   rejection bias and confabulate a justification, often self-contradicting ("cheese
   is not explicitly excluded… however it contains dairy" → reject; "Tomato Sauce
   contains dairy"; "Canned Tomatoes contains fish/seafood"). So the false-rejection
   is neither a knowledge gap nor a wording problem — it's a robust behavioral
   failure of the LLM-as-compliance-judge that grounding does not fix. All three
   model-side levers (SFT, base-swap, prompt) are now exhausted.
   *Shipped (2026-07-15):* **Deterministic nutritionist compliance (Phase 4, step 1)
   — the nutritionist `xfail` is flipped.** New `app/agents/dietary.py` holds the
   compliance oracle (`forbidden_terms_for`/`recipe_violations`/`compliance_report`),
   vocab identical to the fixture's `forbidden_terms_vegetarian` and to
   `training/data_app/seed_catalog.py`. The Nutritionist node
   (`app/agents/validate_recipes`) now computes `approved`/`violations` in code and
   uses the LLM for advisory nutrition facts only (best-effort; a parse failure no
   longer affects the verdict). `test_nutritionist_regression.py` APPROVE controls
   converted from `xfail` to hard asserts; full suite **31 passed, 5 skipped,
   1 xpassed** (the xpass is the still-`xfail` chef test passing stochastically —
   not a fix). Note: `NUTRITIONIST_VALIDATION`'s hardened dietary reference is now
   non-load-bearing (we ignore the LLM's verdict/feedback) — harmless, kept as
   nutrition-context; trim if desired.
   *Shipped (2026-07-15, cont.):* **Deterministic chef compliance — the chef
   `xfail` is flipped too; both acceptance-bar tests now pass deterministically.**
   `ChefOrchestrator.plan_ingredient_groups` pre-filters the deal set through
   `is_compliant` (the LLM only ever sees compliant deals) and post-filters the
   returned groups via `_enforce_group_compliance` (strips any forbidden item the
   model emits anyway, backfills emptied groups from unused compliant deals to keep
   the 3-non-empty-groups contract). `test_chef_groups_respect_vegetarian_restriction`
   demoted from `xfail` to a hard assert. Full suite: **32 passed, 5 skipped, no
   xfail/xpass** — the two failure modes that motivated the whole SFT track
   (structured-JSON dietary grouping + nutritionist false-rejection) are resolved in
   code. Both `dietary.py` and the chef/nutritionist nodes share one oracle whose
   vocab equals the test fixture and the SFT label generator.
   *Next:* broader Phase 4 (offline substitution table + RAG-augmented generation)
   in `training/data_app/NEXT_STEPS.md` — now a *quality/variety* lever (adapt real
   recipes to on-hand deals) rather than a *compliance* one, since compliance is
   handled. Also revisit `estimated_savings` (item 3 / debt) and consider trimming
   the now-unused compliance sections of the chef/nutritionist prompts. — start with the deterministic
   compliance oracle + offline substitution table. (Cheap side-lever noted, not
   blocking: since qwen "knows" tomatoes are vegan in plain form but confabulates
   under the app prompt, a nutritionist-prompt-hardening pass could recover the
   prompt-induced share of false-rejections — but the milk/cheese knowledge gap
   won't yield to prompting, so RAG remains the real lever.)

6. **RAG-augmented recipe generation (Phase 4).** Inference-time retrieval, after
   the qwen2.5:1.5b trial. Index a recipe corpus (Food.com to start), retrieve
   recipes that use the on-hand/deal ingredients, apply an offline-generated,
   compliance-gated **substitution table** to make them diet-safe, and let the
   generator (the base from item 5) adapt them — making dietary compliance a
   deterministic **code path** rather than a learned behavior (the thing
   small-model SFT couldn't do). The substitution model stays offline (no extra
   per-request inference). Adds a proactive substitution node before generation
   (Nutritionist remains the final safety gate), property-based regression tests
   on the deterministic compliance oracle, and a postal-code/deal-set-keyed
   **response cache** that doubles as a self-growing, pre-validated dataset
   (deal sets are shared per postal-code-week, so cross-user reuse is the common
   case). Full task breakdown: `training/data_app/NEXT_STEPS.md` Phase 4.

## Engineering debt / smaller items

- **Restricted users currently get zero deals.** Measured against Neon on
  2026-10-14: an unrestricted user gets 49 valid deals (all `flipp`), a
  vegetarian user gets **0**. The diet gate is working correctly — no `flipp`
  deal leaked — but its "safe" fallback is empty, because **all 41 seed deals
  expired 2026-07-21** and none are still valid. This predates ingestion (every
  user got zero deals before, since the only deals were expired), but the effect
  is that the deferred compliance fix is now *blocking* for anyone with a
  dietary restriction. Two ways out: land the `products` table so real deals can
  be served safely, or refresh the seed catalogue with current dates.
- **The `products` table is now a baseline problem too, not only a compliance
  one.** `sale_baseline` matches `product_name` exactly, and real flyer names
  fragment badly — one product yielded three series (`Boneless, Skinless Chicken
  Breasts`, `Fresh Boneless, Skinless Chicken Breas`, `Maple Leaf Prime raised
  without antibi`). Three LACTANTIA rows at the same $4.96 from one merchant
  survived `dedupe()` because their truncated names differ by a few characters,
  which would triple-weight one offer in a future median. One row arrived as
  `ANNIE'S SHELLS & CHEDDAR 170 G BEATRICE CHOCOLAT` — two products mashed
  together by the flyer. Exact-name matching will rarely accumulate four weeks on
  a single string, so the baseline stays sparse until names are normalised.
  `idx_price_snapshots_product_trgm` is already in place for the fuzzy version.
- A flyer-only baseline samples **sale** prices, so `observed_sale_median` sits
  below true shelf price and any delta against it understates by construction.
  Surfaced per baseline as `measures_sale_prices_only`. Closing it needs a
  non-flyer price source, not more flyer history.
- `starlette` 0.35.1's `TestClient` is incompatible with the installed `httpx`
  0.28.1 (`Client.__init__() got an unexpected keyword argument 'app'`). Went
  unnoticed because the repo had zero route tests until 2026-10-14; the new ones
  drive the app through `httpx.ASGITransport` instead. The pin conflict stands.
- `frontend/tsconfig.app.tsbuildinfo` is a tracked build artifact — it dirties
  the tree on every `tsc -b`. Should be gitignored and removed from the index.
- ~~`estimated_savings` for shopping lists hardcoded to 0.0 pending item 3.~~
  Resolved — computed in `shopping_optimizer` from `regular − sale`. Superseded
  in practice by the 2026-10-14 framing: with ~17% regular-price coverage this
  figure is unreliable on ingested data, and `discount_vs_typical_sale` is the
  intended replacement once the baseline has history.
- Shopping-list v2 levers (deferred): store-assignment that minimizes trips
  (v1 is cheapest-per-item); `mark-complete` targets the user's latest list
  rather than a specific `list_id`.
- No auth — the frontend "session" is a localStorage user profile; revisit
  when the API grows write endpoints worth protecting.
- Frontend deal categories are derived from the loaded page of deals; add a
  `GET /postal-code/categories/{postal}` endpoint if filtering needs to be
  exhaustive.
