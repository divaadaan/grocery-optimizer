"""Instruction steps reference ingredients by index (ROADMAP Track C).

Steps are templates whose ingredients are ``{N}`` placeholders into the
recipe's own ingredients list; validation is structural, rendering substitutes
the *cited* item's name. No name, keyword or food-word matching anywhere --
including the residual case pinned at the bottom. No DB, no LLM.
"""
import json

import pytest
from pydantic import ValidationError

from app.agents.instruction_refs import (
    deal_cited_name,
    parse_step,
    referenced,
    render_step,
    step_problems,
    structured_steps,
    unreferenced,
)
from app.agents.dietary import compliance_report
from app.agents.llm_output import GeneratedRecipe, RecipeBatch, invoke_validated
from app.agents.recipe_build import build_recipe, render_instructions
from app.services import mlflow_logger
from app.tests.test_cited_compliance import (
    CATALOG,
    COCONUT_MILK,
    DEALS,
    PROPOSED_TOFU,
)


def _build(model_json, staples=(), deal_products=None):
    """A stored recipe from one validated model recipe (what dish_writer does)."""
    model = GeneratedRecipe.model_validate(json.loads(model_json))
    return build_recipe("sous_chef_1", model, {str(d["deal_id"]): d for d in DEALS},
                        lambda chef_id, m: "r1", list(staples), deal_products)


@pytest.fixture(autouse=True)
def no_mlflow(monkeypatch):
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", lambda: False)

INGS = [
    {"name": "eggplant", "deal_id": 1},
    {"name": "coconut milk", "deal_id": 2},
    {"name": "salt", "product_id": 101},
]


def recipe(*steps, ingredients=INGS):
    return {"name": "Curry", "ingredients": list(ingredients), "instructions": list(steps)}


# --- parser -----------------------------------------------------------------

def test_parse_step_finds_refs_in_order_with_repeats():
    assert parse_step("Fry {0}, add {2}, then more {0}.") == ([0, 2, 0], [])
    assert parse_step("Preheat the oven.") == ([], [])


@pytest.mark.parametrize("text,bad", [
    ("Add {}.", ["{}"]),
    ("Add {a}.", ["{a}"]),
    ("Add { 1 }.", ["{ 1 }"]),
    ("Add {-1}.", ["{-1}"]),
    ("Add {1.", ["{"]),
    ("Add 1}.", ["}"]),
    ("Add {{0}}.", ["{}"]),
    ("Add {name}.", ["{name}"]),
])
def test_malformed_placeholders_are_reported(text, bad):
    assert parse_step(text).malformed == bad


def test_referenced_and_unreferenced():
    steps = ["Fry {0}.", "Add {0} and {2}."]
    assert referenced(steps) == [0, 2]
    assert unreferenced(steps, 4) == [1, 3]


def test_render_substitutes_every_placeholder():
    assert render_step("Fry {1} in {0}; more {1}.", ["oil", "tofu"].__getitem__) == \
        "Fry tofu in oil; more tofu."


def test_structured_steps_keep_template_and_refs():
    assert structured_steps(["Fry {1} in {0}.", "Serve."]) == [
        {"template": "Fry {1} in {0}.", "ingredient_refs": [1, 0]},
        {"template": "Serve.", "ingredient_refs": []},
    ]


# --- schema validation ------------------------------------------------------

def test_valid_refs_pass_and_steps_without_refs_are_allowed():
    r = GeneratedRecipe.model_validate(recipe("Preheat the pan.", "Fry {0} in {1} with {2}."))
    assert r.unreferenced_ingredients() == []


def test_out_of_range_index_is_a_validation_error():
    with pytest.raises(ValidationError, match=r"index\(es\) \[3\].*valid placeholders: \{0\}..\{2\}"):
        GeneratedRecipe.model_validate(recipe("Fry {0} and {3}."))


@pytest.mark.parametrize("step", ["Fry {0} and {x}.", "Fry {0} and {}.", "Fry {0} {"])
def test_malformed_placeholder_is_a_validation_error(step):
    with pytest.raises(ValidationError, match="malformed placeholder"):
        GeneratedRecipe.model_validate(recipe(step))


def test_steps_referencing_no_ingredient_are_a_validation_error():
    with pytest.raises(ValidationError, match="no instruction step references an ingredient"):
        GeneratedRecipe.model_validate(recipe("Cook everything.", "Serve."))


def test_unreferenced_ingredient_is_reported_not_rejected():
    r = GeneratedRecipe.model_validate(recipe("Fry {0}."))
    assert r.unreferenced_ingredients() == [1, 2]


def test_bad_reference_goes_back_to_the_model_and_retry_succeeds():
    class LLM:
        def __init__(self):
            self.calls = []
            self.replies = [json.dumps([recipe("Fry {7}.")]), json.dumps([recipe("Fry {0}.")])]

        def invoke(self, messages):
            self.calls.append(messages)

            class R:
                content = self.replies.pop(0)
            return R()

    llm = LLM()
    batch, _ = invoke_validated(llm, "prompt", RecipeBatch)
    assert batch.recipes[0].instructions == ["Fry {0}."]
    assert "index(es) [7]" in llm.calls[1][-1].content


def test_step_problems_agree_with_the_schema():
    assert step_problems(["Fry {0}."], 1) == []
    assert len(step_problems(["Fry {1} {x}."], 1)) == 2


# --- rendering --------------------------------------------------------------

def test_rendering_uses_the_cited_names_not_the_model_names():
    ings = [{"name": "tofu", "deal_id": 4},          # model calls chicken "tofu"
            {"name": "olive oil", "product_id": 103}]  # ...and butter "olive oil"
    deal_lookup = {str(d["deal_id"]): d for d in DEALS}
    staples = [{"product_id": 103, "name": "Butter"}]
    assert render_instructions(ings, ["Fry {0} in {1}."], deal_lookup, staples) == \
        ["Fry Boneless Chicken in Butter."]


def test_built_recipe_stores_rendered_text_and_structured_steps():
    r = _build(json.dumps({
        "name": "Ratatouille", "servings": 2,
        "ingredients": [{"name": "aubergine", "deal_id": 1},
                        {"name": "sea salt", "product_id": 101}],
        "instructions": ["Slice {0}.", "Season {0} with {1}."]}),
        staples=[{"product_id": 101, "name": "Salt"}])
    assert r["instructions"] == ["Slice EGGPLANT ITALIAN.", "Season EGGPLANT ITALIAN with Salt."]
    assert r["instruction_steps"] == [
        {"template": "Slice {0}.", "ingredient_refs": [0]},
        {"template": "Season {0} with {1}.", "ingredient_refs": [0, 1]},
    ]


# --- compliance still comes from the citation ---------------------------------

def test_vegan_step_referencing_a_non_vegan_citation_fails_compliance():
    """The model names deal 4 (chicken) "tofu" and staple 103 (butter) "olive
    oil". The rendered step shows the cited names, and the verdict rejects on
    the cited products' attributes."""
    r = _build(json.dumps({
        "name": "Tofu Stir-Fry", "servings": 2,
        "ingredients": [{"name": "tofu", "deal_id": 4},
                        {"name": "olive oil", "product_id": 103}],
        "instructions": ["Fry {0} in {1}."]}),
        staples=[{"product_id": 103, "name": "Butter"}])
    assert r["instructions"] == ["Fry Boneless Chicken in Butter."]
    report = compliance_report(r, ["vegan"], CATALOG)
    assert not report["approved"]
    text = " ".join(report["violations"])
    assert "contains_meat" in text and "contains_dairy" in text


# --- residual risk, pinned on purpose ---------------------------------------

def test_a_literal_food_word_in_step_text_is_not_detected_by_design():
    """The structural guarantee covers what a step *references*, not the words
    the model types. Catching "butter" here would need a word list or name
    match, which CLAUDE.md forbids, so it validates and renders verbatim. If
    this test starts failing, someone added name matching: remove it."""
    r = GeneratedRecipe.model_validate(recipe("Fry {0} in butter.", "Add {1} and {2}."))
    deal_lookup = {str(d["deal_id"]): d for d in DEALS}
    out = render_instructions([i.model_dump() for i in r.ingredients], r.instructions,
                              deal_lookup, [{"product_id": 101, "name": "Salt"}])
    assert out[0] == "Fry EGGPLANT ITALIAN in butter."


# --- deals render as their linked product's canonical name -------------------

def _deal_ings(*deal_ids):
    return [{"name": "model words", "deal_id": d} for d in deal_ids]


def test_linked_deal_renders_the_products_canonical_name():
    deal_lookup = {str(d["deal_id"]): d for d in DEALS}
    out = render_instructions(_deal_ings(2), ["Simmer {0}."], deal_lookup, [],
                              CATALOG.deal_products)
    assert out == ["Simmer Coconut Milk."]
    assert COCONUT_MILK["canonical_name"] != deal_lookup["2"]["product_name"]


def test_unlinked_deal_falls_back_to_the_flyer_name():
    deal_lookup = {str(d["deal_id"]): d for d in DEALS}
    # deal 6 has no junction row; an absent/empty links mapping is the same
    for links in (CATALOG.deal_products, {}, None):
        assert render_instructions(_deal_ings(6), ["Add {0}."], deal_lookup, [],
                                   links) == ["Add UNLINKED THING."]


def test_deal_linked_to_several_products_keeps_the_flyer_name():
    """Deal 8 ("EGGPLANT OR CHEDDAR 2 FOR $5") links two products. No single
    canonical name describes it, and picking one (first, alphabetical) would
    be an arbitrary choice that misdescribes the other, so the flyer name
    stays."""
    deal_lookup = {str(d["deal_id"]): d for d in DEALS}
    assert len(CATALOG.deal_products["8"]) == 2
    assert render_instructions(_deal_ings(8), ["Bake {0}."], deal_lookup, [],
                               CATALOG.deal_products) == ["Bake EGGPLANT OR CHEDDAR 2 FOR $5."]


def test_unconfirmed_or_blank_canonical_names_are_not_used():
    assert deal_cited_name("TOFU", [PROPOSED_TOFU]) == "TOFU"
    assert deal_cited_name("X", [dict(COCONUT_MILK, canonical_name="  ")]) == "X"
    assert deal_cited_name("X", [dict(COCONUT_MILK, canonical_name=None)]) == "X"
    assert deal_cited_name("X", [dict(COCONUT_MILK)]) == "Coconut Milk"
    assert deal_cited_name("X", []) == "X"


def test_staples_still_render_their_own_name_beside_a_linked_deal():
    deal_lookup = {str(d["deal_id"]): d for d in DEALS}
    ings = [{"name": "a", "deal_id": 2}, {"name": "b", "product_id": 103}]
    out = render_instructions(ings, ["Stir {1} into {0}."], deal_lookup,
                              [{"product_id": 103, "name": "Butter"}],
                              CATALOG.deal_products)
    assert out == ["Stir Butter into Coconut Milk."]


def test_built_recipe_renders_canonical_names_from_deal_products():
    reply = json.dumps({
        "name": "Curry", "servings": 2,
        "ingredients": [{"name": "coconut", "deal_id": 2}],
        "instructions": ["Simmer {0}."]})
    r = _build(reply, deal_products=CATALOG.deal_products)
    assert r["instructions"] == ["Simmer Coconut Milk."]
    assert r["instruction_steps"][0]["template"] == "Simmer {0}."
    # Without links (read unavailable) it degrades to the flyer name.
    assert _build(reply)["instructions"] == ["Simmer AROY-D COCONUT MILK 400 ML."]
