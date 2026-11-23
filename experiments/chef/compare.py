"""Side-by-side table of every run under experiments/chef/runs (skips _superseded).

    .venv/bin/python -m experiments.chef.compare
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

RUNS = Path(__file__).parent / "runs"


def spread(q: list) -> float | None:
    """Share of cited deals outside the top fifth of what the model was shown (0.8 = no bias)."""
    return None if not q or not sum(q) else round(1 - q[0] / sum(q), 2)


def main():
    rows = []
    for d in sorted(p for p in RUNS.iterdir() if p.is_dir() and not p.name.startswith("_")):
        cfg = json.loads((d / "config.json").read_text())
        summ = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else []
        ok = [s for s in summ if not s.get("error") and s.get("parse_ok")]
        n = len(summ)
        sp = [spread(s.get("position_quintiles")) for s in ok]
        sp = [x for x in sp if x is not None]
        rows.append({
            "arm": cfg["arm"], "model": cfg["model"].split("/")[-1], "runs": n,
            "valid_plans": f"{sum(1 for s in ok if s['dishes_ok'] and not s['invalid_ids'])}/{n}",
            "invalid_ids": sum(len(s["invalid_ids"]) for s in ok),
            "unseen_ids": sum(len(s.get("cited_unseen") or []) for s in ok),
            "spread": round(statistics.mean(sp), 2) if sp else "-",
            "chains": round(statistics.mean(len(s["chains"]) for s in ok), 1) if ok else "-",
            "beverages": sum(len(s["beverage_ids"]) for s in ok),
            "tools": round(statistics.mean(s.get("tool_calls", 0) for s in ok), 1) if ok else "-",
            "seen": round(statistics.mean(s.get("seen", 0) for s in ok)) if ok else "-",
            "sec": round(statistics.mean(s["seconds"] for s in summ), 0) if summ else "-",
            "dir": d.name,
        })
    cols = ["arm", "model", "runs", "valid_plans", "invalid_ids", "unseen_ids", "spread",
            "chains", "beverages", "tools", "seen", "sec"]
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")


if __name__ == "__main__":
    main()
