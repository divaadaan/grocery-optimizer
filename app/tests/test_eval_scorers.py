"""Deterministic scorers (ROADMAP Track E §4): app/eval/scorers.py.

Offline: the committed fixture artifact and snapshot, variants of them built
in-test, and (for an exhausted schema retry) a fresh recording with scripted
models. No database, no network, no LLM.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.eval import runner, scorers
from app.eval.runner import Scenario, SnapshotDrift, snapshot_identity
from app.eval.scorers import FAIL, NA, PASS, score_run
from app.eval.snapshots import Snapshot, load_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "eval_runs" / "seed27-vegetarian.json"


@pytest.fixture(autouse=True)
def no_database_no_mlflow(monkeypatch):
    from app.db.database import Database
    from app.services import mlflow_logger

    def no_db(*args, **kwargs):
        raise AssertionError("scorer test touched the database")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", no_db)


@pytest.fixture(scope="module")
def base_artifact():
    return runner.load_artifact(FIXTURE)


@pytest.fixture(scope="module")
def base_snapshot():
    return load_snapshot("seed27")


@pytest.fixture
def art(base_artifact):
    return copy.deepcopy(base_artifact)


@pytest.fixture
def snap_data(base_snapshot):
    return copy.deepcopy(base_snapshot.data)


def rebind(artifact, snapshot_data) -> Snapshot:
    """A modified snapshot, with the artifact's recorded hash updated to it."""
    snap = Snapshot.from_dict(snapshot_data)
    artifact["snapshot"]["content_sha256"] = snapshot_identity(snap)["content_sha256"]
    return snap


def plan(artifact):
    st = artifact["final_state"]
    return [st["generated_recipes"][r] for r in st["approved_recipe_ids"]]


def set_restrictions(artifact, labels):
    artifact["scenario"]["user"]["dietary_restrictions"] = list(labels)
    artifact["final_state"]["dietary_restrictions"] = list(labels)


# --------------------------------------------------------------------------
# The committed fixture, pinned
# --------------------------------------------------------------------------

def test_fixture_scores_are_pinned(base_artifact, base_snapshot):
    score = score_run(base_artifact, base_snapshot)
    assert score.verdict == PASS and score.verdict_reasons == []
    assert score.schema_version == scorers.SCORE_SCHEMA_VERSION == 1
    row = score.summary_row()
    assert row == {
        "scenario": "seed27-vegetarian", "snapshot": "seed27", "verdict": "PASS",
        "status": "completed", "recipes_approved": 3, "recipes_requested": 3,
        "schema_retries": 2, "schema_failures": 0,
        "compliance_gate": "PASS", "compliance_rate": 1.0, "verdict_disagreements": 0,
        "unsupported_restrictions": "",
        "citation_validity": 1.0, "invalid_citations": 0,
        "cost_per_recipe_sum_lb": 7.14, "cost_per_unique_deal_lb": 7.14,
        "budget_available": 80.0, "budget_status": "within_lower_bound",
        "deal_share": 0.6667, "deals_used": 6, "deals_offered": 22,
        "savings_vs_regular": 5.8, "mean_jaccard": 0.0, "max_jaccard": 0.0,
        "llm_calls": 9, "llm_seconds": row["llm_seconds"], "total_tokens": 6000,
        "tokens_known_calls": 6,
    }
    assert row["llm_seconds"] == pytest.approx(base_artifact["timing"]["llm_seconds"], abs=1e-5)

    s = score.structural
    assert s["schema"]["sous_chef"] == {
        "calls": 5, "invocations": 3, "retries": 2, "retried_invocations": 2,
        "failed_invocations": 0, "unverified_invocations": 0, "call_errors": 0}
    assert s["schema"]["chef"]["invocations"] == 1 and s["schema"]["chef"]["retries"] == 0
    assert s["schema"]["nutritionist"]["invocations"] == 3
    assert s["agent_call_log"] == {"Chef_Orchestrator:plan_ingredient_groups": 1,
                                   "Nutritionist:validate_recipe": 3}
    assert score.citations["counts"]["deal"] == 6 and score.citations["counts"]["staple"] == 3
    assert score.citations["offered_matches_run"] is True
    k = score.compliance
    assert k["required_attributes"] == ["contains_fish", "contains_meat"]
    assert k["agreement"] == {"compared": 3, "agree": 3, "disagreements": []}
    assert k["served_deals_failing_gate"] == []
    assert score.budget["recorded_matches_per_recipe_sum"] is True
    assert score.budget["recorded_matches_per_unique_deal"] is True  # no deal reused
    assert score.budget["lower_bound"] is True
    assert score.on_sale["regular_price_coverage"] == "regular price known for 6/6 deals used"
    assert score.on_sale["used_deal_ids"] == [3, 12, 15, 16, 21, 22]
    lt = score.latency_tokens
    assert lt["roles"]["nutritionist"]["tokens"]["total_tokens"] == {"sum": None, "known": 0}
    assert lt["roles"]["chef"]["tokens"]["total_tokens"] == {"sum": 1931, "known": 1}
    assert lt["total"]["token_coverage"] == "tokens known for 6/9 calls"


def test_run_score_is_json_serialisable(base_artifact, base_snapshot):
    score = score_run(base_artifact, base_snapshot)
    data = json.loads(score.to_json())
    assert data["schema_version"] == 1 and data["kind"] == "recipe-eval-score"
    assert data == score.to_dict()


def test_snapshot_defaults_to_the_scenario_snapshot(base_artifact, base_snapshot):
    assert score_run(base_artifact).to_dict() == score_run(base_artifact, base_snapshot).to_dict()


def test_snapshot_hash_mismatch_is_refused(art, snap_data):
    snap_data["deals"][0]["sale_price"] += 1
    with pytest.raises(SnapshotDrift):
        score_run(art, Snapshot.from_dict(snap_data))
    with pytest.raises(SnapshotDrift):
        scorers.score_budget(art, Snapshot.from_dict(snap_data))


# --------------------------------------------------------------------------
# Compliance
# --------------------------------------------------------------------------

def test_non_compliant_approved_recipe_fails_the_run(art, base_snapshot):
    # Deal 1 links chicken breast: meat. The run recorded the recipe approved.
    recipe = plan(art)[0]
    recipe["ingredients"].append({"name": "Chicken", "deal_id": 1, "quantity": "1",
                                  "unit": "unit"})
    score = score_run(art, base_snapshot)
    assert score.verdict == FAIL
    assert any(r.startswith("compliance:") for r in score.verdict_reasons)
    k = score.compliance
    assert k["gate"] == FAIL and k["compliant_recipes"] == 2 and k["compliance_rate"] == 0.6667
    bad = [r for r in k["recipes"] if not r["compliant"]]
    assert bad == [{"recipe_id": recipe["recipe_id"], "compliant": False,
                    "violations": [{"citation": "deal 1", "attributes": ["contains_meat"]}]}]
    # The run's recorded verdict disagrees with the recomputation: a bug signal.
    assert k["agreement"]["disagreements"] == [
        {"recipe_id": recipe["recipe_id"], "recorded": True, "recomputed": False}]
    assert score.citations["counts"]["not_offered_deal"] == 1


def test_null_attribute_fails_closed(art, snap_data):
    for p in snap_data["products"]:
        if p["product_id"] == 139:          # the staple every plan recipe cites
            p["contains_fish"] = None
    snap = rebind(art, snap_data)
    k = scorers.score_compliance(art, snap)
    assert k["gate"] == FAIL and k["compliant_recipes"] == 0
    assert all(r["violations"] == [{"citation": "pantry product 139",
                                    "attributes": ["contains_fish"]}] for r in k["recipes"])
    assert score_run(art, snap).verdict == FAIL


def test_unconfirmed_linked_product_fails_closed(art, snap_data):
    for p in snap_data["products"]:
        if p["product_id"] == 153:          # bananas, deal 15
            p["status"] = "proposed"
    snap = rebind(art, snap_data)
    k = scorers.score_compliance(art, snap)
    assert k["gate"] == FAIL and k["compliant_recipes"] == 2


def test_kosher_is_scored_not_na(art, base_snapshot, snap_data):
    """kosher is enforceable since migration 021: scored like any other label."""
    set_restrictions(art, ["kosher"])
    # seed27's not_kosher is null throughout (undecided): fails closed.
    k = scorers.score_compliance(art, base_snapshot)
    assert k["supported_restrictions"] == ["kosher"] and k["unsupported_restrictions"] == []
    assert k["required_attributes"] == ["not_kosher"]
    assert k["gate"] == FAIL and k["compliant_recipes"] == 0

    for p in snap_data["products"]:
        p["not_kosher"] = False
    snap = rebind(art, snap_data)
    assert scorers.score_compliance(art, snap)["gate"] == PASS

    # Meat with dairy breaks kosher even when each product is kosher.
    plan(art)[0]["ingredients"].append({"name": "x", "deal_id": 5})       # ground beef
    plan(art)[0]["ingredients"].append({"name": "y", "product_id": 142})  # butter
    k = scorers.score_compliance(art, snap)
    assert k["gate"] == FAIL
    bad = [r["violations"] for r in k["recipes"] if not r["compliant"]]
    assert bad == [[{"citation": "deal 5; pantry product 142",
                     "attributes": ["contains_meat", "contains_dairy"]}]]


@pytest.mark.parametrize("labels", [["halal"], ["keto"], ["Halal", " "]])
def test_unsupported_restriction_is_na_never_pass(art, base_snapshot, labels):
    set_restrictions(art, labels)
    score = score_run(art, base_snapshot)
    assert score.verdict == NA
    k = score.compliance
    assert k["gate"] == NA
    assert k["supported_restrictions"] == []
    assert k["unsupported_restrictions"] == [l for l in labels if l.strip()]
    assert k["compliance_rate"] == 1.0  # nothing failed -- but nothing was scored
    assert score.summary_row()["compliance_gate"] == NA


def test_mixed_restrictions_score_the_expressible_part(art, base_snapshot):
    set_restrictions(art, ["vegetarian", "halal"])
    score = score_run(art, base_snapshot)
    assert score.verdict == NA and score.compliance["supported_restrictions"] == ["vegetarian"]
    plan(art)[0]["ingredients"].append({"name": "x", "deal_id": 5})   # ground beef
    score = score_run(art, base_snapshot)
    assert score.verdict == FAIL and score.compliance["gate"] == FAIL


def test_unrestricted_run_passes_vacuously(art, base_snapshot):
    set_restrictions(art, [])
    score = score_run(art, base_snapshot)
    assert score.verdict == PASS
    assert score.compliance["unrestricted"] is True
    assert score.compliance["required_attributes"] == []
    assert score.citations["offered_deals"] == 27


def test_vegan_restriction_reads_byproduct_and_dairy(art, base_snapshot):
    # Recompute from attributes, not from the run's verdict: penne / spaghetti
    # clear vegan in seed27, so this plan stays compliant; butter would not.
    set_restrictions(art, ["vegan"])
    assert scorers.score_compliance(art, base_snapshot)["gate"] == PASS
    plan(art)[1]["ingredients"].append({"name": "x", "product_id": 142})   # butter
    k = scorers.score_compliance(art, base_snapshot)
    assert k["gate"] == FAIL
    assert [r["violations"] for r in k["recipes"] if not r["compliant"]] == [
        [{"citation": "pantry product 142", "attributes": ["contains_dairy"]}]]


def test_served_deal_failing_the_gate_is_reported(art, base_snapshot):
    deals = art["final_state"]["available_deals"]
    deals.append(dict(deals[0], deal_id=9))                # salmon: fish
    k = scorers.score_compliance(art, base_snapshot)
    assert k["served_deals_failing_gate"] == ["9"]
    assert scorers.score_citations(art, base_snapshot)["offered_matches_run"] is False


def _rename_everything(artifact, snapshot_data):
    for i, d in enumerate(snapshot_data["deals"]):
        d["product_name"] = f"zzq-deal-{i}"
    for i, p in enumerate(snapshot_data["products"]):
        p["canonical_name"] = f"qqz-product-{i}"
    st = artifact["final_state"]
    for r in st["generated_recipes"].values():
        r["name"] = "nonsense"
        for ing in r["ingredients"]:
            ing["name"] = "vegetarian tofu, definitely no meat"
    for d in st["available_deals"]:
        d["product_name"] = "beef pork chicken"
    for rows in st["deal_products"].values():
        for p in rows:
            p["canonical_name"] = "salmon"
    for p in st["pantry_staples"]:
        p["canonical_name"] = "lard"
    st["deal_index"] = {f"renamed-{i}": v for i, v in enumerate(st["deal_index"].values())}
    return rebind(artifact, snapshot_data)


@pytest.mark.parametrize("variant", ["fixture", "meat", "null"])
def test_compliance_never_reads_names(art, snap_data, base_snapshot, variant):
    if variant == "meat":
        plan(art)[0]["ingredients"].append({"name": "Tofu", "deal_id": 23})   # chicken thighs
    if variant == "null":
        for p in snap_data["products"]:
            if p["product_id"] == 155:                                      # red peppers
                p["contains_meat"] = None
    before_snap = rebind(art, copy.deepcopy(snap_data))
    before = scorers.score_compliance(art, before_snap)
    renamed = copy.deepcopy(art)
    after = scorers.score_compliance(renamed, _rename_everything(renamed, snap_data))
    assert after == before
    assert before["gate"] == (PASS if variant == "fixture" else FAIL)


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------

def test_citation_classes(art, base_snapshot):
    recipe = plan(art)[0]
    recipe["ingredients"] += [
        {"name": "a", "deal_id": 9999},                    # dangling deal
        {"name": "b", "deal_id": 20},                      # pork chops: not offered
        {"name": "c"},                                     # uncited
        {"name": "d", "deal_id": "", "product_id": None},  # uncited (blank ids)
        {"name": "e", "product_id": 99999},                # dangling product
        {"name": "f", "product_id": 153},                  # bananas: not a staple
        {"name": "g", "deal_id": 15, "product_id": 139},   # double-cited
        "salt",                                            # not structured: uncited
        {"name": "h", "deal_id": "16"},                    # string id: valid
    ]
    c = scorers.score_citations(art, base_snapshot)
    assert c["counts"] == {"deal": 7, "staple": 3, "uncited": 3, "double_cited": 1,
                           "dangling_deal": 1, "not_offered_deal": 1,
                           "dangling_product": 1, "not_staple": 1}
    assert c["ingredients"] == 18 and c["valid"] == 10 and c["invalid"] == 8
    kinds = [i["kind"] for i in c["invalid_citations"]]
    assert kinds == ["dangling_deal", "not_offered_deal", "uncited", "uncited",
                     "dangling_product", "not_staple", "double_cited", "uncited"]
    # Every invalid citation also fails compliance closed (pork is also meat);
    # the double citation's halves (deal 15, staple 139) both clear.
    k = scorers.score_compliance(art, base_snapshot)
    assert [v["citation"] for v in k["recipes"][0]["violations"]] == [
        "deal 9999", "deal 20", "no citation", "no citation", "pantry product 99999",
        "pantry product 153", "no citation"]
    assert k["gate"] == FAIL


def test_double_citation_checks_both_halves(art, base_snapshot):
    # dietary.py alone would read only the deal (bananas) and pass this.
    plan(art)[0]["ingredients"].append({"name": "x", "deal_id": 15, "product_id": 142})
    set_restrictions(art, ["vegan"])
    k = scorers.score_compliance(art, base_snapshot)
    assert k["recipes"][0]["violations"] == [
        {"citation": "pantry product 142", "attributes": ["contains_dairy"]}]


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------

def test_budget_over_limit(art, base_snapshot):
    art["scenario"]["user"]["budget"] = 5.0
    b = scorers.score_budget(art, base_snapshot)
    assert b["status"] == "over" and b["headroom_per_unique_deal"] == -2.14
    assert b["lower_bound"] is True and b["caveats"] == list(scorers.BUDGET_CAVEATS)
    # Budget never gates the verdict; it is reported.
    assert score_run(art, base_snapshot).verdict == PASS


def test_budget_spent_this_period_reduces_the_limit(art, base_snapshot):
    art["scenario"]["user"]["budget_spent_this_period"] = 75.0
    b = scorers.score_budget(art, base_snapshot)
    assert b["available"] == 5.0 and b["status"] == "over"


def test_budget_per_recipe_sum_vs_unique_deal(art, base_snapshot):
    first = plan(art)[0]
    for r in plan(art)[1:]:
        r["ingredients"] = copy.deepcopy(first["ingredients"])
    b = scorers.score_budget(art, base_snapshot)
    assert b["cost_per_unique_deal"] == 2.18
    assert b["cost_per_recipe_sum"] == 6.54
    assert b["recorded_matches_per_recipe_sum"] is False   # state was not re-costed
    art["scenario"]["user"]["budget"] = 4.0
    assert scorers.score_budget(art, base_snapshot)["status"] == "over_per_recipe_sum_only"


def test_budget_prices_from_the_snapshot_and_counts_unpriced(art, base_snapshot):
    plan(art)[0]["ingredients"].append({"name": "x", "deal_id": 9999})
    b = scorers.score_budget(art, base_snapshot)
    assert b["unpriced_citations"] == 1 and b["cost_per_recipe_sum"] == 7.14


# --------------------------------------------------------------------------
# On-sale use
# --------------------------------------------------------------------------

def test_null_regular_price_makes_savings_absent(art, snap_data):
    for d in snap_data["deals"]:
        d["regular_price"] = None
    o = scorers.score_on_sale(art, rebind(art, snap_data))
    assert o["savings_vs_regular"] is None
    assert o["regular_price_known"] == 0
    assert o["regular_price_coverage"] == "regular price known for 0/6 deals used"
    assert o["deals_used"] == 6 and o["deal_share"] == 0.6667


def test_partial_regular_price_sums_only_known(art, snap_data):
    for d in snap_data["deals"]:
        if d["deal_id"] == 15:              # bananas: 0.99 - 0.69
            d["regular_price"] = None
    o = scorers.score_on_sale(art, rebind(art, snap_data))
    assert o["savings_vs_regular"] == 5.5 and o["regular_price_known"] == 5


# --------------------------------------------------------------------------
# Diversity
# --------------------------------------------------------------------------

def test_identical_recipes_have_jaccard_one(art, base_snapshot):
    first = plan(art)[0]
    for r in plan(art)[1:]:
        r["ingredients"] = copy.deepcopy(first["ingredients"])
    d = scorers.score_diversity(art, base_snapshot)
    assert d["mean_jaccard"] == 1.0 and d["max_jaccard"] == 1.0
    assert [p["jaccard"] for p in d["pairs"]] == [1.0, 1.0, 1.0]
    assert d["distinct_deals"] == 2


def test_diversity_is_independent_of_plan_order(art, base_snapshot):
    plan(art)[1]["ingredients"].append({"name": "x", "deal_id": 15})
    a = scorers.score_diversity(art, base_snapshot)
    art["final_state"]["approved_recipe_ids"].reverse()
    assert scorers.score_diversity(art, base_snapshot) == a
    assert a["max_jaccard"] == pytest.approx(1 / 4, abs=1e-4)


# --------------------------------------------------------------------------
# Latency and tokens
# --------------------------------------------------------------------------

def test_null_tokens_stay_null(art, base_snapshot):
    for c in art["llm_calls"]:
        c["response"]["usage"] = None
    lt = scorers.score_latency_tokens(art, base_snapshot)
    assert lt["total"]["tokens"]["total_tokens"] == {"sum": None, "known": 0}
    assert lt["total"]["token_coverage"] == "tokens known for 0/9 calls"
    assert score_run(art, base_snapshot).summary_row()["total_tokens"] is None


def test_partially_reported_usage_fields(art, base_snapshot):
    chef = next(c for c in art["llm_calls"] if c["role"] == "chef")
    chef["response"]["usage"]["output_tokens"] = None
    t = scorers.score_latency_tokens(art, base_snapshot)["roles"]["chef"]["tokens"]
    assert t["output_tokens"] == {"sum": None, "known": 0}
    assert t["input_tokens"] == {"sum": 1800, "known": 1}


# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------

def test_attempt_chains_on_the_fixture(base_artifact):
    chains = scorers.attempt_chains(base_artifact["llm_calls"])
    sous = [c for c in chains if c["role"] == "sous_chef"]
    assert sorted((c["attempt"], c["terminal"]) for c in sous) == [
        (1, False), (1, False), (1, True), (2, True), (2, True)]


def test_partial_plan_fails_structurally(art, base_snapshot):
    st = art["final_state"]
    st["approved_recipe_ids"] = st["approved_recipe_ids"][:2]
    score = score_run(art, base_snapshot)
    assert score.verdict == FAIL
    assert score.verdict_reasons == ["structural: 2/3 recipes approved"]
    assert score.compliance["gate"] == PASS


def test_failed_run_fails_structurally(art, base_snapshot):
    art["final_state"]["status"] = "failed"
    assert score_run(art, base_snapshot).verdict == FAIL


def test_plan_naming_a_missing_recipe_is_refused(art, base_snapshot):
    art["final_state"]["approved_recipe_ids"].append("no-such-recipe")
    with pytest.raises(scorers.ScoringError):
        score_run(art, base_snapshot)


def test_prompt_headers_match_the_sous_chef_templates():
    from app.agents.prompts import PromptTemplates
    for template in (PromptTemplates.SOUS_CHEF_RECIPE_GENERATION,
                     PromptTemplates.SOUS_CHEF_RETRY_WITH_FEEDBACK):
        assert scorers.ASSIGNED_DEALS_HEADER in template
        assert scorers.STAPLES_HEADER in template


def test_exhausted_schema_retries_count_as_failures():
    """A fresh recording (scripted models only) whose SousChefs never emit
    valid JSON: every SousChef invocation exhausts its attempts."""
    from app.tests import test_eval_runner as ter

    def factory(role, spec):
        if role == "sous_chef":
            return ter.ConversationModel(respond=lambda t: "never JSON", usage=False)
        return ter.scripted_factory(role, spec)

    artifact = runner.record(ter.scenario(), llm_factory=factory)
    score = score_run(artifact)
    sous = score.structural["schema"]["sous_chef"]
    attempts = score.structural["max_attempts"]
    # 3 initial SousChefs, then each of the 2 retry iterations tops up the
    # 3-recipe shortfall across the 3 groups (graph.retry_generation).
    assert sous["invocations"] == 9 and sous["failed_invocations"] == 9
    assert sous["calls"] == 9 * attempts and sous["retries"] == 9 * (attempts - 1)
    assert score.structural["completed"] is False
    assert score.verdict == FAIL


def test_citation_violation_on_last_attempt_is_a_failure():
    """Last attempt is valid JSON but cites a deal it was not offered: the
    per-call citation context is rebuilt from the prompt, so it counts."""
    from app.tests import test_eval_runner as ter

    def bad_citation(texts):
        reply = json.loads(ter.sous_chef_reply(texts))
        reply["recipes"][0]["ingredients"].append({"name": "x", "deal_id": 9999})
        return json.dumps(reply)

    def factory(role, spec):
        if role == "sous_chef":
            return ter.ConversationModel(respond=bad_citation)
        return ter.scripted_factory(role, spec)

    score = score_run(runner.record(ter.scenario(), llm_factory=factory))
    assert score.structural["schema"]["sous_chef"]["failed_invocations"] == 9  # + 2 top-ups
    assert score.structural["schema"]["sous_chef"]["unverified_invocations"] == 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_summary_and_json(capsys, tmp_path, base_artifact):
    from scripts import score_eval

    assert score_eval.main([str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("seed27-vegetarian [seed27]: PASS")
    assert "tokens known for 6/9 calls" in out and "lower bound $7.14" in out

    assert score_eval.main([str(FIXTURE), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == PASS and data["schema_version"] == 1

    bad = copy.deepcopy(base_artifact)
    plan(bad)[0]["ingredients"].append({"name": "x", "deal_id": 1})
    path = runner.write_artifact(bad, tmp_path / "bad.json")
    assert score_eval.main([str(FIXTURE), str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "compliance  FAIL 2/3" in out

    assert score_eval.main([str(path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == FAIL


def test_cli_refuses_a_drifted_snapshot(capsys, tmp_path, base_artifact):
    from scripts import score_eval

    drifted = copy.deepcopy(base_artifact)
    drifted["snapshot"]["content_sha256"] = "0" * 64
    path = runner.write_artifact(drifted, tmp_path / "drift.json")
    assert score_eval.main([str(path)]) == 2
    assert "cannot score" in capsys.readouterr().err
