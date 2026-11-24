"""Unit tests for Pydantic validation/coercion of the SousChef's recipe
output, and its validate-and-retry through ``llm_chat.chat_validated``. No
LLM or DB required."""
import json

import pytest
from pydantic import ValidationError

from app.agents.llm_chat import Reply, chat_validated
from app.agents.llm_output import GeneratedRecipe, RecipeBatch, citation_context

VALID_RECIPE = {
    "name": "Tomato Basil Pasta",
    "ingredients": [
        {"name": "Pasta Penne", "deal_id": 3, "quantity": "16 oz", "unit": "oz"},
    ],
    "instructions": ["Boil {0}.", "Add sauce."],
    "servings": 2,
}


class FakeChat:
    """A chat function replaying canned replies; records each conversation."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, spec, messages, **params):
        self.calls.append(messages)
        return Reply(text=self.responses.pop(0))


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
        "instructions": "Boil {0}, add sauce.",     # bare string
        "servings": "4",                            # numeric string
        "estimated_prep_time": "45 minutes",        # units in string
        "total_cost": "$12.75",                     # currency string
    })
    assert recipe.instructions == ["Boil {0}, add sauce."]
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
    """Without a context (no per-call ids), only the
    structural one-citation rule applies there."""
    batch = RecipeBatch.model_validate(_recipe_with({"name": "Pasta", "deal_id": 999}))
    assert batch.recipes[0].ingredients[0].deal_id == 999


def test_bad_citation_goes_back_to_the_model_and_retry_succeeds():
    ctx = citation_context(deal_ids=[3], product_ids=[])
    bad = json.dumps({"recipes": [_recipe_with({"name": "Beef", "deal_id": 5})]})
    good = json.dumps({"recipes": [_recipe_with({"name": "Pasta", "deal_id": 3})]})
    chat = FakeChat([bad, good])
    batch, replies = chat_validated(
        chat, "ollama:m", [{"role": "user", "content": "prompt"}],
        lambda data: RecipeBatch.model_validate(data, context=ctx))
    assert replies[-1].text == good and len(chat.calls) == 2
    feedback = chat.calls[1][2]["content"]
    assert "cites deal_id 5" in feedback and "Valid deal_ids: [3]" in feedback
    assert batch.recipes[0].ingredients[0].deal_id == 3
