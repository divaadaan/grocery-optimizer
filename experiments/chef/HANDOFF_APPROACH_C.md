# Handoff: redesign recipe generation around approach C (code-built trays)

For a fresh Claude session. Read this file, then `experiments/chef/RESULTS.md`, then `CLAUDE.md`.
Written 2026-11-23 at the end of the session that ran the experiments.

## The goal

Replace the current Chef / SousChef pipeline with the owner's own planning method, made
deterministic wherever it can be:
1. Find this week's loss leaders, the **anchors**: protein deals that are cheapest for their item
   across nearby stores.
2. Find discounted pantry **staples** worth stocking up on.
3. Pair **sides** with the anchors.

The model only does the cooking part: it plans dishes from small, pre-sorted trays, and writes
recipes for them.

**Product framing (owner, 2026-11-23): budget-agnostic, "here's what you can eat from grocery sales
near you".** The budget no longer constrains a plan. Cost is reported for information only. The
**Product framing** section of `CLAUDE.md` still says "dietary goals within the spend limit"; propose
an edit to the owner and don't change it silently.

The owner considers the current pipeline garbage. **Don't patch it. Replace it.**

## What the experiments established (details in RESULTS.md)

- Putting all the deals in one prompt fails even with nothing truncated. Picks cluster at the top of
  the list, and a larger model doesn't fix it.
- Code-built trays of about 55 rows remove that bias on every model tested. The local qwen2.5:7b
  then plans sensible meals in about 11 s. No model made up a deal id in 36 runs.
- The weak points are **label quality** (tea, coffee pods and Reese's were labelled pantry staples)
  and **package-price comparisons**, since 53% of deals have no unit. $/lb comparisons for meat and
  produce are reliable.
- The prototype code to port from:
  - `experiments/chef/label.py`: the labelling prompt and batching;
  - `experiments/chef/arms/c_trays.py`: `price_basis`, `build_trays`, the tray-row format, the Chef
    prompt;
  - `experiments/chef/common.py`: the dish-first plan format and the scoring.

## Owner decisions already made — don't reopen them

- Budget-agnostic product (above). The old note that stock-up costs count against the budget no
  longer applies.
- Find loss leaders by **comparing across stores**, and **build price history** over time.
  `price_snapshots` and `app/services/price_baseline.py` already exist; check what they hold.
- **The user confirms ingredients before generation:** a carousel of the proposed trays to keep or
  reject (an ingredient whitelist). The user also confirms their pantry items before a run.
- **Leave the Flipp search terms alone for now** (`app/services/flipp_staples.py`), even though
  `tea`, `coffee` and `orange juice` are in them.
- The current focus is **recipe generation and agent orchestration.**

## Decisions to settle with the owner before writing app code

1. **Grouping deals by name, against the rule in `CLAUDE.md`.** CLAUDE.md says "No name, keyword or
   substring matching anywhere downstream of product review". Approach C groups deals by an `item`
   label that an LLM derived from deal names. That grouping never decides dietary access: the deal
   gate still runs first, on curated product attributes. Get an explicit ruling. If the owner allows
   it, propose CLAUDE.md wording that scopes the rule to dietary decisions and requires
   item/role labels to be data, not code string matching.
2. **Reviewed labels or automatic ones.** About 900 new deals arrive each week, and only 37 of the
   current deals are linked to a product. Options:
   - an LLM labels every deal automatically, and the labels only affect which deals are *surfaced*,
     never dietary decisions;
   - labels are proposed and a human reviews them, like `app/services/product_proposer.py`;
   - a hybrid: automatic labels, plus a review queue for items that reach the trays.

   The carousel is itself a per-user review of what reaches the trays, which argues for automatic
   labels.
3. **Where labels live.** Options: a deal-level table (e.g. `deal_labels(deal_id, item, role,
   model, labelled_at)`), or product-level attributes. Product-level only works for deals that are
   linked to a product. Any new table is a migration, and per CLAUDE.md it reaches Neon only with
   the owner's OK, recorded in `schema_migrations`.
4. **Models and hosting.** The labelling used `hf:openai/gpt-oss-120b`. The Chef works on the local
   qwen2.5:7b. The owner is open to Hugging Face hosting; `HUGGINGFACE_API_KEY` in `.env` now has
   credits. Decide each role's model, and whether the app gets an HF client. `llm_factory.py` is
   Ollama-only today.
5. **The API shape.** Generation becomes **propose trays → user confirms → generate**.
   - That changes `POST /recipes/generate` and the in-process job registry
     (`app/services/recipe_jobs.py`).
   - Options: a new `GET/POST /plans/trays` step before generation, or a job that pauses for
     confirmation.
   - The frontend (`frontend/src/features/planner`) needs the carousel.
6. **Which existing stages survive.**
   - The Nutritionist's model verdict has been advisory only since 2026-07 and returned all-zero
     nutrition facts in the 2026-11-23 trace.
   - The retry and rejection loop exists mainly for dietary rejections.
   - Ask whether to keep, simplify or drop each.

## Proposed pipeline (to refine with the owner)

1. **Ingest** (exists). Flipp writes the week's deals.
2. **Label.** New. Once per ingestion batch, give each deal an `{item, role}`, in batches of 40 rows
   (port `label.py`).
   - Roles: protein, vegetable, fruit, starch, dairy, pantry_staple, condiment, prepared_meal,
     snack, beverage, non_food.
   - Store the labels. Labelling never runs at request time.
3. **Gate** (exists, don't change). `fetch_current_deals` → `deal_gate.deal_filter`, applied in SQL.
   - **Remove the `ORDER BY discount_percentage … LIMIT 200` ranking.** It is what kept the major
     chains out.
4. **Trays.** New, deterministic. Port `build_trays`:
   - group the gated deals by `(role, item, price basis)`;
   - compare each against the median across stores, on $/lb where available;
   - ANCHORS: proteins; SIDES: vegetables, starches, fruit; STOCK-UP: staples;
   - prepared meals, snacks, beverages and non-food never reach a tray.

   Later, add the price-history comparison.
5. **Confirm.** New, frontend. The user keeps or rejects tray items and confirms their pantry.
   Persist the confirmed set.
6. **Plan dishes.** This replaces `ChefOrchestrator.plan_ingredient_groups`.
   - One LLM call over the confirmed trays returns the dish-first plan format: anchor deal ids, side
     deal ids, pantry items, stock-up ids.
   - Validate it in code: every id must be one that was offered, and exactly N dishes.
   - On malformed JSON, retry once.
7. **Write recipes.** SousChef, one dish each.
   - Give it only that dish's deals plus the pantry. Remove the "Use ONLY the assigned deals"
     pressure in favour of "this dish's ingredients".
   - Every ingredient still cites a `deal_id` or a staple `product_id`, exactly as today.
8. **Dietary verdict** (exists, don't change). `app/agents/dietary.py` decides compliance from the
   cited products' curated attributes.
9. **Finalize** (exists). `recipe_generations` provenance, MLflow trace, the `chef_plan.json`
   equivalent.

## Constraints you must keep (from CLAUDE.md)

- **Dietary compliance** is a deterministic lookup of curated product attributes.
  - Never ask an LLM to judge it.
  - Never decide it from a deal or ingredient name.
  - NULL attributes fail closed.
  - An unsupported diet gets a 422.
  - The trays must start from the **gated** deal set.
- **The regression oracle stays green:** `.venv/bin/python -m pytest -q -m "not llm"`, 1551 passing
  at `5ff6fed`.
  - Tests are deterministic, using recorded fixtures under `app/tests/fixtures/`.
  - No live network calls and no live LLM.
- **Parallel agents get worktrees** (`isolation: "worktree"`), bootstrapped as described in
  CLAUDE.md. Agents that write to the database are serialized, since there is one Neon instance.
- `ROADMAP.md` is updated by the coordinator only.

## Gotchas learned this session

- **Ollama silently cuts off over-long prompts.** It keeps only the *last* part, about num_ctx/2
  tokens. Size `num_ctx` from the prompt, as `experiments/chef/llm.py` does. Note that changing
  `num_ctx` between calls reloads the model.
- **Hugging Face models:**
  - gpt-oss-120b went into a repeating loop when the JSON example had `//` comments, so keep
    examples as strict JSON;
  - Qwen3.x needs `chat_template_kwargs: {"enable_thinking": false}`, and can still burn its whole
    output budget;
  - Llama-3.3-70B occasionally drops a quote mark.

  Always validate the output and retry once.
- **MLflow** falls back to `./mlruns` when `.env`'s tracking server is down. View it with:
  `.venv/bin/mlflow ui --backend-store-uri ./mlruns --port 5001`.
- **Neon closes idle connections.** Restart uvicorn after a few idle hours.

## Suggested first moves

1. Read RESULTS.md, then skim the `review.md` files under `experiments/chef/runs/` for approach C.
2. Take decisions 1–6 to the owner as one short list. Don't write app code first.
3. Write the design as a short doc for the owner to edit: pipeline, schema, API, frontend.
4. Then build it in PR-sized steps, e.g.:
   1. label storage and the labelling job;
   2. tray builder plus endpoint;
   3. carousel UI;
   4. dish planner;
   5. per-dish SousChef;
   6. remove the old Chef path.
