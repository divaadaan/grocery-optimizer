"""Qualitative generation check for app-targeted models (ROADMAP item 5, step 2).

Loads a tuned model (a QLoRA adapter dir or a merged/full model dir), generates
completions for the held-out app split, and writes everything to disk so the
results are inspectable — not summarized away. For every example it prints and
saves the prompt, the model's generation, the reference completion, whether the
generation parses + validates through the task's real Pydantic schema, and (for
chef_plan) any forbidden dietary terms it emitted.

This is the *qualitative* look — "does it produce on-format output at all". The
deterministic scoring + acceptance bar (flip the xfail tests) is Phase 3's
`evaluate_app.py`; this is its precursor and shares the same schema gate.

Usage (from repo root, training venv active):
    # adapter dir (base auto-detected from adapter_config.json):
    python -m training.data_app.generate_app --model training/runs/smollm-360m-app-qlora

    # merged/full model dir, only chef examples, with sampling:
    python -m training.data_app.generate_app --model training/runs/smollm-360m-merged \\
        --task chef_plan --temperature 0.7

Outputs (default under <model>/eval_app/):
    generations.jsonl  — one record per example (machine-readable)
    generations.txt    — the same, human-readable with separators
and the human-readable form is also streamed to stdout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from app.agents.llm_output import ChefPlan, NutritionistVerdict, RecipeBatch

REPO_ROOT = Path(__file__).resolve().parents[2]

TASK_SCHEMAS = {
    "chef_plan": ChefPlan,
    "nutritionist_verdict": NutritionistVerdict,
    "sous_chef_recipe": RecipeBatch,
}


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def extract_json(text: str) -> str | None:
    """Return the first balanced {...} or [...] block, or None.

    The app's invoke_validated does a strict json.loads on the raw output, so
    strict validity is the honest headline. This is only used to additionally
    report whether trailing prose was the *sole* reason a generation failed."""
    start = next((i for i, c in enumerate(text) if c in "{["), None)
    if start is None:
        return None
    open_c = text[start]
    close_c = "}" if open_c == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def validate(task: str, text: str) -> tuple[bool, str, bool]:
    """(strict_valid, error, valid_after_trim). strict_valid mirrors the app's
    json.loads(raw); valid_after_trim retries on the first JSON block only."""
    schema = TASK_SCHEMAS[task]
    try:
        schema.model_validate(json.loads(text))
        return True, "", True
    except Exception as e:
        strict_err = f"{type(e).__name__}: {str(e)[:200]}"
    block = extract_json(text)
    if block is not None:
        try:
            schema.model_validate(json.loads(block))
            return False, strict_err, True
        except Exception:
            pass
    return False, strict_err, False


def chef_violations(text: str, forbidden: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in forbidden if t.lower() in low]


def load_model(model_dir: Path, base_override: str | None, device: str):
    """Load an adapter dir (base auto-detected) or a full/merged model dir."""
    adapter_cfg = model_dir / "adapter_config.json"
    if adapter_cfg.exists():
        from peft import PeftModel

        base = base_override or json.loads(adapter_cfg.read_text())["base_model_name_or_path"]
        print(f"  adapter dir; base = {base}", flush=True)
        model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float16, device_map=device)
        model = PeftModel.from_pretrained(model, str(model_dir))
    else:
        print(f"  full model dir = {model_dir}", flush=True)
        model = AutoModelForCausalLM.from_pretrained(str(model_dir), dtype=torch.float16, device_map=device)
    # Tokenizer (with chat template) lives in the run dir for tuned models; fall
    # back to the base if not.
    tok_src = model_dir if (model_dir / "tokenizer_config.json").exists() else (
        base_override or json.loads(adapter_cfg.read_text())["base_model_name_or_path"]
        if adapter_cfg.exists() else str(model_dir)
    )
    tokenizer = AutoTokenizer.from_pretrained(str(tok_src))
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    return model.eval(), tokenizer


def render(rec: dict, prompt_chars: int) -> str:
    """Human-readable block for one example."""
    head = f"{'=' * 72}\n[{rec['index']}] task={rec['task']}  prompt_tokens={rec['prompt_tokens']}"
    flags = f"  VALID={rec['valid']}"
    if not rec["valid"] and rec["valid_after_trim"]:
        flags += " (valid after trimming trailing text)"
    if rec["task"] == "chef_plan":
        v = rec["violations"]
        flags += f"  forbidden_terms_emitted={v if v else 'none'}"
    lines = [head + flags]
    if rec["error"]:
        lines.append(f"  schema error: {rec['error']}")
    lines.append(f"\n--- PROMPT (last {prompt_chars} chars) ---\n{rec['prompt_tail']}")
    lines.append(f"\n--- GENERATION ---\n{rec['generation']}")
    lines.append(f"\n--- REFERENCE ---\n{rec['reference'][:1200]}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="adapter dir or full/merged model dir")
    parser.add_argument("--base", default=None, help="override base model (else read from adapter_config.json)")
    parser.add_argument("--data", default="training/data/app/test.jsonl")
    parser.add_argument("--task", default=None, choices=list(TASK_SCHEMAS), help="only this task")
    parser.add_argument("--limit", type=int, default=0, help="cap examples (0 = all)")
    parser.add_argument("--max-new-tokens", type=int, default=1536,
                        help="must exceed the longest reference (chef ~1.35k tokens) "
                             "or completions get cut off mid-JSON and falsely read as invalid")
    parser.add_argument("--temperature", type=float, default=0.0, help="0 = greedy")
    parser.add_argument("--repetition-penalty", type=float, default=1.1)
    parser.add_argument("--prompt-chars", type=int, default=220, help="prompt tail chars to show")
    parser.add_argument("--out-dir", default=None, help="default: <model>/eval_app")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_dir = resolve(args.model)
    print(f"Loading model ({device})…", flush=True)
    model, tokenizer = load_model(model_dir, args.base, device)

    rows = [json.loads(line) for line in open(resolve(args.data), encoding="utf-8")]
    if args.task:
        rows = [r for r in rows if r["task"] == args.task]
    if args.limit:
        rows = rows[:args.limit]
    print(f"Generating for {len(rows)} examples from {args.data}\n", flush=True)

    gen_kwargs = dict(
        max_new_tokens=args.max_new_tokens,
        repetition_penalty=args.repetition_penalty,
        pad_token_id=tokenizer.eos_token_id,
    )
    if args.temperature > 0:
        gen_kwargs.update(do_sample=True, temperature=args.temperature, top_p=0.9)
    else:
        gen_kwargs.update(do_sample=False)

    records = []
    for i, r in enumerate(rows):
        msgs = r["prompt"]                      # [{"role": "user", "content": ...}]
        reference = r["completion"][0]["content"]
        enc = tokenizer.apply_chat_template(
            msgs, add_generation_prompt=True, return_tensors="pt", return_dict=True
        ).to(device)
        with torch.no_grad():
            out = model.generate(**enc, **gen_kwargs)
        generation = tokenizer.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)

        valid, error, valid_after_trim = validate(r["task"], generation)
        violations = (
            chef_violations(generation, r.get("meta", {}).get("forbidden_terms", []))
            if r["task"] == "chef_plan" else []
        )
        rec = {
            "index": i,
            "task": r["task"],
            "prompt_tokens": int(enc["input_ids"].shape[1]),
            "valid": valid,
            "valid_after_trim": valid_after_trim,
            "error": error,
            "violations": violations,
            "generation": generation,
            "reference": reference,
            "prompt_tail": msgs[-1]["content"][-args.prompt_chars:],
        }
        records.append(rec)
        print(render(rec, args.prompt_chars), flush=True)

    out_dir = resolve(args.out_dir) if args.out_dir else model_dir / "eval_app"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "generations.jsonl", "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(out_dir / "generations.txt", "w", encoding="utf-8") as f:
        for rec in records:
            f.write(render(rec, args.prompt_chars) + "\n")

    # Summary per task.
    def pct(n, d):
        return f"{100 * n / d:.0f}%" if d else "—"

    print(f"\n{'=' * 72}\nSUMMARY  (model={args.model}, decoding={'greedy' if args.temperature == 0 else f'temp={args.temperature}'})")
    by_task: dict[str, list[dict]] = {}
    for rec in records:
        by_task.setdefault(rec["task"], []).append(rec)
    for task, recs in sorted(by_task.items()):
        n = len(recs)
        strict = sum(r["valid"] for r in recs)
        trim = sum(r["valid_after_trim"] for r in recs)
        line = f"  {task:22} n={n:3}  json_valid={strict}/{n} ({pct(strict, n)})  valid_after_trim={trim}/{n}"
        if task == "chef_plan":
            clean = sum(not r["violations"] for r in recs)
            line += f"  dietary_clean={clean}/{n} ({pct(clean, n)})"
        print(line)
    print(f"\nWrote {out_dir / 'generations.jsonl'} and generations.txt", flush=True)


if __name__ == "__main__":
    main()
