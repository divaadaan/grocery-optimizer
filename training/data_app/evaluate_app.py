"""Deterministic task eval for app-targeted models (ROADMAP item 5, step 2 / Phase 3).

Scores a model on the held-out app split and reports the metrics that *are* the
acceptance bar, ported from the real tests in
``app/tests/test_nutritionist_regression.py``:

- **JSON-validity rate** per task — does the output parse and validate through the
  task's real Pydantic schema (ChefPlan / NutritionistVerdict / RecipeBatch).
- **Chef dietary-violation rate** — the exact assertion from
  ``test_chef_groups_respect_vegetarian_restriction``: any item whose
  ``product_name`` contains a forbidden term, scanned over the *parsed*
  ``ingredient_groups`` (not raw text). Zero violations on every chef case is the
  proxy for that xfail flipping.
- **Nutritionist confusion matrix + false-rejection rate** — predicted
  ``approved`` vs the fixture's ``expected_approved``. A zero false-rejection rate
  on the APPROVE controls is the proxy for the nutritionist xfail flipping;
  false-approvals on REJECT cases are the hard-guard safety failures.

Two model paths:

- ``--model <dir>``: a local HF model dir or QLoRA adapter dir, run with greedy
  decoding (no JSON grammar). Measures the model's *intrinsic* output quality —
  iterate here before exporting.
- ``--ollama-model <name>``: served via Ollama, replicating how the app actually
  calls it — ``format="json"`` grammar-constrained, per-task temperature (chef
  0.7 / nutritionist 0.3 / sous 0.8). This is the **app-faithful** path that
  predicts whether the xfail tests flip, and the one to use for untuned base-swap
  candidates (e.g. ``--ollama-model qwen2.5:1.5b-instruct``).

Usage (from repo root, training venv active):
    python -m training.data_app.evaluate_app --model training/runs/smollm-360m-app-qlora
    python -m training.data_app.evaluate_app --ollama-model hf.co/danieldsachs/smollm2-360m-recipe:f16 \\
        --base-url http://172.18.128.1:11434
    python -m training.data_app.evaluate_app --ollama-model qwen2.5:1.5b-instruct \\
        --base-url http://172.18.128.1:11434          # eval an untuned swap candidate

Writes ``metrics.json`` (machine) and ``report.txt`` (readable) to --out-dir, and
prints the report to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import requests

from app.agents.llm_output import ChefPlan, NutritionistVerdict, RecipeBatch

REPO_ROOT = Path(__file__).resolve().parents[2]

TASK_SCHEMAS = {
    "chef_plan": ChefPlan,
    "nutritionist_verdict": NutritionistVerdict,
    "sous_chef_recipe": RecipeBatch,
}

# The app's per-agent ChatOllama temperatures (chef_orchestrator/nutritionist/
# sous_chef). The Ollama path mirrors these so the eval matches inference.
APP_TEMPERATURE = {"chef_plan": 0.7, "nutritionist_verdict": 0.3, "sous_chef_recipe": 0.8}


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def parse_validated(task: str, text: str):
    """(ok, obj, error). obj is the schema-validated model_dump on success."""
    schema = TASK_SCHEMAS[task]
    try:
        obj = schema.model_validate(json.loads(text))
        return True, obj.model_dump(), ""
    except Exception as e:
        return False, None, f"{type(e).__name__}: {str(e)[:200]}"


def chef_dietary_violations(plan: dict, forbidden: list[str]) -> list[str]:
    """Ported verbatim from test_chef_groups_respect_vegetarian_restriction:
    flag any grouped item whose product_name contains a forbidden term."""
    return [
        item["product_name"]
        for group in plan["ingredient_groups"]
        for item in group
        if any(term in item["product_name"].casefold() for term in forbidden)
    ]


# --------------------------------------------------------------------------- #
# Generation backends
# --------------------------------------------------------------------------- #
def gen_hf(model_dir: Path, base_override: str | None, rows: list[dict], max_new_tokens: int):
    """Greedy HF generation (intrinsic; no JSON grammar)."""
    import torch

    from .generate_app import load_model

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading HF model ({device})…", flush=True)
    model, tokenizer = load_model(model_dir, base_override, device)
    gen_kwargs = dict(max_new_tokens=max_new_tokens, do_sample=False,
                      repetition_penalty=1.1, pad_token_id=tokenizer.eos_token_id)
    outputs = []
    for r in rows:
        enc = tokenizer.apply_chat_template(
            r["prompt"], add_generation_prompt=True, return_tensors="pt", return_dict=True
        ).to(device)
        with torch.no_grad():
            out = model.generate(**enc, **gen_kwargs)
        outputs.append(tokenizer.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True))
    return outputs


def gen_ollama(model: str, base_url: str, rows: list[dict], max_new_tokens: int,
               seed: int, temperature: float | None, timeout: int):
    """App-faithful Ollama generation: format=json, per-task temperature."""
    try:
        requests.get(f"{base_url}/api/tags", timeout=5).raise_for_status()
    except requests.RequestException as e:
        raise SystemExit(f"Ollama not reachable at {base_url}: {e}")
    print(f"Generating via Ollama {model} @ {base_url} (format=json)…", flush=True)
    outputs = []
    for r in rows:
        temp = temperature if temperature is not None else APP_TEMPERATURE[r["task"]]
        resp = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": r["prompt"],            # [{"role": "user", "content": ...}]
                "format": "json",
                "stream": False,
                "options": {"temperature": temp, "seed": seed, "num_predict": max_new_tokens},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        outputs.append(resp.json()["message"]["content"])
    return outputs


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score(rows: list[dict], generations: list[str]) -> dict:
    by_task: dict[str, dict] = {}
    per_example = []
    for r, gen in zip(rows, generations):
        task = r["task"]
        ok, obj, error = parse_validated(task, gen)
        rec = {"task": task, "json_valid": ok, "error": error}
        t = by_task.setdefault(task, {"n": 0, "json_valid": 0})
        t["n"] += 1
        t["json_valid"] += int(ok)

        if task == "chef_plan":
            t.setdefault("dietary_evaluated", 0)
            t.setdefault("dietary_clean", 0)
            t.setdefault("violation_examples", [])
            if ok:
                viol = chef_dietary_violations(obj, r["meta"].get("forbidden_terms", []))
                t["dietary_evaluated"] += 1
                t["dietary_clean"] += int(not viol)
                if viol:
                    t["violation_examples"].append(viol)
                rec["violations"] = viol
        elif task == "nutritionist_verdict":
            cm = t.setdefault("confusion", {"correct_approve": 0, "false_reject": 0,
                                            "correct_reject": 0, "false_approve": 0,
                                            "unparseable": 0})
            expected = bool(r["meta"]["expected_approved"])
            if not ok:
                cm["unparseable"] += 1
                rec["predicted_approved"] = None
            else:
                pred = bool(obj["approved"])
                rec["predicted_approved"] = pred
                rec["expected_approved"] = expected
                if expected and pred:
                    cm["correct_approve"] += 1
                elif expected and not pred:
                    cm["false_reject"] += 1
                elif not expected and not pred:
                    cm["correct_reject"] += 1
                else:
                    cm["false_approve"] += 1
        per_example.append(rec)

    # Derived rates.
    for task, t in by_task.items():
        t["json_valid_rate"] = round(t["json_valid"] / t["n"], 3) if t["n"] else None
        if task == "chef_plan":
            de = t["dietary_evaluated"]
            t["dietary_clean_rate"] = round(t["dietary_clean"] / de, 3) if de else None
            # whole-task pass = valid AND dietary-clean (the xfail proxy)
            t["pass"] = t["dietary_clean"]
            t["pass_rate"] = round(t["dietary_clean"] / t["n"], 3) if t["n"] else None
        if task == "nutritionist_verdict":
            cm = t["confusion"]
            approve_controls = cm["correct_approve"] + cm["false_reject"]
            reject_controls = cm["correct_reject"] + cm["false_approve"]
            t["approve_controls"] = approve_controls
            t["reject_controls"] = reject_controls
            t["false_rejection_rate"] = round(cm["false_reject"] / approve_controls, 3) if approve_controls else None
            t["false_approval_rate"] = round(cm["false_approve"] / reject_controls, 3) if reject_controls else None

    # Acceptance-bar proxies (the two xfail tests).
    chef = by_task.get("chef_plan", {})
    nut = by_task.get("nutritionist_verdict", {})
    acceptance = {
        "chef_xfail_proxy": {
            "metric": "all chef cases valid AND dietary-clean",
            "value": f"{chef.get('pass', 0)}/{chef.get('n', 0)}",
            "would_pass": chef.get("n", 0) > 0 and chef.get("pass", 0) == chef.get("n", 0),
        },
        "nutritionist_xfail_proxy": {
            "metric": "false-rejection rate on APPROVE controls == 0",
            "false_rejection_rate": nut.get("false_rejection_rate"),
            "would_pass": nut.get("false_rejection_rate") == 0.0,
        },
    }
    return {"by_task": by_task, "acceptance": acceptance, "per_example": per_example}


def render_report(meta: dict, scored: dict) -> str:
    L = ["=" * 72, f"APP-TARGETED EVAL  ·  {meta['model']}  ({meta['mode']})",
         f"data={meta['data']}  decoding={meta['decoding']}", "=" * 72, ""]
    for task, t in sorted(scored["by_task"].items()):
        L.append(f"{task}  (n={t['n']})")
        L.append(f"  json_valid: {t['json_valid']}/{t['n']}  ({t['json_valid_rate']})")
        if task == "chef_plan":
            L.append(f"  dietary_clean (of valid): {t['dietary_clean']}/{t['dietary_evaluated']}  "
                     f"({t['dietary_clean_rate']})")
            L.append(f"  whole-task pass (valid & clean): {t['pass']}/{t['n']}  ({t['pass_rate']})")
            if t["violation_examples"]:
                L.append(f"  violations seen: {t['violation_examples']}")
        if task == "nutritionist_verdict":
            cm = t["confusion"]
            L.append(f"  confusion: correct_approve={cm['correct_approve']} false_reject={cm['false_reject']} "
                     f"correct_reject={cm['correct_reject']} false_approve={cm['false_approve']} "
                     f"unparseable={cm['unparseable']}")
            L.append(f"  false_rejection_rate (APPROVE controls, n={t['approve_controls']}): {t['false_rejection_rate']}")
            L.append(f"  false_approval_rate  (REJECT controls,  n={t['reject_controls']}): {t['false_approval_rate']}")
        L.append("")
    a = scored["acceptance"]
    L.append("-" * 72)
    L.append("ACCEPTANCE-BAR PROXIES (the two xfail tests)")
    c, n = a["chef_xfail_proxy"], a["nutritionist_xfail_proxy"]
    L.append(f"  chef_groups_respect_vegetarian: {c['value']} valid&clean  ->  "
             f"{'WOULD PASS' if c['would_pass'] else 'still failing'}")
    L.append(f"  nutritionist APPROVE controls: false_rejection_rate={n['false_rejection_rate']}  ->  "
             f"{'WOULD PASS' if n['would_pass'] else 'still failing'}")
    L.append("-" * 72)
    return "\n".join(L) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--model", help="local HF model dir or QLoRA adapter dir (greedy)")
    src.add_argument("--ollama-model", help="Ollama model name (format=json, app temps)")
    parser.add_argument("--base", default=None, help="override HF base model (else from adapter_config.json)")
    parser.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    parser.add_argument("--data", default="training/data/app/test.jsonl")
    parser.add_argument("--max-new-tokens", type=int, default=1536)
    parser.add_argument("--seed", type=int, default=0, help="Ollama sampling seed (reproducibility)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="override Ollama temperature for all tasks (default: per-app temps)")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    rows = [json.loads(line) for line in open(resolve(args.data), encoding="utf-8")]
    print(f"Loaded {len(rows)} eval examples from {args.data}", flush=True)

    if args.model:
        model_dir = resolve(args.model)
        generations = gen_hf(model_dir, args.base, rows, args.max_new_tokens)
        meta = {"model": args.model, "mode": "hf-greedy",
                "decoding": f"greedy, max_new_tokens={args.max_new_tokens}"}
        default_out = model_dir / "eval_app"
    else:
        generations = gen_ollama(args.ollama_model, args.base_url, rows, args.max_new_tokens,
                                 args.seed, args.temperature, args.timeout)
        temp_desc = f"temp={args.temperature}" if args.temperature is not None else "per-app temps"
        meta = {"model": args.ollama_model, "mode": "ollama-json",
                "decoding": f"format=json, {temp_desc}, seed={args.seed}"}
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", args.ollama_model)
        default_out = resolve("training/data/app") / f"eval_{safe}"
    meta["data"] = args.data

    scored = score(rows, generations)
    out_dir = resolve(args.out_dir) if args.out_dir else default_out
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump({**meta, **scored}, f, indent=2, ensure_ascii=False)
    report = render_report(meta, scored)
    with open(out_dir / "report.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print("\n" + report, flush=True)
    print(f"Wrote {out_dir / 'metrics.json'} and report.txt", flush=True)


if __name__ == "__main__":
    main()
