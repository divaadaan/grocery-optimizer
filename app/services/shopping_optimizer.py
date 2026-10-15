"""Deterministic shopping-list builder, driven by ingredient citations.

Pure, DB-free, LLM-free. Every SousChef ingredient cites what it is made from
(``llm_output.RecipeIngredient``), and this module reads the citation -- it
never matches an ingredient to a deal by name:

* ``deal_id``    -> a line at that deal's store, product and ``sale_price``.
  The same deal cited by several recipes is one line.
* ``product_id`` -> an always-on-hand pantry staple (output validation only
  admits ``products.assumed_on_hand`` ids). The household already has it, so
  it is LEFT OFF the list. The list response has no "assumed on hand" slot
  (``items`` feed the per-store grouping and the cost total), and a $0 row
  under a store header is exactly the bug this replaces.
* neither        -> a LEGACY ingredient, stored before citations existed. It
  cannot be priced without guessing by name, so it is listed as
  ``UNKNOWN_STORE`` with ``price`` ``None`` and contributes nothing to the
  total. Lines of identical text (case-insensitive) are merged for display
  only; nothing is looked up.

The old version also compared the same product across stores to pick the
cheapest. That required name matching, so it is gone: a citation names one
specific deal and that is the line.

Savings: ``regular_price - sale_price`` over listed deals that carry a regular
price. When none do, ``estimated_savings`` is ``None`` (absent, not 0).
"""
from typing import Dict, List, NamedTuple, Optional

from app.agents.costing import deals_by_id

# Sentinel store for legacy uncited ingredients: price and store are unknown.
UNKNOWN_STORE = "Store unknown"


class OptimizedList(NamedTuple):
    items: List[dict]          # each: {"product", "quantity", "store", "price", "category"}
    total_cost: float          # rounded 2dp; excludes unpriced (legacy) lines
    estimated_savings: Optional[float]  # rounded 2dp; None if no regular price known
    stores: List[str]          # distinct real store names (sorted), excluding UNKNOWN_STORE


def stores_from_items(items: List[dict]) -> List[str]:
    """Distinct real store names from a shopping list's items, sorted."""
    return sorted(
        {i["store"] for i in items if i.get("store") and i["store"] != UNKNOWN_STORE}
    )


def _quantity(count: int, first: object) -> str:
    if count > 1:
        return f"x{count}"
    return "" if first is None else str(first)


def optimize_shopping_list(recipes: List[Dict], deals: List[Dict]) -> OptimizedList:
    """Build one shopping list from the recipes' ingredient citations."""
    if not recipes:
        return OptimizedList([], 0.0, None, [])

    lookup = deals_by_id(deals)

    # Insertion-ordered; first-seen quantity is kept for single occurrences.
    by_deal: Dict[str, dict] = {}
    legacy: Dict[str, dict] = {}

    for recipe in recipes:
        for ingredient in recipe.get("ingredients") or []:
            name = ingredient.get("name")
            deal_id = ingredient.get("deal_id")
            if deal_id is not None:
                key = str(deal_id)
                entry = by_deal.get(key)
                if entry is None:
                    by_deal[key] = {"name": name, "count": 1,
                                    "quantity": ingredient.get("quantity")}
                else:
                    entry["count"] += 1
            elif ingredient.get("product_id") is not None:
                continue  # on-hand staple: not something to buy
            elif name:
                key = str(name).strip().casefold()
                entry = legacy.get(key)
                if entry is None:
                    legacy[key] = {"name": name, "count": 1,
                                   "quantity": ingredient.get("quantity")}
                else:
                    entry["count"] += 1

    items: List[dict] = []
    total_cost = 0.0
    savings = 0.0
    savings_known = False

    for key, entry in by_deal.items():
        deal = lookup.get(key)
        if deal is None:
            # Cited deal no longer in the current deal set (expired or gated
            # out): keep the line, honestly unpriced.
            items.append({"product": entry["name"],
                          "quantity": _quantity(entry["count"], entry["quantity"]),
                          "store": UNKNOWN_STORE, "price": None, "category": None})
            continue
        sale = float(deal.get("sale_price") or 0.0)
        regular = deal.get("regular_price")
        if regular is not None:
            savings_known = True
            savings += max(float(regular) - sale, 0.0)
        total_cost += sale
        items.append({
            "product": deal.get("product_name") or entry["name"],
            "quantity": _quantity(entry["count"], entry["quantity"]),
            "store": deal.get("store_name") or UNKNOWN_STORE,
            "price": sale,
            "category": deal.get("category"),
        })

    for entry in legacy.values():
        items.append({"product": entry["name"],
                      "quantity": _quantity(entry["count"], entry["quantity"]),
                      "store": UNKNOWN_STORE, "price": None, "category": None})

    return OptimizedList(
        items=items,
        total_cost=round(total_cost, 2),
        estimated_savings=round(savings, 2) if savings_known else None,
        stores=stores_from_items(items),
    )
