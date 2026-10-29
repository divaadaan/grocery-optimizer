"""Track E §5: the cross-run report. Offline; the clock and MLflow are never touched."""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.eval import report, runner
from app.eval.report import ReportRun
from app.eval.runner import snapshot_identity
from app.eval.scorers import PASS, score_run
from app.eval.snapshots import Snapshot, load_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "eval_runs" / "seed27-vegetarian.json"
GOLDEN = Path(__file__).parent / "fixtures" / "eval_report"
STAMP = "2026-10-30T12:00:00Z"
CLOCK = lambda: datetime(2026, 10, 30, 12, tzinfo=timezone.utc)  # noqa: E731


@pytest.fixture(autouse=True)
def no_database_no_mlflow(monkeypatch):
    from app.db.database import Database
    from app.services import mlflow_logger

    def boom(*args, **kwargs):
        raise AssertionError("report test touched the database / mlflow")

    monkeypatch.setattr(Database, "initialize", boom)
    monkeypatch.setattr(Database, "get_connection", boom)
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", boom)


@pytest.fixture(scope="module")
def base():
    return runner.load_artifact(FIXTURE)


@pytest.fixture(scope="module")
def snap():
    return load_snapshot("seed27")


def _plan(artifact):
    st = artifact["final_state"]
    return [st["generated_recipes"][r] for r in st["approved_recipe_ids"]]


def make_run(artifact, snapshot, source=None) -> ReportRun:
    return ReportRun.from_artifact(artifact, score_run(artifact, snapshot), source)


def fail_variant(base, snap):
    a = copy.deepcopy(base)
    a["scenario"]["name"] = "seed27-vegetarian-leak"
    _plan(a)[0]["ingredients"].append({"name": "Chicken", "deal_id": 1, "quantity": "1",
                                       "unit": "unit"})
    return make_run(a, snap)


def halal_artifact(base):
    a = copy.deepcopy(base)
    a["scenario"]["name"] = "seed27-halal"
    for d in (a["scenario"]["user"], a["final_state"]):
        d["dietary_restrictions"] = ["halal"]
    return a


def sparse_variant(base, snap):
    """No token usage anywhere and no regular prices: tokens and savings are null."""
    a = copy.deepcopy(base)
    a["scenario"]["name"] = "seed27-sparse-data"
    a["llm"]["chef"]["provider"] = "ollama"
    a["llm"]["chef"]["model"] = "qwen-x"
    for c in a["llm_calls"]:
        c["response"]["usage"] = None
    data = copy.deepcopy(snap.data)
    for d in data["deals"]:
        d["regular_price"] = None
    s2 = Snapshot.from_dict(data)
    a["snapshot"]["content_sha256"] = snapshot_identity(s2)["content_sha256"]
    return make_run(a, s2)


@pytest.fixture
def variants(base, snap):
    return [make_run(base, snap, "base.json"), fail_variant(base, snap),
            make_run(halal_artifact(base), snap), sparse_variant(base, snap)]


def _golden(name):
    return (GOLDEN / name).read_text(encoding="utf-8")


def _check_golden(name, actual):
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        (GOLDEN / name).write_text(actual, encoding="utf-8", newline="\n")
    assert actual == _golden(name)


def _rows(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text)))


# --------------------------------------------------------------------------
# Golden output
# --------------------------------------------------------------------------

def test_fixture_report_golden(base, snap):
    runs = [make_run(base, snap)]
    _check_golden("fixture.md", report.render_markdown(runs, STAMP))
    _check_golden("fixture.csv", report.render_csv(runs))


def test_variants_report_golden(variants):
    _check_golden("variants.md", report.render_markdown(variants, STAMP))
    _check_golden("variants.csv", report.render_csv(variants))


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------

def test_markdown_header_and_section_order(variants):
    md = report.render_markdown(variants, STAMP)
    assert "- Generated at: 2026-10-30T12:00:00Z" in md
    assert "- Runs: 4" in md and "- Verdicts: PASS 2 / FAIL 1 / N/A 1" in md
    assert "9b6c694b3e" in md
    assert md.index("# Recipe") < md.index("## Compliance") < md.index("## Runs") \
        < md.index("## Aggregates")


def test_compliance_section_lists_violations_and_unsupported(variants):
    md = report.render_markdown(variants, STAMP)
    comp = md[md.index("## Compliance"):md.index("## Runs")]
    assert "### FAIL (1)" in comp and "seed27-vegetarian-leak" in comp
    assert "deal 1 -> contains_meat" in comp
    assert "### N/A (1)" in comp and "seed27-halal" in comp
    assert "unsupported restriction label(s): halal" in comp
    assert "Chicken" not in md  # never ingredient names


def test_no_fail_or_na_says_none(base, snap):
    md = report.render_markdown([make_run(base, snap)], STAMP)
    assert "### FAIL (0)\n\nNone." in md and "### N/A (0)\n\nNone." in md


def test_budget_is_labelled_lower_bound_with_both_semantics(base, snap):
    md = report.render_markdown([make_run(base, snap)], STAMP)
    assert "budget LB per-recipe-sum" in md and "budget LB per-unique-deal" in md
    assert "lower bounds" in md


def test_token_coverage_shown(base, snap):
    md = report.render_markdown([make_run(base, snap)], STAMP)
    assert "6000 (known for 6/9 calls)" in md


# --------------------------------------------------------------------------
# Nulls
# --------------------------------------------------------------------------

def test_null_tokens_and_savings_are_empty_cells_never_zero(base, snap):
    run = sparse_variant(base, snap)
    [row] = _rows(report.render_csv([run]))
    assert row["total_tokens"] == "" and row["savings_vs_regular"] == ""
    assert row["tokens_known_calls"] == "0"  # a real count, not a missing value
    assert row["chef_provider"] == "ollama" and row["chef_model"] == "qwen-x"
    md = report.render_markdown([run], STAMP)
    assert "n/a (known for 0/9 calls)" in md
    assert "n/a (known for 0/1 runs)" in md  # aggregate savings absent, not $0.00


def test_aggregate_savings_sum_only_known(variants):
    a = report.aggregate(variants)
    assert a["savings_known_runs"] == 3
    assert a["token_calls"] == 36 and a["token_calls_known"] == 18


def test_aggregate_of_all_null_is_none(base, snap):
    a = report.aggregate([sparse_variant(base, snap)])
    assert a["savings_sum"] is None and a["tokens_sum"] is None


def test_csv_columns_stable_and_complete(variants):
    cols = report.csv_columns()
    assert cols[:3] == ["scenario", "snapshot", "snapshot_as_of"]
    assert len(cols) == len(set(cols))
    text = report.render_csv(variants)
    assert text.splitlines()[0] == ",".join(cols)
    for row in _rows(text):
        assert list(row) == cols
    assert set(variants[0].score.summary_row()) <= set(report.csv_row(variants[0]))


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

def test_output_is_independent_of_input_order(variants):
    md, cs = report.render_markdown(variants, STAMP), report.render_csv(variants)
    rev = list(reversed(variants))
    assert report.render_markdown(rev, STAMP) == md
    assert report.render_csv(rev) == cs
    names = [r["scenario"] for r in _rows(cs)]
    assert names == sorted(names)


# --------------------------------------------------------------------------
# MLflow mapping (pure) and laziness
# --------------------------------------------------------------------------

def test_mlflow_payload_maps_params_and_metrics(base, snap):
    params, metrics, tags = report.mlflow_payload(sparse_variant(base, snap))
    assert params["scenario"] == "seed27-sparse-data" and params["snapshot"] == "seed27"
    assert params["chef_model"] == "qwen-x" and params["chef_provider"] == "ollama"
    assert params["snapshot_as_of"] == "2026-07-15"
    assert tags == {"verdict": PASS}
    assert metrics["citation_validity"] == 1.0 and metrics["llm_calls"] == 9.0
    assert "total_tokens" not in metrics and "savings_vs_regular" not in metrics
    assert all(isinstance(v, float) for v in metrics.values())
    assert "verdict" not in metrics and "scenario" not in metrics


def test_importing_report_does_not_import_mlflow():
    code = "import sys, app.eval.report; sys.exit(1 if 'mlflow' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code],
                          cwd=Path(__file__).parents[2]).returncode == 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_writes_report_and_csv_exit_0(tmp_path):
    from scripts import eval_report

    out = tmp_path / "out"
    assert eval_report.main([str(FIXTURE), "--out-dir", str(out)], clock=CLOCK) == 0
    assert (out / "report.md").read_text() == _golden("fixture.md")
    assert (out / "runs.csv").read_text() == _golden("fixture.csv")


def test_cli_directories_globs_dedup_and_exit_1_on_fail(tmp_path, base, monkeypatch):
    from scripts import eval_report

    runs = tmp_path / "runs"
    runs.mkdir()
    runner.write_artifact(base, runs / "a.json")
    bad = copy.deepcopy(base)
    bad["scenario"]["name"] = "leak"
    _plan(bad)[0]["ingredients"].append({"name": "x", "deal_id": 1})
    runner.write_artifact(bad, runs / "b.json")

    def never_mlflow(*a, **k):
        raise AssertionError("mlflow must be off by default")

    monkeypatch.setattr(report, "log_to_mlflow", never_mlflow)
    assert eval_report.main([str(runs), "--out-dir", str(tmp_path / "o1")]) == 1
    assert eval_report.main([str(runs / "*.json"), "--out-dir", str(tmp_path / "o2")]) == 1
    assert len(_rows((tmp_path / "o1" / "runs.csv").read_text())) == 2
    assert eval_report.main([str(runs), str(runs / "a.json"),
                             "--out-dir", str(tmp_path / "o3")]) == 1
    assert len(_rows((tmp_path / "o3" / "runs.csv").read_text())) == 2


def test_cli_skips_non_artifact_json_found_by_directory(tmp_path, base, capsys):
    from scripts import eval_report

    runs = tmp_path / "runs"
    runs.mkdir()
    runner.write_artifact(base, runs / "a.json")
    scenarios = runs / "scenarios.json"
    scenarios.write_text(json.dumps({"scenarios": [base["scenario"]]}))

    assert eval_report.main([str(runs), "--out-dir", str(tmp_path / "o1")]) == 0
    assert "scenarios.json: not a run artifact, skipped" in capsys.readouterr().err
    assert len(_rows((tmp_path / "o1" / "runs.csv").read_text())) == 1
    # Named explicitly, a non-artifact is still an error, not a silent skip.
    assert eval_report.main([str(scenarios), "--out-dir", str(tmp_path / "o2")]) == 2


def test_cli_na_is_not_a_failure(tmp_path, base):
    from scripts import eval_report

    p = runner.write_artifact(halal_artifact(base), tmp_path / "halal.json")
    assert eval_report.main([str(p), "--out-dir", str(tmp_path / "o")]) == 0


def test_cli_exit_2_on_missing_or_drifted(tmp_path, base, capsys):
    from scripts import eval_report

    assert eval_report.main([str(tmp_path / "missing.json"),
                             "--out-dir", str(tmp_path / "o")]) == 2
    assert "no artifact files found" in capsys.readouterr().err
    drifted = copy.deepcopy(base)
    drifted["snapshot"]["content_sha256"] = "0" * 64
    p = runner.write_artifact(drifted, tmp_path / "drift.json")
    assert eval_report.main([str(p), "--out-dir", str(tmp_path / "o")]) == 2
    assert "cannot score" in capsys.readouterr().err
    assert not (tmp_path / "o" / "report.md").exists()


def test_cli_mlflow_flag_calls_logger(tmp_path, monkeypatch):
    from scripts import eval_report

    seen = {}
    monkeypatch.setattr(report, "log_to_mlflow",
                        lambda runs, exp: seen.update(n=len(runs), exp=exp) or len(runs))
    assert eval_report.main([str(FIXTURE), "--out-dir", str(tmp_path), "--mlflow"]) == 0
    assert seen == {"n": 1, "exp": "recipe-eval"}
