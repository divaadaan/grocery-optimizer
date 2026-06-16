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

## Phase 2 — train on prompt/completion with loss masking ✅ (done 2026-06-16)

Goal: train only on the *completion* tokens, not the prompt (the prompt embeds
the big `deals_json` blob — we must not teach the model to echo it).

Shipped as **conversational prompt-completion**, not raw concatenation. Tracing
the app's real inference path showed it serves these agents via `ChatOllama` with
a single `HumanMessage(content=prompt)` (`app/agents/llm_output.py`), so the model
sees the prompt wrapped in a chat template with an assistant turn appended. Two
findings drove the format choice:

- **Raw concat masked the opening brace.** With raw `prompt + completion`, SmolLM
  BPE merges the prompt's trailing `:` with the completion's leading `{` into one
  `:{` token that lands in the masked region — so the model got no loss on
  emitting the first `{` (and JSON-validity is a target failure mode). TRL also
  logged a prompt/prompt+completion "Mismatch" warning per example.
- **Raw concat didn't match inference.** The app never concatenates; it applies a
  chat template. Training raw would diverge from how the served model is actually
  prompted.

Conversational format fixes both: the assistant header is a clean token boundary
(no `:{` merge, `{` is trained, warning gone) and the wrapped prompt matches
`ChatOllama` byte-for-byte. Verified mask on a sample: `623/812 masked (prompt),
189 trained (completion)`, trained region starts at `{`, masked tail ends
`...now:<|im_end|>\n<|im_start|>assistant\n`.

What landed:

1. **`Example.to_record()`** now emits `{task, prompt: [user turn], completion:
   [assistant turn], meta}`; existing splits converted in place (distilled
   examples preserved).
2. **`train_sft.py` `dataset_format: conversational`** branch (default stays the
   `text` path, step-1 configs untouched): keeps TRL's `prompt`/`completion`
   columns, sets `completion_only_loss=True`. TRL 0.25.1 masks the prompt
   automatically for prompt-completion datasets (confirmed — no custom collator).
3. **Masking sanity print** of the `completion_mask` token split, plus an abort if
   `max_length` truncates the whole completion away.
4. **New app configs** `configs/smollm-360m-app-qlora.yaml` + `smollm-135m-app-full.yaml`,
   on the **Instruct** base (required for the chat template), `data_dir:
   training/data/app`, `dataset_format: conversational`, 4 epochs, `max_length:
   4224` (chef examples top ~4.1k tokens; smaller right-truncates the completion),
   gradient checkpointing + batch 1 so they fit the 8 GB 4060 (no rental).

Acceptance met: smoke run trains end-to-end, masking confirmed. *Still to do:* a
real run + confirm no deals-blob echo in generation (rolls into Phase 3 eval).

*Real-run finding (2026-06-16):* first 360M run **completed but produced garbage**
(chef → repeated `"`; nutritionist → reverted to base Instruct essays) despite a
healthy-looking eval_loss 1.57. Two causes: (1) **wrong base — context overflow.**
The original `SmolLM-360M-Instruct` is v1 with a **2048-token context**; chef
prompts are ~2.8k and prompt+completion ~4.1k, so they overflow the window and
RoPE silently extrapolates into garbage (falling train loss hides it). Fixed by
switching to **SmolLM2** (8192 ctx). (2) **undertrained** — 4 epochs = only ~24
optimizer steps; bumped to 12. Lesson: a smoke run that trains and a loss that
falls do **not** prove the model works — qualitative generation on held-out
prompts is the real check (and Phase 3's deterministic eval is the bar).

## Decision: move off the small SmolLM models

The SmolLM SFT track is **concluded.** 135M/360M produce on-format JSON but can't
internalize the dietary semantics from this corpus (see the Phase 3 finding
below), so **no further SFT on these small models is planned.** The data_app
dataset stays the **evaluation harness** — its test split feeds
`evaluate_app.py` — regardless of which base we serve.

**Next attempt: `qwen2.5:1.5b-instruct`** (already pulled locally). Evaluate it
*untuned* first via `evaluate_app.py --ollama-model qwen2.5:1.5b-instruct`
(app-faithful Ollama path). A stronger base may satisfy the dietary/JSON bar with
**no SFT at all** — a plain `.env` swap — because the compliance judgment is
mostly world knowledge it already has (it "knows" avocado is vegan, milk is fine
for vegetarians). Only revisit SFT if the untuned swap still fails the proxies.

If another candidate is ever needed, the hard requirements are: an Instruct
variant (chat template, to match `ChatOllama`), ≥8k context (chef prompt ~2.8k
tokens), and an Ollama serving path — e.g. `qwen2.5:3b-instruct` (32k ctx) or
`llama3.2:3b-instruct` (128k ctx).

A retrieval-augmented direction (seeding generation with real recipes matched to
the on-hand ingredients) is under separate discussion as an alternative/complement
to relying on the base model alone.

## Phase 3 — deterministic task eval + flip the xfail tests

`evaluate_app.py` ✅ built (2026-06-16). Scores the held-out `test.jsonl` and
ports the exact assertions from `app/tests/test_nutritionist_regression.py`:

- **JSON-validity rate** per task (parse + validate through the task's schema).
- **Chef dietary-violation rate** — the `forbidden_terms` assertion from
  `test_chef_groups_respect_vegetarian_restriction`, run over the *parsed*
  `ingredient_groups[*].product_name` (not raw text).
- **Nutritionist confusion matrix** incl. **false-rejection rate** on APPROVE
  controls (the over-strictness mode), plus false-approval on REJECT cases.
- **Acceptance-bar proxies** for the two xfail tests, with a would_pass flag.

Two model paths: `--model <dir>` (local HF/adapter, greedy, *intrinsic*) and
`--ollama-model <name>` (served via Ollama with `format="json"` + the app's
per-agent temperatures — *app-faithful*, predicts the xfail flip, and the path for
untuned base-swap candidates). `generate_app.py` stays the qualitative companion.

**Acceptance bar (the whole point):** with the SFT'd models wired into `.env`,
the two `xfail` tests flip to passing —
`test_chef_groups_respect_vegetarian_restriction` and the nutritionist APPROVE
controls in `test_nutritionist_regression.py`.

*First eval (2026-06-16, SmolLM2-360M-app-qlora, HF greedy):* both proxies STILL
FAILING. chef 0/2 valid (wrong top-level shape — array not object). nutritionist
6/6 valid JSON but **false_rejection_rate = 1.0** — it rejects every compliant
recipe by *hallucinating* violations ("milk/cheese violate vegetarian", "avocado/
tomato-sauce violate vegan" — all false). Decisive lesson: **valid JSON ≠ correct
answer** — the model learned the rejection *template* but not the dietary
*semantics*, and a qualitative eyeball of one REJECT case had falsely read as
"solved". 360M can't internalize the compliance mapping from ~55 examples;
train balance was approve-weighted (33/22), so this is capacity, not data skew.
Decisive signal for the more-capable-base lever — next: eval an untuned
`qwen2.5:1.5b-instruct` via the `--ollama-model` path (it likely already *knows*
avocado is vegan), **not** more small-model SFT (see "Decision" above).

## Phase 4 — inference-time RAG recipe generation (after the qwen2.5:1.5b trial)

Pursues the same dietary/JSON failure modes from a different angle: instead of
teaching a model the dietary judgment (which small-model SFT couldn't do), make
compliance a **deterministic code path** and let a capable base adapt *real*
recipes. **Inference-time only — no SFT.** This phase graduates beyond `data_app`
into the app's LangGraph runtime; `data_app` + `evaluate_app.py` become the
regression harness. Generator = whichever base the qwen2.5:1.5b trial selects.

**4a · Recipe corpus + retrieval index.** Index Food.com (`RAW_recipes.csv`, 231k,
already in the pipeline) as the v1 corpus — one format, no cross-source parsing
yet. Retrieval is lexical / set-overlap (recipes that *use* the on-hand deal
ingredients), scored by coverage of on-hand ingredients minus a penalty for many
missing ones (set-cover / Jaccard, with pantry-staple tolerance) — not dense
similarity. **The hard part is ingredient normalization:** canonicalize deal
product names and recipe ingredient tokens to one vocabulary (extend
`seed_catalog.categorize` / a normalization layer). "Chicken Breast Boneless" ≈
"boneless skinless chicken breasts" ≈ "chicken".

**4b · Substitution table (offline-generated, deterministically applied).**
Generate `forbidden_ingredient → ranked compliant substitutes` per restriction
with a strong model **offline**, and FILTER every entry through
`is_compliant`/`violating_terms` before it enters the table — correct-by-
construction, the same distill-then-gate pattern as the sous-chef data (stops the
model offering ghee as a vegan butter sub). Bounded by the forbidden ingredients
(meat/poultry/fish, dairy, egg, honey, gelatin…), grown lazily from retrieval
misses. At inference it's a dict lookup: the table picks the *ingredient* swap,
the generator rewrites the *prose/technique*. The strong model never touches the
hot path — which is why model-assisted substitution was rejected (it would put the
expensive base back into per-request inference, the thing we're avoiding).

**4c · Wire into the graph.** Reshape the SousChef from "invent a recipe" to
"adapt a retrieved + substituted one". Add a **proactive substitution node before
generation** (replacing the Nutritionist's reactive reject→retry for dietary
issues; the Nutritionist stays the final safety gate). Retrieval + substitution
are pure functions of (deals, restrictions) → naturally memoizable (see 4e).

**4d · Regression / integration testing (property-based).** No human golden labels
— assert *properties* against the deterministic oracle we already trust: retrieval
covers the on-hand ingredients; post-substitution recipes pass `is_compliant`;
end-to-end plans are schema-valid AND dietary-clean (literally `evaluate_app.py`'s
checks). The `data_app` test split + Food.com index are the regression corpus;
expand via more postal codes / profiles / catalogs.

**4e · Response cache (stage N).** Normalized cache keyed by `(postal-code /
deal-set version, sorted(restrictions), budget bucket, household size)`. The deal
set is shared across everyone in a postal code for the week, so reuse across users
(two vegetarian couples, same area, same week) is the *common* case, not a
coincidence. TTL ties to the deal-refresh cycle (ROADMAP item 4) — invalidate on
catalog change, not a timer. Retrieval + substitution are already memoizable; cache
the LLM generation. For variety, cache the deterministic recipe *set* and re-roll
only the cheap final assembly.
**Flywheel:** every cached generation that passes the compliance gate is a real
`(query → validated meal plan)` pair. Because deal sets are shared per
postal-code-week, real-traffic examples accumulate fast — feeding the regression
corpus automatically, and the training set if a trainable base is ever revisited.

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
