"""Unit tests for deterministic recipe pricing against the deal index."""
from app.agents.costing import find_deal, price_recipe

DEAL_INDEX = {
    "Chicken Breast Boneless": {"deal_id": 1, "sale_price": 8.99, "regular_price": 12.99},
    "Pasta Penne": {"deal_id": 3, "sale_price": 1.49, "regular_price": 2.99},
    "Tomato Sauce": {"deal_id": 4, "sale_price": 1.99, "regular_price": 3.49},
    "Sweet Potatoes": {"deal_id": 10, "sale_price": 1.99, "regular_price": 2.99},
    "Bell Peppers Red": {"deal_id": 12, "sale_price": 1.49, "regular_price": 2.49},
    "Olive Oil Extra Virgin": {"deal_id": 26, "sale_price": 7.99, "regular_price": 11.99},
}


# --- find_deal matching ----------------------------------------------------

def test_exact_match():
    assert find_deal("Pasta Penne", DEAL_INDEX)["deal_id"] == 3

def test_case_and_order_insensitive():
    assert find_deal("penne pasta", DEAL_INDEX)["deal_id"] == 3

def test_token_subset_drops_qualifier():
    # Model output often drops brand/cut qualifiers
    assert find_deal("Chicken Breast", DEAL_INDEX)["deal_id"] == 1

def test_singular_plural():
    assert find_deal("sweet potato", DEAL_INDEX)["deal_id"] == 10

def test_olive_oil_matches_despite_extra_tokens():
    assert find_deal("olive oil", DEAL_INDEX)["deal_id"] == 26

def test_pantry_staples_never_match_deals():
    # "pepper" the spice must not be priced as "Bell Peppers Red"
    assert find_deal("pepper", DEAL_INDEX) is None
    assert find_deal("salt", DEAL_INDEX) is None
    assert find_deal("water", DEAL_INDEX) is None

def test_no_match_returns_none():
    assert find_deal("Dragon Fruit", DEAL_INDEX) is None
    assert find_deal("", DEAL_INDEX) is None


# --- price_recipe ------------------------------------------------------------

def test_price_recipe_uses_deal_prices_not_model_arithmetic():
    ingredients = [
        {"name": "Pasta Penne", "price": 99.0},   # model price ignored when deal matches
        {"name": "Tomato Sauce", "price": 0.0},
    ]
    pricing = price_recipe(ingredients, DEAL_INDEX)
    assert pricing.total_cost == 1.49 + 1.99
    assert pricing.estimated_savings == round((2.99 - 1.49) + (3.49 - 1.99), 2)
    assert pricing.unmatched == []

def test_price_recipe_falls_back_to_model_price_for_unmatched():
    ingredients = [
        {"name": "Pasta Penne", "price": 99.0},
        {"name": "Dried Oregano", "price": 1.25},  # no deal — model price used
    ]
    pricing = price_recipe(ingredients, DEAL_INDEX)
    assert pricing.total_cost == 1.49 + 1.25
    assert pricing.unmatched == ["Dried Oregano"]

def test_price_recipe_handles_missing_price_fields():
    pricing = price_recipe([{"name": "Salt"}, {"name": "Water", "price": None}], DEAL_INDEX)
    assert pricing.total_cost == 0.0
    assert set(pricing.unmatched) == {"Salt", "Water"}
