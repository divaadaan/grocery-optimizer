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

- `estimated_savings` for shopping lists hardcoded to 0.0 pending item 3.
- No auth — the frontend "session" is a localStorage user profile; revisit
  when the API grows write endpoints worth protecting.
- Frontend deal categories are derived from the loaded page of deals; add a
  `GET /postal-code/categories/{postal}` endpoint if filtering needs to be
  exhaustive.
