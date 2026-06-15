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
- **Next:** full 231k-corpus splits regenerated; 360M + 1.7B QLoRA configs set to
  log TensorBoard and ready for a fresh rental. QLoRA runs produce adapters, so
  `merge_lora` before evaluating/exporting. Re-evaluate the 135M against the new
  full-corpus `test.jsonl` (`--output-dir ...eval-fullcorpus`) for an
  apples-to-apples `compare_eval` across all three sizes.

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
`{task, prompt, completion, meta}` records, a combined `app_sft.jsonl`, and
`dataset_stats.json` to `training/data/app/`. A completion that fails its schema
is dropped and counted, never written.

*Not yet wired:* `train_sft.py` consuming the `prompt`/`completion` columns with
completion-only loss masking (Phase 2) — these records are emitted in that shape
ready for it.

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
