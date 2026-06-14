"""Print a side-by-side comparison of evaluate.py / llm_judge.py outputs.

Loads metrics.json (from evaluate.py) and, if present, judge_scores.json (from
llm_judge.py) out of one or more eval directories and renders a single table.
Judge rows are simply omitted for any dir that hasn't been judged yet.

Usage (from repo root):
    python -m training.reproduce_paper.compare_eval                 # the two default dirs
    python -m training.reproduce_paper.compare_eval DIR_A DIR_B ... # explicit eval dirs

The defaults are where evaluate.py writes the tuned 135M run and the untuned
HF baseline (see evaluate.py's output-dir logic).
"""

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DIRS = [
    "training/runs/eval-HuggingFaceTB--SmolLM-135M",   # untuned baseline
    "training/runs/smollm-135m-full/eval",             # tuned full-FT
]

# metric key -> (display label, better-direction)  "+" higher is better,
# "-" lower is better, "" informational / neutral.
METRIC_ROWS = [
    ("perplexity", "perplexity", "-"),
    ("bleu", "BLEU", "+"),
    ("rouge1", "ROUGE-1", "+"),
    ("rouge2", "ROUGE-2", "+"),
    ("rougeL", "ROUGE-L", "+"),
    ("ingredient_coverage", "ingredient coverage", "+"),
    ("mean_step_count", "mean step count", ""),
    ("pct_with_temperature", "% with temperature", ""),
    ("pct_with_time", "% with time", ""),
]

JUDGE_DIMS = [
    "clarity", "completeness", "consistency",
    "practicality", "relevance", "allergen_safety",
]


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def load_dir(d: Path) -> dict:
    """Return {'label', 'metrics', 'judge'} for one eval dir; judge may be None."""
    metrics_path = d / "metrics.json"
    if not metrics_path.exists():
        raise SystemExit(f"no metrics.json in {d} — run evaluate.py for that model first.")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))

    judge_path = d / "judge_scores.json"
    judge = json.loads(judge_path.read_text(encoding="utf-8")) if judge_path.exists() else None

    return {"label": metrics.get("model", d.name), "metrics": metrics, "judge": judge}


def fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 100 else f"{value:.2f}"
    return str(value)


def render(cols: list[dict]) -> str:
    labels = [c["label"] for c in cols]
    # Column width: widest of header label / any cell, floor of 12.
    name_w = max(len("metric  (better)"), max(len(r[1]) for r in METRIC_ROWS) + 11)
    col_w = max(12, *(len(lbl) for lbl in labels))

    def row(name: str, cells: list[str]) -> str:
        return name.ljust(name_w) + "  " + "  ".join(c.rjust(col_w) for c in cells)

    show_delta = len(cols) == 2
    delta_w = col_w
    header_cells = labels[:]
    if show_delta:
        header_cells.append("Δ (B−A)")
    lines = [row("metric  (better)", header_cells)]
    lines.append("-" * len(lines[0]))

    def emit(label: str, direction: str, values: list, is_int: bool = False) -> None:
        arrow = {"+": " (↑)", "-": " (↓)", "": ""}[direction]
        cells = [fmt(v) for v in values]
        if show_delta:
            a, b = values[0], values[1]
            if a is None or b is None:
                cells.append("—")
            else:
                d = b - a
                cells.append(f"{d:+.4f}" if not is_int else f"{d:+.0f}")
        lines.append(row(label + arrow, cells))

    # Text + domain metrics.
    lines.append(row("─ text / domain metrics", ["" for _ in header_cells]))
    for key, label, direction in METRIC_ROWS:
        emit(label, direction, [c["metrics"].get(key) for c in cols])

    # LLM-judge means (only if at least one dir has them).
    if any(c["judge"] for c in cols):
        lines.append("")
        lines.append(row("─ LLM-judge means (1–5)", ["" for _ in header_cells]))
        for dim in JUDGE_DIMS:
            vals = [(c["judge"]["mean_scores"].get(dim) if c["judge"] else None) for c in cols]
            emit(dim, "+", vals)
        overall = [(c["judge"].get("overall_mean") if c["judge"] else None) for c in cols]
        emit("overall_mean", "+", overall)
        n = [(c["judge"].get("num_judged") if c["judge"] else None) for c in cols]
        lines.append(row("(num judged)", [fmt(v) for v in n] + (["—"] if show_delta else [])))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dirs", nargs="*", default=DEFAULT_DIRS,
                        help="eval directories (each holding metrics.json / judge_scores.json)")
    args = parser.parse_args()

    cols = [load_dir(resolve(d)) for d in args.dirs]
    print(render(cols))
    if len(cols) == 2:
        print("\nΔ = column B − column A "
              "(positive is better for ↑ rows, negative is better for ↓ rows).")


if __name__ == "__main__":
    main()
