"""Tests for the label export/import seam and the proposal write path.

No API, no network, no credentials. The labelling step is a Claude session
reading the exported file, so the enforcement that matters lives on **import**:
validation is the only thing between a malformed label and an allergen
attribute defaulting to something permissive. That is where most of these
tests point.
"""
import json
import re

import pytest

from app.services import product_proposer as pp

#: Every ``%(name)s`` in a statement must have a matching parameter. A missing
#: one is invisible until the statement actually runs against Postgres.
PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _label(**overrides):
    base = {
        "raw_name": "CHICKEN BREAST 500g",
        "match_existing_product": None,
        "canonical_name": "chicken breast",
        "category": "meat",
        "contains_meat": True,
        "contains_fish": False,
        "contains_dairy": False,
        "contains_egg": False,
        "contains_nuts": False,
        "contains_animal_byproduct": False,
        "contains_gluten": False,
        "is_pantry_staple": False,
        "confidence": 0.95,
        "reason": "Chicken is poultry.",
        "unmappable": False,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Export bundle
# --------------------------------------------------------------------------

def test_export_bundle_carries_names_instructions_and_catalogue():
    existing = [{
        "canonical_name": "milk 2%", "category": "dairy",
        "contains_meat": False, "contains_fish": False, "contains_dairy": True,
        "contains_egg": False, "contains_nuts": False,
        "contains_animal_byproduct": None,
    }]
    bundle = pp.export_bundle(["RAW MILK 2L"], existing)
    # Undecided stays null in the hand-off; it is not shown to the labeller
    # as false.
    assert bundle["existing_products"][0]["contains_animal_byproduct"] is None
    assert bundle["unmapped"] == ["RAW MILK 2L"]
    assert bundle["existing_products"][0]["canonical_name"] == "milk 2%"
    assert bundle["existing_products"][0]["contains_dairy"] is True
    assert "instructions" in bundle and "output_format" in bundle


def test_export_bundle_is_json_serialisable():
    """It is written to a file and read by another process."""
    bundle = pp.export_bundle(["A", "B"], [])
    assert json.loads(json.dumps(bundle))["unmapped"] == ["A", "B"]


def test_instructions_carry_the_known_trap_cases():
    """These few-shots are load-bearing, not decoration.

    Every one is a case where the substring oracle or a naive reading gets the
    answer wrong. Deleting them should fail a test, not quietly degrade
    labelling.
    """
    text = pp.LABELLING_INSTRUCTIONS.lower()
    for trap in ("beefsteak", "prosciutto", "gelatin", "marzipan", "oyster mushroom"):
        assert trap in text
    assert "allergic" in text


def test_instructions_record_the_settled_judgement_calls():
    """Two calls were made by the owner on 2026-10-14 and must not be
    re-litigated per item by a future labelling pass:

    * coconut is NOT a nut for this app (FDA labels it a tree nut, but coconut
      allergy is rare and largely unrelated, and treating it as one would
      withhold a staple from every nut-free user for nothing).
    * lactose-free milk IS still dairy (the lactose is removed, not the milk).
    """
    text = pp.LABELLING_INSTRUCTIONS.lower()
    assert "coconut is not a nut" in text
    assert "lactose-free milk is still dairy" in text


def test_instructions_carry_the_animal_byproduct_labelling_rule():
    """Honey et al. are a separate, vegan-only attribute; gelatin, lard,
    tallow and rennet stay contains_meat so vegetarians keep avoiding them."""
    text = pp.LABELLING_INSTRUCTIONS.lower()
    assert "contains_animal_byproduct" in text
    for word in ("honey", "beeswax", "carmine", "shellac", "bone-char", "lanolin"):
        assert word in text
    assert ("gelatin, lard, tallow and animal rennet are contains_meat, not this "
            "attribute") in text


def test_a_label_missing_the_byproduct_attribute_is_rejected():
    payload = _label()
    del payload["contains_animal_byproduct"]
    problems = pp.validate_payload(payload)
    assert any("contains_animal_byproduct" in p for p in problems)
    assert pp.validate_payload(_label(contains_animal_byproduct=None))


def test_output_format_example_names_every_attribute():
    """The labeller works from this example, so an omission here becomes a
    missing attribute on every label."""
    example = pp.export_bundle([], [])["output_format"]["labels"][0]
    for attr in pp.ATTRIBUTES:
        assert attr in example
    for key in pp.PROPOSAL_SCHEMA["required"]:
        assert key in example


# --------------------------------------------------------------------------
# Validation — the real gate
# --------------------------------------------------------------------------

def test_valid_label_has_no_problems():
    assert pp.validate_payload(_label()) == []


@pytest.mark.parametrize("missing", pp.PROPOSAL_SCHEMA["required"])
def test_every_required_field_is_enforced(missing):
    payload = _label()
    del payload[missing]
    problems = pp.validate_payload(payload)
    assert any(missing in p for p in problems)


def test_attributes_must_be_booleans_not_strings():
    """A label saying "true" instead of true must be rejected, not coerced.

    Python's truthiness would make the string "false" evaluate as True, turning
    a safe label into an unsafe one.
    """
    problems = pp.validate_payload(_label(contains_nuts="false"))
    assert any("contains_nuts" in p for p in problems)


def test_confidence_must_be_a_number_not_a_bool():
    """bool is a subclass of int; without the explicit check, True would pass
    as a confidence of 1.0."""
    problems = pp.validate_payload(_label(confidence=True))
    assert any("confidence" in p for p in problems)


@pytest.mark.parametrize("value", [-0.1, 1.5, 42])
def test_confidence_must_be_in_range(value):
    problems = pp.validate_payload(_label(confidence=value))
    assert any("confidence" in p for p in problems)


def test_blank_raw_name_is_rejected():
    assert pp.validate_payload(_label(raw_name="   ")) != []


def test_non_object_is_rejected():
    assert pp.validate_payload("nope") != []
    assert pp.validate_payload(None) != []


def test_label_needs_a_target_unless_unmappable():
    orphan = _label(canonical_name="", match_existing_product=None, unmappable=False)
    assert pp.validate_payload(orphan) != []
    # Unmappable is a legitimate answer and needs no target.
    assert pp.validate_payload(
        _label(canonical_name="", match_existing_product=None, unmappable=True)
    ) == []
    # A match to an existing product is a target.
    assert pp.validate_payload(
        _label(canonical_name="", match_existing_product="milk")
    ) == []


# --------------------------------------------------------------------------
# load_labels
# --------------------------------------------------------------------------

def _write(tmp_path, labels, key="labels"):
    path = tmp_path / "labelled.json"
    path.write_text(json.dumps({key: labels}), encoding="utf-8")
    return path


def test_load_labels_reads_valid_entries(tmp_path):
    path = _write(tmp_path, [_label()])
    load = pp.load_labels(path, expected=["CHICKEN BREAST 500g"])
    assert load.ok
    assert load.proposals[0].canonical_name == "chicken breast"
    assert load.proposals[0].attributes["contains_meat"] is True


def test_load_labels_normalises_the_raw_name(tmp_path):
    path = _write(tmp_path, [_label(raw_name="LACTANTIA MILK, 2L")])
    load = pp.load_labels(path)
    assert load.proposals[0].normalized_name == "lactantia milk"


def test_load_labels_rejects_a_name_that_was_not_exported(tmp_path):
    """An altered or invented raw_name would link the wrong deals.

    The name is the join key between a label and the rows it governs, so a
    labeller that reformatted it must be caught, not silently trusted.
    """
    path = _write(tmp_path, [_label(raw_name="CHICKEN BREAST 500 g")])
    load = pp.load_labels(path, expected=["CHICKEN BREAST 500g"])
    assert load.proposals == []
    assert any("not in the exported set" in e for e in load.errors.values())


def test_unlabelled_names_are_reported_but_are_not_errors(tmp_path):
    """Labelling a subset is normal — exports are limited by --limit.

    An unlabelled name stays withheld, which is the safe default, so it must
    not block persisting the labels that were done.
    """
    path = _write(tmp_path, [_label()])
    load = pp.load_labels(path, expected=["CHICKEN BREAST 500g", "WHOLE MILK 2L"])
    assert len(load.proposals) == 1
    assert load.unlabelled == ["WHOLE MILK 2L"]
    assert load.ok


def test_load_labels_rejects_duplicates(tmp_path):
    path = _write(tmp_path, [_label(), _label()])
    load = pp.load_labels(path, expected=["CHICKEN BREAST 500g"])
    assert len(load.proposals) == 1
    assert any("duplicate" in e for e in load.errors.values())


def test_load_labels_keeps_good_entries_and_reports_bad_ones(tmp_path):
    path = _write(tmp_path, [_label(), _label(raw_name="MILK", confidence="high")])
    load = pp.load_labels(path)
    assert len(load.proposals) == 1
    assert "MILK" in load.errors


def test_load_labels_handles_a_bare_list(tmp_path):
    path = tmp_path / "bare.json"
    path.write_text(json.dumps([_label()]), encoding="utf-8")
    assert len(pp.load_labels(path).proposals) == 1


def test_load_labels_reports_a_missing_file(tmp_path):
    load = pp.load_labels(tmp_path / "nope.json")
    assert "__file__" in load.errors


def test_load_labels_reports_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert "__file__" in pp.load_labels(path).errors


def test_load_labels_reports_the_wrong_top_level_shape(tmp_path):
    path = _write(tmp_path, [_label()], key="items")
    assert "__file__" in pp.load_labels(path).errors


# --------------------------------------------------------------------------
# Conservative defaulting
# --------------------------------------------------------------------------

def test_proposal_defaults_a_missing_attribute_to_true():
    """If an attribute somehow gets past validation, the safe reading is
    "contains it". A False default would turn a malformed label permissive."""
    thin = {"raw_name": "MYSTERY", "canonical_name": "mystery", "confidence": 0.1}
    [proposal] = pp.proposals_from_payload(thin)
    assert all(proposal.attributes[a] is True for a in pp.ATTRIBUTES)


def test_proposal_lowercases_the_canonical_name():
    [proposal] = pp.proposals_from_payload(_label(canonical_name="  CHICKEN Breast  "))
    assert proposal.canonical_name == "chicken breast"


def test_single_product_entry_leaves_pricing_undecided():
    """A single product is NOT per-item by definition -- 2-for-$7 is routine.

    This test previously asserted the opposite, under the name
    "..._prices_per_item_by_definition", on the reasoning that a single-product
    tile has nothing to share the price with. The 5-term probe disproved it:
    nine single-product names carried `pre_price_text` of '2/' or '3/' at a
    multi-buy price (Dempster's sliced breads, COMPLIMENTS Peanut Butter, both
    $7.00). And because `reinfer_alias_prices` only fills NULLs, the wrong True
    could never be corrected -- omitting the field shipped halved prices
    permanently.

    None is both honest and recoverable: the backfill fills it from the deal
    text, and whatever it cannot decide stays withheld rather than priced wrong.
    """
    [proposal] = pp.proposals_from_payload(_label())
    assert proposal.price_is_per_item is None
    assert proposal.tile_position == 0


def test_an_explicit_single_product_price_is_kept():
    """A labeller who DOES decide is believed, in both directions."""
    [yes] = pp.proposals_from_payload(_label(price_is_per_item=True))
    assert yes.price_is_per_item is True
    [no] = pp.proposals_from_payload(_label(price_is_per_item=False))
    assert no.price_is_per_item is False


# --------------------------------------------------------------------------
# Persistence — against a fake cursor, like test_flipp_persistence.py
# --------------------------------------------------------------------------

class FakePersistCursor:
    """Records statements and asserts every placeholder is bound."""

    def __init__(self, known_products=(), times_seen=1):
        self.calls = []
        self._known = list(known_products)
        self._times_seen = times_seen
        self._next = None
        #: persist_proposals reads rowcount after the supersede UPDATE.
        self.rowcount = 0

    @property
    def alias_inserts(self):
        """Only the alias INSERTs, excluding the supersede UPDATE.

        Both statements mention product_aliases, so filtering on the table name
        alone double-counts.
        """
        return [
            (sql, params) for sql, params in self.calls
            if "INSERT INTO product_aliases" in sql
        ]

    def execute(self, sql, params=None):
        params = params or {}
        missing = set(PLACEHOLDER.findall(sql)) - set(params)
        assert not missing, f"unbound placeholder(s) {missing} in: {sql[:70]}"
        self.calls.append((sql, params))

        if "FROM products" in sql and "SELECT" in sql:
            self._next = list(self._known)
        elif "INSERT INTO products" in sql:
            self._next = {"product_id": 99}
        elif "INSERT INTO product_aliases" in sql:
            self._next = {"times_seen": self._times_seen}
        else:
            self._next = None

    def fetchall(self):
        return self._next or []

    def fetchone(self):
        return self._next

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, cursor):
        self._cursor = cursor

    def get_cursor(self):
        return self._cursor


def _proposal(**overrides):
    base = dict(
        normalized_name="chicken breast",
        raw_example="CHICKEN BREAST 500g",
        match_existing_product=None,
        canonical_name="chicken breast",
        category="meat",
        attributes={a: False for a in pp.ATTRIBUTES} | {"contains_meat": True},
        is_pantry_staple=False,
        confidence=0.9,
        reason="poultry",
        unmappable=False,
    )
    base.update(overrides)
    return pp.Proposal(**base)


def test_persist_creates_product_and_alias_for_a_new_product():
    cursor = FakePersistCursor()
    stats = pp.persist_proposals([_proposal()], database=FakeDB(cursor))
    statements = " ".join(sql for sql, _ in cursor.calls)
    assert "INSERT INTO products" in statements
    assert "INSERT INTO product_aliases" in statements
    assert stats["products_proposed"] == 1
    assert stats["aliases_new"] == 1


def test_persist_writes_everything_as_proposed_never_confirmed():
    """The safety invariant: this module cannot create a live row."""
    cursor = FakePersistCursor()
    pp.persist_proposals([_proposal()], database=FakeDB(cursor))
    for sql, _ in cursor.calls:
        if "INSERT INTO" in sql:
            assert "'proposed'" in sql
            assert "confirmed" not in sql


def test_persist_reuses_an_existing_product_instead_of_duplicating():
    known = [{"name": "chicken breast", "product_id": 7, "status": "confirmed"}]
    cursor = FakePersistCursor(known_products=known)
    stats = pp.persist_proposals(
        [_proposal(match_existing_product="Chicken Breast")], database=FakeDB(cursor)
    )
    statements = " ".join(sql for sql, _ in cursor.calls)
    assert "INSERT INTO products" not in statements
    assert stats["products_proposed"] == 0
    alias_params = cursor.alias_inserts[0][1]
    assert alias_params["pid"] == 7


def test_persist_leaves_product_null_for_an_unmappable_string():
    """The unplaced path uses its own statement with NULL hardcoded.

    Migration 010 made the uniqueness partial, so the conflict target differs
    between the placed and unplaced cases and they cannot share one statement.
    """
    cursor = FakePersistCursor()
    stats = pp.persist_proposals(
        [_proposal(unmappable=True, canonical_name="")], database=FakeDB(cursor)
    )
    assert stats["products_proposed"] == 0
    alias_sql, alias_params = cursor.alias_inserts[0]
    assert "WHERE product_id IS NULL" in alias_sql
    assert "NULL" in alias_sql
    # No pid is bound at all on this path.
    assert "pid" not in alias_params


def test_only_one_unplaced_row_per_name_is_possible():
    """Guards the partial index that stops an unplaceable string re-queueing.

    Plain (normalized_name, product_id) uniqueness would let unlimited NULL
    rows accumulate, since NULLs compare distinct -- one new review-queue entry
    per ingestion run, forever.
    """
    cursor = FakePersistCursor()
    pp.persist_proposals(
        [_proposal(unmappable=True, canonical_name="")], database=FakeDB(cursor)
    )
    alias_sql = cursor.alias_inserts[0][0]
    assert "ON CONFLICT (normalized_name) WHERE product_id IS NULL" in alias_sql


def test_persist_clamps_confidence_for_the_check_constraint():
    """product_aliases.confidence has a CHECK (0..1); an out-of-range value
    must not become a failed INSERT."""
    cursor = FakePersistCursor()
    pp.persist_proposals([_proposal(confidence=7.5)], database=FakeDB(cursor))
    alias_params = cursor.alias_inserts[0][1]
    assert alias_params["conf"] == 1.0

    cursor = FakePersistCursor()
    pp.persist_proposals([_proposal(confidence=-3)], database=FakeDB(cursor))
    alias_params = cursor.alias_inserts[0][1]
    assert alias_params["conf"] == 0.0


def test_persist_counts_a_reseen_alias_separately():
    """Re-ingesting a weekly flyer must not re-fill the review queue."""
    cursor = FakePersistCursor(times_seen=4)
    stats = pp.persist_proposals([_proposal()], database=FakeDB(cursor))
    assert stats["aliases_seen_again"] == 1
    assert stats["aliases_new"] == 0
    alias_sql = cursor.alias_inserts[0][0]
    # Per (name, product) since migration 010: each product of a tile is
    # tracked separately, so re-seeing a tile bumps each of its rows.
    assert "ON CONFLICT (normalized_name, product_id)" in alias_sql
    assert "DO UPDATE" in alias_sql


# --------------------------------------------------------------------------
# Multi-product tiles
# --------------------------------------------------------------------------

def _tile(**overrides):
    base = {
        "raw_name": ("ANNIE'S SHELLS & CHEDDAR 170 G BEATRICE CHOCOLATE MILK 1 L "
                     "GOLDFISH CRACKERS 168 - 227 G"),
        "confidence": 0.8,
        "reason": "three unrelated products at one price",
        "unmappable": False,
        "products": [
            {"match_existing_product": None, "canonical_name": "mac and cheese",
             "category": "pantry", "contains_meat": False, "contains_fish": False,
             "contains_dairy": True, "contains_egg": False, "contains_nuts": False,
             "contains_animal_byproduct": False,
             "contains_gluten": False,
             "is_pantry_staple": True, "price_is_per_item": True},
            {"match_existing_product": None, "canonical_name": "chocolate milk",
             "category": "dairy", "contains_meat": False, "contains_fish": False,
             "contains_dairy": True, "contains_egg": False, "contains_nuts": False,
             "contains_animal_byproduct": False,
             "contains_gluten": False,
             "is_pantry_staple": False, "price_is_per_item": True},
            {"match_existing_product": None, "canonical_name": "crackers",
             "category": "snacks", "contains_meat": False, "contains_fish": False,
             "contains_dairy": True, "contains_egg": False, "contains_nuts": False,
             "contains_animal_byproduct": False,
             "contains_gluten": False,
             "is_pantry_staple": True, "price_is_per_item": True},
        ],
    }
    base.update(overrides)
    return base


def test_a_tile_validates():
    assert pp.validate_payload(_tile()) == []


def test_a_tile_yields_one_proposal_per_product_with_positions():
    proposals = pp.proposals_from_payload(_tile())
    assert [p.canonical_name for p in proposals] == [
        "mac and cheese", "chocolate milk", "crackers"]
    assert [p.tile_position for p in proposals] == [0, 1, 2]
    # Every proposal shares the one source string, which is what links them.
    assert len({p.normalized_name for p in proposals}) == 1


def test_tile_products_are_validated_individually():
    tile = _tile()
    del tile["products"][1]["contains_nuts"]
    problems = pp.validate_payload(tile)
    assert any("products[1]" in p and "contains_nuts" in p for p in problems)


def test_tile_rejects_the_same_product_twice():
    """(normalized_name, product_id) is unique, so a duplicate would collide --
    better to say so than to let the upsert silently absorb it."""
    tile = _tile()
    tile["products"][2]["canonical_name"] = "chocolate milk"
    assert any("same product twice" in p for p in pp.validate_payload(tile))


def test_tile_cannot_also_be_unmappable():
    assert any("cannot be true alongside" in p
               for p in pp.validate_payload(_tile(unmappable=True)))


def test_tile_needs_a_non_empty_products_list():
    assert pp.validate_payload(_tile(products=[])) != []
    assert pp.validate_payload(_tile(products="milk")) != []


def test_undecided_tile_price_is_allowed_and_preserved():
    """null is a real answer. Guessing is what we are avoiding."""
    tile = _tile()
    for spec in tile["products"]:
        spec["price_is_per_item"] = None
    assert pp.validate_payload(tile) == []
    proposals = pp.proposals_from_payload(tile)
    assert all(p.price_is_per_item is None for p in proposals)


def test_tile_price_must_be_a_bool_or_null_not_a_string():
    tile = _tile()
    tile["products"][0]["price_is_per_item"] = "yes"
    assert any("price_is_per_item" in p for p in pp.validate_payload(tile))


def test_persist_writes_one_alias_row_per_tile_product():
    cursor = FakePersistCursor()
    stats = pp.persist_proposals(
        pp.proposals_from_payload(_tile()), database=FakeDB(cursor)
    )
    alias_calls = [params for _, params in cursor.alias_inserts]
    assert len(alias_calls) == 3
    assert [p["pos"] for p in alias_calls] == [0, 1, 2]
    assert stats["products_proposed"] == 3
    assert stats["tile_rows"] == 2  # positions 1 and 2


def test_load_counts_tiles(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"labels": [_label(), _tile()]}), encoding="utf-8")
    load = pp.load_labels(path)
    assert load.ok
    assert len(load.proposals) == 4  # 1 single + 3 from the tile
    assert load.tiles == 1


def test_persist_skips_a_name_that_normalises_to_nothing():
    cursor = FakePersistCursor()
    stats = pp.persist_proposals([_proposal(normalized_name="")], database=FakeDB(cursor))
    assert stats["skipped"] == 1
    assert not [sql for sql, _ in cursor.calls if "INSERT" in sql]


def test_instructions_carry_the_gluten_labelling_rule():
    text = pp.LABELLING_INSTRUCTIONS.lower()
    assert "contains_gluten" in text
    for word in ("spelt", "kamut", "durum", "semolina", "farro", "barley", "rye",
                 "triticale", "malt", "brewer's yeast", "certified gluten-free",
                 "may contain gluten"):
        assert word in text, word


def test_a_label_missing_the_gluten_attribute_is_rejected():
    payload = _label()
    del payload["contains_gluten"]
    problems = pp.validate_payload(payload)
    assert any("contains_gluten" in p for p in problems)
