"""Tests for inferring whether a tile's price is any one product's price.

The asymmetry that drives every case here: a wrong ``True`` puts a bundle price
on a single product's line in a shopping list, and the user discovers it at the
till. A wrong ``None`` withholds a deal. So ``None`` is a real answer and the
function must reach for it rather than guess.
"""
import pytest

from app.services.products import infer_price_is_per_item as infer


# --------------------------------------------------------------------------
# Per-item units
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unit",
    ["each", "ea", "ea.", "EACH", "Ea.", "lb", "/lb", "LB", "lbs", "kg",
     "100 g", "unit", "pk", "pack"],
)
def test_per_item_units_decide_true(unit):
    """These are the 11-of-17 case: the tile prices items separately."""
    assert infer(unit) is True


@pytest.mark.parametrize("unit", [None, "", "   ", "/100 ml", "box", "case"])
def test_units_that_do_not_decide(unit):
    """A unit we do not recognise leaves it undecided, not false.

    False would be a claim ("this is bundle pricing") we have no basis for.
    """
    assert infer(unit) is None


# --------------------------------------------------------------------------
# Multi-buy beats the unit
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "story",
    ["2 FOR $5", "2 for $5", "3/$10", "2/5.00", "4 FOR 10", "2 for $7.00"],
)
def test_multibuy_story_decides_false(story):
    assert infer(None, story) is False


@pytest.mark.parametrize("story", ["BOGO", "Buy 2 Get 1", "buy one get one"])
def test_bogo_story_decides_false(story):
    assert infer(None, story) is False


def test_multibuy_overrides_a_per_item_unit():
    """THE precedence case, and the reason the order is not arbitrary.

    "2 FOR $5" with unit "ea." means two items for five dollars. Reading the
    unit first would record $5 as each item's price and halve the real cost of
    the promotion in every downstream total.
    """
    assert infer("ea.", "2 FOR $5") is False
    assert infer("each", "3/$10") is False


# --------------------------------------------------------------------------
# A 'from' prefix is a floor, not a price
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["from", "From", "FROM $"])
def test_from_prefix_decides_false(prefix):
    assert infer("each", None, prefix) is False


@pytest.mark.parametrize("prefix", ["NOW", "Online Price", None, ""])
def test_other_prefixes_do_not_override_the_unit(prefix):
    assert infer("each", None, prefix) is True


# --------------------------------------------------------------------------
# Savings stories say nothing about per-item pricing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("story", ["save $10", "SAVE 30%", "Rollback", "NEW"])
def test_savings_story_alone_leaves_it_undecided(story):
    """A discount claim is not a quantity claim.

    These are the most common sale_story values in the live data, and none of
    them tells you whether the price is per item.
    """
    assert infer(None, story) is None


def test_savings_story_does_not_cancel_a_per_item_unit():
    assert infer("ea.", "SAVE 30%") is True
    assert infer("lb", "Rollback") is True


def test_save_dollar_amount_is_not_read_as_multibuy():
    """"save $10" contains a digit next to a dollar sign but is not "N for $X".

    A looser regex would read it as multi-buy and withhold a decidable deal.
    """
    assert infer("each", "save $10") is True
    assert infer("each", "SAVE $2.50") is True


# --------------------------------------------------------------------------
# Real values observed in the live catalogue
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unit, expected",
    [
        ("each", True),   # ANNIE'S / LACTANTIA / NEILSON tiles
        ("ea.", True),    # MARC ANGELO chicken-or-pork
        ("ea", True),     # ORGANIC COCONUT MILK OR CREAM
        ("lb", True),     # PC FREE FROM chicken breasts or thighs
        (None, None),     # the six tiles that stayed withheld
    ],
)
def test_the_seventeen_known_tiles(unit, expected):
    assert infer(unit) is expected


# --------------------------------------------------------------------------
# A bare quantity in the PREFIX, not the story
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["2/", "3 /", " 2 / ", "4/"])
def test_bare_quantity_prefix_decides_false(prefix):
    """Observed live: pre_price_text=2/ on a .00 tile means two for .

    Checking only sale_story missed this and left the tile undecided, so the
    prefix is checked for a quantity as well.
    """
    assert infer(None, None, prefix) is False


def test_quantity_prefix_overrides_a_per_item_unit():
    assert infer("each", None, "2/") is False


@pytest.mark.parametrize(
    "prefix", ["NOW", "Online Price", "Save 2 Ways", "2 Day Sale", "20/20"]
)
def test_prefixes_that_merely_contain_a_number_do_not_match(prefix):
    """Anchored and terminated, so only a bare "N/" counts.

    "20/20" is two numbers either side of a slash, not a quantity prefix, and
    reading it as multi-buy would withhold a decidable deal.
    """
    assert infer("each", None, prefix) is True


# --------------------------------------------------------------------------
# Loyalty-points stories are not multi-buy
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "story",
    [
        "100 Scene+ PTS when you buy 4",
        "200 Scene+ PTS when you buy 1",
        "PC Optimum 2,000 pts",
        "Get 500 Scene+ PTS when you spend 0 on any participating products",
    ],
)
def test_points_offers_do_not_decide_false(story):
    """All real values from the live catalogue.

    "when you buy 4" earns points on a fourth item; the shelf price is still
    per item. Reading these as bundle pricing would withhold them, and
    "Get ... when you spend" must not trip the BOGO pattern either.
    """
    assert infer("each", story) is True
    assert infer(None, story) is None


# --------------------------------------------------------------------------
# The backfill
# --------------------------------------------------------------------------

class FakeReinferCursor:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def execute(self, sql, params=None):
        if "UPDATE product_aliases" in sql:
            self.updates.append(params)
            # Only ever fills NULL: a decided value must not be overruled by a
            # heuristic, whether the labeller or a reviewer decided it.
            assert "price_is_per_item IS NULL" in sql
        else:
            assert "price_is_per_item IS NULL" in sql
            assert "status = 'proposed'" in sql

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeReinferDB:
    def __init__(self, cursor):
        self._cursor = cursor

    def get_cursor(self):
        return self._cursor


def test_backfill_fills_only_what_it_can_decide():
    from app.services.products import reinfer_alias_prices

    cursor = FakeReinferCursor([
        {"alias_id": 1, "unit": "each", "sale_story": None, "pre_price_text": None},
        {"alias_id": 2, "unit": None, "sale_story": "2 FOR $5", "pre_price_text": None},
        {"alias_id": 3, "unit": None, "sale_story": None, "pre_price_text": "2/"},
        {"alias_id": 4, "unit": None, "sale_story": None, "pre_price_text": None},
        {"alias_id": 5, "unit": None, "sale_story": "Rollback", "pre_price_text": None},
    ])
    stats = reinfer_alias_prices(database=FakeReinferDB(cursor))

    assert stats["examined"] == 5
    assert stats["filled_true"] == 1       # alias 1, unit "each"
    assert stats["filled_false"] == 2      # aliases 2 and 3, multi-buy
    assert stats["still_null"] == 2        # aliases 4 and 5, nothing to go on
    assert [u["id"] for u in cursor.updates] == [1, 2, 3]
    assert [u["v"] for u in cursor.updates] == [True, False, False]


def test_backfill_writes_nothing_when_nothing_is_decidable():
    from app.services.products import reinfer_alias_prices

    cursor = FakeReinferCursor([
        {"alias_id": 9, "unit": None, "sale_story": None, "pre_price_text": None},
    ])
    stats = reinfer_alias_prices(database=FakeReinferDB(cursor))
    assert stats["still_null"] == 1
    assert cursor.updates == []
