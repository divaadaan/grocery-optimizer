#!/usr/bin/env python
"""Record or replay one recipe-evaluation run (ROADMAP Track E §3).

``record`` runs a scenario through the real recipe graph against LIVE chat
models (``LLM_PROVIDER`` / the per-role model settings, or the scenario's
``llm`` block) and writes a run artifact holding every LLM call. It reads no
database: deals and products come from the scenario's frozen snapshot.

``replay`` re-runs an artifact from its recorded calls -- no model, no
network -- and reports whether the final state is reproduced. Exit status 1
if anything differs.

Usage::

    .venv/bin/python scripts/run_eval.py record \\
        --scenario app/tests/fixtures/eval_runs/scenarios.json \\
        [--name seed27-vegetarian] --out runs/seed27-vegetarian.json

    .venv/bin/python scripts/run_eval.py replay \\
        --artifact runs/seed27-vegetarian.json [--out replayed.json] [--lenient]

Artifact and scenario formats: ``app/eval/runner.py``.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app.eval import runner  # noqa: E402

MAX_SHOWN = 50


def _summary(artifact: dict) -> str:
    state = artifact["final_state"]
    calls = artifact["llm_calls"]
    return (f"status={state.get('status')} approved={len(state.get('approved_recipe_ids') or [])}"
            f"/{state.get('num_meals')} total_cost={state.get('total_cost')} "
            f"llm_calls={len(calls)} wall={artifact['timing']['wall_seconds']:.1f}s")


def _record(args: argparse.Namespace) -> int:
    scenarios = runner.load_scenarios(args.scenario)
    if args.name:
        chosen = [s for s in scenarios if s.name == args.name]
        if not chosen:
            raise SystemExit(f"no scenario named {args.name!r} in {args.scenario} "
                             f"(have {[s.name for s in scenarios]})")
    elif len(scenarios) == 1:
        chosen = scenarios
    else:
        raise SystemExit(f"{args.scenario} holds {len(scenarios)} scenarios; pick one with --name")
    # Looked up at call time so tests can substitute scripted models.
    artifact = runner.record(chosen[0], llm_factory=runner.default_llm_factory)
    path = runner.write_artifact(artifact, args.out)
    print(f"recorded {chosen[0].name}: {_summary(artifact)}")
    print(f"wrote {path}")
    return 0


def _replay(args: argparse.Namespace) -> int:
    result = runner.replay(args.artifact, strict=not args.lenient)
    if args.out:
        print(f"wrote {runner.write_artifact(result.replayed, args.out)}")
    print(f"replayed: {_summary(result.replayed)}")
    if result.ok:
        print("replay matches the recorded run")
        return 0
    print(f"replay DIFFERS in {len(result.differences)} place(s):")
    for diff in result.differences[:MAX_SHOWN]:
        print(f"  {diff}")
    if len(result.differences) > MAX_SHOWN:
        print(f"  ... and {len(result.differences) - MAX_SHOWN} more")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="run a scenario against live models and record it")
    rec.add_argument("--scenario", required=True, help="scenario file (JSON or YAML)")
    rec.add_argument("--name", help="scenario to run, if the file holds several")
    rec.add_argument("--out", required=True, help="artifact path to write")
    rec.set_defaults(func=_record)

    rep = sub.add_parser("replay", help="re-run an artifact from its recorded calls")
    rep.add_argument("--artifact", required=True)
    rep.add_argument("--out", help="also write the replay's own artifact here")
    rep.add_argument("--lenient", action="store_true",
                     help="do not fail on recorded calls the replay never asked for")
    rep.set_defaults(func=_replay)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
