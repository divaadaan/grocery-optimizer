"""Tests for the products catalog: normalisation, compliance, and the gate.

The safety-critical assertions here are the fail-closed ones. Everything that
is not a confirmed product clearing the user's restrictions must withhold the
deal: unlinked, proposed, rejected, and a partially-selected product row all
count as "not safe". A regression in any of those is an allergen exposure, not
a UX bug, which is why each has its own test rather than sharing one.
"""
import re

import pytest

from app.services import products

PLACEHOLDER = re.compile(r"%\((\w+)\)s")


# --------------------------------------------------------------------------
# normalize_name
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Size and pack noise is what fragments one product into many series.
        ("LACTANTIA LACTOSE FREE MILK, 2L", "lactantia lactose free milk"),
        ("LACTANTIA LACTOSE FREE MILK 2 L", "lactantia lactose free milk"),
        ("Butter 500 g", "butter"),
        ("Butter, 500g", "butter"),
        ("2 x 500g Butter", "butter"),
        ("Cheese x4", "cheese"),
        # Price fragments leak into flyer names.
        ("$4.96 Milk 2%", "milk 2"),
        ("4/$5 Yogurt", "yogurt"),
        # Punctuation and case collapse; word order is preserved.
        ("Boneless, Skinless Chicken Breasts", "boneless skinless chicken breasts"),
        ("ANNIE'S SHELLS & CHEDDAR", "annie s shells cheddar"),
        ("  Extra   Whitespace  ", "extra whitespace"),
        # Nothing to key on.
        ("", ""),
        (None, ""),
        ("$$$", ""),
        ("500 g", ""),
    ],
)
def test_normalize_name(raw, expected):
    assert products.normalize_name(raw) == expected


def test_normalize_name_does_not_strip_varietal_words():
    """The normaliser must not try to be clever about brands or adjectives.

    "Beefsteak" is a tomato varietal. Any normaliser that dropped tokens it
    guessed were brands or modifiers could turn this into "beef", which is
    precisely the false positive the products table exists to end.
    """
    assert products.normalize_name("Beefsteak Tomatoes") == "beefsteak tomatoes"


def test_normalize_name_is_idempotent():
    once = products.normalize_name("LACTANTIA MILK, 2L")
    assert products.normalize_name(once) == once


# --------------------------------------------------------------------------
# restrictions -> attributes
# --------------------------------------------------------------------------

def test_restriction_attribute_map_covers_the_enforceable_labels():
    """Every label the app enforces must map to attributes.

    ``RESTRICTION_ATTRIBUTES`` is the single source of truth; a label missing
    here enforces nothing beyond "linked and confirmed".
    """
    labels = {"vegan", "vegetarian", "pescatarian", "no_nuts", "nut_free",
              "dairy_free"}
    assert labels <= set(products.RESTRICTION_ATTRIBUTES)
    assert set(products.REVIEW_LABELS) <= set(products.RESTRICTION_ATTRIBUTES)


@pytest.mark.parametrize(
    "restrictions, expected",
    [
        ([], ()),
        (None, ()),
        (["vegetarian"], ("contains_fish", "contains_meat")),
        (["pescatarian"], ("contains_meat",)),
        (["no_nuts"], ("contains_nuts",)),
        (["nut_free"], ("contains_nuts",)),
        (["dairy_free"], ("contains_dairy",)),
        (
            ["vegan"],
            ("contains_dairy", "contains_egg", "contains_fish", "contains_meat"),
        ),
        # Union, not last-wins.
        (
            ["vegetarian", "no_nuts"],
            ("contains_fish", "contains_meat", "contains_nuts"),
        ),
        # Blank entries are noise, not labels.
        (["", "  ", "vegetarian"], ("contains_fish", "contains_meat")),
    ],
)
def test_restrictions_to_attributes(restrictions, expected):
    assert products.restrictions_to_attributes(restrictions) == expected


def test_unknown_restriction_label_is_logged_not_swallowed(caplog):
    """A typo'd label must not pass silently.

    It contributes no attribute (there is nothing to enforce), but a user whose
    profile says "vegitarian" is getting no protection and someone has to find
    out.
    """
    with caplog.at_level("WARNING"):
        assert products.restrictions_to_attributes(["vegitarian"]) == ()
    assert "Unrecognised dietary restriction" in caplog.text


# --------------------------------------------------------------------------
# product_allows — fail closed
# --------------------------------------------------------------------------

def _product(**overrides):
    base = {
        "product_id": 1,
        "canonical_name": "tomato",
        "category": "produce",
        "contains_meat": False,
        "contains_fish": False,
        "contains_dairy": False,
        "contains_egg": False,
        "contains_nuts": False,
        "is_pantry_staple": False,
    }
    base.update(overrides)
    return base


def test_product_allows_plain_vegetable():
    assert products.product_allows(_product(), ["vegan"]) is True


def test_product_allows_blocks_on_the_relevant_attribute():
    pork = _product(canonical_name="prosciutto", contains_meat=True)
    assert products.product_allows(pork, ["vegetarian"]) is False
    assert products.product_allows(pork, ["pescatarian"]) is False
    # Nut-free says nothing about pork.
    assert products.product_allows(pork, ["no_nuts"]) is True


def test_product_allows_dairy_splits_vegan_from_vegetarian():
    cheese = _product(canonical_name="cheddar", contains_dairy=True)
    assert products.product_allows(cheese, ["vegetarian"]) is True
    assert products.product_allows(cheese, ["vegan"]) is False


@pytest.mark.parametrize("name, attrs, restriction", [
    ("prosciutto", {"contains_meat": True}, "vegetarian"),
    ("kielbasa", {"contains_meat": True}, "vegetarian"),
    ("capicollo", {"contains_meat": True}, "vegetarian"),
    ("gelatin", {"contains_meat": True}, "vegetarian"),
    ("lard", {"contains_meat": True}, "vegetarian"),
    ("marzipan", {"contains_nuts": True}, "no_nuts"),
])
def test_product_allows_blocks_by_attribute_whatever_the_name(name, attrs, restriction):
    """No word in these names says meat or nut; the attribute does."""
    product = _product(canonical_name=name, **attrs)
    assert products.product_allows(product, [restriction]) is False


@pytest.mark.parametrize("name, restriction", [
    # The owner's intent, pinned. Each name contains a word a name-matcher
    # would read as forbidden ("butter", "nut", "beef", "ham"); each product
    # is correctly labelled safe, and the label is what decides.
    ("Peanut Butter", "vegan"),
    ("Beefsteak Tomatoes", "vegetarian"),
    ("Butternut Squash", "no_nuts"),
    ("Hamburger Buns", "vegetarian"),
    ("Coconut Milk", "vegan"),
])
def test_product_allows_safe_food_whatever_the_name(name, restriction):
    product = _product(canonical_name=name)
    assert products.product_allows(product, [restriction]) is True


def test_product_allows_fails_closed_on_a_missing_attribute(caplog):
    """A partially-selected row must not read as safe."""
    partial = _product()
    del partial["contains_meat"]
    with caplog.at_level("WARNING"):
        assert products.product_allows(partial, ["vegetarian"]) is False
    assert "contains_meat" in caplog.text


def test_product_allows_fails_closed_on_none_attribute():
    assert products.product_allows(_product(contains_nuts=None), ["no_nuts"]) is False


def test_product_allows_with_no_restrictions_allows_anything():
    steak = _product(canonical_name="ribeye", contains_meat=True)
    assert products.product_allows(steak, []) is True


# --------------------------------------------------------------------------
# review grouping — attributes only
# --------------------------------------------------------------------------

def _alias_row(**overrides):
    row = {
        "alias_id": 1, "normalized_name": "peanut butter",
        "raw_example": "KRAFT PEANUT BUTTER 1KG", "confidence": 0.9,
        "proposal_reason": None, "times_seen": 3, "source": "x",
        "tile_position": 0, "price_is_per_item": True,
        "product_status": "proposed", "tile_products": ["peanut butter"],
        **_product(canonical_name="peanut butter", category="pantry",
                   contains_nuts=True),
    }
    row.update(overrides)
    return row


def test_review_group_clears_follow_the_attributes_not_the_name():
    """Peanut butter, labelled plant-based with nuts, clears vegan in review."""
    groups, orphans = products.group_proposals([_alias_row()])
    assert orphans == []
    assert groups[0]["clears"] == ["vegan", "vegetarian", "pescatarian"]
    # The reviewer's labels decide, in both directions.
    groups, _ = products.group_proposals([_alias_row(contains_dairy=True)])
    assert groups[0]["clears"] == ["vegetarian", "pescatarian"]


# --------------------------------------------------------------------------
# compliant_product_filter
# --------------------------------------------------------------------------

def test_filter_is_empty_without_restrictions():
    assert products.compliant_product_filter([]) == ("", {})
    assert products.compliant_product_filter(None) == ("", {})
    assert products.compliant_product_filter(["", "  "]) == ("", {})


def test_filter_still_gates_a_restriction_with_no_known_attribute():
    """An unrecognised label is still a restriction, so the user is gated.

    It enforces no attribute, but unlinked and unconfirmed deals are withheld
    exactly as for any other restricted user — never "open".
    """
    fragment, params = products.compliant_product_filter(["halal"])
    assert "AND EXISTS (" in fragment
    assert "AND NOT EXISTS (" in fragment
    assert "p.status <> 'confirmed'" in fragment
    assert "contains_" not in fragment
    assert params == {}


def test_filter_requires_confirmed_status_and_every_attribute():
    fragment, params = products.compliant_product_filter(["vegan"])
    # Anything not confirmed counts as violating.
    assert "p.status <> 'confirmed'" in fragment
    for attr in ("contains_meat", "contains_fish", "contains_dairy", "contains_egg"):
        assert f"p.{attr}" in fragment
    # Nuts are not implied by vegan.
    assert "contains_nuts" not in fragment
    assert params == {}


def test_filter_reads_the_junction_not_the_single_fk():
    """A deal can advertise several products, so compliance is over all of them.

    Reading ``deals.product_id`` would see only one product of a multi-product
    flyer tile and could clear a deal whose other products violate.
    """
    fragment, _ = products.compliant_product_filter(["vegetarian"])
    assert "deal_products" in fragment
    assert "dp.deal_id = d.deal_id" in fragment
    assert "d.product_id" not in fragment


def test_filter_needs_both_exists_and_not_exists():
    """The two clauses do different jobs and neither is redundant.

    EXISTS makes an unlinked deal fail. Without it, NOT EXISTS alone is
    vacuously true for a deal with no products -- "nothing violates it" --
    which would serve a completely unknown item to a restricted user.
    """
    fragment, _ = products.compliant_product_filter(["vegetarian"])
    assert "AND EXISTS (" in fragment
    assert "AND NOT EXISTS (" in fragment
    # The EXISTS clause must not itself be filtered by attributes, or an
    # unlinked deal and a violating deal become indistinguishable.
    head = fragment.split("NOT EXISTS")[0]
    assert "contains_" not in head


def test_filter_treats_an_unconfirmed_product_as_violating():
    """Proposed and rejected are not 'unknown but probably fine'."""
    fragment, _ = products.compliant_product_filter(["no_nuts"])
    violation = fragment.split("AND (")[1]
    assert "p.status <> 'confirmed'" in violation
    assert "p.contains_nuts" in violation


def test_filter_has_no_placeholders_to_bind():
    """Attribute names are interpolated from our own constant, never bound.

    If this ever grows a placeholder, the caller must bind it — the test exists
    so that change cannot be made silently.
    """
    fragment, params = products.compliant_product_filter(["vegan", "no_nuts"])
    assert PLACEHOLDER.findall(fragment) == []
    assert params == {}


def test_filter_rejects_an_attribute_outside_the_constant(monkeypatch):
    """Guards the one place a name reaches SQL unparameterised."""
    monkeypatch.setitem(
        products.RESTRICTION_ATTRIBUTES, "bogus", ("contains_sqlinjection",)
    )
    with pytest.raises(AssertionError):
        products.compliant_product_filter(["bogus"])
