"""Deterministic shopping-list optimizer (v1 = cheapest-per-item).

Pure, DB-free, LLM-free: consolidates ingredients across a batch of recipes
and assigns each one to the cheapest store carrying a matching deal, using
the same token-set matching as ``app.agents.costing`` so behavior stays
consistent between recipe pricing and shopping-list generation.

Unlike ``costing.find_deal`` (which matches against a ``deal_index`` that has
already collapsed multiple stores' listings of the same product down to one
entry), this module searches the FULL flat ``deals`` list so it can compare
prices for the same product ACROSS stores and pick the cheapest one.
"""
from typing import Dict, List, NamedTuple, Optional, Set

from app.agents.costing import PANTRY_STAPLES, _token_set

# Sentinel store for ingredients with no matching deal (pantry staples or
# genuinely unmatched items) — priced from the ingredient's own price, if any.
ANY_STORE = "Any store"


class OptimizedList(NamedTuple):
    items: List[dict]          # each: {"product", "quantity", "store", "price", "category"}
    total_cost: float          # rounded 2dp
    estimated_savings: float   # rounded 2dp
    stores: List[str]          # distinct real store names (sorted), excluding ANY_STORE


def _find_cheapest_deal(tokens: Set[str], deals: List[Dict]) -> Optional[Dict]:
    """Cheapest deal (across all stores) whose product matches ``tokens``.

    Matching mirrors ``costing.find_deal``: one name's token set must be a
    subset of the other's (case/order/plural-insensitive). Pantry staples
    never match. Among all matching deals — any product, any store — the
    lowest ``sale_price`` wins; ties break on store_name (alphabetical) so
    results are deterministic.
    """
    if not tokens or tokens <= PANTRY_STAPLES:
        return None

    candidates = []
    for deal in deals:
        product_name = deal.get("product_name")
        if not product_name:
            continue
        deal_tokens = _token_set(product_name)
        if not deal_tokens:
            continue
        if tokens <= deal_tokens or deal_tokens <= tokens:
            candidates.append(deal)

    if not candidates:
        return None

    candidates.sort(key=lambda d: (float(d.get("sale_price") or 0.0), d.get("store_name") or ""))
    return candidates[0]


def optimize_shopping_list(recipes: List[Dict], deals: List[Dict]) -> OptimizedList:
    """Consolidate recipe ingredients into a single cheapest-per-item shopping list."""
    if not recipes:
        return OptimizedList([], 0.0, 0.0, [])

    # Consolidate by normalized (token-set) name, preserving first-seen order
    # and first-seen display casing/quantity/price.
    consolidated: Dict[frozenset, dict] = {}
    order: List[frozenset] = []

    for recipe in recipes:
        for ingredient in recipe.get("ingredients") or []:
            name = ingredient.get("name")
            if not name:
                continue
            tokens = frozenset(_token_set(name))
            if not tokens:
                continue
            if tokens not in consolidated:
                quantity = ingredient.get("quantity")
                consolidated[tokens] = {
                    "name": name,
                    "count": 1,
                    "quantity": "" if quantity is None else str(quantity),
                    "price": ingredient.get("price"),
                }
                order.append(tokens)
            else:
                consolidated[tokens]["count"] += 1

    items = []
    total_cost = 0.0
    total_savings = 0.0
    stores: Set[str] = set()

    for tokens in order:
        entry = consolidated[tokens]
        count = entry["count"]
        quantity = f"x{count}" if count > 1 else entry["quantity"]

        deal = _find_cheapest_deal(set(tokens), deals)
        if deal is not None:
            sale = float(deal.get("sale_price") or 0.0)
            regular_raw = deal.get("regular_price")
            regular = float(regular_raw) if regular_raw is not None else sale
            total_savings += max(regular - sale, 0.0)
            store = deal.get("store_name")
            price = sale
            category = deal.get("category")
            stores.add(store)
        else:
            store = ANY_STORE
            price = float(entry["price"] or 0.0)
            category = None

        total_cost += price
        items.append({
            "product": entry["name"],
            "quantity": quantity,
            "store": store,
            "price": price,
            "category": category,
        })

    return OptimizedList(
        items=items,
        total_cost=round(total_cost, 2),
        estimated_savings=round(total_savings, 2),
        stores=sorted(stores),
    )
