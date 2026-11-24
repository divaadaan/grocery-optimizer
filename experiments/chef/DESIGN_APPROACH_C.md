# Design: recipe generation on code-built trays (approach C)

Status: **accepted**, 2026-11-23; the four open questions were answered the same day (§14). Builds on `HANDOFF_APPROACH_C.md` and
`RESULTS.md`. All six handoff decisions were accepted by the owner on 2026-11-23 as recommended;
they are restated in §1 so this doc stands alone.

## 1. Decisions (settled)

| # | Decision |
|---|---|
| 1 | Grouping deals by an LLM-derived `item`/`role` label is allowed, because it only decides what is *surfaced*. The CLAUDE.md rule is narrowed to dietary decisions (wording in §10). Labels are stored data, never string matching in code, and they only ever apply to the already-gated deal set. |
| 2 | Labels are **automatic**, with no human review queue. The carousel is the per-user review. Carousel rejections are recorded so a review queue can be added later if label errors warrant it. |
| 3 | Labels live in a new **deal-level table**, `deal_labels`. The labeller also extracts package size, to fix the 53% of deals with no unit. |
| 4 | Labeller: `hf:openai/gpt-oss-120b`. Dish planner and SousChef: local `qwen2.5:7b`, each configurable per role to an HF model. The app gets an HF provider in `llm_factory.py`. |
| 5 | Two plain calls: `POST /plans/trays`, then `POST /recipes/generate` with the confirmed set. No job that pauses for a human. The server re-checks every id. |
| 6 | The Nutritionist is dropped. The dietary verdict is unchanged. The rejection loop becomes a per-dish retry. The old Chef path (`initialize_chef`, `load_recipe_catalog`, `plan_ingredient_groups`, `handle_rejections`) is removed. Finalize is kept. |

Product framing: budget-agnostic, "here's what you can eat from grocery sales near you". Cost is
reported, never a constraint.

## 2. Pipeline

```
 ingestion (exists) ──► LABEL (new, offline, per batch) ──► deal_labels
                                                              │
 request time:                                                ▼
 POST /plans/trays:   GATE (exists) ──► TRAYS (new, code) ──► carousel (new, frontend)
                                                              │ kept ids + pantry
 POST /recipes/generate:                                      ▼
   RE-GATE + VALIDATE ──► PLAN DISHES (1 LLM call) ──► WRITE RECIPES (SousChef, 1 call per dish)
        ──► DIETARY VERDICT (exists) ──► per-dish retry ──► FINALIZE (exists) / handle_failure
```

No LLM call ever sees a deal that the gate removed. No LLM call ever judges dietary compliance.

## 3. Labelling

**What:** port `experiments/chef/label.py` to `app/services/deal_labeller.py`.
- Input: deals with no `deal_labels` row at the current `PROMPT_VERSION`.
- Batches of 40 rows, in the compact `deal_row` format.
- Output per deal: `{item, role, size_qty, size_unit}`.

**Roles:** `protein, vegetable, fruit, starch, dairy, pantry_staple, condiment, prepared_meal,
snack, beverage, non_food`, plus `unknown` when the model returns something else.

**Prompt changes from the prototype:**
- Add size extraction: `size_qty` + `size_unit ∈ {g, ml, each}`, converted from the name or unit
  text ("1.36 kg" → 1360 g, "12 x 355 mL" → 4260 ml). NULL when the text doesn't say.
- Tighten `pantry_staple`: tea, coffee, candy and drinks are **not** staples. These were the
  observed errors.
- Strict-JSON example only (no `//` comments, since gpt-oss loops on them).

**When it runs:**
- At the end of `run_ingestion` when labelling is enabled, and on demand via
  `scripts/label_deals.py`.
- The job is idempotent and resumable. A failed batch leaves its deals unlabelled, and the next
  run picks them up.
- **Never at request time.**

**Validation:**
- Every returned id must belong to the batch.
- Unknown roles are stored as `unknown`.
- On malformed JSON, retry once, then leave the rest of the batch unlabelled.

**Unlabelled deals:** they never reach a tray, which fails closed for surfacing. The trays response
reports how many deals were left out this way.

**Later (not v1):** reuse a stored label for a deal whose exact `product_name` + store was already
labelled last week. That reuse is a stored-data lookup, not a decision made by matching text, but
it is out of scope until cost or latency calls for it.

## 4. Trays (deterministic)

Port `price_basis` and `build_trays` from `experiments/chef/arms/c_trays.py` to
`app/services/trays.py`, as pure functions over (gated deals, labels).

**Input:**
- `fetch_current_deals(postal, restrictions)` with the `ORDER BY discount_percentage … LIMIT 200`
  **removed**.
- The SQL gate stays exactly as it is.
- Joined to `deal_labels`.

**Grouping:**
- The key is `(role, item, basis)`.
- `basis` is `lb` when the price is per weight; otherwise per 100 g or 100 ml when
  `size_qty/size_unit` are known; otherwise `pkg`.
- The size-based basis is the improvement over the prototype: package prices become comparable
  when the label carries a size.

**Comparison:** within a group, take the cheapest deal and its gap against the median **across
chains**.

**Trays:**

| Tray | Roles | Size | Order |
|---|---|---|---|
| ANCHORS | protein | 15 | gap ↓, then `lb` before others, then cheapest |
| SIDES | vegetable, starch, fruit, dairy | 25 | same |
| STOCK-UP | pantry_staple, condiment, with ≥ 2 stores | 15 | same |

`dairy` joins SIDES (owner, 2026-11-23).

`prepared_meal`, `snack`, `beverage`, `non_food` and `unknown` never reach a tray.

**Later:** once `price_baseline.py` has its 4+ weeks of history, add `discount_vs_typical_sale` as a
second signal. It must never be labelled a regular price.

**Restricted users:** the gate passes only deals linked to confirmed products (37 today), so their
trays will be thin. The response says so through its counts, and planning refuses with a clear
message when there is no anchor at all. Improving product linking is separate work.

## 5. API

### `POST /plans/trays`

```json
// request
{"user_id": 1, "num_meals": 5}
// response
{
  "trays": {
    "anchors":  [TrayItem, ...],
    "sides":    [TrayItem, ...],
    "stock_up": [TrayItem, ...]
  },
  "pantry": [{"product_id": 12, "name": "olive oil"}, ...],
  "counts": {"gated_deals": 916, "labelled": 911, "unlabelled": 5, "in_trays": 55},
  "label_version": "v2"
}
```

`TrayItem` = `{deal_id, product_name, store_name, chain, sale_price, unit, item, role, basis,
compare_price, median_price, n_chains, gap}`.

- **Pantry** comes from `recipe_catalog.fetch_pantry_staples()`: confirmed products with
  `assumed_on_hand`. Pantry items are cited by `product_id`, as today, not by the prototype's
  free-text list.
- **Unsupported diet:** 422, as on generate.
- **Statelessness:** nothing is persisted at this step. The tray builder is cheap and
  deterministic, so it can rebuild the trays at generate time.

### `POST /recipes/generate` (changed)

```json
{"user_id": 1, "num_meals": 5, "preferences": {},
 "kept_deal_ids": [ ... ], "pantry_product_ids": [ ... ]}
```

Server-side, before the job starts:
1. **Rebuild the trays** for this user, so the set of ids that were offered is known.
2. **`kept_deal_ids` must be a subset of the offered ids.**
   - Ids that are no longer current (expired or re-ingested in between) are dropped, and the drop
     is recorded.
   - Any other id gets a 422. The client cannot add deals.
3. **`pantry_product_ids` must be a subset of the pantry staples.** Any other id gets a 422.
4. **No kept anchor at all** gets a 422. Fewer kept anchors than `num_meals` is allowed: one
   anchor may serve more than one dish (owner, 2026-11-23).

The job registry, status polling and `GenerationProgress` are unchanged. The progress stages are
renamed.

## 6. Plan dishes (replaces `ChefOrchestrator.plan_ingredient_groups`)

**The call:**
- One LLM call, role `chef`.
- The prompt is the C system prompt + the `PLAN_CONTRACT` (dish-first) + the kept tray rows
  (`tray_row` format) + the confirmed pantry.
- `num_ctx` is sized from the prompt, as in `experiments/chef/llm.py`.

**The contract** is unchanged from `experiments/chef/common.py`, except that `pantry_items` becomes
`pantry_product_ids`.

**Validation, in code:**
- Exactly `num_meals` dishes.
- Every dish has at least one anchor. An anchor may appear in more than one dish.
- Every deal id is one that was kept.
- Every pantry id is one that was confirmed.
- On malformed JSON or a failed check, retry once with the error appended to the prompt, then call
  `handle_failure`.

**Output:** `dish_plan`, which is written to `chef_plan.json` / the MLflow trace and persisted
(§9).

## 7. Write recipes (SousChef, one dish per call)

**Input per dish:** the dish name and its `why`; its anchor and side deals (tray rows); the
stock-up deals; the confirmed pantry (product_id + name); household size; preferences.

**Prompt framing:** "this dish's ingredients", replacing "Use ONLY the assigned deals". A deal is
not forced into a dish: the SousChef may leave a side unused.

**Output:**
- The existing recipe schema, with `instruction_steps`.
- **Every ingredient cites a `deal_id` or a staple `product_id`**, as today.

**Validation:**
- A citation must be in (kept deals ∪ confirmed pantry). Citing a kept deal from another dish is
  allowed.
- An uncited or unknown ingredient fails the recipe.

**Concurrency:** the per-dish calls fan out in parallel, as `sous_chef_generate` does today, so
infrastructure errors still fail fast.

## 8. Verdict, retry, finalize

- **Dietary verdict:** `app/agents/dietary.py`, **unchanged**: a lookup of the cited products'
  attributes, with the combination rules.
- **Per-dish retry:**
  - A dish whose recipe failed validation or was rejected on dietary grounds gets **one** retry.
    The retry is given the reason (e.g. "deal 123's product contains dairy").
  - If the retry also fails, the dish is dropped.
  - Infrastructure errors (unreachable, timeout) go straight to `handle_failure`, per the scoring
    decision of 2026-11-23.
- **Status:**
  - `completed` if every dish was approved;
  - `partial` if at least 60% were approved;
  - otherwise `failed`.

  The eval rule is unchanged: a partial plan is a FAIL.
- **Finalize:**
  - Kept: the `recipe_generations` row, `save_recipes`, the MLflow trace and costing (reported
    only).
  - `nutrition_facts` / `health_score` stop being written (NULL). `RecipeCard.tsx` already hides
    them when they are absent.

## 9. Schema: migration 024

```sql
CREATE TABLE deal_labels (
    deal_id        INTEGER PRIMARY KEY REFERENCES deals(deal_id) ON DELETE CASCADE,
    item           TEXT NOT NULL,
    role           VARCHAR(16) NOT NULL CHECK (role IN ('protein','vegetable','fruit','starch',
                     'dairy','pantry_staple','condiment','prepared_meal','snack','beverage',
                     'non_food','unknown')),
    size_qty       NUMERIC(10,2),
    size_unit      VARCHAR(8) CHECK (size_unit IN ('g','ml','each')),
    model          VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(16)  NOT NULL,
    labelled_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_deal_labels_role_item ON deal_labels (role, item);

ALTER TABLE recipe_generations
    ADD COLUMN offered_deal_ids   INTEGER[],   -- the trays as rebuilt at generate time
    ADD COLUMN kept_deal_ids      INTEGER[],   -- offered minus kept = carousel rejections
    ADD COLUMN pantry_product_ids INTEGER[],
    ADD COLUMN dish_plan          JSONB;       -- the validated plan from §6
```

- Idempotent (`IF NOT EXISTS`), with a rollback block, in the style of 022 and 023.
- Applied to Neon only with the owner's OK, via `scripts/apply_migrations.py --check` then
  `--apply`, and recorded in `schema_migrations`.
- Rejections are recorded only for generations that run. A carousel the user abandons leaves no
  trace, which is acceptable for v1.

## 10. Models, config, CLAUDE.md

**`llm_factory.py`:**
- Per-role spec strings: `ollama:<name>` or `hf:<org/model>`.
- Roles: `labeller`, `chef`, `sous_chef`. `nutritionist` is removed.
- The HF builder ports the httpx client from `experiments/chef/llm.py` to the router's
  OpenAI-compatible endpoint. Its settings: `HUGGINGFACE_API_KEY`, timeout, `max_tokens`, and
  `enable_thinking: false` for Qwen3.
- The new agents use a plain `chat(spec, messages) → Reply(text, usage)` interface
  (`app/agents/llm_chat.py`), not a LangChain wrapper (owner, 2026-11-23). No new dependency.
  Usage metadata still reaches MLflow.

**Tests:** recorded HF and Ollama responses under `app/tests/fixtures/`; no live calls.

**CLAUDE.md edits** (owner-approved; applied in PR 1):

- *Product framing*, first paragraph →
  > The hero metric is **what you can cook from grocery sales near you while meeting your dietary
  > restrictions**. Cost is reported, never a constraint. Deal data mostly lacks a reliable regular
  > price, so savings is a secondary metric and must degrade to absent rather than to a fabricated
  > number.
- The rule paragraph that begins **"No name, keyword or substring matching anywhere downstream
  of product review"** →
  > **No name, keyword or substring matching may grant, deny or veto a deal or recipe on dietary
  > grounds** — not as a filter, a veto, a review alarm or a backfill heuristic. An ingredient or
  > deal *name* never decides dietary access. Item/role labels that decide only what is *surfaced*
  > (grouping, trays, price comparison) are allowed, but they must be stored data from the
  > labelling job (`deal_labels`), never string matching in code, and they apply only to the
  > already-gated deal set.

## 11. Frontend (`frontend/src/features/planner`)

`PlannerPage` becomes a three-step flow.

**1. Trays.** Calls `POST /plans/trays`.
- Shows three carousels: **Anchors**, **Sides**, **Stock-up**.
- Each card shows the item, deal name, store, price, and "cheapest of N stores (median $X/lb)".
- Every card starts kept, with a reject toggle.

**2. Pantry.** A checklist of the pantry staples, all checked by default.

**3. Generate.**
- Sends the kept ids and the pantry selection.
- Uses the existing `GenerationProgress` and `RecipeCard`.
- If no anchor is kept, the generate button is disabled (the server also returns 422).

Types for the new request and response fields go in the frontend API types.

## 12. What is removed

- `ChefOrchestrator`: `initialize`, `plan_ingredient_groups`, `handle_rejections`.
- `load_recipe_catalog` as a graph node.
- The Nutritionist agent and its role.
- The `discount_percentage` ranking and `LIMIT 200` in `fetch_current_deals`.
- The old prompts in `prompts.py`.
- Tests for these are deleted or rewritten. The eval harness (`app/eval/`) moves to the new graph
  with a "keep everything offered" confirmation, and its snapshots carry labels.

## 13. Build order (PR-sized)

1. **Labels.**
   - Migration 024 (file only; Neon after the owner's OK).
   - The `llm_factory` HF provider.
   - `deal_labeller.py` + `scripts/label_deals.py`, with recorded-fixture tests.
   - The CLAUDE.md edits.
   - Then, with the owner's OK: apply 024 to Neon and label the current L4G3E8 deals.
2. **Trays.**
   - `trays.py`, and `fetch_current_deals` without the ranking/LIMIT.
   - `POST /plans/trays`, with tests on a frozen snapshot.
3. **New generation graph.**
   - Re-gate and validate, plan dishes, per-dish SousChef, verdict, per-dish retry, finalize.
   - `POST /recipes/generate` takes the confirmed set.
   - The old nodes are removed in this PR, so two graphs never coexist.
   - Eval harness moved over.
4. **Carousel UI**, plus the frontend types.
5. **Cleanup:** dead prompts and settings, and stale docs (`.env.example`, SETUP.md). The
   coordinator updates the ROADMAP.

PRs 2 and 4 can run in parallel worktrees once PR 1 has merged. PR 3 depends on 1 and 2. Only
PR 1's Neon step writes to the database.

## 14a. As built (PR 3) — differences from the text above

- **Selection checks run inside the job, not as a 422.** `kept_deal_ids` / `pantry_product_ids`
  are intersected with freshly rebuilt trays. Ids no longer offered are dropped and reported as a
  warning, never trusted. No kept anchor fails the job with the reason. Omitting either field
  means "keep everything offered", so the API works before the carousel exists.
- **The old LangGraph pipeline is gone (PR 5).** The API and the eval harness both run
  `app/agents/plan_pipeline.py`. Eval snapshots are format v3 and carry `deal_labels`; the
  recorder wraps the plain `chat()` interface; run artifacts are format v2; the scorers report
  cost instead of a budget (score schema v3). `prompts.PromptTemplates` and the old
  `llm_output` schemas stay only because `training/` imports them.
- **Planner repairs instead of failing (PR 5, from live checks).** Stray side / pantry /
  stock-up ids are dropped and recorded; a kept side cited as an anchor moves to the sides; a
  dish with no ANCHORS id is dropped, never given a protein the model didn't choose. A plan can
  therefore come back short of `num_meals`, and the 60% partial rule applies. When there are
  fewer anchors than meals, the prompt says so and lists the anchor ids to reuse.
- **SIDES quota (PR #64):** vegetable 12, starch 5, fruit 4, dairy 4; one row per item per tray.
- **Display names come from citations.** Each ingredient is shown as the cited staple's name or
  the deal's label `item`, and steps render the same way. The model's wording is kept as
  `written_as`. Display only.
- Settings: `LLM_PLANNER_MODEL`, `LLM_WRITER_MODEL` (default `ollama:qwen2.5:7b`).

## 14. Answered questions (owner, 2026-11-23)

- §4: **dairy joins SIDES.**
- §5: **one anchor may serve more than one dish**; only "no anchor kept" is refused.
- §10: **plain `chat()` interface**, no LangChain wrapper.
- **The labeller runs automatically after every ingestion** (`DEAL_LABELLING_ENABLED`, on by
  default; background thread after `POST /postal-code/refresh`, synchronous in
  `scripts/ingest_deals.py`). It never runs at request time and never fails ingestion.
