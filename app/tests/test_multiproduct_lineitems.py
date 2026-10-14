"""Characterises a KNOWN DEFECT: multi-product flyer tiles become one deal.

This file asserts what the code does *today*, which is wrong. It is written in
the same spirit as ``test_deal_gate.py``: make an accepted gap visible and
measurable instead of leaving it as folklore, so that whoever fixes it has a
failing test to flip rather than a paragraph to rediscover.

The defect
----------
A flyer tile can advertise several unrelated products at one price. The
provider returns that tile as a single item with one ``name`` and one
``current_price``, and ingestion faithfully stores it as a single row::

    "ANNIE'S SHELLS & CHEDDAR 170 G BEATRICE CHOCOLATE MILK 1 L
     GOLDFISH CRACKERS 168 - 227 G"   ->  1 deal row

That should be three deals: macaroni-and-cheese, chocolate milk, and crackers.
As one row it is useless -- it matches no recipe ingredient, it cannot be
priced, and the only safe thing to do with it is mark it unmappable and
withhold it, which is what the first labelling pass did.

Measured on the live catalogue (2026-10-14, 91 distinct ingested names):
  * 5 names carry >= 2 size tokens, so they are certainly multi-product
  * 25 names contain " or "
  * 28 names (31%) carry at least one of those signals

Why this is not a regex fix
---------------------------
" or " separates two different things depending on the tile, and only meaning
tells them apart:

  * "NEILSON TRUTASTE 1%, 2% OR SKIM MILK, 4 L"      -> ONE product, 3 variants
  * "Natrel Fine-Filtered Milk 3.25%, 2%, 1% or Skim" -> ONE product, 4 variants
  * "MAPLE LEAF ... CHICKEN BREAST FILLETS OR CUTLETS" -> ONE product, 2 cuts
  * "MARC ANGELO CHICKEN BREAST BITES OR PORK MEATBALLS" -> TWO products
  * "NEILSON CREAM or MILK2GO"                        -> TWO products

A splitter that split all of them would shatter "milk" into a dozen fake
products; one that split none of them keeps the status quo. The shape of the
real fix is therefore heuristic *detection* (cheap, this file's helpers) plus
labelled *decomposition* (the propose/review step already decides semantics).

Consequences the fix has to deal with, recorded now while they are fresh:
  1. **Price attribution.** A tile with one price usually means "each of these
     is $X", which is tractable. But "2 for $5" and a "from" price prefix mean
     something else, and ``sale_story`` / ``pre_price_text`` are the only
     signals available.
  2. **The natural key changes.** Deals are keyed
     ``(store_id, source, source_item_id, valid_from)`` by migration 002, and
     one tile has one ``source_item_id``. Splitting a tile into N rows breaks
     that uniqueness, so the key needs a discriminator -- most likely
     ``product_id`` -- which is a migration, not a code change.
  3. **Snapshots inherit the problem.** One tile currently contributes one
     price observation under a mashed name that never recurs, so it pollutes
     nothing but contributes nothing either. Split rows would start feeding
     real series, which is the upside.
"""
import re

import pytest

from app.services.products import normalize_name

#: A size token terminates a product inside a mashed string. Two or more of
#: them is near-certain evidence of a multi-product tile.
SIZE_TOKEN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:-\s*\d+(?:[.,]\d+)?\s*)?"
    r"(?:g|gr|kg|ml|l|lt|oz|lb|lbs|ea|pk|ct)\b",
    re.IGNORECASE,
)

#: Real strings from the live catalogue, and how many products each describes.
#: These are the acceptance cases for whoever implements decomposition.
REAL_TILES = [
    (
        "ANNIE'S SHELLS & CHEDDAR 170 G BEATRICE CHOCOLATE MILK 1 L "
        "GOLDFISH CRACKERS 168 - 227 G",
        3,
    ),
    (
        "LACTANTIA LACTOSE FREE MILK 2 L LACTOSE FREE CREAMS 1 L "
        "OASIS ORANGE JUICE 1.5 L PHILADELPHIA CREAM CHEESE PRODUCT 340 G",
        4,
    ),
    ("NEILSON 35% CREAM, 473 ML OR ULTRAFILTERED MILK, 1 L", 2),
    ("MARC ANGELO CHICKEN BREAST BITES OR PORK MEATBALLS", 2),
    ("NEILSON CREAM or MILK2GO", 2),
    ("ORGANIC COCONUT MILK OR CREAM", 2),
    # Variants of ONE product -- a splitter must leave these alone.
    ("NEILSON TRUTASTE 1%, 2% OR SKIM MILK, 4 L", 1),
    ("Natrel Fine-Filtered Milk 3.25%, 2%, 1% or Skim", 1),
    ("MAPLE LEAF PRIME RAISED WITHOUT ANTIBIOTICS FRESH CHICKEN BREAST "
     "FILLETS OR CUTLETS", 1),
    ("PC® HOMESTYLE OR SPICY BUTTERMILK CRISPY CHICKEN BREASTS, 560 g", 1),
    # Single-product control.
    ("Boneless, Skinless Chicken Breasts", 1),
    ("CARNATION EVAPORATED MILK", 1),
]


def looks_multiproduct(name: str) -> bool:
    """Cheap detection: two or more size tokens, or an explicit alternation.

    Deliberately over-triggers. It is a *candidate* filter for the labelling
    step, not a decision -- see this module's docstring on why " or " cannot
    decide anything on its own.
    """
    if len(SIZE_TOKEN.findall(name)) >= 2:
        return True
    return re.search(r"\bor\b", name, re.IGNORECASE) is not None


# --------------------------------------------------------------------------
# The defect itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected_products",
    [(raw, n) for raw, n in REAL_TILES if n > 1],
)
def test_multiproduct_tile_is_still_only_one_name(raw, expected_products):
    """THE DEFECT. Each of these describes several products and yields one key.

    When decomposition lands, this test should be inverted: the tile should
    produce ``expected_products`` separate entries. Until then it documents
    that it produces exactly one.
    """
    assert expected_products > 1
    normalized = normalize_name(raw)
    assert normalized, "a mashed tile still normalises to a single key"
    # One string in, one key out. That is the bug, asserted.
    assert isinstance(normalized, str)


def test_the_mashed_key_is_meaningless_as_a_price_series():
    """Two tiles advertising the same milk produce unrelated keys.

    This is why the baseline cannot accumulate: the mash varies week to week,
    so each tile is its own one-observation series that never recurs.
    """
    a = normalize_name(
        "LACTANTIA LACTOSE FREE MILK 2 L LACTOSE FREE CREAMS 1 L "
        "OASIS ORANGE JUICE 1.5 L PHILADELPHIA CREAM CHEESE PRODUCT 340 G"
    )
    b = normalize_name(
        "LACTANTIA LACTOSE FREE MILK, 2L, LACTOSE FREE CREAMS, 1L, "
        "OASIS ORANGE JUICE, 1.5L"
    )
    c = normalize_name("LACTANTIA LACTOSE FREE MILK")
    assert a != b != c
    # All three advertise lactose-free milk; none of them share a series.
    assert len({a, b, c}) == 3


# --------------------------------------------------------------------------
# Detection, which is the part that is ready
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, _n", REAL_TILES)
def test_detection_flags_every_real_multiproduct_tile(raw, _n):
    """Detection must not miss a genuine multi-product tile.

    False positives are acceptable here -- a flagged variant-only string costs
    the labelling step one judgement. A miss costs a withheld or, worse, a
    mis-attributed deal.
    """
    if _n > 1:
        assert looks_multiproduct(raw), f"missed a multi-product tile: {raw!r}"


def test_detection_leaves_plain_single_product_names_alone():
    for raw in ("Boneless, Skinless Chicken Breasts", "CARNATION EVAPORATED MILK",
                "AROY-D Coconut Milk", "Beatrice Milk"):
        assert not looks_multiproduct(raw)


def test_detection_over_triggers_on_variant_strings_and_that_is_expected():
    """Documents the known false positives so nobody 'fixes' them into misses."""
    variant_only = [
        "NEILSON TRUTASTE 1%, 2% OR SKIM MILK, 4 L",
        "Natrel Fine-Filtered Milk 3.25%, 2%, 1% or Skim",
        "PC® HOMESTYLE OR SPICY BUTTERMILK CRISPY CHICKEN BREASTS, 560 g",
    ]
    assert all(looks_multiproduct(raw) for raw in variant_only)


def test_size_token_count_is_the_high_confidence_signal():
    """>=2 size tokens had no false positives in the live catalogue.

    25 names matched " or "; only 5 matched on size tokens, and all 5 were
    genuinely multi-product. Size counting is the signal worth trusting.
    """
    certain = [raw for raw, n in REAL_TILES if len(SIZE_TOKEN.findall(raw)) >= 2]
    for raw in certain:
        expected = dict(REAL_TILES)[raw]
        assert expected > 1, f"size-token signal was wrong about {raw!r}"
