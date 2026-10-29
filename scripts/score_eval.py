#!/usr/bin/env python
"""Score recipe-evaluation run artifacts (ROADMAP Track E §4).

Deterministic and offline: reads each artifact and the snapshot it ran
against (by the scenario's snapshot name, or ``--snapshot``), refuses a
snapshot whose content hash differs from the recorded one, and prints a
compact per-run summary -- or, with ``--json``, the full RunScore(s).

Exit status: 0 if no run FAILs, 1 if any does, 2 if an artifact cannot be
scored. ``NA`` (a restriction product attributes cannot express) is not a
failure but is never a pass either.

Usage::

    .venv/bin/python scripts/score_eval.py runs/a.json [runs/b.json ...] [--json]
        [--snapshot app/tests/fixtures/snapshots/seed27.json]

Scorer definitions and the RunScore layout: ``app/eval/scorers.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app.eval import runner, scorers  # noqa: E402
from app.eval.snapshots import SnapshotError, load_snapshot  # noqa: E402


def _fmt(x, pct=False) -> str:
    if x is None:
        return "n/a"
    return f"{x:.0%}" if pct else f"{x}"


def summary(score: scorers.RunScore) -> str:
    s, k, c = score.structural, score.compliance, score.citations
    b, o, d = score.budget, score.on_sale, score.diversity
    t = score.latency_tokens["total"]
    retries = sum(r["retries"] for r in s["schema"].values())
    failures = sum(r["failed_invocations"] for r in s["schema"].values())
    restrictions = ",".join(k["restrictions"]) or "none"
    if k["unsupported_restrictions"]:
        restrictions += f" (N/A: {','.join(k['unsupported_restrictions'])})"
    agree = k["agreement"]
    lines = [
        f"{score.run['scenario']} [{score.run['snapshot']}]: {score.verdict}",
        *(f"  ! {r}" for r in score.verdict_reasons),
        f"  structural  status={s['status']} recipes={s['recipes_approved']}/"
        f"{s['recipes_requested']} schema retries={retries} failures={failures} "
        f"errors={len(s['errors'])}",
        f"  compliance  {k['gate']} {k['compliant_recipes']}/{k['approved_recipes']} "
        f"restrictions={restrictions} agreement={agree['agree']}/{agree['compared']}"
        + (f" gate-leaks={k['served_deals_failing_gate']}"
           if k["served_deals_failing_gate"] else ""),
        f"  citations   valid={c['valid']}/{c['ingredients']} "
        + " ".join(f"{kind}={n}" for kind, n in c["counts"].items()
                   if n and kind not in ("deal", "staple")),
        f"  budget      lower bound ${b['cost_per_recipe_sum']:.2f} per-recipe-sum / "
        f"${b['cost_per_unique_deal']:.2f} per-unique-deal vs ${b['available']:.2f} "
        f"available ({b['status']})",
        f"  on-sale     deal share={_fmt(o['deal_share'], True)} deals used="
        f"{o['deals_used']}/{o['deals_offered']} savings="
        f"{'absent' if o['savings_vs_regular'] is None else '$%.2f' % o['savings_vs_regular']}"
        f" ({o['regular_price_coverage']})",
        f"  diversity   jaccard mean={_fmt(d['mean_jaccard'])} max={_fmt(d['max_jaccard'])} "
        f"distinct deals={d['distinct_deals']}",
        f"  llm         calls={t['calls']} latency={_fmt(t['latency_s']['sum'])}s "
        f"tokens={_fmt(t['tokens']['total_tokens']['sum'])} ({t['token_coverage']})",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("artifacts", nargs="+", help="run artifact JSON file(s)")
    parser.add_argument("--snapshot", help="snapshot path or name (default: each "
                        "artifact's scenario snapshot)")
    parser.add_argument("--json", action="store_true", help="print full RunScore JSON")
    args = parser.parse_args(argv)

    snapshot = load_snapshot(args.snapshot) if args.snapshot else None
    scores, failed = [], False
    for path in args.artifacts:
        try:
            score = scorers.score_run(runner.load_artifact(path), snapshot)
        except (runner.ArtifactError, SnapshotError, scorers.ScoringError) as e:
            print(f"{path}: cannot score: {e}", file=sys.stderr)
            return 2
        scores.append(score)
        failed |= score.verdict == scorers.FAIL
    if args.json:
        out = [s.to_dict() for s in scores]
        print(json.dumps(out[0] if len(out) == 1 else out, indent=1, sort_keys=True,
                         ensure_ascii=False))
    else:
        print("\n".join(summary(s) for s in scores))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
