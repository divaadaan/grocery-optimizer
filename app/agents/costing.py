"""Deterministic recipe pricing from ingredient citations.

Roadmap: "compute recipe cost in Python ... instead of trusting model
arithmetic." Every SousChef ingredient cites its source (``llm_output``):

* ``deal_id``    -> that deal's ``sale_price`` is added to ``total_cost``.
* ``product_id`` -> an always-on-hand pantry staple: costs nothing for this
  plan (the household already has it), so it adds 0.

Nothing is matched by name any more. The old token matcher priced "pepper" the
spice as "Bell Peppers Red" unless a hard-coded staple list intervened, and
silently fell back to model-invented prices for anything it could not match.

Savings: ``regular_price - sale_price`` is summed only over cited deals that
actually carry a regular price. Ingested deal data mostly does not, so when no
cited deal has one, ``estimated_savings`` is ``None`` -- absent, not a
fabricated 0 or a guess (CLAUDE.md "Product framing").

Assumes one deal unit per cited ingredient, as before -- quantity-aware
pricing needs unit parsing the data cannot support yet.
"""
from typing import Dict, Iterable, List, NamedTuple, Optional, Set


class RecipePricing(NamedTuple):
    total_cost: float
    # None when no cited deal carries a regular price: savings unknown, not 0.
    estimated_savings: Optional[float]
    # Display names of ingredients whose citation did not resolve. Output
    # validation should make this empty; they are priced at 0 and reported.
    unpriced: List[str]


def deals_by_id(deals: Iterable[Dict]) -> Dict[str, Dict]:
    """Index deal rows by ``str(deal_id)`` for citation lookups."""
    out: Dict[str, Dict] = {}
    for deal in deals or []:
        deal_id = deal.get("deal_id", deal.get("id"))
        if deal_id is not None:
            out[str(deal_id)] = deal
    return out


def ingredient_price(ingredient: Dict, deal_lookup: Dict[str, Dict]) -> Optional[float]:
    """Price of one cited ingredient: the cited deal's sale price, 0.0 for a
    pantry staple, or None when the citation does not resolve."""
    deal_id = ingredient.get("deal_id")
    if deal_id is not None:
        deal = deal_lookup.get(str(deal_id))
        if deal is None:
            return None
        return float(deal.get("sale_price") or 0.0)
    if ingredient.get("product_id") is not None:
        return 0.0
    return None


def price_recipe(
    ingredients: Iterable[Dict], deal_lookup: Dict[str, Dict]
) -> RecipePricing:
    """Price a recipe's cited ingredients. ``deal_lookup`` is :func:`deals_by_id`.

    A deal cited twice in one recipe is bought once.
    """
    total = 0.0
    savings = 0.0
    savings_known = False
    unpriced: List[str] = []
    seen_deals: Set[str] = set()

    for ingredient in ingredients:
        price = ingredient_price(ingredient, deal_lookup)
        if price is None:
            unpriced.append(str(ingredient.get("name", "")))
            continue
        deal_id = ingredient.get("deal_id")
        if deal_id is None:
            continue  # staple: on hand, costs nothing
        key = str(deal_id)
        if key in seen_deals:
            continue
        seen_deals.add(key)
        total += price
        regular = deal_lookup[key].get("regular_price")
        if regular is not None:
            savings_known = True
            savings += max(float(regular) - price, 0.0)

    return RecipePricing(
        round(total, 2),
        round(savings, 2) if savings_known else None,
        unpriced,
    )


def sum_savings(values: Iterable[Optional[float]]) -> Optional[float]:
    """Sum per-recipe savings, skipping unknowns; None if all are unknown."""
    known = [v for v in values if v is not None]
    return round(sum(known), 2) if known else None
