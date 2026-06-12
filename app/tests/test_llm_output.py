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
    invoke_validated,
)

VALID_RECIPE = {
    "name": "Tomato Basil Pasta",
    "ingredients": [{"name": "Pasta Penne", "quantity": "16 oz", "unit": "oz", "price": 1.99}],
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
        "ingredients": [{"name": "Pasta", "quantity": 2, "price": "$1.99"}],
    })
    ingredient = recipe.ingredients[0]
    assert ingredient.quantity == "2"
    assert ingredient.price == 1.99

def test_recipe_requires_name_and_ingredients():
    with pytest.raises(ValidationError):
        GeneratedRecipe.model_validate({"ingredients": [], "instructions": ["x"]})


# --- ChefPlan -------------------------------------------------------------

def _group(*names):
    return [{"product_name": n, "quantity_estimate": "1", "deal_id": i} for i, n in enumerate(names)]

def test_chef_plan_valid():
    plan = ChefPlan.model_validate({
        "ingredient_groups": [_group("Tofu Firm"), _group("Pasta Penne"), _group("Rice Basmati")],
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
    verdict = NutritionistVerdict.model_validate({"approved": "false", "health_score": "85"})
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
