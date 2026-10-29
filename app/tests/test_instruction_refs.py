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
    parse_step,
    referenced,
    render_step,
    step_problems,
    structured_steps,
    unreferenced,
)
from app.agents.llm_output import GeneratedRecipe, RecipeBatch, invoke_validated
from app.agents.sous_chef import render_instructions, retry_view
from app.services import mlflow_logger
from app.tests.test_cited_compliance import (
    BUTTER,
    DEALS,
    FISH_SAUCE,
    SALT,
    _nutritionist,
    _nutritionist_state,
    _sous_chef,
)


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


def test_generate_recipes_stores_rendered_text_and_structured_steps():
    chef = _sous_chef([json.dumps([{
        "name": "Ratatouille", "servings": 2,
        "ingredients": [{"name": "aubergine", "deal_id": 1},
                        {"name": "sea salt", "product_id": 101}],
        "instructions": ["Slice {0}.", "Season {0} with {1}."]}])])
    (r,) = chef.generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[{"product_name": "E", "deal_id": 1}],
        target_recipe_count=1, household_size=2, dietary_restrictions=["vegan"],
        available_deals=DEALS, pantry_staples=[SALT],
    )
    assert r["instructions"] == ["Slice EGGPLANT ITALIAN.", "Season EGGPLANT ITALIAN with Salt."]
    assert r["instruction_steps"] == [
        {"template": "Slice {0}.", "ingredient_refs": [0]},
        {"template": "Season {0} with {1}.", "ingredient_refs": [0, 1]},
    ]
    prompt = chef.llm.calls[0][0].content
    assert "placeholder {N}" in prompt and "Heat {1} in a pan." in prompt


def test_retry_prompt_shows_templates_not_rendered_text():
    original = {"name": "Old", "ingredients": INGS,
                "instructions": ["Fry EGGPLANT ITALIAN."],
                "instruction_steps": [{"template": "Fry {0}.", "ingredient_refs": [0]}]}
    assert retry_view(original) == {"name": "Old", "ingredients": INGS,
                                    "instructions": ["Fry {0}."]}
    legacy = {"name": "Old", "ingredients": [], "instructions": ["Cook."]}
    assert retry_view(legacy) == legacy


# --- compliance still comes from the citation ---------------------------------

def test_vegan_step_referencing_a_non_vegan_citation_fails_compliance():
    """The model names deal 4 (chicken) "tofu" and staple 103 (butter) "olive
    oil". The rendered step shows the cited names, and the Nutritionist
    rejects on the cited products' attributes, exactly as before."""
    chef = _sous_chef([json.dumps([{
        "name": "Tofu Stir-Fry", "servings": 2,
        "ingredients": [{"name": "tofu", "deal_id": 4},
                        {"name": "olive oil", "product_id": 103}],
        "instructions": ["Fry {0} in {1}."]}])])
    (r,) = chef.generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[{"product_name": "C", "deal_id": 4}],
        target_recipe_count=1, household_size=2, dietary_restrictions=[],
        available_deals=DEALS, pantry_staples=[SALT, FISH_SAUCE, BUTTER],
    )
    assert r["instructions"] == ["Fry Boneless Chicken in Butter."]
    update = _nutritionist().validate_recipes(_nutritionist_state([r], ["vegan"]))
    assert update["rejected_recipe_ids"] == [r["recipe_id"]]
    feedback = update["validation_results"][r["recipe_id"]]["feedback"]
    assert "contains_meat" in feedback and "contains_dairy" in feedback


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
