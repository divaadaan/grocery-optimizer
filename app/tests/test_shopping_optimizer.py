"""Unit tests for the deterministic shopping-list optimizer."""
from app.services.shopping_optimizer import ANY_STORE, optimize_shopping_list


def _item(items, product):
    match = [i for i in items if i["product"] == product]
    assert len(match) == 1, f"expected exactly one item for {product!r}, got {match}"
    return match[0]


# --- cheapest-store selection ----------------------------------------------

def test_cheapest_store_selected_when_two_stores_carry_same_product():
    recipes = [{"ingredients": [{"name": "Pasta Penne", "quantity": "1 box"}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 2.49, "regular_price": 2.99, "store_name": "Store B", "category": "Pasta"},
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Store A", "category": "Pasta"},
    ]
    result = optimize_shopping_list(recipes, deals)
    item = _item(result.items, "Pasta Penne")
    assert item["store"] == "Store A"
    assert item["price"] == 1.49
    assert result.total_cost == 1.49
    assert result.estimated_savings == round(2.99 - 1.49, 2)


def test_deterministic_tie_break_at_equal_price():
    recipes = [{"ingredients": [{"name": "Pasta Penne"}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Zeta Mart", "category": "Pasta"},
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Ace Grocer", "category": "Pasta"},
    ]
    result = optimize_shopping_list(recipes, deals)
    item = _item(result.items, "Pasta Penne")
    assert item["store"] == "Ace Grocer"


# --- consolidation / dedup --------------------------------------------------

def test_consolidation_across_recipes_yields_single_xn_line():
    recipes = [
        {"ingredients": [{"name": "Chicken Breast", "quantity": "2 lb"}]},
        {"ingredients": [{"name": "chicken breast", "quantity": "1 lb"}]},
        {"ingredients": [{"name": "Pasta Penne"}]},
    ]
    deals = [
        {"product_name": "Chicken Breast Boneless", "sale_price": 8.99, "regular_price": 12.99, "store_name": "Store A", "category": "Meat"},
    ]
    result = optimize_shopping_list(recipes, deals)
    assert len(result.items) == 2
    chicken = _item(result.items, "Chicken Breast")
    assert chicken["quantity"] == "x2"


def test_single_occurrence_keeps_own_quantity_or_empty_string():
    recipes = [{"ingredients": [{"name": "Tomato Sauce", "quantity": "1 jar"}, {"name": "Onion"}]}]
    result = optimize_shopping_list(recipes, [])
    tomato = _item(result.items, "Tomato Sauce")
    onion = _item(result.items, "Onion")
    assert tomato["quantity"] == "1 jar"
    assert onion["quantity"] == ""


# --- pantry staples & unmatched ---------------------------------------------

def test_pantry_staple_prices_as_any_store():
    recipes = [{"ingredients": [{"name": "salt", "price": 0.5}]}]
    deals = [
        {"product_name": "Bell Peppers Red", "sale_price": 1.49, "regular_price": 2.49, "store_name": "Store A", "category": "Produce"},
    ]
    result = optimize_shopping_list(recipes, deals)
    salt = _item(result.items, "salt")
    assert salt["store"] == ANY_STORE
    assert salt["price"] == 0.5
    assert salt["category"] is None


def test_genuinely_unmatched_ingredient_prices_as_any_store():
    recipes = [{"ingredients": [{"name": "Dragon Fruit", "price": 3.25}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Store A", "category": "Pasta"},
    ]
    result = optimize_shopping_list(recipes, deals)
    item = _item(result.items, "Dragon Fruit")
    assert item["store"] == ANY_STORE
    assert item["price"] == 3.25


def test_unmatched_ingredient_missing_price_defaults_to_zero():
    recipes = [{"ingredients": [{"name": "Dried Oregano"}]}]
    result = optimize_shopping_list(recipes, [])
    item = _item(result.items, "Dried Oregano")
    assert item["store"] == ANY_STORE
    assert item["price"] == 0.0


# --- estimated_savings -------------------------------------------------------

def test_estimated_savings_sums_regular_minus_sale_across_items():
    recipes = [{"ingredients": [{"name": "Pasta Penne"}, {"name": "Tomato Sauce"}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Store A", "category": "Pasta"},
        {"product_name": "Tomato Sauce", "sale_price": 1.99, "regular_price": 3.49, "store_name": "Store A", "category": "Sauce"},
    ]
    result = optimize_shopping_list(recipes, deals)
    assert result.estimated_savings == round((2.99 - 1.49) + (3.49 - 1.99), 2)


def test_null_regular_price_contributes_zero_savings():
    recipes = [{"ingredients": [{"name": "Pasta Penne"}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": None, "store_name": "Store A", "category": "Pasta"},
    ]
    result = optimize_shopping_list(recipes, deals)
    assert result.estimated_savings == 0.0
    assert result.total_cost == 1.49


# --- token matching parity with costing.find_deal ---------------------------

def test_token_subset_matching_penne_pasta_matches_pasta_penne_deal():
    recipes = [{"ingredients": [{"name": "penne pasta"}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Store A", "category": "Pasta"},
    ]
    result = optimize_shopping_list(recipes, deals)
    item = _item(result.items, "penne pasta")
    assert item["store"] == "Store A"
    assert item["price"] == 1.49


# --- stores list -------------------------------------------------------------

def test_stores_excludes_any_store_and_is_deduplicated_and_sorted():
    recipes = [{"ingredients": [{"name": "Pasta Penne"}, {"name": "Tomato Sauce"}, {"name": "salt"}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Zeta Mart", "category": "Pasta"},
        {"product_name": "Tomato Sauce", "sale_price": 1.99, "regular_price": 3.49, "store_name": "Ace Grocer", "category": "Sauce"},
    ]
    result = optimize_shopping_list(recipes, deals)
    assert result.stores == ["Ace Grocer", "Zeta Mart"]
    assert ANY_STORE not in result.stores


def test_stores_deduplicated_when_same_store_wins_multiple_items():
    recipes = [{"ingredients": [{"name": "Pasta Penne"}, {"name": "Tomato Sauce"}]}]
    deals = [
        {"product_name": "Pasta Penne", "sale_price": 1.49, "regular_price": 2.99, "store_name": "Store A", "category": "Pasta"},
        {"product_name": "Tomato Sauce", "sale_price": 1.99, "regular_price": 3.49, "store_name": "Store A", "category": "Sauce"},
    ]
    result = optimize_shopping_list(recipes, deals)
    assert result.stores == ["Store A"]


# --- edge cases ---------------------------------------------------------------

def test_empty_recipes_returns_empty_optimized_list():
    result = optimize_shopping_list([], [])
    assert result.items == []
    assert result.total_cost == 0.0
    assert result.estimated_savings == 0.0
    assert result.stores == []


def test_ingredient_with_no_name_is_skipped():
    recipes = [{"ingredients": [{"quantity": "1 cup"}, {"name": "Pasta Penne"}]}]
    result = optimize_shopping_list(recipes, [])
    assert len(result.items) == 1
    assert result.items[0]["product"] == "Pasta Penne"
