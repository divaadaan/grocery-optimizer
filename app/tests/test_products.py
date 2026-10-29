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
              "dairy_free", "gluten_free"}
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
        (["gluten_free"], ("contains_gluten",)),
        (
            ["vegan"],
            ("contains_animal_byproduct", "contains_dairy", "contains_egg",
             "contains_fish", "contains_meat"),
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
        "contains_animal_byproduct": False,
        "contains_gluten": False,
        "not_kosher": False,
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


# --------------------------------------------------------------------------
# contains_animal_byproduct (migration 015)
# --------------------------------------------------------------------------

def test_animal_byproduct_is_a_vegan_attribute_only():
    assert "contains_animal_byproduct" in products.ATTRIBUTES
    for label, attrs in products.RESTRICTION_ATTRIBUTES.items():
        assert ("contains_animal_byproduct" in attrs) == (label == "vegan"), label


def test_honey_fails_vegan_and_clears_everything_else():
    honey = _product(canonical_name="honey", contains_animal_byproduct=True)
    assert products.product_allows(honey, ["vegan"]) is False
    for label in ("vegetarian", "pescatarian", "no_nuts", "nut_free", "dairy_free"):
        assert products.product_allows(honey, [label]) is True, label


def test_undecided_byproduct_fails_vegan_quietly(caplog):
    """NULL is a legitimate state since 015 -- fails closed, but it is not a
    partial-row bug, so it does not warn on every product every request."""
    jam = _product(canonical_name="jam", contains_animal_byproduct=None)
    with caplog.at_level("WARNING"):
        assert products.product_allows(jam, ["vegan"]) is False
        assert products.product_allows(jam, ["vegetarian"]) is True
    assert caplog.text == ""


# --------------------------------------------------------------------------
# contains_gluten (migration 017)
# --------------------------------------------------------------------------

def test_gluten_is_a_gluten_free_attribute_only():
    assert "contains_gluten" in products.ATTRIBUTES
    assert "contains_gluten" in products.NULLABLE_ATTRIBUTES
    assert products.RESTRICTION_ATTRIBUTES["gluten_free"] == ("contains_gluten",)
    for label, attrs in products.RESTRICTION_ATTRIBUTES.items():
        assert ("contains_gluten" in attrs) == (label == "gluten_free"), label


def test_gluten_free_is_enforced_not_just_logged(caplog):
    with caplog.at_level("WARNING"):
        assert products.restrictions_to_attributes(["gluten_free"]) == (
            "contains_gluten",)
    assert caplog.text == ""


def test_gluten_free_allows_only_on_an_explicit_false(caplog):
    with caplog.at_level("WARNING"):
        assert products.product_allows(
            _product(contains_gluten=False), ["gluten_free"]) is True
        assert products.product_allows(
            _product(contains_gluten=True), ["gluten_free"]) is False
        # NULL = undecided: fails closed, quietly.
        assert products.product_allows(
            _product(contains_gluten=None), ["gluten_free"]) is False
    assert caplog.text == ""


def test_contains_gluten_does_not_affect_other_restrictions():
    bread = _product(canonical_name="bread", contains_gluten=True)
    undecided = _product(canonical_name="granola", contains_gluten=None)
    for item in (bread, undecided):
        for label in ("vegan", "vegetarian", "pescatarian", "no_nuts",
                      "nut_free", "dairy_free"):
            assert products.product_allows(item, [label]) is True, label


def test_gluten_rule_states_the_labelling_decisions():
    rule = products.ATTRIBUTE_RULES["contains_gluten"].lower()
    for word in ("wheat", "spelt", "kamut", "durum", "semolina", "farro",
                 "barley", "rye", "triticale", "malt", "brewer's yeast",
                 "oats", "certified gluten-free", "may contain gluten"):
        assert word in rule, word


def test_gluten_null_decides_a_gluten_free_verdict():
    undecided = _product(contains_gluten=None)
    assert "gluten_free" in products._null_decides(undecided)
    # ...but not for a user it cannot change anything for.
    assert products._null_decides(_product(contains_gluten=True)) == []


def test_attribute_rules_cover_every_attribute_and_state_the_split():
    assert set(products.ATTRIBUTE_RULES) == set(products.ATTRIBUTES)
    meat = products.ATTRIBUTE_RULES["contains_meat"]
    byproduct = products.ATTRIBUTE_RULES["contains_animal_byproduct"]
    for word in ("gelatin", "lard", "tallow", "rennet"):
        assert word in meat
    for word in ("honey", "beeswax", "carmine", "shellac", "bone-char", "lanolin"):
        assert word in byproduct


def test_review_clears_treat_undecided_as_failing():
    groups, _ = products.group_proposals(
        [_alias_row(contains_nuts=False, contains_animal_byproduct=None)])
    assert groups[0]["clears"] == ["vegetarian", "pescatarian", "no_nuts",
                                   "gluten_free", "kosher"]
    groups, _ = products.group_proposals(
        [_alias_row(contains_nuts=False, contains_animal_byproduct=True)])
    assert groups[0]["clears"] == ["vegetarian", "pescatarian", "no_nuts",
                                   "gluten_free", "kosher"]


class _RecordingCursor:
    def __init__(self, rows=(), one=None):
        self.rows = list(rows)
        self.one = one
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingDB:
    def __init__(self, cursor):
        self.cursor = cursor

    def get_cursor(self):
        return self.cursor


def test_undecided_products_lists_only_rows_whose_null_decides_a_verdict():
    rows = [
        _product(product_id=1, canonical_name="jam", status="confirmed",
                 contains_animal_byproduct=None),
        # Fails vegan on meat already: no by-product value changes anything.
        _product(product_id=2, canonical_name="chicken", status="confirmed",
                 contains_meat=True, contains_animal_byproduct=None),
    ]
    cursor = _RecordingCursor(rows)
    out = products.undecided_products(database=_RecordingDB(cursor))
    assert [p["product_id"] for p in out] == [1]
    assert out[0]["undecided"] == ["contains_animal_byproduct"]
    assert out[0]["would_clear"] == ["vegan"]
    assert "vegan" not in out[0]["clears"]
    sql = cursor.calls[0][0]
    assert "status = 'confirmed'" in sql and "contains_animal_byproduct IS NULL" in sql


def test_fill_undecided_attributes_only_ever_replaces_null():
    cursor = _RecordingCursor(one={a: False for a in products.ATTRIBUTES})
    out = products.fill_undecided_attributes(
        7, {"contains_animal_byproduct": False}, database=_RecordingDB(cursor))
    assert out["contains_animal_byproduct"] is False
    sql, params = cursor.calls[0]
    assert ("contains_animal_byproduct = COALESCE(contains_animal_byproduct, "
            "%(contains_animal_byproduct)s)") in sql
    assert set(PLACEHOLDER.findall(sql)) <= set(params)
    assert params["pid"] == 7


def test_fill_undecided_attributes_rejects_unknown_columns():
    with pytest.raises(ValueError):
        products.fill_undecided_attributes(
            7, {"contains_sqlinjection": True},
            database=_RecordingDB(_RecordingCursor()))


def test_set_attributes_never_turns_none_into_false():
    cursor = _RecordingCursor()
    out = products.set_attributes(
        7, {"contains_dairy": True, "contains_animal_byproduct": None},
        database=_RecordingDB(cursor))
    assert out == {"contains_dairy": True}
    assert "contains_animal_byproduct" not in cursor.calls[-1][0]


class _LockedRowCursor(_RecordingCursor):
    """Returns the product row for the SELECT ... FOR UPDATE."""


def _stored(status, **attrs):
    row = {"status": status, "canonical_name": "milk",
           **{a: False for a in products.ATTRIBUTES}}
    row.update(attrs)
    return row


def test_set_attributes_confirmed_same_value_is_accepted_without_overwrite():
    cursor = _LockedRowCursor(one=_stored("confirmed", contains_dairy=True))
    out = products.set_attributes(
        7, {"contains_dairy": True, "contains_nuts": False},
        database=_RecordingDB(cursor))
    assert out == {"contains_dairy": True, "contains_nuts": False}
    sql = cursor.calls[-1][0]
    assert "contains_dairy = COALESCE(contains_dairy," in sql


def test_set_attributes_confirmed_fills_null_byproduct():
    cursor = _LockedRowCursor(
        one=_stored("confirmed", contains_animal_byproduct=None))
    out = products.set_attributes(
        7, {"contains_animal_byproduct": True}, database=_RecordingDB(cursor))
    assert out == {"contains_animal_byproduct": True}
    assert "COALESCE(contains_animal_byproduct," in cursor.calls[-1][0]


def test_set_attributes_confirmed_change_raises_and_writes_nothing():
    cursor = _LockedRowCursor(one=_stored("confirmed", contains_nuts=True))
    with pytest.raises(products.AttributeConflict) as err:
        products.set_attributes(
            7, {"contains_nuts": False, "contains_dairy": False},
            database=_RecordingDB(cursor))
    assert err.value.conflicts == {"contains_nuts": (True, False)}
    assert "contains_nuts: stored True, requested False" in str(err.value)
    assert "'milk'" in str(err.value)
    assert not any(sql.lstrip().startswith("UPDATE") for sql, _ in cursor.calls)


def test_set_attributes_proposed_is_freely_editable():
    cursor = _LockedRowCursor(one=_stored("proposed", contains_nuts=True))
    out = products.set_attributes(
        7, {"contains_nuts": False}, database=_RecordingDB(cursor))
    assert out == {"contains_nuts": False}
    sql = cursor.calls[-1][0]
    assert "contains_nuts = %(contains_nuts)s" in sql and "COALESCE" not in sql


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
    assert groups[0]["clears"] == ["vegan", "vegetarian", "pescatarian",
                                   "gluten_free", "kosher"]
    # The reviewer's labels decide, in both directions.
    groups, _ = products.group_proposals([_alias_row(contains_dairy=True)])
    assert groups[0]["clears"] == ["vegetarian", "pescatarian", "gluten_free",
                                   "kosher"]


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
    for attr in ("contains_meat", "contains_fish", "contains_dairy", "contains_egg",
                 "contains_animal_byproduct"):
        # IS NOT FALSE, so a NULL (undecided) attribute violates too.
        assert f"p.{attr} IS NOT FALSE" in fragment
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


# --------------------------------------------------------------------------
# not_kosher (migration 021) and unsupported labels
# --------------------------------------------------------------------------

def test_not_kosher_is_a_kosher_attribute_only():
    assert "not_kosher" in products.ATTRIBUTES
    assert "not_kosher" in products.NULLABLE_ATTRIBUTES
    assert products.RESTRICTION_ATTRIBUTES["kosher"] == ("not_kosher",)
    for label, attrs in products.RESTRICTION_ATTRIBUTES.items():
        assert ("not_kosher" in attrs) == (label == "kosher"), label


def test_kosher_allows_only_on_an_explicit_false(caplog):
    assert products.product_allows(_product(not_kosher=False), ["kosher"]) is True
    assert products.product_allows(_product(not_kosher=True), ["kosher"]) is False
    assert products.product_allows(_product(not_kosher=None), ["kosher"]) is False
    missing = _product()
    del missing["not_kosher"]
    assert products.product_allows(missing, ["kosher"]) is False


def test_not_kosher_does_not_affect_other_restrictions():
    shrimp = _product(canonical_name="xx", contains_fish=True, not_kosher=True)
    undecided = _product(canonical_name="yy", not_kosher=None)
    for label in ("vegan", "vegetarian", "no_nuts", "dairy_free", "gluten_free"):
        assert products.product_allows(undecided, [label]) is True, label
    assert products.product_allows(shrimp, ["no_nuts"]) is True


def test_kosher_rule_states_the_labelling_decisions():
    rule = products.ATTRIBUTE_RULES["not_kosher"]
    for word in ("hechsher", "OU", "COR", "MK", "Kof-K", "Star-K",
                 "raw/fresh produce", "eggs in shell", "dry grains",
                 "Uncertified meat and poultry", "uncertified cheese", "pork",
                 "shellfish", "uncertain", "least permissions"):
        assert word in rule, word


def test_kosher_null_decides_a_kosher_verdict():
    assert "kosher" in products._null_decides(_product(not_kosher=None))
    assert "kosher" in products.REVIEW_LABELS
    assert products._null_decides(_product(not_kosher=True)) == []


def test_kosher_combination_rule_is_meat_with_dairy_only():
    assert products.RESTRICTION_COMBINATION_RULES == {
        "kosher": (("contains_meat", "contains_dairy"),)}
    for rules in products.RESTRICTION_COMBINATION_RULES.values():
        for rule in rules:
            assert set(rule) <= set(products.ATTRIBUTES)
    assert set(products.RESTRICTION_COMBINATION_RULES) <= set(
        products.RESTRICTION_ATTRIBUTES)


def test_supported_restrictions_are_exactly_the_enforced_labels():
    assert products.supported_restrictions() == sorted(products.RESTRICTION_ATTRIBUTES)
    assert "kosher" in products.supported_restrictions()
    assert "halal" not in products.supported_restrictions()


@pytest.mark.parametrize("labels, unsupported", [
    (["halal"], ["halal"]),
    (["vegan", "Halal ", "halal", "keto"], ["halal", "keto"]),
    (["kosher", "Vegan", "", "  "], []),
    (None, []),
])
def test_unsupported_restrictions(labels, unsupported):
    assert products.unsupported_restrictions(labels) == unsupported


def test_require_supported_restrictions_names_the_supported_list():
    products.require_supported_restrictions(["kosher", "vegan"])
    with pytest.raises(products.UnsupportedRestrictionError) as err:
        products.require_supported_restrictions(["vegan", "halal"])
    assert err.value.labels == ["halal"]
    msg = str(err.value)
    assert "halal" in msg
    for label in products.supported_restrictions():
        assert label in msg
