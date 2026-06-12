"""Prepare the Food.com dataset (Majumder et al., 2019) for SFT.

Reads RAW_recipes.csv from the Kaggle dataset
`shuyangli94/food-com-recipes-and-user-interactions` (231,637 recipes),
extracts name/ingredients/steps, applies minimal cleaning, and writes an
80/10/10 train/validation/test split as JSONL.

Records are stored as structured fields (name, ingredients, steps), not as a
single text string, so the prompt rendering stays tokenizer-agnostic — the
train/eval scripts build the text with their tokenizer's own EOS token.

Usage (from repo root, training venv active):
    python -m training.reproduce_paper.prepare_data
    python -m training.reproduce_paper.prepare_data --sample 100000   # paper's small-scale setup
"""

import argparse
import ast
import json
import random
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]

KAGGLE_HINT = """\
RAW_recipes.csv not found at {path}

Download it from Kaggle (dataset: shuyangli94/food-com-recipes-and-user-interactions):
    kaggle datasets download -d shuyangli94/food-com-recipes-and-user-interactions \\
        -f RAW_recipes.csv -p training/data/raw --unzip
or download manually from
    https://www.kaggle.com/datasets/shuyangli94/food-com-recipes-and-user-interactions
and place RAW_recipes.csv under training/data/raw/.
"""


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def parse_listish(value) -> list[str]:
    """Food.com stores ingredients/steps as stringified Python lists."""
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [" ".join(str(item).split()) for item in parsed if str(item).strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", default="training/data/raw/RAW_recipes.csv")
    parser.add_argument("--output-dir", default="training/data/foodcom")
    parser.add_argument("--sample", type=int, default=None,
                        help="Randomly sample N recipes before splitting (paper small-scale: 100000)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    csv_path = resolve(args.csv)
    if not csv_path.exists():
        raise SystemExit(KAGGLE_HINT.format(path=csv_path))

    df = pd.read_csv(csv_path, usecols=["name", "ingredients", "steps"])
    total_raw = len(df)

    records = []
    for row in df.itertuples(index=False):
        name = " ".join(str(row.name).split()) if pd.notna(row.name) else ""
        ingredients = parse_listish(row.ingredients)
        steps = parse_listish(row.steps)
        if not name or not ingredients or not steps:
            continue
        records.append({"name": name, "ingredients": ingredients, "steps": steps})

    rng = random.Random(args.seed)
    rng.shuffle(records)
    if args.sample is not None and args.sample < len(records):
        records = records[: args.sample]

    n = len(records)
    n_train = int(n * 0.8)
    n_val = int(n * 0.1)
    splits = {
        "train": records[:n_train],
        "validation": records[n_train : n_train + n_val],
        "test": records[n_train + n_val :],
    }

    output_dir = resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for split, rows in splits.items():
        with open(output_dir / f"{split}.jsonl", "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    stats = {
        "source_csv": str(csv_path),
        "raw_rows": total_raw,
        "kept_after_cleaning": n,
        "sample_arg": args.sample,
        "seed": args.seed,
        "splits": {split: len(rows) for split, rows in splits.items()},
    }
    with open(output_dir / "dataset_stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
