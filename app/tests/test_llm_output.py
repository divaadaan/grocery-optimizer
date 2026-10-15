"""Unit tests for Pydantic validation/coercion of agent LLM output and the
validate-and-retry invoke helper. No LLM or DB required."""
import json

import pytest
from pydantic import ValidationError

from app.agents.llm_output import (
    ChefPlan,
    GeneratedRecipe,
    LLMOutputError,
    NutritionistVerdict,
    RecipeBatch,
    citation_context,
    invoke_validated,
    raw_output_report,
)

VALID_RECIPE = {
    "name": "Tomato Basil Pasta",
    "ingredients": [
        {"name": "Pasta Penne", "deal_id": 3, "quantity": "16 oz", "unit": "oz"},
    ],
    "instructions": ["Boil pasta.", "Add sauce."],
    "servings": 2,
}


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    """Replays canned responses; records the message lists it was invoked with."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return FakeResponse(self.responses.pop(0))


# --- RecipeBatch shape coercion ------------------------------------------

def test_recipe_batch_from_bare_list():
    batch = RecipeBatch.model_validate([VALID_RECIPE, VALID_RECIPE])
    assert len(batch.recipes) == 2


def test_recipe_batch_from_wrapper_object():
    batch = RecipeBatch.model_validate({"recipes": [VALID_RECIPE]})
    assert len(batch.recipes) == 1


def test_recipe_batch_from_single_recipe_object():
    batch = RecipeBatch.model_validate(VALID_RECIPE)
    assert batch.recipes[0].name == "Tomato Basil Pasta"


def test_recipe_batch_from_keyed_recipes():
    batch = RecipeBatch.model_validate({"recipe_1": VALID_RECIPE, "recipe_2": VALID_RECIPE})
    assert len(batch.recipes) == 2


def test_recipe_batch_rejects_empty_and_garbage():
    with pytest.raises(ValidationError):
        RecipeBatch.model_validate([])
    with pytest.raises(ValidationError):
        RecipeBatch.model_validate({"message": "I cannot create recipes"})


# --- GeneratedRecipe field tolerance --------------------------------------

def test_recipe_lenient_fields():
    recipe = GeneratedRecipe.model_validate({
        **VALID_RECIPE,
        "instructions": "Boil pasta, add sauce.",   # bare string
        "servings": "4",                            # numeric string
        "estimated_prep_time": "45 minutes",        # units in string
        "total_cost": "$12.75",                     # currency string
    })
    assert recipe.instructions == ["Boil pasta, add sauce."]
    assert recipe.servings == 4
    assert recipe.estimated_prep_time == 45
    assert recipe.total_cost == 12.75


def test_recipe_ingredient_price_tolerance():
    recipe = GeneratedRecipe.model_validate({
        **VALID_RECIPE,
        "ingredients": [{"name": "Pasta", "deal_id": 3, "quantity": 2, "price": "$1.99"}],
    })
    ingredient = recipe.ingredients[0]
    assert ingredient.quantity == "2"
    assert ingredient.price == 1.99


def test_recipe_requires_name_and_ingredients():
    with pytest.raises(ValidationError):
        GeneratedRecipe.model_validate({"ingredients": [], "instructions": ["x"]})


# --- ingredient citations ---------------------------------------------------

def _recipe_with(*ingredients):
    return {**VALID_RECIPE, "ingredients": list(ingredients)}


def test_uncited_ingredient_fails_validation():
    with pytest.raises(ValidationError, match="no citation"):
        RecipeBatch.model_validate(_recipe_with({"name": "Pasta Penne", "quantity": "1"}))


def test_doubly_cited_ingredient_fails_validation():
    with pytest.raises(ValidationError, match="both a deal_id and a product_id"):
        RecipeBatch.model_validate(
            _recipe_with({"name": "Pasta", "deal_id": 3, "product_id": 9})
        )


def test_citation_ids_coerced_from_strings():
    batch = RecipeBatch.model_validate(_recipe_with(
        {"name": "Pasta", "deal_id": "3"}, {"name": "Salt", "product_id": " 101 "},
    ))
    ings = batch.recipes[0].ingredients
    assert (ings[0].deal_id, ings[0].product_id) == (3, None)
    assert (ings[1].deal_id, ings[1].product_id) == (None, 101)


def test_non_numeric_citation_fails_validation():
    with pytest.raises(ValidationError):
        RecipeBatch.model_validate(
            _recipe_with({"name": "Pasta", "deal_id": "the pasta one"})
        )


def test_citation_of_an_id_not_offered_fails_with_context():
    ctx = citation_context(deal_ids=[3, 4], product_ids=[101])
    with pytest.raises(ValidationError) as excinfo:
        RecipeBatch.model_validate(
            _recipe_with({"name": "Chicken", "deal_id": 1},
                         {"name": "Fish Sauce", "product_id": 7}),
            context=ctx,
        )
    msg = str(excinfo.value)
    assert "'Chicken' cites deal_id 1" in msg
    assert "'Fish Sauce' cites product_id 7" in msg
    assert "Valid deal_ids: [3, 4]" in msg and "[101]" in msg


def test_offered_citations_pass_with_context():
    ctx = citation_context(deal_ids=["3", 4], product_ids=[101])
    batch = RecipeBatch.model_validate(
        _recipe_with({"name": "Pasta", "deal_id": 3}, {"name": "Salt", "product_id": 101}),
        context=ctx,
    )
    assert len(batch.recipes[0].ingredients) == 2


def test_membership_is_not_checked_without_context():
    """The training/ dataset gate validates without per-call ids; only the
    structural one-citation rule applies there."""
    batch = RecipeBatch.model_validate(_recipe_with({"name": "Pasta", "deal_id": 999}))
    assert batch.recipes[0].ingredients[0].deal_id == 999


def test_bad_citation_goes_back_to_the_model_and_retry_succeeds():
    ctx = citation_context(deal_ids=[3], product_ids=[])
    bad = json.dumps([_recipe_with({"name": "Beef", "deal_id": 5})])
    uncited = json.dumps([_recipe_with({"name": "Pasta"})])
    good = json.dumps([_recipe_with({"name": "Pasta", "deal_id": 3})])
    llm = FakeLLM([bad, uncited, good])
    batch, raw = invoke_validated(llm, "prompt", RecipeBatch, context=ctx)
    assert raw == good
    assert len(llm.calls) == 3
    first_feedback = llm.calls[1][2].content
    assert "cites deal_id 5" in first_feedback and "Valid deal_ids: [3]" in first_feedback
    assert "no citation" in llm.calls[2][4].content


# --- ChefPlan -------------------------------------------------------------

def _group(*names):
    return [{"product_name": n, "quantity_estimate": "1", "deal_id": i}
            for i, n in enumerate(names)]


def test_chef_plan_valid():
    plan = ChefPlan.model_validate({
        "ingredient_groups": [
            _group("Tofu Firm"), _group("Pasta Penne"), _group("Rice Basmati"),
        ],
        "ingredient_reuse_map": {"Tomato Sauce": "2"},  # string count coerced
        "rationale": "spread the staples",
    })
    assert len(plan.ingredient_groups) == 3
    assert plan.ingredient_reuse_map["Tomato Sauce"] == 2


def test_chef_plan_rejects_wrong_group_count():
    with pytest.raises(ValidationError):
        ChefPlan.model_validate({"ingredient_groups": [_group("Tofu"), _group("Rice")]})


def test_chef_plan_rejects_empty_group():
    with pytest.raises(ValidationError):
        ChefPlan.model_validate({"ingredient_groups": [_group("Tofu"), [], _group("Rice")]})


# --- NutritionistVerdict ----------------------------------------------------

def test_verdict_requires_approved():
    with pytest.raises(ValidationError):
        NutritionistVerdict.model_validate({"feedback": "looks fine"})


def test_verdict_defaults_and_coercion():
    verdict = NutritionistVerdict.model_validate(
        {"approved": "false", "health_score": "85"}
    )
    assert verdict.approved is False
    assert verdict.health_score == 85.0
    assert verdict.nutrition_facts == {}
    assert verdict.dietary_compliance == {}


# --- invoke_validated retry loop --------------------------------------------

def test_invoke_validated_first_try():
    llm = FakeLLM([json.dumps([VALID_RECIPE])])
    batch, raw = invoke_validated(llm, "prompt", RecipeBatch)
    assert len(batch.recipes) == 1
    assert raw == json.dumps([VALID_RECIPE])
    assert len(llm.calls) == 1


def test_invoke_validated_retries_with_error_feedback():
    llm = FakeLLM(["not json {", json.dumps({"approved": True, "feedback": "ok"})])
    verdict, _raw = invoke_validated(llm, "prompt", NutritionistVerdict)
    assert verdict.approved is True
    assert len(llm.calls) == 2
    # Retry conversation carries the bad response and the parse error back
    retry_messages = llm.calls[1]
    assert retry_messages[1].content == "not json {"
    assert "invalid" in retry_messages[2].content


def test_invoke_validated_exhausts_attempts():
    llm = FakeLLM(["{}", "{}", "{}"])
    with pytest.raises(LLMOutputError):
        invoke_validated(llm, "prompt", NutritionistVerdict, max_attempts=3)
    assert len(llm.calls) == 3


def test_llm_output_error_carries_raw_attempts():
    """On exhaustion the raw model responses that never validated are attached
    to the exception (and the report helper renders them) so callers can report
    exactly what the model emitted."""
    llm = FakeLLM(['{"a": 1}', '{"b": 2}', '{"c": 3}'])
    with pytest.raises(LLMOutputError) as excinfo:
        invoke_validated(llm, "prompt", NutritionistVerdict, max_attempts=3)
    err = excinfo.value
    assert err.raw_attempts == ['{"a": 1}', '{"b": 2}', '{"c": 3}']
    assert err.last_raw == '{"c": 3}'
    report = raw_output_report(err)
    assert '{"a": 1}' in report and '{"c": 3}' in report
    assert "attempt 3/3" in report


def test_raw_output_report_truncates_long_output():
    err = LLMOutputError("boom", raw_attempts=["x" * 5000])
    report = raw_output_report(err, max_chars=100)
    assert "+4900 chars" in report
