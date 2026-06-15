"""Single source of truth for the seed deals, dietary profiles, and the
forbidden-term compliance rule shared by every generator.

The deals and the vegetarian forbidden list are read from
``app/tests/fixtures/nutritionist_cases.json`` — the *same* fixture the
acceptance-bar tests (``test_chef_groups_respect_vegetarian_restriction`` and
the nutritionist regression cases) read, so the training data and the tests can
never drift apart.

Compliance uses the exact substring rule the chef test asserts with::

    any(term in product_name.casefold() for term in forbidden)
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "app" / "tests" / "fixtures" / "nutritionist_cases.json"

# --- Forbidden-term vocabularies -----------------------------------------
# MEAT_POULTRY + FISH_SEAFOOD is, by construction, exactly the fixture's
# ``forbidden_terms_vegetarian`` list (asserted in load_fixture below).
MEAT_POULTRY = ["chicken", "beef", "pork", "turkey", "lamb", "veal", "steak", "bacon", "ham", "sausage"]
FISH_SEAFOOD = ["salmon", "tuna", "fish", "shrimp", "anchovy"]
DAIRY = ["milk", "cheese", "yogurt", "butter", "cream"]
EGG = ["egg"]
NUTS = ["nut", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "peanut"]


def forbidden_terms_for(restrictions: list[str]) -> list[str]:
    """Union of forbidden substrings implied by a list of restriction labels."""
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


def is_compliant(product_name: str, restrictions: list[str]) -> bool:
    forbidden = forbidden_terms_for(restrictions)
    name = product_name.casefold()
    return not any(term in name for term in forbidden)


def violating_terms(product_name: str, restrictions: list[str]) -> list[str]:
    """Which forbidden terms a product name trips (for verdict feedback)."""
    name = product_name.casefold()
    return [term for term in forbidden_terms_for(restrictions) if term in name]


# --- Dietary profiles to render examples for ------------------------------
# Headline target is ["vegetarian", "no_nuts"] (the user profile in the
# fixture and the xfail acceptance test). The others add what cross-restriction
# variety the single-postal-code seed catalog allows.
PROFILES: list[dict] = [
    {"restrictions": ["vegetarian", "no_nuts"], "budget": 75.0, "household_size": 2, "num_meals": 7},
    {"restrictions": ["vegetarian"], "budget": 50.0, "household_size": 4, "num_meals": 5},
    {"restrictions": ["vegan"], "budget": 60.0, "household_size": 2, "num_meals": 6},
    {"restrictions": ["pescatarian"], "budget": 80.0, "household_size": 3, "num_meals": 7},
]


# --- Product categorization (for complementary chef groups) ---------------
# Checked in priority order so e.g. "Tomato Sauce"/"Canned Tomatoes" land in
# pantry, not vegetable, and "Sweet Potatoes" lands in starch.
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("pantry", ["oil", "sauce", "canned", "vinegar", "stock", "broth", "flour", "sugar"]),
    ("dairy", ["milk", "cheese", "yogurt", "butter", "cream"]),
    ("protein", ["tofu", "egg", "bean", "lentil", "chickpea", "chicken", "beef", "pork",
                 "turkey", "lamb", "veal", "steak", "bacon", "ham", "sausage",
                 "salmon", "tuna", "fish", "shrimp"]),
    ("starch", ["pasta", "penne", "spaghetti", "rice", "bread", "potato", "quinoa",
                "noodle", "tortilla", "oat"]),
    ("fruit", ["banana", "apple", "berry", "orange", "grape", "lemon", "lime", "mango"]),
    ("vegetable", ["broccoli", "spinach", "pepper", "carrot", "onion", "avocado",
                   "lettuce", "tomato", "kale", "zucchini", "mushroom", "cabbage", "cauliflower"]),
]

# Dairy and protein both count as a "protein source" when balancing groups.
PROTEIN_CATEGORIES = ("protein", "dairy")

_QTY_BY_CATEGORY = {
    "protein": ("1", "lb"),
    "dairy": ("500", "mL"),
    "starch": ("16", "oz"),
    "vegetable": ("2", "cups"),
    "fruit": ("3", ""),
    "pantry": ("2", "tbsp"),
    "other": ("1", ""),
}


def categorize(product_name: str) -> str:
    name = product_name.casefold()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in name for kw in keywords):
            return category
    return "other"


def quantity_estimate(category: str) -> str:
    qty, unit = _QTY_BY_CATEGORY.get(category, _QTY_BY_CATEGORY["other"])
    return f"{qty} {unit}".strip()


def recipe_amount(category: str) -> tuple[str, str]:
    qty, unit = _QTY_BY_CATEGORY.get(category, _QTY_BY_CATEGORY["other"])
    return qty, unit


# --- Fixture loading ------------------------------------------------------
def load_fixture(path: Path | str | None = None) -> dict:
    path = Path(path) if path else FIXTURE_PATH
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    # Guard the invariant our forbidden vocab depends on: the fixture's
    # vegetarian list must equal what vegetarian compliance enforces.
    fixture_veg = set(fixture["chef_grouping"]["forbidden_terms_vegetarian"])
    ours = set(MEAT_POULTRY) | set(FISH_SEAFOOD)
    if fixture_veg != ours:
        raise ValueError(
            "vegetarian forbidden terms drifted from the fixture: "
            f"fixture-only={sorted(fixture_veg - ours)} code-only={sorted(ours - fixture_veg)}"
        )
    return fixture


def load_deals(path: Path | str | None = None) -> list[dict]:
    """The 27 M5V3A8 seed deals in ``fetch_current_deals`` row shape."""
    return load_fixture(path)["chef_grouping"]["deals"]
