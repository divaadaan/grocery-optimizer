"""Cross-run evaluation report (ROADMAP Track E §5): Markdown + CSV, optional MLflow.

Pure and deterministic: the inputs are scored runs (``RunScore`` plus the
identity fields the score does not carry -- snapshot ``as_of``, providers,
``git_dirty``), the output is text. Nothing here touches a database, the
network or the clock; the caller injects ``generated_at``. MLflow is imported
lazily inside :func:`log_to_mlflow` and nothing else needs it.

Missing data stays missing: a null is an empty CSV cell and ``n/a`` in
Markdown, never 0. Compliance violations are reported exactly as the scorer
gives them (citations and attributes); ingredient names never appear.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.eval.recorder import ROLES
from app.eval.scorers import FAIL, NA, PASS, RunScore

NA_TEXT = "n/a"


@dataclass(frozen=True)
class ReportRun:
    """One scored run plus the artifact identity the report needs."""

    score: RunScore
    snapshot_as_of: Optional[str] = None
    providers: Mapping[str, Optional[str]] = field(default_factory=dict)
    git_dirty: Optional[bool] = None
    source: Optional[str] = None

    @classmethod
    def from_artifact(cls, artifact: Mapping[str, Any], score: RunScore,
                      source: Optional[str] = None) -> "ReportRun":
        llm = artifact.get("llm") or {}
        return cls(
            score=score,
            snapshot_as_of=(artifact.get("snapshot") or {}).get("as_of"),
            providers={r: (llm.get(r) or {}).get("provider") for r in ROLES},
            git_dirty=(artifact.get("code") or {}).get("git_dirty"),
            source=source,
        )

    @property
    def scenario(self) -> str:
        return self.score.run["scenario"]

    @property
    def snapshot(self) -> str:
        return self.score.run["snapshot"]

    @property
    def models(self) -> Dict[str, Optional[str]]:
        return {r: self.score.run["models"].get(r) for r in ROLES}

    @property
    def config(self) -> str:
        """Model configuration label: ``role=provider/model`` per role."""
        parts = []
        for r in ROLES:
            m, p = self.models[r], self.providers.get(r)
            parts.append(f"{r}={p + '/' if p else ''}{m if m is not None else '?'}")
        return "; ".join(parts)

    @property
    def git_sha(self) -> Optional[str]:
        return self.score.run.get("git_sha")


def sort_runs(runs: Iterable[ReportRun]) -> List[ReportRun]:
    return sorted(runs, key=lambda r: (r.scenario, r.snapshot, r.config,
                                       str(r.score.run.get("created_at") or ""),
                                       r.source or ""))


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

# summary_row keys other than scenario/snapshot, in the scorer's order.
_SUMMARY_REST = [
    "verdict", "status", "recipes_approved", "recipes_requested", "schema_retries",
    "schema_failures", "instruction_steps", "instruction_invalid_refs",
    "unreferenced_ingredients", "unstructured_instructions", "compliance_gate", "compliance_rate", "verdict_disagreements",
    "unsupported_restrictions", "citation_validity", "invalid_citations",
    "cost_per_recipe_sum_lb", "cost_per_unique_deal_lb", "budget_available",
    "budget_status", "deal_share", "deals_used", "deals_offered", "savings_vs_regular",
    "mean_jaccard", "max_jaccard", "llm_calls", "llm_seconds", "total_tokens",
    "tokens_known_calls",
]


def csv_columns() -> List[str]:
    cols = ["scenario", "snapshot", "snapshot_as_of"]
    for r in ROLES:
        cols += [f"{r}_model", f"{r}_provider"]
    cols += ["git_sha", "git_dirty", "created_at"]
    return cols + _SUMMARY_REST


def csv_row(run: ReportRun) -> Dict[str, Any]:
    row = dict(run.score.summary_row())
    unknown = set(row) - set(_SUMMARY_REST) - {"scenario", "snapshot"}
    if unknown:  # the scorer grew a field: make the column order an explicit decision
        raise ValueError(f"summary_row has unmapped columns: {sorted(unknown)}")
    row["snapshot_as_of"] = run.snapshot_as_of
    for r in ROLES:
        row[f"{r}_model"] = run.models[r]
        row[f"{r}_provider"] = run.providers.get(r)
    row["git_sha"] = run.git_sha
    row["git_dirty"] = run.git_dirty
    row["created_at"] = run.score.run.get("created_at")
    return row


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def render_csv(runs: Sequence[ReportRun]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    cols = csv_columns()
    w.writerow(cols)
    for run in sort_runs(runs):
        row = csv_row(run)
        w.writerow([_cell(row.get(c)) for c in cols])
    return out.getvalue()


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _max(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return max(vals) if vals else None


def _sum_known(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) if vals else None


def aggregate(runs: Sequence[ReportRun]) -> Dict[str, Any]:
    rows = [r.score.summary_row() for r in runs]
    n = len(runs)
    secs = _sum_known([r["llm_seconds"] for r in rows])
    sav = [r["savings_vs_regular"] for r in rows]
    passed = sum(r.score.verdict == PASS for r in runs)
    return {
        "runs": n,
        "pass": passed,
        "fail": sum(r.score.verdict == FAIL for r in runs),
        "na": sum(r.score.verdict == NA for r in runs),
        "pass_rate": (passed / n) if n else None,
        "citation_validity_mean": _mean([r["citation_validity"] for r in rows]),
        "deal_share_mean": _mean([r["deal_share"] for r in rows]),
        "jaccard_mean": _mean([r["mean_jaccard"] for r in rows]),
        "jaccard_max": _max([r["max_jaccard"] for r in rows]),
        "cost_per_recipe_sum_lb_mean": _mean([r["cost_per_recipe_sum_lb"] for r in rows]),
        "cost_per_unique_deal_lb_mean": _mean([r["cost_per_unique_deal_lb"] for r in rows]),
        "over_budget": sum(r["budget_status"] != "within_lower_bound" for r in rows),
        "savings_sum": _sum_known(sav),
        "savings_known_runs": sum(s is not None for s in sav),
        "latency_sum": secs,
        "latency_mean": (secs / n) if (secs is not None and n) else None,
        "tokens_sum": _sum_known([r["total_tokens"] for r in rows]),
        "token_calls_known": sum(r["tokens_known_calls"] for r in rows),
        "token_calls": sum(r["llm_calls"] for r in rows),
    }


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def _esc(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _num(x: Optional[float], nd: int = 2) -> str:
    return NA_TEXT if x is None else f"{x:.{nd}f}"


def _pct(x: Optional[float]) -> str:
    return NA_TEXT if x is None else f"{x:.0%}"


def _money(x: Optional[float]) -> str:
    return NA_TEXT if x is None else f"${x:.2f}"


def _tokens(total: Optional[float], known: int, calls: int) -> str:
    t = NA_TEXT if total is None else f"{int(total)}"
    return f"{t} (known for {known}/{calls} calls)"


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(_esc(c) for c in r) + " |" for r in rows]
    return out


def _instruction_refs(s: Mapping[str, Any]) -> str:
    """Instruction-step references: steps, invalid refs, unreferenced
    ingredients; n/a for a run whose recipes predate placeholder steps."""
    if s["instruction_steps"] is None:
        return NA_TEXT
    text = (f"{s['instruction_steps']} steps, {s['instruction_invalid_refs']} invalid, "
            f"{s['unreferenced_ingredients']} unreferenced")
    if s["unstructured_instructions"]:
        text += f" ({s['unstructured_instructions']} unstructured)"
    return text


def _label(run: ReportRun) -> str:
    return f"{run.scenario} [{run.snapshot}] {run.config}"


def _compliance_section(runs: Sequence[ReportRun]) -> List[str]:
    out = ["## Compliance", "",
           "Compliance is decided only from curated product attributes; "
           "violations are cited by deal/product id.", ""]
    fails = [r for r in runs if r.score.verdict == FAIL]
    nas = [r for r in runs if r.score.verdict == NA]
    out += [f"### FAIL ({len(fails)})", ""]
    if not fails:
        out += ["None.", ""]
    for r in fails:
        k = r.score.compliance
        out.append(f"- **{_label(r)}**")
        for reason in r.score.verdict_reasons:
            out.append(f"  - {reason}")
        for rec in k["recipes"]:
            for v in rec["violations"]:
                out.append(f"  - recipe {rec['recipe_id']}: {v['citation']} -> "
                           f"{', '.join(v['attributes']) or 'no attributes'}")
        if k["agreement"]["disagreements"]:
            out.append(f"  - run's recorded verdict disagrees on "
                       f"{len(k['agreement']['disagreements'])} recipe(s)")
        if k["served_deals_failing_gate"]:
            out.append("  - served deals failing the gate: "
                       + ", ".join(str(d) for d in k["served_deals_failing_gate"]))
    if fails:
        out.append("")
    out += [f"### N/A ({len(nas)})", ""]
    if not nas:
        out += ["None.", ""]
    for r in nas:
        labels = ", ".join(r.score.compliance["unsupported_restrictions"])
        out.append(f"- **{_label(r)}**: unsupported restriction label(s): {labels} "
                   "(not scored; never a pass)")
    if nas:
        out.append("")
    return out


def _runs_table(runs: Sequence[ReportRun]) -> List[str]:
    header = ["scenario", "snapshot", "config", "verdict", "approved", "compliance",
              "citation validity", "instruction refs", "deal share", "jaccard mean/max",
              "budget LB per-recipe-sum", "budget LB per-unique-deal", "budget status",
              "savings", "llm s", "tokens"]
    rows = []
    for r in runs:
        s = r.score.summary_row()
        rows.append([
            s["scenario"], f"{s['snapshot']} ({r.snapshot_as_of or NA_TEXT})", r.config,
            s["verdict"], f"{s['recipes_approved']}/{s['recipes_requested']}",
            s["compliance_gate"], _pct(s["citation_validity"]), _instruction_refs(s),
            _pct(s["deal_share"]),
            f"{_num(s['mean_jaccard'])}/{_num(s['max_jaccard'])}",
            _money(s["cost_per_recipe_sum_lb"]), _money(s["cost_per_unique_deal_lb"]),
            f"{s['budget_status']} (avail {_money(s['budget_available'])})",
            _money(s["savings_vs_regular"]), _num(s["llm_seconds"], 3),
            _tokens(s["total_tokens"], s["tokens_known_calls"], s["llm_calls"]),
        ])
    return _table(header, rows)


def _agg_table(title: str, key_name: str, groups: Mapping[str, Sequence[ReportRun]]) -> List[str]:
    header = [key_name, "runs", "PASS/FAIL/N/A", "pass rate", "citation validity (mean)",
              "deal share (mean)", "jaccard mean (mean)", "jaccard max (max)",
              "budget LB per-recipe-sum (mean)", "budget LB per-unique-deal (mean)",
              "not within LB", "savings (sum, known runs)", "llm s (sum)", "llm s (mean/run)",
              "tokens (sum)"]
    rows = []
    for key in sorted(groups):
        a = aggregate(groups[key])
        rows.append([
            key, a["runs"], f"{a['pass']}/{a['fail']}/{a['na']}", _pct(a["pass_rate"]),
            _pct(a["citation_validity_mean"]), _pct(a["deal_share_mean"]),
            _num(a["jaccard_mean"]), _num(a["jaccard_max"]),
            _money(a["cost_per_recipe_sum_lb_mean"]), _money(a["cost_per_unique_deal_lb_mean"]),
            a["over_budget"],
            f"{_money(a['savings_sum'])} (known for {a['savings_known_runs']}/{a['runs']} runs)",
            _num(a["latency_sum"], 3), _num(a["latency_mean"], 3),
            _tokens(a["tokens_sum"], a["token_calls_known"], a["token_calls"]),
        ])
    return [f"### {title}", ""] + _table(header, rows) + [""]


def _group(runs: Sequence[ReportRun], key) -> Dict[str, List[ReportRun]]:
    g: Dict[str, List[ReportRun]] = {}
    for r in runs:
        g.setdefault(key(r), []).append(r)
    return g


def _git_line(runs: Sequence[ReportRun]) -> str:
    shas = sorted({f"{r.git_sha[:10] if r.git_sha else NA_TEXT}"
                   f"{' (dirty)' if r.git_dirty else ''}" for r in runs})
    return ", ".join(shas) if shas else NA_TEXT


def render_markdown(runs: Sequence[ReportRun], generated_at: str) -> str:
    runs = sort_runs(runs)
    a = aggregate(runs)
    out = [
        "# Recipe evaluation report", "",
        f"- Generated at: {generated_at}",
        f"- Git sha(s): {_git_line(runs)}",
        f"- Runs: {len(runs)}",
        f"- Verdicts: PASS {a['pass']} / FAIL {a['fail']} / N/A {a['na']}",
        "",
    ]
    out += _compliance_section(runs)
    out += ["## Runs", ""]
    out += _runs_table(runs)
    out += ["", "Budget columns are **lower bounds** (one deal unit per cited ingredient; "
            "ignores quantity, household size, multi-buy pricing; pantry staples cost 0). "
            "Both cost semantics are shown: *per-recipe-sum* charges a deal reused across "
            "recipes once per recipe; *per-unique-deal* charges each deal once (the "
            "shopping list, and the plan total since Track C). `savings` is shown only "
            "where regular prices are known, otherwise n/a. Token totals sum only calls that reported usage.", ""]
    out += ["## Aggregates", ""]
    out += _agg_table("By model configuration", "model configuration",
                      _group(runs, lambda r: r.config))
    out += _agg_table("By snapshot", "snapshot",
                      _group(runs, lambda r: f"{r.snapshot} ({r.snapshot_as_of or NA_TEXT})"))
    return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# MLflow (optional, off by default)
# --------------------------------------------------------------------------

def mlflow_payload(run: ReportRun) -> Tuple[Dict[str, str], Dict[str, float], Dict[str, str]]:
    """Pure mapping of one run to MLflow ``(params, metrics, tags)``.

    Params: scenario, snapshot (+as_of), git sha, mode, model/provider per
    role. Metrics: every numeric ``summary_row`` field that is not null (bools
    and strings are skipped; nulls are absent, never 0). Tag: verdict.
    """
    params: Dict[str, str] = {"scenario": run.scenario, "snapshot": run.snapshot}
    optional = {"snapshot_as_of": run.snapshot_as_of, "git_sha": run.git_sha,
                "mode": run.score.run.get("mode")}
    for r in ROLES:
        optional[f"{r}_model"] = run.models[r]
        optional[f"{r}_provider"] = run.providers.get(r)
    params.update({k: str(v) for k, v in optional.items() if v is not None})
    metrics = {k: float(v) for k, v in run.score.summary_row().items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return params, metrics, {"verdict": run.score.verdict}


def log_to_mlflow(runs: Sequence[ReportRun], experiment: str = "recipe-eval") -> int:
    """Log one MLflow run per scored run. Imports mlflow lazily; returns the count."""
    import mlflow  # noqa: PLC0415 -- optional dependency

    mlflow.set_experiment(experiment)
    for run in sort_runs(runs):
        params, metrics, tags = mlflow_payload(run)
        with mlflow.start_run(run_name=f"{run.scenario}@{run.snapshot}"):
            mlflow.log_params(params)
            mlflow.log_metrics(metrics)
            mlflow.set_tags(tags)
    return len(runs)


__all__ = [
    "ReportRun", "aggregate", "csv_columns", "csv_row", "log_to_mlflow", "mlflow_payload",
    "render_csv", "render_markdown", "sort_runs",
]
