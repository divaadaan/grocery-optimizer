"""Build the app-targeted SFT dataset (ROADMAP item 5, step 2).

Renders examples through the app's real PromptTemplates and validates every
completion through the app's real Pydantic schemas, then writes 80/10/10
train/validation/test JSONL of ``{task, prompt, completion, meta}`` records to
``training/data/app/``.

Usage (from repo root, training venv active):
    python -m training.data_app.build_app_dataset                 # incl. qwen2.5:7b distillation
    python -m training.data_app.build_app_dataset --no-distill    # programmatic tasks only (offline)
    python -m training.data_app.build_app_dataset \\
        --base-url http://172.18.128.1:11434 --teacher-model qwen2.5:7b

The schema gate is the contract: a completion that fails ChefPlan / RecipeBatch /
NutritionistVerdict is dropped and counted, never written.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path

from app.agents.llm_output import ChefPlan, NutritionistVerdict, RecipeBatch

from . import chef_plan, nutritionist, sous_chef
from .example import Example
from .seed_catalog import load_deals

REPO_ROOT = Path(__file__).resolve().parents[2]

TASK_SCHEMAS = {
    "chef_plan": ChefPlan,
    "nutritionist_verdict": NutritionistVerdict,
    "sous_chef_recipe": RecipeBatch,
}


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def schema_gate(examples: list[Example]) -> tuple[list[Example], int]:
    """Drop any example whose completion doesn't validate through its task's
    real schema. Generators build valid output already; this is the contract."""
    kept, dropped = [], 0
    for ex in examples:
        schema = TASK_SCHEMAS[ex.task]
        try:
            schema.model_validate(json.loads(ex.completion))
        except Exception as e:  # json or pydantic
            dropped += 1
            print(f"  [gate] dropped {ex.task}: {type(e).__name__}: {str(e)[:120]}")
            continue
        kept.append(ex)
    return kept, dropped


def dedupe(examples: list[Example]) -> list[Example]:
    seen, out = set(), []
    for ex in examples:
        key = (ex.task, ex.prompt, ex.completion)
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    return out


def split(examples: list[Example], seed: int, val_frac: float, test_frac: float) -> dict[str, list[Example]]:
    """Stratified by task so every split contains every task type."""
    by_task: dict[str, list[Example]] = {}
    for ex in examples:
        by_task.setdefault(ex.task, []).append(ex)

    splits: dict[str, list[Example]] = {"train": [], "validation": [], "test": []}
    for task, rows in sorted(by_task.items()):
        rows = list(rows)
        random.Random(seed + hash(task) % 10_000).shuffle(rows)
        n = len(rows)
        n_test = int(n * test_frac)
        n_val = int(n * val_frac)
        splits["test"] += rows[:n_test]
        splits["validation"] += rows[n_test:n_test + n_val]
        splits["train"] += rows[n_test + n_val:]

    for name in splits:
        random.Random(seed + 7).shuffle(splits[name])
    return splits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", default=None, help="override the seed fixture path")
    parser.add_argument("--out-dir", default="training/data/app")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--chef-per-profile", type=int, default=6)
    parser.add_argument("--nutritionist-per-profile", type=int, default=10)
    parser.add_argument("--no-distill", action="store_true",
                        help="skip the qwen2.5:7b SousChef distillation (offline build)")
    parser.add_argument("--teacher-model", default="qwen2.5:7b")
    parser.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
                        help="Ollama base URL holding the teacher model (gateway IP from WSL)")
    parser.add_argument("--sous-per-group", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--val-frac", type=float, default=0.1)
    parser.add_argument("--test-frac", type=float, default=0.1)
    args = parser.parse_args()

    deals = load_deals(args.fixture)
    print(f"Loaded {len(deals)} seed deals from {args.fixture or 'fixture'}")

    examples: list[Example] = []

    print("\n== chef_plan (programmatic) ==")
    examples += chef_plan.build_examples(deals, seed=args.seed, per_profile=args.chef_per_profile)

    print("== nutritionist_verdict (programmatic) ==")
    examples += nutritionist.build_examples(deals, seed=args.seed, per_profile=args.nutritionist_per_profile)

    if args.no_distill:
        print("== sous_chef_recipe: SKIPPED (--no-distill) ==")
    else:
        print(f"== sous_chef_recipe (distill {args.teacher_model} @ {args.base_url}) ==")
        try:
            examples += sous_chef.build_examples(
                deals, base_url=args.base_url, model=args.teacher_model,
                seed=args.seed, per_group=args.sous_per_group, timeout=args.timeout,
            )
        except sous_chef.TeacherUnavailable as e:
            print(f"  WARNING: SousChef distillation skipped — {e}")

    print(f"\nGenerated {len(examples)} raw examples")
    examples, dropped = schema_gate(examples)
    examples = dedupe(examples)
    print(f"After schema gate ({dropped} dropped) + dedupe: {len(examples)} examples")

    splits = split(examples, args.seed, args.val_frac, args.test_frac)

    out_dir = resolve(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in splits.items():
        with open(out_dir / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for ex in rows:
                f.write(json.dumps(ex.to_record(), ensure_ascii=False) + "\n")
    with open(out_dir / "app_sft.jsonl", "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex.to_record(), ensure_ascii=False) + "\n")

    def task_counts(rows: list[Example]) -> dict[str, int]:
        c: dict[str, int] = {}
        for ex in rows:
            c[ex.task] = c.get(ex.task, 0) + 1
        return c

    stats = {
        "seed": args.seed,
        "total": len(examples),
        "dropped_by_gate": dropped,
        "by_task": task_counts(examples),
        "splits": {name: {"total": len(rows), "by_task": task_counts(rows)} for name, rows in splits.items()},
        "distillation": "skipped" if args.no_distill else {"model": args.teacher_model, "base_url": args.base_url},
    }
    with open(out_dir / "dataset_stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    print(f"\nWrote splits to {out_dir}")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
