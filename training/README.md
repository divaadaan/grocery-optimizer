# Training

SFT pipeline for the recipe agents.

- **Step 1 — `reproduce_paper/`**: reproduces arXiv 2502.02028 — fine-tuning
  SmolLM models on Food.com recipe generation. Teaches recipe *form*; shown not
  to move the dietary/allergen failure mode (see `ROADMAP.md` item 5).
- **Step 2 — `data_app/` (removed 2026-11-23)**: built an app-targeted SFT
  dataset through the old Chef / SousChef / Nutritionist prompts and schemas.
  Those agents were replaced by approach C (`experiments/chef/DESIGN_APPROACH_C.md`)
  and dietary compliance is a deterministic lookup, so `data_app/` was retired
  with them. It is in git history (last on `main` before the retirement PR).

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
  through `evaluate_app.py --ollama-model`. That was superseded too: the app
  moved off small fine-tuned models (2026-10-15) and onto approach C
  (2026-11-23), and `data_app/` was removed. The paper-repro scaffold
  (`reproduce_paper/`) remains as reference.

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

## Step 2 — app-targeted dataset (retired)

`data_app/` rendered examples through the app's old `PromptTemplates` and
validated them with the old `ChefPlan` / `RecipeBatch` / `NutritionistVerdict`
schemas. Both the agents and those schemas are gone from `app/`, and the
`*-app-*` configs in `reproduce_paper/configs/` only make sense with a dataset it
built. Recover it from git history if the track is ever revived; it would need
rewriting against `app/agents/dish_planner.py` / `dish_writer.py` first.
