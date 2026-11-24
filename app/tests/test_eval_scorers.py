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
    assert score.schema_version == scorers.SCORE_SCHEMA_VERSION == 4
    row = score.summary_row()
    assert row == {
        "scenario": "seed27-vegetarian", "snapshot": "seed27", "verdict": "PASS",
        "status": "completed", "recipes_approved": 3, "recipes_requested": 3,
        "schema_retries": 1, "schema_failures": 0, "writer_repairs": 0,
        "instruction_steps": 6, "instruction_invalid_refs": 0,
        "unreferenced_ingredients": 0, "unstructured_instructions": 0,
        "compliance_gate": "PASS", "compliance_rate": 1.0, "verdict_disagreements": 0,
        "unsupported_restrictions": "",
        "citation_validity": 1.0, "invalid_citations": 0,
        # tofu (deal 27) anchors two dishes: charged twice per recipe, once per plan
        "cost_per_recipe_sum_lb": 15.64, "cost_per_unique_deal_lb": 13.15,
        "deal_share": 0.6667, "deals_used": 5, "deals_offered": 22,
        "savings_vs_regular": 6.8, "mean_jaccard": 0.1111, "max_jaccard": 0.3333,
        "llm_calls": 5, "llm_seconds": row["llm_seconds"],
        "total_tokens": row["total_tokens"], "tokens_known_calls": 5,
    }
    assert row["llm_seconds"] == pytest.approx(base_artifact["timing"]["llm_seconds"], abs=1e-5)
    assert row["total_tokens"] == sum(c["response"]["usage"]["total_tokens"]
                                      for c in base_artifact["llm_calls"])

    s = score.structural
    assert s["schema"]["sous_chef"] == {
        "calls": 4, "invocations": 3, "retries": 1, "retried_invocations": 1,
        "failed_invocations": 0, "unverified_invocations": 0, "call_errors": 0}
    assert s["schema"]["chef"]["invocations"] == 1 and s["schema"]["chef"]["retries"] == 0
    assert set(s["schema"]) == {"chef", "sous_chef"}
    assert s["agent_call_log"] == {"chef:ok": 1, "sous_chef_1:ok": 1, "sous_chef_2:ok": 1,
                                   "sous_chef_3:ok": 1}
    assert s["instructions"] == {
        "recipes": 3, "structured_recipes": 3, "unstructured_recipes": 0,
        "steps": 6, "references": 9, "invalid_references": 0,
        "steps_without_references": 3, "unreferenced_ingredients": 0,
        "unreferenced": [], "render_mismatches": 0}
    assert score.citations["counts"]["deal"] == 6 and score.citations["counts"]["staple"] == 3
    assert score.citations["offered_matches_run"] is True
    k = score.compliance
    assert k["required_attributes"] == ["contains_fish", "contains_meat"]
    assert k["agreement"] == {"compared": 3, "agree": 3, "disagreements": []}
    assert k["served_deals_failing_gate"] == []
    assert score.cost["recorded_matches_per_unique_deal"] is True
    assert score.cost["lower_bound"] is True
    assert "budget" not in score.to_dict()
    assert score.on_sale["regular_price_coverage"] == "regular price known for 5/5 deals used"
    assert score.on_sale["used_deal_ids"] == [6, 15, 17, 18, 27]
    lt = score.latency_tokens
    assert lt["roles"]["chef"]["tokens"]["total_tokens"]["known"] == 1
    assert lt["total"]["token_coverage"] == "tokens known for 5/5 calls"


def test_run_score_is_json_serialisable(base_artifact, base_snapshot):
    score = score_run(base_artifact, base_snapshot)
    data = json.loads(score.to_json())
    assert data["schema_version"] == 4 and data["kind"] == "recipe-eval-score"
    assert data == score.to_dict()


def test_snapshot_defaults_to_the_scenario_snapshot(base_artifact, base_snapshot):
    assert score_run(base_artifact).to_dict() == score_run(base_artifact, base_snapshot).to_dict()


def test_snapshot_hash_mismatch_is_refused(art, snap_data):
    snap_data["deals"][0]["sale_price"] += 1
    with pytest.raises(SnapshotDrift):
        score_run(art, Snapshot.from_dict(snap_data))
    with pytest.raises(SnapshotDrift):
        scorers.score_cost(art, Snapshot.from_dict(snap_data))


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
    # seed27's tofu (deal 27, two of the three dishes) is not kosher.
    k = scorers.score_compliance(art, base_snapshot)
    assert k["supported_restrictions"] == ["kosher"] and k["unsupported_restrictions"] == []
    assert k["required_attributes"] == ["not_kosher"]
    assert k["gate"] == FAIL and k["compliant_recipes"] == 1
    assert [r["violations"] for r in k["recipes"] if not r["compliant"]] == [
        [{"citation": "deal 27", "attributes": ["not_kosher"]}]] * 2

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
    # Recompute from attributes, not from the run's verdict (it was vegetarian):
    # the eggs dish fails vegan; without the eggs the plan clears; butter would not.
    set_restrictions(art, ["vegan"])
    k = scorers.score_compliance(art, base_snapshot)
    assert k["gate"] == FAIL
    assert [r["violations"] for r in k["recipes"] if not r["compliant"]] == [
        [{"citation": "deal 6", "attributes": ["contains_egg"]}]]
    eggs_dish = plan(art)[1]
    eggs_dish["ingredients"] = [i for i in eggs_dish["ingredients"] if i.get("deal_id") != 6]
    assert scorers.score_compliance(art, base_snapshot)["gate"] == PASS
    eggs_dish["ingredients"].append({"name": "x", "product_id": 142})   # butter
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
    for lab in snapshot_data["deal_labels"]:
        lab["item"] = "vegan tofu"
    return rebind(artifact, snapshot_data)


@pytest.mark.parametrize("variant", ["fixture", "meat", "null"])
def test_compliance_never_reads_names(art, snap_data, base_snapshot, variant):
    if variant == "meat":
        plan(art)[0]["ingredients"].append({"name": "Tofu", "deal_id": 23})   # chicken thighs
    if variant == "null":
        for p in snap_data["products"]:
            if p["product_id"] == 169:                                      # russet potatoes
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
# Cost (information only)
# --------------------------------------------------------------------------

def test_cost_is_reported_and_never_gates(art, base_snapshot):
    b = scorers.score_cost(art, base_snapshot)
    assert b["lower_bound"] is True and b["caveats"] == list(scorers.COST_CAVEATS)
    assert set(b) >= {"cost_per_recipe_sum", "cost_per_unique_deal", "per_recipe"}
    assert not {"budget", "available", "status"} & set(b)   # budget-agnostic
    assert score_run(art, base_snapshot).verdict == PASS


def test_cost_per_recipe_sum_vs_unique_deal(art, base_snapshot):
    first = plan(art)[0]
    for r in plan(art)[1:]:
        r["ingredients"] = copy.deepcopy(first["ingredients"])
    b = scorers.score_cost(art, base_snapshot)
    assert b["cost_per_unique_deal"] == 5.48            # tofu 2.49 + potatoes 2.99
    assert b["cost_per_recipe_sum"] == 16.44
    assert b["recorded_matches_per_unique_deal"] is False   # state was not re-costed


def test_cost_prices_from_the_snapshot_and_counts_unpriced(art, base_snapshot):
    plan(art)[0]["ingredients"].append({"name": "x", "deal_id": 9999})
    b = scorers.score_cost(art, base_snapshot)
    assert b["unpriced_citations"] == 1 and b["cost_per_recipe_sum"] == 15.64


# --------------------------------------------------------------------------
# On-sale use
# --------------------------------------------------------------------------

def test_null_regular_price_makes_savings_absent(art, snap_data):
    for d in snap_data["deals"]:
        d["regular_price"] = None
    o = scorers.score_on_sale(art, rebind(art, snap_data))
    assert o["savings_vs_regular"] is None
    assert o["regular_price_known"] == 0
    assert o["regular_price_coverage"] == "regular price known for 0/5 deals used"
    assert o["deals_used"] == 5 and o["deal_share"] == 0.6667


def test_partial_regular_price_sums_only_known(art, snap_data):
    for d in snap_data["deals"]:
        if d["deal_id"] == 15:              # bananas: 0.99 - 0.69
            d["regular_price"] = None
    o = scorers.score_on_sale(art, rebind(art, snap_data))
    assert o["savings_vs_regular"] == 6.5 and o["regular_price_known"] == 4


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
    plan(art)[1]["ingredients"].append({"name": "x", "deal_id": 17})
    a = scorers.score_diversity(art, base_snapshot)
    art["final_state"]["approved_recipe_ids"].reverse()
    assert scorers.score_diversity(art, base_snapshot) == a
    # {27,17} vs {27,15} share tofu: 1/3; {6,18,17} vs {27,17}: 1/4
    assert a["max_jaccard"] == pytest.approx(1 / 3, abs=1e-4)
    assert sorted(p["jaccard"] for p in a["pairs"]) == [0.0, 0.25, 0.3333]


# --------------------------------------------------------------------------
# Latency and tokens
# --------------------------------------------------------------------------

def test_null_tokens_stay_null(art, base_snapshot):
    for c in art["llm_calls"]:
        c["response"]["usage"] = None
    lt = scorers.score_latency_tokens(art, base_snapshot)
    assert lt["total"]["tokens"]["total_tokens"] == {"sum": None, "known": 0}
    assert lt["total"]["token_coverage"] == "tokens known for 0/5 calls"
    assert score_run(art, base_snapshot).summary_row()["total_tokens"] is None


def test_partially_reported_usage_fields(art, base_snapshot):
    chef = next(c for c in art["llm_calls"] if c["role"] == "chef")
    chef["response"]["usage"]["output_tokens"] = None
    t = scorers.score_latency_tokens(art, base_snapshot)["roles"]["chef"]["tokens"]
    assert t["output_tokens"] == {"sum": None, "known": 0}
    assert t["input_tokens"] == {"sum": chef["response"]["usage"]["input_tokens"], "known": 1}


# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------

def test_attempt_chains_on_the_fixture(base_artifact):
    chains = scorers.attempt_chains(base_artifact["llm_calls"])
    sous = [c for c in chains if c["role"] == "sous_chef"]
    # One dish's first reply was not JSON: that call continues into attempt 2.
    assert sorted((c["attempt"], c["terminal"]) for c in sous) == [
        (1, False), (1, True), (1, True), (2, True)]


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


def test_citation_patterns_match_the_sous_chef_prompt_lines():
    from app.agents.dish_writer import deal_line, pantry_line
    line = deal_line({"deal_id": 27, "product_name": "Tofu Firm", "sale_price": 2.49,
                      "item": "tofu"})
    assert scorers.DEAL_CITE_RE.findall(line) == ["27"]
    assert scorers.STAPLE_CITE_RE.findall(pantry_line({"product_id": 139, "name": "Flour"})) \
        == ["139"]


def test_exhausted_schema_retries_count_as_failures():
    """A fresh recording (scripted models only) whose SousChefs never emit
    valid JSON: every SousChef invocation exhausts its attempts."""
    from app.tests import test_eval_runner as ter

    def factory(role, spec):
        if role == "sous_chef":
            return ter.scripted_chat(lambda t: "never JSON")
        return ter.scripted_factory(role, spec)

    artifact = runner.record(ter.scenario(), chat_factory=factory)
    score = score_run(artifact)
    sous = score.structural["schema"]["sous_chef"]
    attempts = score.structural["max_attempts"]
    # 3 dishes, each written once and retried once (plan_pipeline: one retry
    # round per failed dish), each invocation exhausting its attempts.
    assert sous["invocations"] == 6 and sous["failed_invocations"] == 6
    assert sous["calls"] == 6 * attempts and sous["retries"] == 6 * (attempts - 1)
    assert score.structural["completed"] is False
    assert score.verdict == FAIL


def test_citation_violation_on_last_attempt_is_a_failure():
    """Last attempt is valid JSON but cites a deal it was not offered: the
    per-call citation context is rebuilt from the prompt, so it counts."""
    from app.tests import test_eval_runner as ter

    def bad_citation(texts):
        reply = json.loads(ter.sous_chef_reply([texts[0]]))
        reply["ingredients"].append({"name": "x", "deal_id": 9999})
        return json.dumps(reply)

    def factory(role, spec):
        if role == "sous_chef":
            return ter.scripted_chat(bad_citation)
        return ter.scripted_factory(role, spec)

    score = score_run(runner.record(ter.scenario(), chat_factory=factory))
    assert score.structural["schema"]["sous_chef"]["failed_invocations"] == 6
    assert score.structural["schema"]["sous_chef"]["unverified_invocations"] == 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_summary_and_json(capsys, tmp_path, base_artifact):
    from scripts import score_eval

    assert score_eval.main([str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("seed27-vegetarian [seed27]: PASS")
    assert "tokens known for 5/5 calls" in out and "lower bound $15.64" in out

    assert score_eval.main([str(FIXTURE), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == PASS and data["schema_version"] == 4

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


# --------------------------------------------------------------------------
# Instruction-step references (Track C)
# --------------------------------------------------------------------------

def test_instructions_metric_counts_bad_and_missing_references(base_artifact, base_snapshot):
    a = copy.deepcopy(base_artifact)
    first = plan(a)[0]
    n = len(first["ingredients"])
    first["instruction_steps"] = [
        {"template": "Slice {0}.", "ingredient_refs": [0]},
        {"template": f"Add {{{n}}} and {{x}}.", "ingredient_refs": [n]},
    ]
    i = scorers.score_instructions(a, base_snapshot)
    assert i["invalid_references"] == 2       # out of range + malformed
    assert i["unreferenced"] == [{"recipe_id": a["final_state"]["approved_recipe_ids"][0],
                                  "ingredient_indices": list(range(1, n))}]
    assert i["unreferenced_ingredients"] == n - 1
    assert i["steps"] == 6 and i["steps_without_references"] == 2


def test_instructions_metric_detects_text_not_rendered_from_citations(base_artifact, base_snapshot):
    a = copy.deepcopy(base_artifact)
    plan(a)[0]["instructions"][0] = "Edited after rendering."
    assert scorers.score_instructions(a, base_snapshot)["render_mismatches"] == 1


def test_scorer_renders_deals_as_the_renderer_does(base_artifact, base_snapshot):
    """The committed run renders linked deals by canonical name and the scorer
    agrees (0 mismatches); text rendered from the flyer names instead is
    reported."""
    assert scorers.score_instructions(base_artifact, base_snapshot)["render_mismatches"] == 0
    a = copy.deepcopy(base_artifact)
    deals = {str(d["deal_id"]): d["product_name"] for d in base_snapshot.data["deals"]}
    changed = 0
    for r in plan(a):
        names = [deals[str(i["deal_id"])] if i.get("deal_id") is not None
                 else i.get("name", "") for i in r["ingredients"]]
        flyer = [s["template"].format(*names) for s in r["instruction_steps"]]
        changed += flyer != r["instructions"]
        r["instructions"] = flyer
    assert changed > 0
    assert scorers.score_instructions(a, base_snapshot)["render_mismatches"] == changed


def test_legacy_artifact_without_instruction_steps_is_unstructured(base_artifact, base_snapshot):
    a = copy.deepcopy(base_artifact)
    for r in plan(a):
        r.pop("instruction_steps")
    i = scorers.score_instructions(a, base_snapshot)
    assert i["unstructured_recipes"] == 3 and i["structured_recipes"] == 0
    assert i["steps"] is None and i["unreferenced_ingredients"] is None
    row = score_run(a, base_snapshot).summary_row()
    assert row["instruction_steps"] is None and row["unstructured_instructions"] == 3


# --------------------------------------------------------------------------
# SousChef repair: scored as the pipeline accepted it
# --------------------------------------------------------------------------

def _writer_call(reply):
    prompt = ("**The dish:** Tofu bowl\n(x)\n\n"
              "- tofu → deal_id 27 (Tofu Firm, $2.49)\n- spinach → deal_id 8 (Baby Spinach, $3)\n"
              "- olive oil → product_id 50\n- salt → product_id 51\n")
    return {"role": "sous_chef", "request": {"messages": [{"type": "human", "content": prompt}]},
            "response": {"content": json.dumps(reply)}, "error": None}


def test_a_repaired_writer_reply_counts_as_valid_and_is_counted(base_artifact, base_snapshot):
    reply = {"name": "Tofu bowl", "ingredients": [
        {"name": "tofu", "deal_id": 27}, {"name": "spinach", "deal_id": 8},
        {"name": "oil", "product_id": 50}, {"name": "mushrooms", "deal_id": "N/A"}],
        "instructions": ["Fry {0} in {2}.", "Add {1} and {3}."]}
    anchors = {"Tofu bowl": [27]}
    assert scorers._last_attempt_valid(_writer_call(reply), anchors) is True
    assert scorers._last_attempt_valid(_writer_call(reply), {"Tofu bowl": [9]}) is False
    reply["ingredients"][3]["deal_id"] = 999  # not offered: never repaired
    assert scorers._last_attempt_valid(_writer_call(reply), anchors) is False

    a = copy.deepcopy(base_artifact)
    a["final_state"]["agent_call_log"][1]["repairs"] = [{"kind": "dropped_ingredient"}]
    assert score_run(a, base_snapshot).summary_row()["writer_repairs"] == 1
