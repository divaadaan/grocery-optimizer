# `data_app` — status & next steps

App-targeted SFT dataset (ROADMAP item 5, step 2). This doc tracks the phased
plan; the package overview lives in `training/README.md` ("Step 2").

## Phase 1 — dataset builder ✅ (done 2026-06-15)

`build_app_dataset.py` renders examples through the app's real
`PromptTemplates` and gates every completion through the real
`ChefPlan`/`RecipeBatch`/`NutritionistVerdict` schemas. Hybrid labelling:
`chef_plan` + `nutritionist_verdict` programmatic (zero label noise);
`sous_chef_recipe` distilled from `qwen2.5:7b` and filtered through
`RecipeBatch` + a forbidden-term rule check.

Verified full build: **115 examples** (24 chef + 67 nutritionist + 24 distilled),
**0 dropped by the schema gate**; 0 chef completions select a forbidden product;
0 inconsistent nutritionist verdicts; app unit tests still 26/26.

## Phase 2 — train on prompt/completion with loss masking (next)

Goal: train only on the *completion* tokens, not the prompt (the prompt embeds
the big `deals_json` blob — we must not teach the model to echo it).

1. **`train_sft.py` config flag** `dataset_format: prompt_completion` (default
   stays the current `text` path, so step-1 configs are untouched).
2. **New `load_splits` branch** that reads `training/data/app/{train,validation}.jsonl`
   and yields TRL `prompt`/`completion` columns instead of a single `text` field.
   Keep the `remove_columns` / smoke-subset logic consistent with the existing
   branch.
3. **Completion-only loss masking.** With TRL's prompt/completion dataset the
   trainer masks the prompt automatically; confirm the installed TRL version does
   this (else use a response-template collator). Add an assertion/print of the
   masked token count on a sample so we can eyeball that the `deals_json` region
   is masked.
4. **New app configs** `configs/smollm-360m-app-qlora.yaml` (and a 135M variant)
   pointing `data_dir: training/data/app`, `dataset_format: prompt_completion`,
   shorter run (the corpus is small — start ~3–5 epochs, watch eval loss for
   overfit given the low-diversity catalog; see caveats below).

Acceptance for Phase 2: a smoke run trains and a real run converges without
echoing the deals blob in greedy generation.

## Phase 3 — deterministic task eval + flip the xfail tests

`evaluate_app.py`: load a tuned model (merged, or via Ollama once exported) and
score the held-out `test.jsonl` deterministically:

- **JSON-validity rate** per task (does output parse + validate through the
  task's schema).
- **Chef dietary-violation rate** — port the `forbidden_terms` assertion from
  `test_chef_groups_respect_vegetarian_restriction`: count completions that put
  a forbidden product in any group.
- **Nutritionist confusion matrix** incl. **false-rejection rate** on APPROVE
  controls (the over-strictness mode), not just violation catches.

**Acceptance bar (the whole point):** with the SFT'd models wired into `.env`,
the two `xfail` tests flip to passing —
`test_chef_groups_respect_vegetarian_restriction` and the nutritionist APPROVE
controls in `test_nutritionist_regression.py`.

## Dataset caveats / expansion levers

- **Low diversity.** All deals come from one postal code (M5V3A8, 27 products).
  The targeted failure modes are catalog-specific so this is acceptable for the
  acceptance bar, but watch for overfitting to these exact product strings.
  Levers when we want more: add `PROFILES` (more restriction combos),
  raise `--chef-per-profile` / `--nutritionist-per-profile` / `--sous-per-group`,
  and (biggest) add more seed catalogs once real Flipp ingestion lands
  (ROADMAP item 4) and point `--fixture` at them.
- **Distilled split is non-deterministic** (depends on the live teacher); the
  programmatic splits are seeded and reproducible. `dataset_stats.json` records
  the teacher model + base_url for provenance. Re-running overwrites
  `training/data/app/` (gitignored).
- **Single source of truth.** Deals + vegetarian forbidden list come from
  `app/tests/fixtures/nutritionist_cases.json`; `seed_catalog.load_fixture`
  asserts the vegetarian terms match, so the data can't silently drift from the
  acceptance tests.

## Dependency: Phase 0 export-path validation ✅ (done 2026-06-15)

Validated end-to-end on the `smollm-360m-qlora` adapter: `merge_lora` → GGUF
(llama.cpp `convert_hf_to_gguf`, f16) → HF Hub (`danieldsachs/smollm-360m-recipe`)
→ `ollama run hf.co/...` into the app's Ollama instance. Coherent on-format
output; the greedy-decoding repetition loop from `evaluate.py` does not appear
under Ollama's default sampling. GGUF conversion needs `pip install sentencepiece`
(the converter's SP-first vocab probe imports it before falling back to SmolLM's
BPE path). So Phase 2's QLoRA→serve path is unblocked.
