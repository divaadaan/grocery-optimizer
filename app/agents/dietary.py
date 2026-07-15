"""Deterministic dietary-compliance oracle.

Dietary compliance is decided in **code**, not by the Nutritionist LLM. Empirically
(ROADMAP item 5, 2026-07-15) every model tried — SmolLM2-360M SFT, qwen2.5:1.5b,
llama3.1:8b, mistral:7b — false-rejects ~100% of compliant recipes and confabulates
violations, and none of SFT, a larger base, or an authoritative in-prompt reference
table fixes it (the model ignores the reference and rejects anyway). So the LLM is
demoted to advisory nutrition facts and this module owns the approve/reject decision.

The vocabulary is kept identical to ``app/tests/fixtures/nutritionist_cases.json``'s
``forbidden_terms_vegetarian`` and mirrors ``training/data_app/seed_catalog.py``
(which generated the SFT labels) — so training labels, test fixtures, and production
all enforce exactly the same rule.

Caveat: matching is casefold *substring* (mirrors the established oracle) — "ham"
matches "graham", "nut" matches "butternut". No current seed/deal item collides;
tighten to token boundaries if the catalog grows one that does.
"""
from __future__ import annotations

MEAT_POULTRY = ["chicken", "beef", "pork", "turkey", "lamb", "veal", "steak", "bacon", "ham", "sausage"]
FISH_SEAFOOD = ["salmon", "tuna", "fish", "shrimp", "anchovy"]
DAIRY = ["milk", "cheese", "yogurt", "butter", "cream"]
EGG = ["egg"]
NUTS = ["nut", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "peanut"]


def forbidden_terms_for(restrictions: list[str]) -> list[str]:
    """Union of forbidden name-substrings implied by the restriction labels."""
    r = {x.strip().lower() for x in restrictions}
    terms: set[str] = set()
    if "vegan" in r:
        terms |= set(MEAT_POULTRY) | set(FISH_SEAFOOD) | set(DAIRY) | set(EGG)
    if "vegetarian" in r:
        terms |= set(MEAT_POULTRY) | set(FISH_SEAFOOD)
    if "pescatarian" in r:
        terms |= set(MEAT_POULTRY)
    if "no_nuts" in r or "nut_free" in r:
        terms |= set(NUTS)
    return sorted(terms)


def _name_violates(name: str, forbidden: list[str]) -> bool:
    n = (name or "").casefold()
    return any(term in n for term in forbidden)


def recipe_violations(recipe: dict, restrictions: list[str]) -> list[str]:
    """Ingredient names in ``recipe`` that violate the restrictions (empty == compliant).

    Reads the SousChef recipe shape: ``ingredients[*].name``.
    """
    forbidden = forbidden_terms_for(restrictions)
    if not forbidden:
        return []
    return [
        ing["name"]
        for ing in recipe.get("ingredients", [])
        if isinstance(ing, dict) and _name_violates(ing.get("name", ""), forbidden)
    ]


def compliance_report(recipe: dict, restrictions: list[str]) -> dict:
    """Full deterministic verdict for a recipe.

    Returns ``approved`` (no violations at all), the ``violations`` list, and the
    ``allergen_free`` / ``meets_restrictions`` split the ValidationResult carries.
    """
    violations = recipe_violations(recipe, restrictions)
    nut_active = bool({x.strip().lower() for x in restrictions} & {"no_nuts", "nut_free"})
    allergen_viol = [v for v in violations if _name_violates(v, NUTS)] if nut_active else []
    diet_viol = [v for v in violations if v not in allergen_viol]
    return {
        "approved": not violations,
        "violations": violations,
        "allergen_free": not allergen_viol,
        "meets_restrictions": not diet_viol,
    }
