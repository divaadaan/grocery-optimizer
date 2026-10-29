#!/usr/bin/env python
"""Cross-run evaluation report (ROADMAP Track E §5).

Scores every run artifact (files, directories of ``*.json``, or globs) and
writes ``report.md`` and ``runs.csv`` into ``--out-dir``. Offline and
deterministic apart from the generated-at stamp. ``--mlflow`` additionally logs
one MLflow run per artifact (off by default; mlflow is imported only then).

Exit status (as ``score_eval.py``): 0 if no run FAILs, 1 if any does, 2 if an
artifact cannot be found or scored. ``NA`` is not a failure.

Usage::

    .venv/bin/python scripts/eval_report.py runs/ --out-dir reports/ [--mlflow]
        [--snapshot app/tests/fixtures/snapshots/seed27.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app.eval import report, runner, scorers  # noqa: E402
from app.eval.snapshots import SnapshotError, load_snapshot  # noqa: E402


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_run_artifact(path: str) -> bool:
    """Whether ``path`` is a run artifact (``artifact.kind``), cheaply. Unreadable
    or non-JSON files count as artifacts so that loading them reports the error."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return True
    header = data.get("artifact") if isinstance(data, dict) else None
    return isinstance(header, dict) and header.get("kind") == runner.ARTIFACT_KIND


def expand(inputs: List[str]) -> List[str]:
    """Files as given; directories -> their ``*.json`` (recursive); globs expanded.
    JSON that a directory or glob turns up but that is not a run artifact (a
    scenario file next to the runs) is skipped with a note; a file named
    explicitly is always scored, so a wrong one still fails loudly.
    De-duplicated, sorted. Raises FileNotFoundError for an input matching nothing."""
    found = set()
    for item in inputs:
        if os.path.isdir(item) or glob.has_magic(item):
            if os.path.isdir(item):
                candidates = [str(p) for p in Path(item).rglob("*.json")]
            else:
                candidates = [p for p in glob.glob(item, recursive=True) if os.path.isfile(p)]
            matches = [p for p in candidates if _is_run_artifact(p)]
            for p in sorted(set(candidates) - set(matches)):
                print(f"{p}: not a run artifact, skipped", file=sys.stderr)
        else:
            matches = [item] if os.path.isfile(item) else []
        if not matches:
            raise FileNotFoundError(f"{item}: no artifact files found")
        found.update(matches)
    return sorted(found)


def main(argv=None, clock: Callable[[], datetime] = _utc_now) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("artifacts", nargs="+", help="artifact file(s), directories or globs")
    parser.add_argument("--out-dir", required=True, help="directory for report.md and runs.csv")
    parser.add_argument("--snapshot", help="snapshot path or name (default: each "
                        "artifact's scenario snapshot)")
    parser.add_argument("--mlflow", action="store_true", help="also log runs to MLflow")
    parser.add_argument("--mlflow-experiment", default="recipe-eval")
    args = parser.parse_args(argv)

    try:
        paths = expand(args.artifacts)
        snapshot = load_snapshot(args.snapshot) if args.snapshot else None
    except (FileNotFoundError, SnapshotError) as e:
        print(f"cannot read inputs: {e}", file=sys.stderr)
        return 2

    runs = []
    for path in paths:
        try:
            artifact = runner.load_artifact(path)
            score = scorers.score_run(artifact, snapshot)
        except (runner.ArtifactError, SnapshotError, scorers.ScoringError) as e:
            print(f"{path}: cannot score: {e}", file=sys.stderr)
            return 2
        runs.append(report.ReportRun.from_artifact(artifact, score, source=path))

    stamp = clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report.render_markdown(runs, stamp), encoding="utf-8",
                                   newline="\n")
    (out / "runs.csv").write_text(report.render_csv(runs), encoding="utf-8", newline="\n")
    print(f"wrote {out / 'report.md'} and {out / 'runs.csv'} ({len(runs)} runs)")

    if args.mlflow:
        report.log_to_mlflow(runs, args.mlflow_experiment)
        print(f"logged {len(runs)} run(s) to MLflow experiment {args.mlflow_experiment!r}")
    return 1 if any(r.score.verdict == scorers.FAIL for r in runs) else 0


if __name__ == "__main__":
    sys.exit(main())
