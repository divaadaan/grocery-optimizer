# Training

SFT pipeline for the recipe agents.

- **Step 1 — `reproduce_paper/`**: reproduces arXiv 2502.02028 — fine-tuning
  SmolLM models on Food.com recipe generation. Teaches recipe *form*; shown not
  to move the dietary/allergen failure mode (see `ROADMAP.md` item 5).
- **Step 2 — `data_app/`**: builds an app-targeted SFT dataset rendered through
  the app's *actual* `PromptTemplates` and validated through its *actual*
  Pydantic schemas, targeting the failure modes found in verification
  (structured-JSON output, dietary-restriction compliance, nutritionist false
  rejections). See "Step 2" below.

Rules of the split with `app/`:

- **`app/` never imports from `training/`.** The app only ever consumes the
  result as an Ollama model name in `.env`.
- **`training/` has its own venv** (`training/requirements.txt`); never merge
  these deps into the root `requirements.txt` or the Docker image.

## Setup (WSL)

The system default python3 is 3.14, which torch/bitsandbytes don't ship wheels
for yet — use 3.12:

```bash
cd ~/projects/grocery-optimizer
python3.12 -m venv training/.venv
source training/.venv/bin/activate
pip install -r training/requirements.txt
```

GPU: RTX 4060 (8 GB) via WSL CUDA passthrough. Fits full FT of SmolLM-135M and
QLoRA on 360M/1.7B (1.7B needs gradient checkpointing, already set in its config).

## Data

Food.com dataset (Majumder et al., 2019; 231,637 recipes). Get `RAW_recipes.csv`
from Kaggle into `training/data/raw/`:

```bash
kaggle datasets download -d shuyangli94/food-com-recipes-and-user-interactions \
    -f RAW_recipes.csv -p training/data/raw --unzip
```

(or download manually from kaggle.com/datasets/shuyangli94/food-com-recipes-and-user-interactions)

Then build the 80/10/10 splits:

```bash
python -m training.reproduce_paper.prepare_data                  # full corpus (paper large-scale)
python -m training.reproduce_paper.prepare_data --sample 100000  # paper small-scale corpus
```

## Train

Always smoke-test the pipeline first (20 steps on 512 examples, ~a minute):

```bash
python -m training.reproduce_paper.train_sft \
    --config training/reproduce_paper/configs/smollm-135m-full.yaml --smoke
```

Real runs:

```bash
python -m training.reproduce_paper.train_sft --config training/reproduce_paper/configs/smollm-135m-full.yaml
python -m training.reproduce_paper.train_sft --config training/reproduce_paper/configs/smollm-360m-qlora.yaml
python -m training.reproduce_paper.train_sft --config training/reproduce_paper/configs/smollm-1.7b-qlora.yaml
```

Set `report_to: mlflow` in a config to log into the project's MLflow.

## Evaluate

Perplexity, BLEU, ROUGE-1/2/L, and the paper's domain metrics (ingredient
coverage, step count, temperature/time mentions) on the first 500 test samples:

```bash
python -m training.reproduce_paper.evaluate --model training/runs/smollm-135m-full
python -m training.reproduce_paper.evaluate --model HuggingFaceTB/SmolLM-135M   # untuned baseline
```

LLM-as-judge (paper used Qwen2.5-7B; we use the local Ollama `qwen2.5:7b` —
note the two-Ollama-instances quirk on this machine, make sure `OLLAMA_BASE_URL`
points at the instance that has the model):

```bash
python -m training.reproduce_paper.llm_judge \
    --generations training/runs/smollm-135m-full/eval/generations.jsonl
```

From WSL, `OLLAMA_BASE_URL` must point at the *network-exposed* Windows Ollama
via the gateway IP (the loopback instance isn't reachable from WSL and lacks the
model) — same value as `.env`'s `OLLAMA_BASE_URL`/`DOCKER_OLLAMA_URL`. `llm_judge`
does not read `.env`, so export it or pass `--base-url`:

```bash
export OLLAMA_BASE_URL=http://172.18.128.1:11434   # gateway IP; re-derive after reboot
```

Then render any evaluated runs (and their judge scores, if present) side by side:

```bash
python -m training.reproduce_paper.compare_eval                    # the two default 135M dirs
python -m training.reproduce_paper.compare_eval DIR_A DIR_B ...    # explicit eval dirs
```

## Status

- **SmolLM-135M full-FT** (100k-sample corpus, 3 epochs): trained on a rented
  vast.ai 4090 (~67 min, see `CLOUD.md`), evaluated vs the untuned baseline.
  Strong fluency/format gains (perplexity 12.3→4.7, BLEU 2.4→12.1, judge overall
  2.64→3.52); allergen-safety judge dim flat (2.86→2.86) — Food.com SFT improves
  recipe form, not the dietary/allergen failure mode (see `ROADMAP.md` item 5).
- **Conclusion (2026-06-16):** the small-model SFT track is **wound down.** Step-2
  (app-targeted, see below) trained SmolLM2-360M through the app's real prompts +
  schemas and scored it with `evaluate_app.py`: it emits on-format JSON but can't
  internalize the dietary *semantics* (the nutritionist false-rejects 100% of
  compliant recipes by hallucinating violations). ~360M params are simply
  undersized for this judgment. **No further SmolLM SFT is planned** — the next
  attempt is a more capable base, `qwen2.5:1.5b-instruct`, evaluated *untuned*
  through `evaluate_app.py --ollama-model` (see `data_app/NEXT_STEPS.md`). The
  paper-repro scaffold (`reproduce_paper/`) remains as reference.

## Export to Ollama

QLoRA → merge → GGUF → HF Hub → Ollama. Validated end-to-end 2026-06-15 on
`smollm-360m-qlora`.

**1. Merge the LoRA adapter into full weights** (skip for full-FT runs):

```bash
python -m training.reproduce_paper.merge_lora \
    --adapter training/runs/smollm-360m-qlora --output training/runs/smollm-360m-merged
```

**2. Convert to GGUF** with llama.cpp's converter (clone only — no C++ build
needed; quantization isn't worth it at 360M, f16 is ~724 MB):

```bash
git clone https://github.com/ggerganov/llama.cpp ~/llama.cpp
pip install gguf sentencepiece        # sentencepiece: the converter's SP-first
                                      # vocab probe imports it before falling
                                      # back to SmolLM's BPE path
python ~/llama.cpp/convert_hf_to_gguf.py training/runs/smollm-360m-merged \
    --outfile training/runs/smollm-360m-merged/smollm-360m-recipe-f16.gguf --outtype f16
```

**3. Push to HF Hub** (the bridge — Ollama pulls the GGUF over the internet, so
the file's WSL location doesn't matter):

```bash
huggingface-cli login   # write token
huggingface-cli upload danieldsachs/smollm-360m-recipe \
    training/runs/smollm-360m-merged/smollm-360m-recipe-f16.gguf smollm-360m-recipe-f16.gguf
```

**4. Pull + run.** Run `ollama` from *Windows* PowerShell so the model lands in
the single all-interfaces Ollama server the app reaches at the WSL gateway IP
(there's one server; the "ollama app" process is just the tray GUI):

```powershell
ollama run hf.co/danieldsachs/smollm-360m-recipe:f16 "Generate a recipe. ingredients: chicken, rice, soy sauce"
```

Greedy `evaluate.py` can show a repetition loop; Ollama's default sampling
(`repeat_penalty 1.1` + temperature) tames it. For tighter control, bake a
Modelfile (`PARAMETER repeat_penalty 1.3`, `repeat_last_n 64`, `num_predict 256`).

## Step 2 — app-targeted dataset (`data_app/`)

Renders training examples through the app's real prompts
(`app.agents.prompts.PromptTemplates`) and validates every completion through
the app's real Pydantic schemas (`app.agents.llm_output`: `ChefPlan`,
`RecipeBatch`, `NutritionistVerdict`) at build time — so train-time inputs match
inference byte-for-byte and no malformed completion can enter the corpus. The
import direction is training → app (the one the split rule allows); the schemas
import with just `pydantic` (the `langchain_core` import in `llm_output.py` is
lazy), which is why `pydantic` is in `requirements.txt`.

Hybrid labelling, one strategy per failure mode:

- **`chef_plan`** (programmatic): prompt shows *all* deals incl. chicken/beef/
  salmon; completion groups only the dietary-compliant ones into 3 complementary
  sets, with a rationale that states the exclusion. Zero label noise — the direct
  fix for `test_chef_groups_respect_vegetarian_restriction`.
- **`nutritionist_verdict`** (programmatic): REJECT recipes containing a
  forbidden ingredient (hard guards) + APPROVE compliant recipes incl. the
  minimal 2-ingredient control phi4-mini false-rejects (the over-strictness fix).
- **`sous_chef_recipe`** (distilled): recipe prose distilled from `qwen2.5:7b`
  via Ollama, kept only if it validates through `RecipeBatch` *and* passes a
  forbidden-term rule check.

Seed deals + the vegetarian forbidden list come from
`app/tests/fixtures/nutritionist_cases.json` — the same fixture the acceptance
tests read, so data and tests can't drift (enforced by an assertion in
`seed_catalog.load_fixture`).

```bash
# Offline (programmatic tasks only — no Ollama needed):
python -m training.data_app.build_app_dataset --no-distill

# Full build incl. distillation. From WSL, point --base-url at the
# network-exposed Windows Ollama (gateway IP, same as .env's OLLAMA_BASE_URL):
python -m training.data_app.build_app_dataset --base-url http://172.18.128.1:11434
```

Writes 80/10/10 `train`/`validation`/`test.jsonl` (stratified by task) of
conversational records — `{task, prompt: [user turn], completion: [assistant
turn], meta}` — a combined `app_sft.jsonl`, and `dataset_stats.json` to
`training/data/app/`. A completion that fails its schema is dropped and counted,
never written. The prompt/completion are message lists (not raw strings) because
the app serves these agents via `ChatOllama` with a single `HumanMessage`, so the
training format mirrors inference once the chat template is applied (see Step 2
training below).

### Training on the app dataset

`train_sft.py` consumes these records via `dataset_format: conversational`: it
keeps TRL's `prompt`/`completion` columns, applies the model's chat template, and
masks everything up to the assistant header so loss falls only on the completion
(it never learns to echo the big `deals_json` blob in the prompt). It prints the
masked/trained token split of a sample and aborts if `max_length` truncates the
whole completion away. Requires an **Instruct** base — base SmolLM has no chat
template.

```bash
python -m training.reproduce_paper.train_sft \
    --config training/reproduce_paper/configs/smollm-360m-app-qlora.yaml --smoke
python -m training.reproduce_paper.train_sft \
    --config training/reproduce_paper/configs/smollm-360m-app-qlora.yaml   # ~10 min locally
python -m training.reproduce_paper.train_sft \
    --config training/reproduce_paper/configs/smollm-135m-app-full.yaml
```

These configs set `max_length: 4224` (chef prompt+completion tops ~4.1k tokens —
a smaller cap right-truncates the completion away) with gradient checkpointing
and batch 1, so they fit the 8 GB 4060 without a rental. Use a **SmolLM2** base,
not v1 — v1's 2048-token context can't hold the ~2.8k-token chef prompts and RoPE
extrapolates them into garbage (falling loss won't reveal it).

### Inspecting what the model actually generates

A loss that falls does **not** prove the model learned the format — check the
generations. `generate_app.py` runs a tuned model over the held-out split and
writes the prompt / generation / reference plus per-task JSON-validity (and chef
dietary-violation) to disk, and streams the same to stdout:

```bash
python -m training.data_app.generate_app --model training/runs/smollm-360m-app-qlora
python -m training.data_app.generate_app --model training/runs/smollm-360m-app-qlora \
    --task chef_plan --temperature 0.7        # one task, with sampling
```

The base is auto-detected from `adapter_config.json` (or pass `--base`). Output
lands in `<model>/eval_app/{generations.jsonl,generations.txt}`.

### Deterministic task eval (Phase 3)

`evaluate_app.py` scores the held-out split and reports the acceptance-bar
metrics ported from `app/tests/test_nutritionist_regression.py`: per-task
JSON-validity, chef dietary-violation rate (the `forbidden_terms` assertion over
parsed `ingredient_groups`), and the nutritionist confusion matrix +
false-rejection rate — plus a would_pass proxy for each of the two `xfail` tests.

```bash
# intrinsic (local adapter, greedy):
python -m training.data_app.evaluate_app --model training/runs/smollm-360m-app-qlora

# app-faithful (served via Ollama, format=json + app temps) — predicts the xfail
# flip, and the way to eval an untuned base-swap candidate:
python -m training.data_app.evaluate_app --ollama-model qwen2.5:1.5b-instruct \
    --base-url http://172.18.128.1:11434
```

Writes `metrics.json` + `report.txt`. `generate_app.py` is the qualitative
companion (full prompt/generation/reference dumps); `evaluate_app.py` is the
aggregate scorer. **A loss that falls, or even valid JSON, does not mean the task
is solved** — the first 360M eval was 6/6 valid JSON on the nutritionist yet
false-rejected 100% of compliant recipes. See `data_app/NEXT_STEPS.md`.

## Known divergences from the paper

- `<|startoftext|>` is a literal string, not a registered special token (see
  `formatting.py` docstring for why).
- LoRA alpha is unstated in the paper; we use 16 (2×r).
- Small-scale epoch count is unstated; the 135M config uses 3.
- Decoding params for evaluation are unstated; we use greedy decoding.
- Per-device batch 8 × grad-accum 4 is our reading of the paper's
  "batch size 32, gradient accumulation 4".
- We start with the SmolLM family only (skipping GPT-2/T5/Phi-2), since SmolLM
  is the model family we plan to serve.
