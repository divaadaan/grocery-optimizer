"""Deterministic recipe pricing from the deal index.

Roadmap: "compute recipe cost in Python from deal_index instead of trusting
model arithmetic." Each recipe ingredient is matched back to a deal by name;
the deal's sale_price is summed for total_cost and (regular - sale) for
estimated_savings. Model-provided ingredient prices are only a fallback for
names that don't match any deal (typically pantry staples).

Assumes one deal unit per matched ingredient — quantity-aware pricing would
need unit parsing the seed data can't support yet.
"""
from typing import Dict, Iterable, List, NamedTuple, Optional, Set

# The SousChef prompt allows these without a deal; never price them off the
# deal index ("pepper" must not match "Bell Peppers Red").
PANTRY_STAPLES = {"salt", "pepper", "water", "oil"}


class RecipePricing(NamedTuple):
    total_cost: float
    estimated_savings: float
    unmatched: List[str]  # ingredient names priced from model output instead


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _token_set(name: str) -> Set[str]:
    return {_singularize(tok) for tok in name.casefold().split()}


def find_deal(ingredient_name: str, deal_index: Dict[str, Dict]) -> Optional[Dict]:
    """Match an ingredient name to a deal.

    Exact key first, then token-set matching: a match requires one name's
    tokens to be a subset of the other's (case/order/plural-insensitive), so
    "Chicken Breast" finds "Chicken Breast Boneless" and "penne pasta" finds
    "Pasta Penne". Ties go to the highest token overlap (Jaccard).
    """
    if not ingredient_name:
        return None
    if ingredient_name in deal_index:
        return deal_index[ingredient_name]

    tokens = _token_set(ingredient_name)
    if not tokens or tokens <= PANTRY_STAPLES:
        return None

    best, best_score = None, 0.0
    for product_name, deal in deal_index.items():
        deal_tokens = _token_set(product_name)
        if not deal_tokens:
            continue
        if tokens <= deal_tokens or deal_tokens <= tokens:
            score = len(tokens & deal_tokens) / len(tokens | deal_tokens)
            if score > best_score:
                best, best_score = deal, score
    return best


def price_recipe(ingredients: Iterable[Dict], deal_index: Dict[str, Dict]) -> RecipePricing:
    """Price a recipe's ingredient list against the deal index."""
    total = 0.0
    savings = 0.0
    unmatched = []

    for ingredient in ingredients:
        name = ingredient.get("name", "")
        deal = find_deal(name, deal_index)
        if deal is not None:
            sale = float(deal.get("sale_price") or 0.0)
            regular = float(deal.get("regular_price") or sale)
            total += sale
            savings += max(regular - sale, 0.0)
        else:
            unmatched.append(name)
            total += float(ingredient.get("price") or 0.0)

    return RecipePricing(round(total, 2), round(savings, 2), unmatched)
