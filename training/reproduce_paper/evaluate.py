"""Evaluate a recipe-generation model per arXiv 2502.02028.

Metrics:
- Perplexity over full formatted test examples
- Corpus BLEU (sacrebleu) and ROUGE-1/2/L against reference instructions
- Domain metrics: ingredient coverage, mean step count, % mentioning a
  temperature, % mentioning a time

Generation is greedy (the paper does not specify decoding params; greedy keeps
runs deterministic and comparable). Evaluates the first --num-samples test
examples (paper: 500). Writes generations.jsonl (input for llm_judge.py) and
metrics.json into --output-dir.

Usage (from repo root, training venv active):
    python -m training.reproduce_paper.evaluate --model training/runs/smollm-135m-full
    python -m training.reproduce_paper.evaluate --model HuggingFaceTB/SmolLM-135M   # untuned baseline
"""

import argparse
import json
import re
from pathlib import Path

import torch
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

try:
    from .formatting import build_full_text, build_prompt, format_steps
except ImportError:
    from formatting import build_full_text, build_prompt, format_steps

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPERATURE_RE = re.compile(r"\b\d{2,3}\s*(?:degrees?|°)\s*[cf]?\b|\bfahrenheit\b|\bcelsius\b", re.I)
TIME_RE = re.compile(r"\b\d+\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?)\b", re.I)
STEP_NUMBER_RE = re.compile(r"\b\d+\.\s")


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def load_model_and_tokenizer(model_arg: str, device: str):
    """Accepts a local run dir (full or LoRA-adapter) or a HF Hub model id."""
    local = resolve(model_arg)
    source = str(local) if local.exists() else model_arg
    if local.exists() and (local / "adapter_config.json").exists():
        from peft import AutoPeftModelForCausalLM

        model = AutoPeftModelForCausalLM.from_pretrained(source, dtype=torch.float16)
        model = model.merge_and_unload()
    else:
        model = AutoModelForCausalLM.from_pretrained(source, dtype=torch.float16)
    tokenizer = AutoTokenizer.from_pretrained(source)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    return model.to(device).eval(), tokenizer


def compute_perplexity(model, tokenizer, examples, device: str, max_length: int) -> float:
    total_loss, total_tokens = 0.0, 0
    for ex in tqdm(examples, desc="perplexity"):
        text = build_full_text(ex["name"], ex["ingredients"], ex["steps"], tokenizer.eos_token)
        ids = tokenizer(text, return_tensors="pt", truncation=True, max_length=max_length)
        ids = {k: v.to(device) for k, v in ids.items()}
        n_tokens = ids["input_ids"].shape[1]
        if n_tokens < 2:
            continue
        with torch.no_grad():
            loss = model(**ids, labels=ids["input_ids"]).loss
        total_loss += loss.item() * (n_tokens - 1)
        total_tokens += n_tokens - 1
    return float(torch.exp(torch.tensor(total_loss / total_tokens)))


def generate(model, tokenizer, examples, device: str, batch_size: int, max_new_tokens: int):
    tokenizer.padding_side = "left"
    generations = []
    for start in tqdm(range(0, len(examples), batch_size), desc="generating"):
        batch = examples[start : start + batch_size]
        prompts = [build_prompt(ex["name"], ex["ingredients"]) for ex in batch]
        inputs = tokenizer(prompts, return_tensors="pt", padding=True,
                           truncation=True, max_length=512).to(device)
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )
        decoded = tokenizer.batch_decode(out[:, inputs["input_ids"].shape[1]:],
                                         skip_special_tokens=True)
        for ex, prompt, text in zip(batch, prompts, decoded):
            generations.append({
                "name": ex["name"],
                "ingredients": ex["ingredients"],
                "prompt": prompt,
                "generated": text.strip(),
                "reference": format_steps(ex["steps"]),
            })
    return generations


def ingredient_coverage(ingredients: list[str], generated: str) -> float:
    """Fraction of ingredients mentioned in the generation. An ingredient
    counts if its full string or its final word (>=3 chars, usually the head
    noun: 'boneless chicken breast' -> 'breast') appears, case-insensitive."""
    if not ingredients:
        return 0.0
    text = generated.lower()
    covered = 0
    for ing in ingredients:
        ing = ing.lower().strip()
        head = ing.split()[-1] if ing.split() else ""
        if ing in text or (len(head) >= 3 and head in text):
            covered += 1
    return covered / len(ingredients)


def domain_metrics(generations: list[dict]) -> dict:
    coverage = [ingredient_coverage(g["ingredients"], g["generated"]) for g in generations]
    step_counts = [len(STEP_NUMBER_RE.findall(g["generated"])) for g in generations]
    has_temp = [bool(TEMPERATURE_RE.search(g["generated"])) for g in generations]
    has_time = [bool(TIME_RE.search(g["generated"])) for g in generations]
    n = len(generations)
    return {
        "ingredient_coverage": sum(coverage) / n,
        "mean_step_count": sum(step_counts) / n,
        "pct_with_temperature": sum(has_temp) / n,
        "pct_with_time": sum(has_time) / n,
    }


def text_metrics(generations: list[dict]) -> dict:
    import sacrebleu
    from rouge_score import rouge_scorer

    hyps = [g["generated"] for g in generations]
    refs = [g["reference"] for g in generations]
    bleu = sacrebleu.corpus_bleu(hyps, [refs]).score

    scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=True)
    totals = {key: 0.0 for key in ("rouge1", "rouge2", "rougeL")}
    for hyp, ref in zip(hyps, refs):
        scores = scorer.score(ref, hyp)
        for key in totals:
            totals[key] += scores[key].fmeasure
    return {"bleu": bleu, **{k: v / len(hyps) for k, v in totals.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="Run dir (full/LoRA) or HF Hub model id")
    parser.add_argument("--data-dir", default="training/data/foodcom")
    parser.add_argument("--output-dir", default=None,
                        help="Default: <model dir>/eval, or training/runs/eval-<id> for hub models")
    parser.add_argument("--num-samples", type=int, default=500)
    parser.add_argument("--max-new-tokens", type=int, default=400)
    parser.add_argument("--gen-batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, tokenizer = load_model_and_tokenizer(args.model, device)

    test_path = resolve(args.data_dir) / "test.jsonl"
    with open(test_path, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f][: args.num_samples]

    if args.output_dir:
        output_dir = resolve(args.output_dir)
    elif resolve(args.model).exists():
        output_dir = resolve(args.model) / "eval"
    else:
        output_dir = resolve("training/runs") / f"eval-{args.model.replace('/', '--')}"
    output_dir.mkdir(parents=True, exist_ok=True)

    perplexity = compute_perplexity(model, tokenizer, examples, device, args.max_length)
    generations = generate(model, tokenizer, examples, device,
                           args.gen_batch_size, args.max_new_tokens)

    metrics = {
        "model": args.model,
        "num_samples": len(examples),
        "perplexity": perplexity,
        **text_metrics(generations),
        **domain_metrics(generations),
    }

    with open(output_dir / "generations.jsonl", "w", encoding="utf-8") as f:
        for g in generations:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    with open(output_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print(json.dumps(metrics, indent=2))
    print(f"Wrote {output_dir / 'generations.jsonl'} and {output_dir / 'metrics.json'}")


if __name__ == "__main__":
    main()
