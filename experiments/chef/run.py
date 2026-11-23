"""Run one arm x one model x N seeds over a frozen snapshot; write results + a review sheet.

    .venv/bin/python -m experiments.chef.run --arm A --model ollama:qwen2.5:7b \
        --snapshot experiments/chef/snapshots/2026-11-23_L4G3E8_open.json --repeats 3

Output: experiments/chef/runs/<stamp>_<arm>_<model>/
    run_<seed>.json   plan, score, per-call metadata, full transcript
    summary.json      per-run scores side by side
    review.md         every plan rendered for the owner's dish-quality review
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import re
import traceback
from pathlib import Path

from experiments.chef.common import Scenario, as_dict, review_markdown, score_plan

ARMS = {"A": "experiments.chef.arms.a_full_list", "B": "experiments.chef.arms.b_browse",
        "C": "experiments.chef.arms.c_trays"}
RUNS_DIR = Path(__file__).parent / "runs"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=sorted(ARMS))
    ap.add_argument("--model", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--budget", type=float, default=100.0)
    ap.add_argument("--meals", type=int, default=5)
    ap.add_argument("--household", type=int, default=2)
    a = ap.parse_args()

    arm = importlib.import_module(ARMS[a.arm])
    scenario = Scenario(snapshot=a.snapshot, budget=a.budget,
                        household_size=a.household, num_meals=a.meals)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RUNS_DIR / f"{stamp}_{arm.NAME}_{re.sub(r'[^A-Za-z0-9.]+', '-', a.model)}"
    out.mkdir(parents=True)
    (out / "config.json").write_text(json.dumps({"arm": arm.NAME, "model": a.model,
                                                 "scenario": scenario.__dict__}, indent=1))
    summary, review = [], [f"# {arm.NAME} · {a.model}\n"]
    for seed in range(1, a.repeats + 1):
        try:
            res = arm.run(a.model, scenario, seed)
            score = as_dict(score_plan(res["plan"], scenario, res.get("presented_ids")))
            err = None
        except Exception as e:  # a failed run is a result, not a crash
            res, score, err = {"plan": None, "calls": [], "transcript": []}, None, repr(e)
            traceback.print_exc()
        rec = {"seed": seed, "error": err, "score": score, "calls": res["calls"],
               "tool_log": res.get("tool_log"),
               "plan": res["plan"], "transcript": res["transcript"]}
        (out / f"run_{seed}.json").write_text(json.dumps(rec, indent=1, default=str))
        secs = sum(c.get("seconds") or 0 for c in res["calls"])
        summary.append({"seed": seed, "error": err, "seconds": round(secs, 1),
                        "calls": len(res["calls"]),
                        "tool_calls": len(res.get("tool_log") or []),
                        "seen": len(res.get("presented_ids") or []),
                        "truncated": any(c.get("truncated_prompt") for c in res["calls"]),
                        **(score or {})})
        review.append(review_markdown(res["plan"], scenario, f"seed {seed}")
                      if not err else f"## seed {seed}\n\n_error: {err}_\n")
        _print_row(summary[-1])
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    (out / "review.md").write_text("\n".join(review))
    print(out)


def _print_row(s):
    if s.get("error"):
        print(f"seed {s['seed']}: ERROR {s['error'][:200]}")
        return
    print(f"seed {s['seed']}: parse={s['parse_ok']} dishes={s['num_dishes']} "
          f"invalid={len(s['invalid_ids'])} cost=${s['total_cost']} "
          f"anchors={s['anchor_categories']} pos={s['position_quintiles']} "
          f"chains={list(s['chains'])[:4]} bev={len(s['beverage_ids'])} "
          f"unseen={len(s.get('cited_unseen') or [])} tools={s['tool_calls']} seen={s['seen']} "
          f"truncated={s['truncated']} {s['seconds']}s")


if __name__ == "__main__":
    main()
