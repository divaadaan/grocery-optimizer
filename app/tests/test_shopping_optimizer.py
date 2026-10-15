"""Unit tests for the citation-driven shopping-list builder.

Ingredients cite a ``deal_id`` (buy it) or a ``product_id`` (on-hand staple).
Nothing here matches by name: the "legacy" tests prove an uncited ingredient
stays unpriced even when a deal with the identical name is on offer.
"""
from app.services.shopping_optimizer import (
    UNKNOWN_STORE,
    optimize_shopping_list,
    stores_from_items,
)

PENNE = {"deal_id": 1, "product_name": "Pasta Penne", "sale_price": 1.49,
         "regular_price": 2.99, "store_name": "Store A", "category": "Pasta"}
SAUCE = {"deal_id": 2, "product_name": "Tomato Sauce", "sale_price": 1.99,
         "regular_price": 3.49, "store_name": "Ace Grocer", "category": "Sauce"}


def _item(items, product):
    match = [i for i in items if i["product"] == product]
    assert len(match) == 1, f"expected exactly one item for {product!r}, got {match}"
    return match[0]


# --- deal citations ----------------------------------------------------------

def test_deal_citation_gives_that_deals_store_price_and_product():
    recipes = [{"ingredients": [{"name": "penne", "deal_id": 1, "quantity": "1 box"}]}]
    result = optimize_shopping_list(recipes, [PENNE, SAUCE])
    item = _item(result.items, "Pasta Penne")  # deal's product, not the display name
    assert (item["store"], item["price"], item["category"]) == ("Store A", 1.49, "Pasta")
    assert item["quantity"] == "1 box"
    assert result.total_cost == 1.49
    assert result.stores == ["Store A"]


def test_citation_is_followed_even_when_the_display_name_disagrees():
    recipes = [{"ingredients": [{"name": "Tomato Sauce", "deal_id": 1}]}]
    result = optimize_shopping_list(recipes, [PENNE, SAUCE])
    assert [i["product"] for i in result.items] == ["Pasta Penne"]


def test_string_deal_ids_resolve():
    recipes = [{"ingredients": [{"name": "x", "deal_id": "1"}]}]
    assert _item(optimize_shopping_list(recipes, [PENNE]).items, "Pasta Penne")


def test_same_deal_cited_by_two_recipes_is_one_line():
    recipes = [
        {"ingredients": [{"name": "penne", "deal_id": 1}, {"name": "sauce", "deal_id": 2}]},
        {"ingredients": [{"name": "pasta", "deal_id": 1}]},
    ]
    result = optimize_shopping_list(recipes, [PENNE, SAUCE])
    assert len(result.items) == 2
    assert _item(result.items, "Pasta Penne")["quantity"] == "x2"
    assert result.total_cost == round(1.49 + 1.99, 2)


def test_cited_deal_missing_from_current_deals_is_unpriced_not_dropped():
    recipes = [{"ingredients": [{"name": "Peanut Butter", "deal_id": 99}]}]
    result = optimize_shopping_list(recipes, [PENNE])
    item = _item(result.items, "Peanut Butter")
    assert item["store"] == UNKNOWN_STORE
    assert item["price"] is None
    assert result.total_cost == 0.0


# --- staples -----------------------------------------------------------------

def test_staple_citation_never_appears_as_a_store_line():
    recipes = [{"ingredients": [
        {"name": "salt", "product_id": 5, "price": 0.5},
        {"name": "penne", "deal_id": 1},
    ]}]
    result = optimize_shopping_list(recipes, [PENNE])
    assert [i["product"] for i in result.items] == ["Pasta Penne"]
    assert all(i["store"] != UNKNOWN_STORE for i in result.items)


def test_recipe_of_only_staples_gives_an_empty_list():
    recipes = [{"ingredients": [{"name": "salt", "product_id": 5},
                                {"name": "oil", "product_id": 6}]}]
    result = optimize_shopping_list(recipes, [PENNE])
    assert result.items == []
    assert result.total_cost == 0.0
    assert result.estimated_savings is None


# --- legacy (uncited) recipes -------------------------------------------------

def test_uncited_legacy_ingredient_is_unpriced_even_with_a_same_name_deal():
    recipes = [{"ingredients": [{"name": "Pasta Penne", "price": 3.25, "quantity": "1"}]}]
    result = optimize_shopping_list(recipes, [PENNE])
    item = _item(result.items, "Pasta Penne")
    assert item["store"] == UNKNOWN_STORE
    assert item["price"] is None  # neither the deal's price nor the model's
    assert result.total_cost == 0.0
    assert result.estimated_savings is None
    assert result.stores == []


def test_uncited_staple_like_name_is_still_listed_not_guessed_free():
    recipes = [{"ingredients": [{"name": "salt"}]}]
    item = _item(optimize_shopping_list(recipes, []).items, "salt")
    assert item["store"] == UNKNOWN_STORE and item["price"] is None


def test_identical_legacy_lines_merge_for_display_only():
    recipes = [{"ingredients": [{"name": "Onion"}]}, {"ingredients": [{"name": "onion "}]}]
    result = optimize_shopping_list(recipes, [])
    assert len(result.items) == 1
    assert result.items[0]["quantity"] == "x2"


# --- estimated_savings -------------------------------------------------------

def test_estimated_savings_sums_regular_minus_sale():
    recipes = [{"ingredients": [{"name": "a", "deal_id": 1}, {"name": "b", "deal_id": 2}]}]
    result = optimize_shopping_list(recipes, [PENNE, SAUCE])
    assert result.estimated_savings == round((2.99 - 1.49) + (3.49 - 1.99), 2)
    assert result.stores == ["Ace Grocer", "Store A"]


def test_no_regular_price_gives_none_savings_not_zero():
    deal = dict(PENNE, regular_price=None)
    recipes = [{"ingredients": [{"name": "a", "deal_id": 1}]}]
    result = optimize_shopping_list(recipes, [deal])
    assert result.estimated_savings is None
    assert result.total_cost == 1.49


def test_savings_known_when_any_listed_deal_has_a_regular_price():
    recipes = [{"ingredients": [{"name": "a", "deal_id": 1}, {"name": "b", "deal_id": 2}]}]
    result = optimize_shopping_list(recipes, [dict(PENNE, regular_price=None), SAUCE])
    assert result.estimated_savings == round(3.49 - 1.99, 2)


# --- edge cases ---------------------------------------------------------------

def test_empty_recipes_returns_empty_optimized_list():
    result = optimize_shopping_list([], [])
    assert result.items == []
    assert result.total_cost == 0.0
    assert result.estimated_savings is None
    assert result.stores == []


def test_ingredient_with_no_name_and_no_citation_is_skipped():
    recipes = [{"ingredients": [{"quantity": "1 cup"}, {"name": "a", "deal_id": 1}]}]
    assert len(optimize_shopping_list(recipes, [PENNE]).items) == 1


# --- stores_from_items --------------------------------------------------------

def test_stores_from_items_excludes_unknown_dedups_and_sorts():
    items = [
        {"product": "Pasta Penne", "store": "Zeta Mart"},
        {"product": "Tomato Sauce", "store": "Ace Grocer"},
        {"product": "Onion", "store": "Zeta Mart"},
        {"product": "salt", "store": UNKNOWN_STORE},
        {"product": "x"},
        {"product": "y", "store": None},
    ]
    assert stores_from_items(items) == ["Ace Grocer", "Zeta Mart"]


def test_stores_from_items_handles_empty_list():
    assert stores_from_items([]) == []
