"""Unit tests for deterministic recipe pricing from ingredient citations."""
from app.agents.costing import deals_by_id, ingredient_price, price_recipe, sum_savings


def _deal(deal_id, name, sale, regular):
    return {"deal_id": deal_id, "product_name": name, "sale_price": sale,
            "regular_price": regular}


DEALS = deals_by_id([
    _deal(1, "Chicken Breast Boneless", 8.99, 12.99),
    _deal(3, "Pasta Penne", 1.49, 2.99),
    _deal(4, "Tomato Sauce", 1.99, 3.49),
    _deal(12, "Bell Peppers Red", 1.49, 2.49),
    # Ingested deals mostly carry no regular price.
    _deal(40, "Eggplant", 2.50, None),
])


def test_deals_by_id_keys_are_strings():
    assert set(DEALS) == {"1", "3", "4", "12", "40"}


def test_price_comes_from_the_cited_deal_not_the_name_or_model():
    # Display name says "pepper", model says $99: the citation is all that counts.
    ing = {"name": "pepper", "deal_id": 3, "price": 99.0}
    assert ingredient_price(ing, DEALS) == 1.49


def test_staple_citation_costs_nothing():
    assert ingredient_price({"name": "Salt", "product_id": 101}, DEALS) == 0.0


def test_unresolvable_citation_prices_as_none():
    assert ingredient_price({"name": "Ghost", "deal_id": 999}, DEALS) is None
    assert ingredient_price({"name": "Uncited"}, DEALS) is None


def test_price_recipe_sums_deals_and_ignores_staples():
    pricing = price_recipe([
        {"name": "Penne", "deal_id": 3, "price": 99.0},
        {"name": "Sauce", "deal_id": "4"},
        {"name": "Salt", "product_id": 101},
        {"name": "Olive oil", "product_id": 102, "price": 7.99},
    ], DEALS)
    assert pricing.total_cost == round(1.49 + 1.99, 2)
    assert pricing.estimated_savings == round((2.99 - 1.49) + (3.49 - 1.99), 2)
    assert pricing.unpriced == []


def test_a_deal_cited_twice_is_bought_once():
    pricing = price_recipe([
        {"name": "Penne for the bake", "deal_id": 3},
        {"name": "Penne for the salad", "deal_id": 3},
    ], DEALS)
    assert pricing.total_cost == 1.49


def test_savings_absent_when_no_cited_deal_has_a_regular_price():
    pricing = price_recipe([{"name": "Eggplant", "deal_id": 40}], DEALS)
    assert pricing.total_cost == 2.50
    assert pricing.estimated_savings is None


def test_savings_counts_only_deals_with_a_regular_price():
    pricing = price_recipe(
        [{"name": "Eggplant", "deal_id": 40}, {"name": "Penne", "deal_id": 3}], DEALS
    )
    assert pricing.estimated_savings == round(2.99 - 1.49, 2)


def test_unresolved_citations_are_reported_and_priced_zero():
    pricing = price_recipe(
        [{"name": "Ghost", "deal_id": 999}, {"name": "Penne", "deal_id": 3}], DEALS
    )
    assert pricing.total_cost == 1.49
    assert pricing.unpriced == ["Ghost"]


def test_sum_savings_skips_unknowns():
    assert sum_savings([None, None]) is None
    assert sum_savings([None, 1.5, 0.25]) == 1.75
    assert sum_savings([]) is None
