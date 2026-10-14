"""The staple-grocery search terms that drive deal ingestion.

**This is the file you edit to change what gets ingested.**

Why a hand-written list at all? The Flipp backend we ingest from
(``app/services/flipp_client.py``) is *search-by-term* only — there is no
"give me every item in this week's flyer" call and no enumerable flyer id we
can walk. So ingestion coverage is exactly the union of the terms below, and
this list is both the scoping lever (one HTTP request per term per postal
code, so keep it bounded) and the thing that makes a search-only endpoint
usable as an ingestion source at all.

Editing
-------
Add, remove or re-categorise rows in ``STAPLES``. Rules enforced by
``app/tests/test_flipp_staples.py``:

* ``term`` is lowercase, non-empty, and unique.
* ``term`` is >= 3 characters (the endpoint returns near-random results for
  one- and two-letter queries, and an empty ``q`` returns the *entire*
  national flyer corpus — mattresses, tool accessories, diapers).
* ``category`` is one of ``CATEGORIES``.
* the list stays at or under ``MAX_STAPLES`` entries, so a well-meaning edit
  cannot quietly turn one refresh into a 500-request crawl of someone else's
  server.

Categories exist because the Flipp payload's own taxonomy is useless to us:
every grocery item comes back as ``_L1="Food, Beverages & Tobacco"`` /
``_L2="Food Items"`` (verified against live responses, 2026-10-14). The
category we store on a deal is therefore *derived from the search term that
found it*, not read from the source. That is a deliberate, documented
approximation: a hit for "rice" is filed under Pantry even if the matched
flyer item is a rice cooker.
"""
from __future__ import annotations

from typing import NamedTuple

# Deal categories. Kept aligned with the values used by
# ``scripts/seed_sample_data.sql`` so ingested and seeded deals are filterable
# through the same ``category`` facet in the UI.
CATEGORIES = (
    "Produce",
    "Meat & Poultry",
    "Seafood",
    "Dairy & Eggs",
    "Bakery",
    "Pantry",
    "Frozen",
    "Beverages",
)

# Hard ceiling on list length — see module docstring.
MAX_STAPLES = 60


class Staple(NamedTuple):
    """One search term and the category its hits are filed under."""

    term: str
    category: str


# ---------------------------------------------------------------------------
# EDIT HERE
# ---------------------------------------------------------------------------
STAPLES: tuple[Staple, ...] = (
    # --- Produce ---
    Staple("bananas", "Produce"),
    Staple("apples", "Produce"),
    Staple("onions", "Produce"),
    Staple("potatoes", "Produce"),
    Staple("carrots", "Produce"),
    Staple("broccoli", "Produce"),
    Staple("spinach", "Produce"),
    Staple("tomatoes", "Produce"),
    Staple("bell peppers", "Produce"),
    Staple("lettuce", "Produce"),
    Staple("mushrooms", "Produce"),
    Staple("sweet potatoes", "Produce"),
    # --- Meat & Poultry ---
    Staple("chicken breast", "Meat & Poultry"),
    Staple("chicken thighs", "Meat & Poultry"),
    Staple("whole chicken", "Meat & Poultry"),
    Staple("ground beef", "Meat & Poultry"),
    Staple("pork chops", "Meat & Poultry"),
    Staple("ground turkey", "Meat & Poultry"),
    # --- Seafood ---
    Staple("salmon fillet", "Seafood"),
    Staple("canned tuna", "Seafood"),
    Staple("shrimp", "Seafood"),
    # --- Dairy & Eggs ---
    Staple("milk", "Dairy & Eggs"),
    Staple("eggs", "Dairy & Eggs"),
    Staple("butter", "Dairy & Eggs"),
    Staple("cheddar cheese", "Dairy & Eggs"),
    Staple("greek yogurt", "Dairy & Eggs"),
    Staple("cream cheese", "Dairy & Eggs"),
    # --- Bakery ---
    Staple("bread", "Bakery"),
    Staple("tortillas", "Bakery"),
    Staple("bagels", "Bakery"),
    # --- Pantry ---
    Staple("rice", "Pantry"),
    Staple("pasta", "Pantry"),
    Staple("pasta sauce", "Pantry"),
    Staple("canned tomatoes", "Pantry"),
    Staple("canned beans", "Pantry"),
    Staple("chickpeas", "Pantry"),
    Staple("lentils", "Pantry"),
    Staple("olive oil", "Pantry"),
    Staple("flour", "Pantry"),
    Staple("sugar", "Pantry"),
    Staple("oats", "Pantry"),
    Staple("peanut butter", "Pantry"),
    Staple("chicken broth", "Pantry"),
    Staple("tofu", "Pantry"),
    # --- Frozen ---
    Staple("frozen vegetables", "Frozen"),
    Staple("frozen berries", "Frozen"),
    Staple("frozen pizza", "Frozen"),
    # --- Beverages ---
    Staple("orange juice", "Beverages"),
    Staple("coffee", "Beverages"),
    Staple("tea", "Beverages"),
)
# ---------------------------------------------------------------------------


def staple_terms() -> list[str]:
    """Just the search terms, in list order."""
    return [s.term for s in STAPLES]


def category_for_term(term: str) -> str | None:
    """The category a term's hits are filed under, or None for unknown terms.

    Unknown terms are legal (``scripts/ingest_deals.py --terms`` lets you pass
    ad-hoc ones); their deals simply land with ``category = NULL``.
    """
    needle = (term or "").strip().casefold()
    for staple in STAPLES:
        if staple.term == needle:
            return staple.category
    return None
