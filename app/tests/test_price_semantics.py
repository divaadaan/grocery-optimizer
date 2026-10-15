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
def test_from_prefix_decides_true(prefix):
    """Product-owner decision: "from $X" carries no quantity, so per item."""
    assert infer("each", None, prefix) is True
    assert infer(None, None, prefix) is True


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
            # Proposed AND confirmed. Confirmed rows from before migration 010
            # all carry NULL, and skipping them left newly ingested deals
            # under confirmed names permanently unlinkable. Filling a NULL
            # overrules nobody; the safety property is "only ever fills NULL".
            assert "status IN ('proposed', 'confirmed')" in sql
            assert "rejected" not in sql

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


# --------------------------------------------------------------------------
# Renderings found by the 5-term probe
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["2 for", "2 FOR", "3 for", "2 for $5"])
def test_worded_quantity_prefix_decides_false(prefix):
    """THE bug the probe found.

    This function's docstring promises a multi-buy beats a per-item unit. It
    did not for this rendering: the multi-buy pattern was applied only to
    sale_story, and the prefix pattern required a slash, so "2 for" with
    unit "ea." returned True and halved the price in exactly the documented
    case. A comment that was wrong about working code.
    """
    assert infer("ea.", None, prefix) is False
    assert infer("each", None, prefix) is False


@pytest.mark.parametrize("prefix", ["20/20", "Save 2 Ways", "2 Day Sale", "NOW"])
def test_a_number_in_the_prefix_is_still_not_a_quantity(prefix):
    """Widening the pattern for "2 for" must not swallow "20/20".

    Two numbers either side of a slash is not a quantity, and reading it as
    multi-buy would withhold a decidable deal. The slash form therefore
    accepts only a bare trailing slash or a currency amount after it. This
    test caught that regression when the pattern was first widened.
    """
    assert infer("each", None, prefix) is True


@pytest.mark.parametrize("prefix", ["starting at", "Starting At", "STARTING AT $"])
def test_starting_at_is_per_item_like_from(prefix):
    """Identical meaning to "from"."""
    assert infer("each", None, prefix) is True
    assert infer(None, None, prefix) is True


@pytest.mark.parametrize(
    "unit",
    ["or $2.89 each", "2.87 /ea", "or 4.29/ 4.77 ea", "or $4.00 each"],
)
def test_a_price_inside_the_unit_field_means_the_headline_is_not_per_item(unit):
    """13 probe rows put the per-item price INTO the unit field.

    That is simultaneously proof the headline price is not per-item, and the
    per-item number you actually want. Reading the field as a plain unit took
    the headline as per-item.
    """
    assert infer(unit) is False


@pytest.mark.parametrize("unit", ["ea", "ea.", "each", "lb", "pkg"])
def test_a_bare_unit_is_unaffected_by_that_rule(unit):
    """The price-in-unit rule requires a digit, so plain units still decide."""
    assert infer(unit) is True


def test_pkg_is_a_per_item_unit():
    """Found live by the probe; a one-word addition."""
    assert infer("pkg") is True


def test_points_promos_still_are_not_multibuy_after_the_widening():
    """Regression guard: "N PTS when you buy 2" is a points offer.

    Ten probe rows look like this. The price IS per item; only the points need
    a second item. Widening the quantity patterns must not turn these False.
    """
    for story in ["100 Scene+ PTS when you buy 2",
                  "200 Scene+ PTS when you buy 1"]:
        assert infer("each", story) is True


# --------------------------------------------------------------------------
# Common flyer phrasings: unit pricing, package sizes, promo text
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unit",
    ["ea/lb", "EA/LB", "/lb", "per lb", "Per LB", "/kg", "per kg", "$3.99 /lb",
     "ea./lb"],
)
def test_weight_unit_pricing_decides_true(unit):
    assert infer(unit) is True


@pytest.mark.parametrize(
    "unit",
    ["312g", "675 g", "2 L", "12 count", "1lb bag", "1.5 L", "500 ml", "6 pk",
     "355 mL", "12 ct", "16 oz"],
)
def test_package_size_without_multibuy_decides_true(unit):
    assert infer(unit) is True


@pytest.mark.parametrize(
    "unit, story, prefix",
    [
        ("312g", "2 FOR $5", None),         # multi-buy beats a size
        ("2 L", "BOGO", None),
        ("12 count", None, "2/"),
        ("1lb bag", "Buy 2 Get 1", None),
        ("ea/lb", "3/$10", None),
        ("2 for 12 count", None, None),      # multi-buy inside the unit field
    ],
)
def test_multibuy_still_beats_size_and_unit_pricing(unit, story, prefix):
    assert infer(unit, story, prefix) is False


@pytest.mark.parametrize(
    "unit", ["/100 ml", "box", "case", "bag", "L", "g", "lb. bag of 2 for"]
)
def test_sizes_need_a_number_and_a_known_unit(unit):
    """No number, or a unit-price per 100 ml: still not decided as a size.

    ("lb. bag of 2 for" is False via the multi-buy rule, the rest are None.)
    """
    expected = False if "2 for" in unit else None
    assert infer(unit) is expected


@pytest.mark.parametrize(
    "story",
    ["only", "Only $5", "ALWAYS LOW PRICE", "Always Low Price", "Rollback",
     "SAVE $3", "Save $2.50", "100 Scene+ PTS when you buy 3",
     "20 PC Optimum pts"],
)
def test_promo_text_is_never_multibuy(story):
    """Promo wording neither makes a multi-buy nor, alone, decides anything."""
    assert infer("312g", story) is True
    assert infer("each", story) is True


@pytest.mark.parametrize(
    "story", ["Only $5", "Rollback", "SAVE $3", "Save $2.50",
              "100 Scene+ PTS when you buy 3", "20 PC Optimum pts"],
)
def test_savings_and_points_stories_alone_stay_undecided(story):
    assert infer(None, story) is None


@pytest.mark.parametrize(
    "prefix", ["only", "Only", "ONLY $", "ALWAYS LOW PRICE", "Always Low Price"]
)
def test_price_lead_in_prefix_decides_true(prefix):
    """The wording sits where a "2/" would; it is a single price, not a bundle."""
    assert infer(None, None, prefix) is True
    assert infer(None, "Rollback", prefix) is True
    assert infer(None, "2 FOR $5", prefix) is False


@pytest.mark.parametrize("prefix", ["only", "Only", "NOW", "Save $3"])
def test_promo_prefix_does_not_block_a_unit(prefix):
    assert infer("675 g", None, prefix) is True


def test_bare_price_with_no_markers_stays_undecided():
    """A dropped "2/" prefix is exactly what a bare whole-dollar price hides."""
    assert infer(None, None, None) is None
    assert infer("", "", "") is None
    assert infer(None, "", "20/20") is None
    assert infer(None, None, "Online Price") is None
