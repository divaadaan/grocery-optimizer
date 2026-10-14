"""Tests for the products catalog: normalisation, compliance, and the gate.

The safety-critical assertions here are the fail-closed ones. Everything that
is not a confirmed product clearing the user's restrictions must withhold the
deal: unlinked, proposed, rejected, and a partially-selected product row all
count as "not safe". A regression in any of those is an allergen exposure, not
a UX bug, which is why each has its own test rather than sharing one.
"""
import re

import pytest

from app.agents import dietary
from app.services import deal_gate, products

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

def test_restriction_attribute_map_matches_the_oracle_labels():
    """Every label the substring oracle understands must have attributes.

    The two must move together. If a label is added to
    ``dietary.forbidden_terms_for`` and not here, the products gate would
    silently stop enforcing it.
    """
    oracle_labels = {"vegan", "vegetarian", "pescatarian", "no_nuts", "nut_free"}
    assert oracle_labels <= set(products.RESTRICTION_ATTRIBUTES)


@pytest.mark.parametrize(
    "restrictions, expected",
    [
        ([], ()),
        (None, ()),
        (["vegetarian"], ("contains_fish", "contains_meat")),
        (["pescatarian"], ("contains_meat",)),
        (["no_nuts"], ("contains_nuts",)),
        (["nut_free"], ("contains_nuts",)),
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


def test_product_allows_catches_the_hidden_cases_the_oracle_misses():
    """The whole point: these have no forbidden substring in their names."""
    for name, attrs in [
        ("prosciutto", {"contains_meat": True}),
        ("kielbasa", {"contains_meat": True}),
        ("capicollo", {"contains_meat": True}),
        ("gelatin", {"contains_meat": True}),
        ("lard", {"contains_meat": True}),
        ("marzipan", {"contains_nuts": True}),
    ]:
        product = _product(canonical_name=name, **attrs)
        restriction = "no_nuts" if attrs.get("contains_nuts") else "vegetarian"
        # The oracle lets every one of these through...
        assert dietary.is_compliant(name, [restriction]) is True
        # ...and the curated attributes do not.
        assert products.product_allows(product, [restriction]) is False


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
# oracle disagreement — the review-time alarm
# --------------------------------------------------------------------------

def test_oracle_disagreement_flags_the_false_positive_case():
    """A correctly-labelled beefsteak tomato disagrees with the oracle.

    This is the alarm doing its job in the harmless direction: the attributes
    are right, the oracle is wrong, and the reviewer sees why their name choice
    is confusing.
    """
    tomato = _product(canonical_name="beefsteak tomato")
    labels = products.oracle_disagreement(tomato)
    assert "vegetarian" in labels and "vegan" in labels


def test_oracle_disagreement_flags_the_dangerous_mislabel():
    """A human marking marzipan nut-free must show up as a disagreement."""
    mislabelled = _product(canonical_name="marzipan almond paste", contains_nuts=False)
    assert "no_nuts" in products.oracle_disagreement(mislabelled)


def test_oracle_disagreement_silent_when_they_agree():
    assert products.oracle_disagreement(_product(canonical_name="tomato")) == []
    beef = _product(canonical_name="ground beef", contains_meat=True)
    assert products.oracle_disagreement(beef) == []


# --------------------------------------------------------------------------
# compliant_product_filter
# --------------------------------------------------------------------------

def test_filter_is_empty_without_restrictions():
    assert products.compliant_product_filter([]) == ("", {})
    assert products.compliant_product_filter(None) == ("", {})


def test_filter_requires_confirmed_status_and_every_attribute():
    fragment, params = products.compliant_product_filter(["vegan"])
    assert "p.status = 'confirmed'" in fragment
    for attr in ("contains_meat", "contains_fish", "contains_dairy", "contains_egg"):
        assert f"p.{attr} IS NOT TRUE" in fragment
    # Nuts are not implied by vegan.
    assert "contains_nuts" not in fragment
    assert params == {}


def test_filter_uses_exists_so_unlinked_deals_are_excluded():
    """``product_id IS NULL`` must fail the predicate, not bypass it."""
    fragment, _ = products.compliant_product_filter(["vegetarian"])
    assert "EXISTS" in fragment
    assert "p.product_id = d.product_id" in fragment


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


# --------------------------------------------------------------------------
# gate_mode precedence
# --------------------------------------------------------------------------

def test_gate_open_for_unrestricted_users():
    assert deal_gate.gate_mode([], products_enabled=False) == deal_gate.GATE_OPEN
    assert deal_gate.gate_mode(None, products_enabled=True) == deal_gate.GATE_OPEN


def test_gate_falls_back_to_sources_when_products_disabled():
    mode = deal_gate.gate_mode(
        ["vegetarian"], products_enabled=False, allow_for_restricted=False
    )
    assert mode == deal_gate.GATE_CURATED_SOURCES


def test_gate_products_outranks_the_older_waiver():
    """The stronger guarantee wins. Both flags on must not mean "trust all".

    ``INGESTED_DEALS_FOR_RESTRICTED_DIETS`` waives the source gate on the
    strength of the substring oracle. Once the products gate is on, that waiver
    must not reopen the unsafe path.
    """
    mode = deal_gate.gate_mode(
        ["vegetarian"], products_enabled=True, allow_for_restricted=True
    )
    assert mode == deal_gate.GATE_PRODUCTS


def test_gate_old_waiver_still_opens_when_products_off():
    mode = deal_gate.gate_mode(
        ["vegetarian"], products_enabled=False, allow_for_restricted=True
    )
    assert mode == deal_gate.GATE_OPEN
